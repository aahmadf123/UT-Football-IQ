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
PATCH /api/v1/tracklets/{id} — no tracklet schema changes, single-camera only.
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
from pipeline.tracking.min_cost_flow_stitcher import (
    MinCostFlowStitcher,
    TrackletNode,
)
from pipeline.tracking.trajectory_prior_reid import (
    KnownPlayer,
    TrajectoryPriorReID,
    UnknownTrack,
)

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
    log.info(
        "stage_reid_start",
        clip_id=clip_id,
        tracklet_count=len(tracklets),
        variant=variant,
        nightly=nightly,
    )

    jersey_map: dict[int, str] = {
        p["jersey_number"]: p["id"]
        for p in roster
        if p.get("jersey_number") is not None
    }

    adapter = _get_reid_adapter()
    ocr_adapter = _get_ocr_adapter(variant)
    cap = cv2.VideoCapture(str(video_path))

    # tracklet_id -> player_id, covering both pre-identified and newly assigned.
    assigned_ids: dict[str, str] = {}
    assigned = 0

    # Gallery of (embedding, player_id) built from already-identified tracklets.
    gallery: list[tuple[np.ndarray, str]] = []

    # ── Layers 1+2: OCR (PARSeq/Tesseract) and appearance gallery ──────────
    for tracklet in tracklets:
        if tracklet.get("player_id"):
            assigned_ids[tracklet["id"]] = tracklet["player_id"]
            if adapter is not None:
                emb = _extract_tracklet_embedding(tracklet, cap, adapter)
                if emb is not None:
                    gallery.append((emb, tracklet["player_id"]))
            continue

        player_id = _identify_tracklet(
            tracklet, cap, jersey_map, adapter, gallery, ocr_adapter
        )
        if player_id:
            _patch_tracklet(tracklet["id"], player_id, backend_api_url)
            assigned_ids[tracklet["id"]] = player_id
            assigned += 1
            if adapter is not None:
                emb = _extract_tracklet_embedding(tracklet, cap, adapter)
                if emb is not None:
                    gallery.append((emb, player_id))

    cap.release()

    # ── Layer 3: trajectory-prior re-ID for the still-unidentified ─────────
    trajectory_assigned = _assign_by_trajectory(
        tracklets, assigned_ids, backend_api_url
    )
    assigned += trajectory_assigned

    # ── Layer 4: min-cost-flow stitching (nightly only) ────────────────────
    stitched_assigned = 0
    if nightly:
        stitched_assigned = _stitch_and_propagate(
            tracklets, assigned_ids, backend_api_url
        )
        assigned += stitched_assigned

    log.info(
        "stage_reid_done",
        clip_id=clip_id,
        assigned=assigned,
        trajectory_assigned=trajectory_assigned,
        stitched_assigned=stitched_assigned,
    )
    return {
        "assigned": assigned,
        "total": len(tracklets),
        "reid_strategy": {
            "ocr": PARSEQ_OCR if ocr_adapter is not None else "jersey-ocr",
            "trajectory_prior_assigned": trajectory_assigned,
            "min_cost_flow_stitched": stitched_assigned,
            "stitching_enabled": nightly,
        },
    }


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
        _patch_tracklet(tracklet["id"], player_id, backend_api_url)
        assigned_ids[tracklet["id"]] = player_id
        count += 1
    return count


def _stitch_and_propagate(
    tracklets: list[dict[str, Any]],
    assigned_ids: dict[str, str],
    backend_api_url: str,
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
                _patch_tracklet(tid, known_pid, backend_api_url)
                assigned_ids[tid] = known_pid
                count += 1
    return count


def _extract_tracklet_embedding(
    tracklet: dict[str, Any],
    cap: Any,
    adapter: NvidiaReIDAdapter,
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
        cap.set(cv2.CAP_PROP_POS_FRAMES, pt.get("frame_number", 0))
        ret, frame = cap.read()
        if not ret:
            continue
        x1, y1, x2, y2 = [int(v) for v in bbox]
        h, w = frame.shape[:2]
        crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        if crop.size == 0:
            continue
        try:
            embeddings.append(adapter.extract_embedding(crop))
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
) -> str | None:
    """Return a player_id UUID string if we can identify this tracklet, else None."""
    points = tracklet.get("track_points", [])
    if not points:
        return None

    # Sample a few frames near the middle of the track
    mid = len(points) // 2
    sample_indices = list({0, mid, len(points) - 1})
    for idx in sample_indices:
        pt = points[idx]
        bbox = pt.get("bbox")
        if not bbox:
            continue
        frame_number = pt.get("frame_number", 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = cap.read()
        if not ret:
            continue

        number = _read_jersey(frame, bbox, ocr_adapter)
        if number is not None and number in jersey_map:
            return jersey_map[number]

    # OCR found nothing — fall back to appearance-based matching via the ReID adapter.
    if adapter is not None and gallery:
        try:
            emb = _extract_tracklet_embedding(tracklet, cap, adapter)
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
        except Exception as exc:
            log.warning("reid_gallery_match_failed", error=str(exc))

    return None


def _get_ocr_adapter(variant: str | None) -> Any | None:
    """Return a PARSeq OCR adapter when routed to ``parseq-ocr``, else None.

    Imported lazily so the heavy PARSeq/torch import never enters module import
    or ``pytest`` collection. Returns ``None`` (Tesseract fallback) when the
    variant is not ``parseq-ocr`` or PARSeq is unavailable at runtime.
    """
    if variant != PARSEQ_OCR:
        return None
    from pipeline.tracking.parseq_ocr_adapter import get_parseq_adapter

    return get_parseq_adapter()


def _read_jersey(frame: Any, bbox: list[float], ocr_adapter: Any | None) -> int | None:
    """Read a jersey number, preferring PARSeq when available, else Tesseract."""
    if ocr_adapter is not None:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        h, w = frame.shape[:2]
        crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        if crop.size > 0:
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
    weights = reid_model[len(_NVIDIA_TAO_PREFIX):]
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
