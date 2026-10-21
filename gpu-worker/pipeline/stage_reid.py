"""Stage 6 — Identity Association (Re-ID).

Layered approach, applied in priority order (Issue #131):
  1. **OCR** — jersey-number OCR on the bbox region. Same-session uses
     Tesseract (``jersey-ocr``); nightly uses PARSeq (``parseq-ocr``) with an
     automatic Tesseract fallback when the PARSeq checkpoint is unavailable.
  2. **Appearance gallery** — cosine match against an embedding gallery of
     already-identified tracklets (optional ReID adapter).
  3. **Trajectory prior** — for tracklets OCR/gallery could not resolve,
     constant-velocity prediction + roster-position + team priors assign the
     remaining tracklets to known players (``tracking.trajectory_prior_reid``).
  4. **Min-cost-flow stitching (nightly only)** — stitch fragmented tracklets
     of the same identity within the clip and propagate the ``player_id``
     across each stitched group (``tracking.min_cost_flow_stitcher``).

The variant is chosen by ``pipeline.model_router`` and passed in by the
dispatcher. Every layer only ever fills the existing ``player_id`` via
PATCH /api/v1/tracklets/{id}. Associations are within one recording; multiple
independent session recordings do not imply automatic cross-view identity.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from queue.same_session_queue import SAME_SESSION_PRIORITY
from typing import Any

import cv2
import numpy as np
import structlog

from pipeline.model_router import PARSEQ_OCR
from pipeline.runtime import PipelineInterrupted, check_control
from pipeline.tracking.min_cost_flow_stitcher import (
    MinCostFlowStitcher,
    TrackletNode,
)
from pipeline.tracking.trajectory_prior_reid import (
    KnownPlayer,
    TrajectoryPriorReID,
    UnknownTrack,
)
from pipeline.video_timeline import VideoTimeline, read_frames

log = structlog.get_logger(__name__)

MIN_OCR_CONFIDENCE = 60  # Tesseract confidence threshold


def run(
    clip_id: str,
    video_path: Path,
    tracklet_ids: list[str],
    tracklets: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    backend_api_url: str,
    *,
    variant: str | None = None,
    priority: int | None = None,
    timeline: VideoTimeline | None = None,
    auto_identity_approved: bool = False,
) -> dict[str, Any]:
    """Attempt to associate player identities with tracklets.

    Args:
        variant: routing variant from ``model_router`` (``jersey-ocr`` |
            ``parseq-ocr``). ``parseq-ocr`` upgrades OCR to PARSeq with a
            Tesseract fallback. ``None`` keeps the legacy Tesseract path.
        priority: ``processing_jobs.priority``. Nightly priority unlocks the
            min-cost-flow stitching layer; ``None`` is treated as nightly.
    """
    nightly = priority is None or priority < SAME_SESSION_PRIORITY
    if not roster and not any(t.get("player_id") for t in tracklets):
        return {
            "assigned": 0,
            "total": len(tracklets),
            "tracklets": tracklets,
            "skipped": True,
            "reason": "roster_and_identity_seeds_unavailable",
            "reid_strategy": {
                "ocr": "disabled",
                "trajectory_prior_assigned": 0,
                "min_cost_flow_stitched": 0,
                "stitching_enabled": False,
            },
        }
    log.info(
        "stage_reid_start",
        clip_id=clip_id,
        tracklet_count=len(tracklets),
        variant=variant,
        nightly=nightly,
    )

    adapter = _get_reid_adapter()
    ocr_adapter = _get_ocr_adapter(variant)
    cap = cv2.VideoCapture(str(video_path))
    timeline = timeline or VideoTimeline.from_path(video_path)

    # tracklet_id -> player_id, covering both pre-identified and newly assigned.
    assigned_ids: dict[str, str] = {}
    assigned = 0

    # Gallery of (embedding, player_id) built from already-identified tracklets.
    gallery: list[tuple[np.ndarray, str]] = []

    # ── Layers 1+2: OCR (PARSeq/Tesseract) and appearance gallery ──────────
    try:
        for tracklet in tracklets:
            check_control()
            if tracklet.get("player_id"):
                assigned_ids[tracklet["id"]] = tracklet["player_id"]
                if adapter is not None:
                    emb = _extract_tracklet_embedding(tracklet, cap, adapter, timeline=timeline)
                    if emb is not None:
                        gallery.append((emb, tracklet["player_id"]))
                continue

            candidates = roster_candidates(tracklet, roster)
            by_number: dict[int, list[str]] = {}
            for player in candidates:
                if player.get("jersey_number") is not None:
                    by_number.setdefault(int(player["jersey_number"]), []).append(str(player["id"]))
            jersey_map = {number: ids[0] for number, ids in by_number.items() if len(set(ids)) == 1}
            # Candidate narrowing is evidence based. Geometry alone cannot
            # turn a same-number opponent or teammate into a resolved athlete.
            scope_required = any(p.get("_identity_scope") == "resolved_team_required" for p in roster)
            permitted = not scope_required or bool(candidates)
            player_id = None
            if permitted:
                allowed_ids = {str(player["id"]) for player in candidates}
                scoped_gallery = [(embedding, pid) for embedding, pid in gallery
                                  if not roster or pid in allowed_ids]
                player_id = _identify_tracklet(
                    tracklet, cap, jersey_map, adapter, scoped_gallery, ocr_adapter, timeline=timeline
                )
            identity = tracklet.setdefault("provenance", {}).setdefault("identity", {})
            number = identity.get("jersey_number")
            identity.update(
                definition_version="team-scoped-jersey-v1",
                candidates=[str(p["id"]) for p in candidates
                            if number is None or p.get("jersey_number") == number] if roster else None,
                status="candidate" if player_id else "unresolved",
                reason=None if player_id else ("team_membership_unresolved" if not permitted
                                              else "jersey_or_roster_candidates_unresolved"),
            )
            if player_id and _identity_conflicts(tracklet, player_id, tracklets, assigned_ids):
                identity.update(status="unresolved", reason="concurrent_identity_conflict")
                player_id = None
            if backend_api_url:
                from pipeline import backend
                backend.patch_tracklet_provenance(tracklet["id"], tracklet["provenance"])
            if player_id:
                if auto_identity_approved:
                    _patch_tracklet(tracklet["id"], player_id, backend_api_url)
                assigned_ids[tracklet["id"]] = player_id
                assigned += 1
                if adapter is not None:
                    emb = _extract_tracklet_embedding(tracklet, cap, adapter, timeline=timeline)
                    if emb is not None:
                        gallery.append((emb, player_id))
    finally:
        cap.release()

    # ── Layer 3: trajectory-prior re-ID for the still-unidentified ─────────
    scoped_tracks = [t for t in tracklets if not roster or roster_candidates(t, roster)]
    shot_groups: dict[Any, list[dict[str, Any]]] = {}
    for track in scoped_tracks:
        points = track.get("track_points") or [{}]
        shot_groups.setdefault(points[0].get("shot_id"), []).append(track)
    trajectory_assigned = sum(_assign_by_trajectory(group, assigned_ids, backend_api_url,
                                                    write_identity=auto_identity_approved)
                              for group in shot_groups.values())
    assigned += trajectory_assigned

    # ── Layer 4: min-cost-flow stitching (nightly only) ────────────────────
    stitched_assigned = 0
    if nightly:
        stitched_assigned = sum(_stitch_and_propagate(group, assigned_ids, backend_api_url,
                                                     write_identity=auto_identity_approved)
                                for group in shot_groups.values())
        assigned += stitched_assigned

    log.info(
        "stage_reid_done",
        clip_id=clip_id,
        assigned=assigned,
        trajectory_assigned=trajectory_assigned,
        stitched_assigned=stitched_assigned,
    )
    enriched = []
    for tracklet in tracklets:
        observation = dict(tracklet)
        pid = assigned_ids.get(tracklet["id"])
        if pid and not observation.get("player_id"):
            identity = observation.setdefault("provenance", {}).setdefault("identity", {})
            identity.update(candidate_player_id=pid, status="approved" if auto_identity_approved else "candidate",
                            approved=auto_identity_approved)
            if auto_identity_approved:
                observation.update(player_id=pid, identity_source="approved_automatic",
                                   identity_verified=True)
            if backend_api_url:
                from pipeline import backend
                backend.patch_tracklet_provenance(tracklet["id"], observation["provenance"])
        enriched.append(observation)
    return {
        "assigned": assigned if auto_identity_approved else 0,
        "unapproved_candidate_count": 0 if auto_identity_approved else assigned,
        "auto_identity_approved": auto_identity_approved,
        "total": len(tracklets),
        "tracklets": enriched,
        "reid_strategy": {
            "ocr": _ocr_backend_name(ocr_adapter),
            "trajectory_prior_assigned": trajectory_assigned,
            "min_cost_flow_stitched": stitched_assigned,
            "stitching_enabled": nightly,
        },
    }


_OFFENSIVE_POSITIONS = {"QB", "RB", "FB", "HB", "WR", "TE", "OL", "OT", "OG", "C", "LT", "LG", "RG", "RT"}
_DEFENSIVE_POSITIONS = {"DL", "DT", "DE", "NT", "EDGE", "LB", "ILB", "OLB", "DB", "CB", "S", "FS", "SS"}


def roster_candidates(tracklet: dict[str, Any], roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain duplicates until independently observed team/unit/role resolves them.

    ``team_label=offense`` is side of ball, not Toledo membership. A game
    membership must be explicitly resolved in provenance by a supported
    specialist or staff evidence; home/away jersey convention is insufficient.
    """
    membership = (tracklet.get("provenance") or {}).get("team_membership") or {}
    role = tracklet.get("observed_role") if tracklet.get("role_resolved") is True else None
    unit = tracklet.get("team_label")
    result = []
    for player in roster:
        if player.get("_identity_scope") == "resolved_team_required":
            if (membership.get("resolved") is not True or not membership.get("evidence")
                    or str(membership.get("team", "")).casefold()
                    != str(player.get("_roster_team", "Toledo")).casefold()):
                continue
        position = str(player.get("position") or "").upper()
        assignment_unit = (player.get("roster_assignment") or {}).get("unit")
        if (unit in {"offense", "defense", "special_teams"}
                and assignment_unit in {"offense", "defense", "special_teams"}
                and unit != assignment_unit):
            continue
        if role and position and position != str(role).upper():
            continue
        if assignment_unit not in {"offense", "defense", "special_teams"} and unit == "offense" and position in _DEFENSIVE_POSITIONS:
            continue
        if assignment_unit not in {"offense", "defense", "special_teams"} and unit == "defense" and position in _OFFENSIVE_POSITIONS:
            continue
        result.append(player)
    return result


def _identity_conflicts(track: dict[str, Any], player_id: str,
                        tracks: list[dict[str, Any]], assigned: dict[str, str]) -> bool:
    candidates = ((track.get("provenance") or {}).get("identity") or {}).get("candidates")
    if candidates is not None and player_id not in candidates:
        return True
    points = track.get("track_points") or []
    if not points:
        return False
    start, end = min(p["frame_number"] for p in points), max(p["frame_number"] for p in points)
    for other in tracks:
        if other["id"] == track["id"] or assigned.get(other["id"]) != player_id:
            continue
        observations = other.get("track_points") or []
        if observations and max(start, min(p["frame_number"] for p in observations)) <= min(
            end, max(p["frame_number"] for p in observations)
        ):
            return True
    return False


# ── Geometry / trajectory helpers ──────────────────────────────────────────


def _point_center(point: dict[str, Any]) -> tuple[float, float] | None:
    bbox = point.get("bbox")
    if not bbox or len(bbox) < 4:
        return None
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def _tracklet_velocity(points: list[dict[str, Any]]) -> tuple[float, float]:
    """Average per-frame velocity of the bbox center over the tracklet."""
    if len(points) < 2:
        return 0.0, 0.0
    first, last = _point_center(points[0]), _point_center(points[-1])
    f0 = points[0].get("frame_number", 0)
    f1 = points[-1].get("frame_number", 0)
    if first is None or last is None or f1 <= f0:
        return 0.0, 0.0
    dt = f1 - f0
    return (last[0] - first[0]) / dt, (last[1] - first[1]) / dt


def _assign_by_trajectory(
    tracklets: list[dict[str, Any]],
    assigned_ids: dict[str, str],
    backend_api_url: str,
    *, write_identity: bool = True,
) -> int:
    """Resolve OCR/gallery-missing tracklets via constant-velocity + priors."""
    known: list[KnownPlayer] = []
    unknown: list[UnknownTrack] = []
    index_to_tracklet: dict[int, dict[str, Any]] = {}

    for i, tracklet in enumerate(tracklets):
        points = tracklet.get("track_points", [])
        if not points:
            continue
        pid = assigned_ids.get(tracklet["id"])
        if pid:
            last_center = _point_center(points[-1])
            if last_center is None:
                continue
            known.append(
                KnownPlayer(
                    player_id=pid,
                    position=last_center,
                    velocity=_tracklet_velocity(points),
                    last_frame=points[-1].get("frame_number", 0),
                    team=tracklet.get("team_label")
                    if tracklet.get("team_label") not in (None, "unknown")
                    else None,
                    position_group=tracklet.get("position_group"),
                )
            )
        else:
            start_center = _point_center(points[0])
            if start_center is None:
                continue
            index_to_tracklet[i] = tracklet
            unknown.append(
                UnknownTrack(
                    index=i,
                    position=start_center,
                    frame=points[0].get("frame_number", 0),
                    team=tracklet.get("team_label")
                    if tracklet.get("team_label") not in (None, "unknown")
                    else None,
                    position_group=tracklet.get("position_group"),
                )
            )

    if not known or not unknown:
        return 0

    assignments = TrajectoryPriorReID().assign(unknown, known)
    count = 0
    for index, player_id in assignments.items():
        tracklet = index_to_tracklet[index]
        if _identity_conflicts(tracklet, player_id, tracklets, assigned_ids):
            continue
        if write_identity:
            _patch_tracklet(tracklet["id"], player_id, backend_api_url)
        assigned_ids[tracklet["id"]] = player_id
        count += 1
    return count


def _stitch_and_propagate(
    tracklets: list[dict[str, Any]],
    assigned_ids: dict[str, str],
    backend_api_url: str,
    *, write_identity: bool = True,
) -> int:
    """Stitch fragmented tracklets and propagate player_id across each group."""
    nodes: list[TrackletNode] = []
    by_id: dict[str, dict[str, Any]] = {}
    for tracklet in tracklets:
        points = tracklet.get("track_points", [])
        if not points:
            continue
        start_center = _point_center(points[0])
        end_center = _point_center(points[-1])
        if start_center is None or end_center is None:
            continue
        by_id[tracklet["id"]] = tracklet
        nodes.append(
            TrackletNode(
                tracklet_id=tracklet["id"],
                start_frame=points[0].get("frame_number", 0),
                end_frame=points[-1].get("frame_number", 0),
                start_position=start_center,
                end_position=end_center,
                end_velocity=_tracklet_velocity(points),
            )
        )

    if len(nodes) < 2:
        return 0

    result = MinCostFlowStitcher().stitch(nodes)
    count = 0
    for group in result.groups:
        # If any tracklet in a stitched group already has an identity, share it
        # with the rest of the group. Never overwrite an existing player_id.
        known_pid = next((assigned_ids.get(tid) for tid in group if assigned_ids.get(tid)), None)
        if known_pid is None:
            continue
        for tid in group:
            if not assigned_ids.get(tid):
                if _identity_conflicts(by_id[tid], known_pid, tracklets, assigned_ids):
                    continue
                if write_identity:
                    _patch_tracklet(tid, known_pid, backend_api_url)
                assigned_ids[tid] = known_pid
                count += 1
    return count


def _frame_at(cap: Any, frame_number: int, timeline: VideoTimeline | None) -> Any:
    """The picture for a track point's frame, found by timestamp when known."""
    if timeline is None:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ok, frame = cap.read()
        return frame if ok else None
    return read_frames(cap, timeline, [frame_number]).get(frame_number)


def _extract_tracklet_embedding(
    tracklet: dict[str, Any],
    cap: Any,
    adapter: NvidiaReIDAdapter,
    *,
    timeline: VideoTimeline | None = None,
) -> np.ndarray | None:
    """Extract a representative L2-normalised embedding for a tracklet."""
    points = tracklet.get("track_points", [])
    if not points:
        return None
    mid = len(points) // 2
    sample_indices = list({0, mid, len(points) - 1})
    embeddings: list[np.ndarray] = []
    for idx in sample_indices:
        pt = points[idx]
        bbox = pt.get("bbox")
        if not bbox:
            continue
        frame = _frame_at(cap, int(pt.get("frame_number", 0)), timeline)
        if frame is None:
            continue
        x1, y1, x2, y2 = [int(v) for v in bbox]
        h, w = frame.shape[:2]
        crop = frame[max(0, y1) : min(h, y2), max(0, x1) : min(w, x2)]
        if crop.size == 0:
            continue
        try:
            embeddings.append(adapter.extract_embedding(crop))
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning("reid_embedding_failed", error=str(exc))
    if not embeddings:
        return None
    avg: np.ndarray = np.mean(embeddings, axis=0)
    norm = float(np.linalg.norm(avg))
    if norm > 1e-6:
        avg = avg / norm
    return avg


def _identify_tracklet(
    tracklet: dict[str, Any],
    cap: Any,
    jersey_map: dict[int, str],
    adapter: NvidiaReIDAdapter | None = None,
    gallery: list[tuple[np.ndarray, str]] | None = None,
    ocr_adapter: Any | None = None,
    *,
    timeline: VideoTimeline | None = None,
) -> str | None:
    """Return a player_id UUID string if we can identify this tracklet, else None."""
    points = tracklet.get("track_points", [])
    if not points:
        return None

    # Read the largest sightings (most legible) and vote: a single read of a
    # small, blurred number is often a partial ("7" of "75"), so a number is
    # only trusted when several sightings agree.
    sightings = sorted(
        (p for p in points if p.get("bbox")),
        key=lambda p: -(float(p["bbox"][3]) - float(p["bbox"][1])),
    )[:JERSEY_SAMPLES]
    votes: dict[int, int] = {}
    for pt in sightings:
        frame_number = pt.get("frame_number", 0)
        frame = _frame_at(cap, int(frame_number), timeline)
        if frame is None:
            continue
        number = _read_jersey(frame, pt["bbox"], ocr_adapter)
        if number is not None:
            votes[number] = votes.get(number, 0) + 1
    number = jersey_vote(votes)
    identity = tracklet.setdefault("provenance", {}).setdefault("identity", {})
    identity.update(jersey_votes={str(n): count for n, count in votes.items()}, jersey_number=number)
    if number is not None and number in jersey_map:
        return jersey_map[number]

    # OCR found nothing — fall back to appearance-based matching via the ReID adapter.
    if adapter is not None and gallery:
        try:
            emb = _extract_tracklet_embedding(tracklet, cap, adapter, timeline=timeline)
            if emb is not None:
                best_pid: str | None = None
                best_sim = -1.0
                for ref_emb, pid in gallery:
                    sim = adapter.match(emb, ref_emb)
                    if sim >= adapter.threshold and sim > best_sim:
                        best_sim = sim
                        best_pid = pid
                if best_pid is not None:
                    return best_pid
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning("reid_gallery_match_failed", error=str(exc))

    return None


#: Sightings read per track, and how many must agree on a number.
JERSEY_SAMPLES = 10
JERSEY_MIN_VOTES = 3
JERSEY_MIN_AGREEMENT = 0.75


def jersey_vote(votes: dict[int, int]) -> int | None:
    """The track's number if enough sightings agree, else ``None``."""
    if not votes:
        return None
    number, count = max(votes.items(), key=lambda kv: kv[1])
    if count >= JERSEY_MIN_VOTES and count / sum(votes.values()) >= JERSEY_MIN_AGREEMENT:
        return number
    return None


def _get_ocr_adapter(variant: str | None) -> Any | None:
    """Return the jersey reader: PARSeq when routed to ``parseq-ocr``, else
    EasyOCR when installed (it ships with the worker for scoreboards), else
    ``None`` (Tesseract fallback, usually not installed).

    Imported lazily so heavy OCR imports never enter module import or
    ``pytest`` collection.
    """
    if variant == PARSEQ_OCR:
        from pipeline.tracking.parseq_ocr_adapter import get_parseq_adapter

        adapter = get_parseq_adapter()
        if adapter is not None:
            return adapter
    from pipeline.tracking.easyocr_jersey import get_easyocr_jersey_reader

    return get_easyocr_jersey_reader()


def _ocr_backend_name(ocr_adapter: Any | None) -> str:
    if ocr_adapter is None:
        return "jersey-ocr"

    from pipeline.tracking.easyocr_jersey import EasyOCRJerseyReader

    if isinstance(ocr_adapter, EasyOCRJerseyReader):
        return "easyocr-jersey"

    from pipeline.tracking.parseq_ocr_adapter import PARSeqOCRAdapter

    if isinstance(ocr_adapter, PARSeqOCRAdapter):
        return PARSEQ_OCR
    return "jersey-ocr"


def _read_jersey(frame: Any, bbox: list[float], ocr_adapter: Any | None) -> int | None:
    """Read a jersey number, preferring PARSeq when available, else Tesseract."""
    if ocr_adapter is not None:
        from pipeline.tracking.easyocr_jersey import EasyOCRJerseyReader, torso_crop

        if isinstance(ocr_adapter, EasyOCRJerseyReader):
            crop = torso_crop(frame, bbox)
        else:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            h, w = frame.shape[:2]
            crop = frame[max(0, y1) : min(h, y2), max(0, x1) : min(w, x2)]
        if crop is not None and crop.size > 0:
            number, _conf = ocr_adapter.recognize(crop)
            if number is not None:
                return number
    return _ocr_jersey(frame, bbox)


def _ocr_jersey(frame: Any, bbox: list[float]) -> int | None:
    """Crop the bbox, run Tesseract on it, return jersey number or None."""
    try:
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return None  # Tesseract not installed in this environment

    x1, y1, x2, y2 = [int(v) for v in bbox]
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    data = pytesseract.image_to_data(
        thresh,
        config="--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789",
        output_type=pytesseract.Output.DICT,
    )
    for text, conf in zip(data["text"], data["conf"]):
        text = text.strip()
        if text and re.fullmatch(r"\d{1,2}", text) and int(conf) >= MIN_OCR_CONFIDENCE:
            return int(text)
    return None


# ── NVIDIA TAO ReIdentificationNet adapter (Phase 2.5 spike) ──────────────


class NvidiaReIDAdapter:
    """Optional Re-ID adapter using NVIDIA TAO ReIdentificationNet.

    Extracts 256-d appearance embeddings from player bounding-box crops and
    compares them across tracklets using cosine similarity.  Falls back
    gracefully to jersey OCR when weights or VRAM are unavailable.

    Activation: set ``REID_MODEL=nvidia-tao:/path/to/resnet50_reid.onnx``.

    The model expects 256×128 RGB crops and outputs a 256-d L2-normalised
    embedding vector.

    Args:
        model_path: Path to the ``.onnx`` weights file.
        device_id:  CUDA device ordinal (default 0).
        threshold:  Cosine-similarity threshold for a positive match (default 0.6).
    """

    def __init__(
        self,
        model_path: str,
        device_id: int = 0,
        threshold: float = 0.6,
    ) -> None:
        from pathlib import Path as _P

        weights = _P(model_path)
        if not weights.is_file():
            raise FileNotFoundError(
                f"TAO ReIdentificationNet weights not found: {weights}. "
                "Download from NGC: ngc registry model download-version "
                "nvidia/tao/reidentificationnet:deployable_onnx_v1.0"
            )

        try:
            import onnxruntime as ort  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "onnxruntime-gpu is required for NvidiaReIDAdapter. "
                "Install with: pip install onnxruntime-gpu"
            ) from exc

        providers = [("CUDAExecutionProvider", {"device_id": device_id})]
        self._session = ort.InferenceSession(str(weights), providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        self._threshold = threshold
        log.info("nvidia_reid_initialized", model_path=model_path, device_id=device_id)

    def extract_embedding(self, crop: np.ndarray) -> np.ndarray:
        """Return a 256-d L2-normalised embedding for a BGR player crop."""
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (128, 256))
        blob = resized.astype(np.float32).transpose(2, 0, 1)[np.newaxis] / 255.0
        outputs = self._session.run(None, {self._input_name: blob})
        emb = outputs[0].flatten().astype(np.float64)
        norm = np.linalg.norm(emb)
        if norm > 1e-6:
            emb /= norm
        return emb

    def match(self, emb_a: np.ndarray, emb_b: np.ndarray) -> float:
        """Return cosine similarity between two embeddings."""
        return float(np.dot(emb_a, emb_b))

    @property
    def threshold(self) -> float:
        """Cosine-similarity threshold for a positive match."""
        return self._threshold

    def is_match(self, emb_a: np.ndarray, emb_b: np.ndarray) -> bool:
        return self.match(emb_a, emb_b) >= self._threshold


_NVIDIA_TAO_PREFIX = "nvidia-tao:"


def _get_reid_adapter() -> NvidiaReIDAdapter | None:
    """Return an NvidiaReIDAdapter if configured and available, else None."""
    reid_model = os.environ.get("REID_MODEL", "")
    if not reid_model.startswith(_NVIDIA_TAO_PREFIX):
        return None
    weights = reid_model[len(_NVIDIA_TAO_PREFIX) :]
    try:
        return NvidiaReIDAdapter(weights)
    except (ImportError, FileNotFoundError, RuntimeError) as exc:
        log.warning("nvidia_reid_unavailable_fallback", error=str(exc))
        return None


def _patch_tracklet(tracklet_id: str, player_id: str, backend_api_url: str) -> None:
    # Route through pipeline.backend so the worker's service-account token is
    # attached (a bare client here would 401 silently).
    if not backend_api_url:
        return
    from pipeline import backend as backend_mod

    backend_mod.patch_tracklet_player(tracklet_id, player_id)
