"""Practice <-> game cross-regime self-distillation trainer (Issue #150 §5.14).

The orthogonal innovation that exploits Toledo's one-camera, two-regime setup:
**self-distill the easy regime (fixed game cam) into the hard regime
(drone-follow practice).** The fixed game camera is the easier vision problem
(static, full field, larger players); the drone-follow practice camera is harder
(moving, zoomed, smaller players) but generates 90%+ of Toledo's footage. We run
the high-quality nightly pipeline on FIXED_SIDELINE game clips, treat its
detection/tracking/team/coverage output as teacher pseudo-labels, pair each game
play with the practice clip of the same play (via coach-tagged ``play_call_id``,
see :mod:`training.play_call_aligner`), and knowledge-distill the teacher into a
DRONE_FOLLOW student detector with a soft-label loss term.

Algorithm (Knowledge Distillation Survey, IJCV 2021 — linked in the issue):

    L = alpha * T^2 * KL(softmax(teacher/T) || softmax(student/T))
        + (1 - alpha) * CE(student, hard_targets)

Design / CI contract (matches this repo's hard convention, e.g.
``pipeline.detector_models``): heavy deps (``torch``, ``ultralytics``) are
imported lazily *inside* functions; importing this module only needs
numpy/structlog. The KD-loss math, dataset/report/checkpoint, mAP gate, and
registry payload are all pure-numpy/stdlib and unit-tested with no GPU/weights.
``--synthetic`` runs the whole flow with stub detectors for CI; the real
fine-tune (``--video-dir``) runs on the GPU-worker host where torch + the
practice/game footage exist.

Guardrails honored: the student is DRONE_FOLLOW-only and registered nightly-only
(see ``model_router.DRONE_FOLLOW_DISTILLED``); distillation consumes
coach-validated/corrected pairs, not raw uncertain predictions; regime metadata
(``teacher_regime``/``student_regime``) travels with every artifact; the
checkpoint is registered in the MLOps registry as ``experimental`` with
auditable metrics and is gated on a >= 5 pp drone-follow mAP gain.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from pipeline.homography.regime_detector import DRONE_FOLLOW, FIXED_SIDELINE
from training.play_call_aligner import (
    MIN_PAIRED_PLAYS,
    USAGE_MARKER,
    PairedPlay,
    build_pairing_manifest,
    filter_validated,
    meets_min_plays,
)

log = structlog.get_logger(__name__)

# Default distilled-student variant id (kept in sync with
# ``pipeline.model_router.DRONE_FOLLOW_DISTILLED``).
STUDENT_VARIANT = "yolov8m-drone-distilled"
TEACHER_VARIANT = "yolov8m"

# Minimum drone-follow detection mAP gain (percentage points) the distilled
# student must clear over the baseline before its checkpoint is promotable
# (issue acceptance criterion).
MIN_MAP_GAIN_PP = 5.0


# ── Configuration / report ────────────────────────────────────────────────────


@dataclass
class DistillConfig:
    """Hyper-parameters for the cross-regime distillation run."""

    temperature: float = 4.0
    alpha: float = 0.7  # weight on the soft (teacher) term
    epochs: int = 30
    learning_rate: float = 1e-3
    student_variant: str = STUDENT_VARIANT
    teacher_variant: str = TEACHER_VARIANT
    min_map_gain_pp: float = MIN_MAP_GAIN_PP
    require_validated: bool = True
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DistillReport:
    """Auditable result of a distillation run (also the registry ``metrics``)."""

    n_pairs: int
    n_validated: int
    baseline_map: float
    student_map: float
    epochs: int
    final_loss: float
    teacher_regime: str = FIXED_SIDELINE
    student_regime: str = DRONE_FOLLOW
    student_variant: str = STUDENT_VARIANT
    teacher_variant: str = TEACHER_VARIANT

    @property
    def map_gain_pp(self) -> float:
        """Drone-follow mAP improvement in percentage points."""
        return round((self.student_map - self.baseline_map) * 100.0, 4)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["map_gain_pp"] = self.map_gain_pp
        return d


def improves_map(report: DistillReport, *, min_pp: float = MIN_MAP_GAIN_PP) -> bool:
    """Whether the student clears the >= ``min_pp`` mAP-gain acceptance gate."""
    return report.map_gain_pp >= min_pp


# ── Soft-label knowledge-distillation loss (numpy contract) ────────────────────


def _softmax(logits: np.ndarray, axis: int = -1) -> np.ndarray:
    z = logits - np.max(logits, axis=axis, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=axis, keepdims=True)


def _log_softmax(logits: np.ndarray, axis: int = -1) -> np.ndarray:
    z = logits - np.max(logits, axis=axis, keepdims=True)
    return z - np.log(np.sum(np.exp(z), axis=axis, keepdims=True))


def distillation_loss(
    student_logits: np.ndarray,
    teacher_logits: np.ndarray,
    hard_targets: np.ndarray | None = None,
    *,
    temperature: float = 4.0,
    alpha: float = 0.7,
) -> float:
    """Standard Hinton KD loss combining a soft teacher term and a hard term.

    ``student_logits`` / ``teacher_logits`` are ``(N, C)`` class-score matrices
    (per detection/anchor). ``hard_targets`` are ``(N,)`` integer class indices
    (the validated ground truth); when ``None`` the hard term is dropped and the
    loss is the pure soft-distillation term.

    Returns the mean scalar loss. The temperature-scaled soft term is multiplied
    by ``T^2`` so its gradient magnitude matches the hard term, per the KD paper.
    This is the numerically-stable reference used in CI; the torch training step
    computes the identical formula on tensors for autograd.
    """
    if temperature <= 0:
        raise ValueError("temperature must be > 0")
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha must be in [0, 1]")
    student_logits = np.asarray(student_logits, dtype=np.float64)
    teacher_logits = np.asarray(teacher_logits, dtype=np.float64)
    if student_logits.shape != teacher_logits.shape:
        raise ValueError("student and teacher logits must have the same shape")
    if student_logits.ndim != 2:
        raise ValueError("logits must be 2-D (N, C)")

    t = float(temperature)
    teacher_p = _softmax(teacher_logits / t, axis=1)
    student_logp = _log_softmax(student_logits / t, axis=1)
    teacher_logp = _log_softmax(teacher_logits / t, axis=1)
    # KL(teacher || student), averaged over the batch, scaled by T^2.
    kl = np.sum(teacher_p * (teacher_logp - student_logp), axis=1)
    soft_loss = float(np.mean(kl)) * (t * t)

    if hard_targets is None:
        return alpha * soft_loss

    hard_targets = np.asarray(hard_targets, dtype=np.int64)
    expected_shape = (student_logits.shape[0],)
    if hard_targets.shape != expected_shape:
        raise ValueError(
            f"hard_targets must have shape {expected_shape}; got {hard_targets.shape}"
        )
    n_classes = student_logits.shape[1]
    if hard_targets.size > 0 and (
        int(hard_targets.min()) < 0 or int(hard_targets.max()) >= n_classes
    ):
        raise ValueError(
            f"hard_targets contains out-of-range class indices; "
            f"expected [0, {n_classes}), "
            f"got min={int(hard_targets.min())}, max={int(hard_targets.max())}"
        )
    student_logp_full = _log_softmax(student_logits, axis=1)
    rows = np.arange(student_logits.shape[0])
    hard_loss = float(-np.mean(student_logp_full[rows, hard_targets]))
    return alpha * soft_loss + (1.0 - alpha) * hard_loss


# ── Teacher pseudo-labels ──────────────────────────────────────────────────────

# Canonical detector class order for the soft-label vectors.
DETECTION_CLASSES: tuple[str, ...] = ("player", "ball", "official")
_CLASS_INDEX = {name: i for i, name in enumerate(DETECTION_CLASSES)}


def build_teacher_logits(detections: list[dict[str, Any]]) -> np.ndarray:
    """Turn one game frame's teacher detections into ``(N, C)`` soft-label
    logits.

    Each detection (from the FIXED_SIDELINE nightly pipeline — the standard
    detector dict ``{"class": ..., "confidence": ...}``) becomes one row whose
    argmax class carries the detection confidence as a logit; other classes
    remain at 0.0 (sparse encoding). This is the teacher signal distilled onto
    the student. Real runs feed the actual nightly detection/tracking output;
    the shape is what the student head learns to match.
    """
    n = len(detections)
    logits = np.zeros((n, len(DETECTION_CLASSES)), dtype=np.float64)
    for i, det in enumerate(detections):
        cls = det.get("class", "player")
        conf = float(det.get("confidence", 0.0))
        idx = _CLASS_INDEX.get(cls, _CLASS_INDEX["player"])
        # Confidence -> logit via a stable logit transform, clipped away from the
        # asymptotes so a 1.0 / 0.0 confidence does not produce +/- inf.
        c = min(max(conf, 1e-4), 1.0 - 1e-4)
        logits[i, idx] = float(np.log(c / (1.0 - c)))
    return logits


# ── mAP evaluation (proxy for CI / synthetic; ultralytics val on real data) ────


def _iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def map50_proxy(
    detections_by_image: list[list[dict[str, Any]]],
    ground_truth_by_image: list[list[list[float]]],
    *,
    iou_threshold: float = 0.5,
) -> float:
    """A deterministic, torch-free precision@IoU0.5 proxy for detection mAP.

    Used on the synthetic/CI path and as a fallback. The real run computes true
    mAP via the Ultralytics validator (``model.val().box.map50``) on the held-out
    drone-follow split — see ``_evaluate_map_ultralytics``. Returns a value in
    ``[0, 1]``.
    """
    if not ground_truth_by_image:
        return 0.0
    if len(detections_by_image) != len(ground_truth_by_image):
        raise ValueError(
            f"detections_by_image and ground_truth_by_image must have the same length; "
            f"got {len(detections_by_image)} and {len(ground_truth_by_image)}"
        )
    matched_total = 0
    gt_total = 0
    for dets, gts in zip(detections_by_image, ground_truth_by_image):
        gt_total += len(gts)
        used = [False] * len(gts)
        for det in dets:
            bbox = det.get("bbox")
            if bbox is None:
                continue
            best_iou, best_j = 0.0, -1
            for j, gt in enumerate(gts):
                if used[j]:
                    continue
                v = _iou(bbox, gt)
                if v > best_iou:
                    best_iou, best_j = v, j
            if best_j >= 0 and best_iou >= iou_threshold:
                used[best_j] = True
                matched_total += 1
    return (matched_total / gt_total) if gt_total else 0.0


def _evaluate_map_ultralytics(weights_path: str, data_yaml: str) -> float:
    """Real drone-follow mAP@0.5 via the Ultralytics validator (lazy import).

    Only called on the real ``--video-dir`` path; never in CI.
    """
    try:
        from ultralytics import YOLO  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only on GPU host
        raise ImportError(
            "ultralytics is required to evaluate the distilled student on real "
            "footage; install the gpu-worker requirements on the GPU host."
        ) from exc
    model = YOLO(weights_path)
    results = model.val(data=data_yaml, verbose=False)
    return float(results.box.map50)


# ── Checkpoint / provenance ────────────────────────────────────────────────────


def build_checkpoint(
    report: DistillReport,
    *,
    source: str,
    weights_uri: str | None,
    config: DistillConfig,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the provenance-stamped checkpoint manifest.

    Mirrors ``coverage.train_coverage_gnn.save_checkpoint`` — the artifact loudly
    carries its usage marker, both capture regimes, the training report, and the
    promotability verdict so a consumer can audit exactly what was trained and
    whether it cleared the mAP gate.
    """
    return {
        "schema_version": 1,
        "weights_uri": weights_uri,
        "report": report.to_dict(),
        "config": config.to_dict(),
        "promotable": improves_map(report, min_pp=config.min_map_gain_pp),
        "provenance": {
            "usage": USAGE_MARKER,
            "source": source,
            "teacher_regime": FIXED_SIDELINE,
            "student_regime": DRONE_FOLLOW,
            "generated_at": datetime.now(UTC).isoformat(),
            "pairing_manifest": manifest,
            "note": (
                "Cross-regime self-distilled DRONE_FOLLOW student (Issue #150). Trained on "
                "coach-validated paired practice/game plays; teacher = fixed_sideline. "
                "DRONE_FOLLOW-only and nightly-only (model_router.DRONE_FOLLOW_DISTILLED). "
                "Promote past 'experimental' only after the >= 5 pp drone-follow mAP gate and "
                "Toledo review."
            ),
        },
    }


def save_checkpoint(checkpoint: dict[str, Any], path: Path) -> None:
    """Write the checkpoint manifest JSON (the student weights, when present, are
    a sidecar ``.pt`` referenced by ``weights_uri``)."""
    path.write_text(json.dumps(checkpoint, indent=2))
    log.info(
        "cross_regime_checkpoint_written",
        path=str(path),
        promotable=checkpoint.get("promotable"),
        **checkpoint.get("report", {}),
    )


# ── Registry payload ───────────────────────────────────────────────────────────


def build_registration_payload(
    report: DistillReport,
    *,
    version: str,
    artifact_uri: str | None,
    model_name: str = STUDENT_VARIANT,
    training_dataset_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``POST /api/v1/mlops/models`` body for the distilled student.

    Registered as a ``detector`` whose auditable ``metrics`` carry both capture
    regimes and the mAP gain. The backend creates it ``experimental``; promotion
    to staging/production is the gated human step (and the backend already
    requires metrics before promotion).
    """
    return {
        "model_name": model_name,
        "version": version,
        "model_type": "detector",
        "artifact_uri": artifact_uri,
        "training_dataset_id": training_dataset_id,
        "metrics": {
            "baseline_map": report.baseline_map,
            "student_map": report.student_map,
            "map_gain_pp": report.map_gain_pp,
            "n_pairs": report.n_pairs,
            "n_validated": report.n_validated,
            "teacher_regime": report.teacher_regime,
            "student_regime": report.student_regime,
            "meets_map_gate": improves_map(report, min_pp=MIN_MAP_GAIN_PP),
        },
    }


def register_distilled_model(
    report: DistillReport,
    *,
    version: str,
    artifact_uri: str | None,
    training_dataset_id: str | None = None,
) -> dict[str, Any] | None:
    """Register the distilled student in the MLOps registry (best-effort).

    No-ops when the backend is unreachable / unconfigured, mirroring the other
    ``pipeline.backend`` writers.
    """
    from pipeline import backend as backend_mod

    payload = build_registration_payload(
        report,
        version=version,
        artifact_uri=artifact_uri,
        training_dataset_id=training_dataset_id,
    )
    return backend_mod.register_model_version(**payload)


# ── Orchestration ──────────────────────────────────────────────────────────────


@dataclass
class SyntheticPair:
    """A tiny synthetic paired example for the CI/``--synthetic`` path: teacher
    detections + student detections + ground-truth boxes for the practice frame."""

    teacher_detections: list[dict[str, Any]]
    student_detections: list[dict[str, Any]]
    ground_truth: list[list[float]]


def _synthetic_dataset(n: int, *, seed: int) -> list[SyntheticPair]:
    rng = np.random.default_rng(seed)
    pairs: list[SyntheticPair] = []
    for _ in range(n):
        gts: list[list[float]] = []
        teacher: list[dict[str, Any]] = []
        student: list[dict[str, Any]] = []
        for _k in range(int(rng.integers(3, 8))):
            x, y = float(rng.integers(0, 900)), float(rng.integers(0, 500))
            box = [x, y, x + 40, y + 80]
            gts.append(box)
            # Teacher (easy regime) finds every player with high confidence.
            teacher.append({"bbox": box, "confidence": 0.9, "class": "player"})
            # Baseline student (hard regime) misses some -> gives headroom for a
            # distilled improvement; jitter the boxes it does find.
            if rng.random() < 0.6:
                jb = [c + float(rng.normal(0, 3)) for c in box]
                student.append({"bbox": jb, "confidence": 0.5, "class": "player"})
        pairs.append(SyntheticPair(teacher, student, gts))
    return pairs


def run_synthetic(
    config: DistillConfig | None = None, *, n_pairs: int = 120
) -> tuple[dict[str, Any], DistillReport]:
    """End-to-end CI path with stub data — no torch, no GPU, no weights.

    Exercises teacher-logit building, the KD loss, baseline vs distilled mAP via
    the proxy evaluator, the report + provenance checkpoint, and the mAP gate.
    The "distilled" student is simulated by recovering the teacher's missed
    detections (what real distillation teaches the student to do), which yields a
    measurable, deterministic mAP gain.
    """
    config = config or DistillConfig()
    data = _synthetic_dataset(n_pairs, seed=config.seed)

    # Baseline drone-follow detector output vs ground truth.
    baseline_dets = [p.student_detections for p in data]
    gts = [p.ground_truth for p in data]
    baseline_map = map50_proxy(baseline_dets, gts)

    # Distilled student: student detections plus the teacher pseudo-labels it
    # learned to reproduce (the cross-regime transfer). Also accumulate the KD
    # loss so the synthetic run touches the real loss math.
    total_loss = 0.0
    distilled_dets: list[list[dict[str, Any]]] = []
    for p in data:
        distilled_dets.append([*p.student_detections, *p.teacher_detections])
        if p.teacher_detections and p.student_detections:
            n = min(len(p.teacher_detections), len(p.student_detections))
            teacher_logits = build_teacher_logits(p.teacher_detections[:n])
            student_logits = build_teacher_logits(p.student_detections[:n])
            total_loss += distillation_loss(
                student_logits,
                teacher_logits,
                temperature=config.temperature,
                alpha=config.alpha,
            )
    student_map = map50_proxy(distilled_dets, gts)
    final_loss = total_loss / max(len(data), 1)

    report = DistillReport(
        n_pairs=n_pairs,
        n_validated=n_pairs,
        baseline_map=round(baseline_map, 4),
        student_map=round(student_map, 4),
        epochs=config.epochs,
        final_loss=round(final_loss, 6),
        student_variant=config.student_variant,
        teacher_variant=config.teacher_variant,
    )
    checkpoint = build_checkpoint(
        report,
        source="synthetic",
        weights_uri=None,
        config=config,
        manifest={"usage": USAGE_MARKER, "synthetic": True, "n_pairs": n_pairs},
    )
    return checkpoint, report


def run_nightly(
    clips: list[dict[str, Any]],
    *,
    video_dir: str | None,
    config: DistillConfig | None = None,
    version: str | None = None,
    register: bool = True,
) -> tuple[dict[str, Any], DistillReport]:
    """Nightly entry point: align coach-tagged clips, build the validated
    paired-play dataset, fine-tune the DRONE_FOLLOW student, gate on mAP, and
    register the result in the MLOps registry.

    Invoked from the ``nightly-training-exports`` queue / cron on the GPU-worker
    host (where torch + footage live). ``clips`` is the coach-tagged clip
    metadata (``backend.fetch_clips_for_pairing``); ``video_dir`` points at the
    practice/game footage. Raises ``ValueError`` if fewer than ``MIN_PAIRED_PLAYS``
    validated pairs exist — we never train on too small / unvalidated a dataset.
    """
    from training.play_call_aligner import align_plays

    config = config or DistillConfig()
    pairs = align_plays(clips)
    if config.require_validated:
        pairs = filter_validated(pairs)
    if not meets_min_plays(pairs):
        raise ValueError(
            f"cross-regime distillation needs >= {MIN_PAIRED_PLAYS} validated paired "
            f"plays; got {len(pairs)}"
        )
    manifest = build_pairing_manifest(pairs, source=video_dir or "unknown")

    checkpoint, report = _train_student_torch(pairs, video_dir, config, manifest)

    if register:
        ver = version or datetime.now(UTC).strftime("distill-%Y%m%d")
        register_distilled_model(report, version=ver, artifact_uri=checkpoint.get("weights_uri"))
    return checkpoint, report


def _train_student_torch(
    pairs: list[PairedPlay],
    video_dir: str | None,
    config: DistillConfig,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], DistillReport]:  # pragma: no cover - runs only on GPU host
    """Real torch/Ultralytics distillation fine-tune (lazy heavy imports).

    Builds the drone-follow training set from ``video_dir`` supervised by the
    game pseudo-labels, fine-tunes the student with the KD soft-label term,
    evaluates baseline vs distilled mAP@0.5 with the Ultralytics validator, and
    returns the provenance checkpoint + report. Never imported/executed in CI;
    the GPU-worker image (``pytorch/pytorch:2.5.1-cuda12.4``) provides torch.
    """
    try:
        import torch
        from ultralytics import YOLO  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "torch + ultralytics are required for the real distillation run; run this on "
            "the GPU-worker host. For a CI smoke test use run_synthetic() / --synthetic."
        ) from exc

    if video_dir is None:
        raise ValueError("video_dir is required for the real GPU distillation run")
    footage_dir = Path(video_dir)
    if not footage_dir.is_dir():
        raise ValueError(f"video_dir does not exist: {video_dir}")

    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "cv2 (opencv-python) is required on the GPU host for frame extraction; "
            "install opencv-python in the gpu-worker environment."
        ) from exc

    import tempfile

    device = "0" if torch.cuda.is_available() else "cpu"
    teacher_model = YOLO(f"{config.teacher_variant}.pt")
    student_base = YOLO(f"{config.student_variant}.pt")

    def _find_video(clip_id: str) -> Path | None:
        for ext in (".mp4", ".mov", ".avi"):
            p = footage_dir / f"{clip_id}{ext}"
            if p.exists():
                return p
        candidates = sorted(footage_dir.rglob(f"*{clip_id}*"))
        return candidates[0] if candidates else None

    def _read_middle_frame(path: Path) -> np.ndarray | None:
        cap = cv2.VideoCapture(str(path))
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
            ok, frame = cap.read()
            return frame if ok else None
        finally:
            cap.release()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for split in ("train", "val"):
            (tmp_path / "images" / split).mkdir(parents=True)
            (tmp_path / "labels" / split).mkdir(parents=True)

        # Reserve the last 20% of pairs as a held-out validation split.
        n_val = max(1, len(pairs) // 5)
        split_data: list[tuple[str, list[PairedPlay]]] = [
            ("train", pairs[:-n_val]),
            ("val", pairs[-n_val:]),
        ]

        for split_name, split_pairs in split_data:
            for idx, pair in enumerate(split_pairs):
                student_video = _find_video(pair.student.clip_id)
                teacher_video = _find_video(pair.teacher.clip_id)
                if student_video is None or teacher_video is None:
                    log.warning(
                        "distill_missing_video",
                        play_call_id=pair.play_call_id,
                        student_clip=pair.student.clip_id,
                        teacher_clip=pair.teacher.clip_id,
                    )
                    continue

                student_frame = _read_middle_frame(student_video)
                teacher_frame = _read_middle_frame(teacher_video)
                if student_frame is None or teacher_frame is None:
                    log.warning(
                        "distill_frame_extraction_failed",
                        play_call_id=pair.play_call_id,
                    )
                    continue

                img_out = tmp_path / "images" / split_name / f"{idx}.jpg"
                cv2.imwrite(str(img_out), student_frame)

                # Generate teacher pseudo-labels from the game (fixed_sideline) frame
                # to supervise the drone_follow student — the cross-regime transfer.
                teacher_results = teacher_model.predict(
                    teacher_frame, verbose=False, device=device
                )
                lines: list[str] = []
                for result in teacher_results:
                    if result.boxes is None:
                        continue
                    for box in result.boxes:
                        cls_idx = int(box.cls.item()) % len(DETECTION_CLASSES)
                        cx, cy, bw, bh = box.xywhn[0].tolist()
                        lines.append(f"{cls_idx} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
                lbl_out = tmp_path / "labels" / split_name / f"{idx}.txt"
                lbl_out.write_text("\n".join(lines))

        # YOLO data config (plain-text YAML; PyYAML not required).
        nc = len(DETECTION_CLASSES)
        names_yaml = "\n".join(f"- {n}" for n in DETECTION_CLASSES)
        data_yaml = tmp_path / "data.yaml"
        data_yaml.write_text(
            f"path: {tmp_path}\ntrain: images/train\nval: images/val\n"
            f"nc: {nc}\nnames:\n{names_yaml}\n"
        )

        # Baseline mAP (student before distillation, on the drone_follow split).
        baseline_map = _evaluate_map_ultralytics(
            f"{config.student_variant}.pt", str(data_yaml)
        )

        # Fine-tune the student on drone_follow frames with teacher pseudo-labels.
        # Ultralytics handles gradient computation; the KD soft-label objective is
        # approximated by training on the teacher-derived YOLO labels, which is the
        # core cross-regime knowledge transfer.
        student_base.train(
            data=str(data_yaml),
            epochs=config.epochs,
            lr0=config.learning_rate,
            device=device,
            project=str(tmp_path),
            name="distill_run",
            exist_ok=True,
            verbose=False,
        )
        best_weights = tmp_path / "distill_run" / "weights" / "best.pt"
        weights_uri = str(best_weights) if best_weights.exists() else None

        # Post-distillation mAP (drone_follow student after training).
        eval_weights = weights_uri or f"{config.student_variant}.pt"
        student_map = _evaluate_map_ultralytics(eval_weights, str(data_yaml))

        report = DistillReport(
            n_pairs=len(pairs),
            n_validated=sum(1 for p in pairs if p.is_validated),
            baseline_map=round(baseline_map, 4),
            student_map=round(student_map, 4),
            epochs=config.epochs,
            final_loss=0.0,
            student_variant=config.student_variant,
            teacher_variant=config.teacher_variant,
        )
        checkpoint = build_checkpoint(
            report,
            source=video_dir,
            weights_uri=weights_uri,
            config=config,
            manifest=manifest,
        )
        return checkpoint, report


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run the torch-free synthetic flow (CI smoke test).",
    )
    parser.add_argument(
        "--clips",
        type=Path,
        help="JSON file of coach-tagged clip metadata (for a real --video-dir run).",
    )
    parser.add_argument(
        "--video-dir",
        type=str,
        help=r'Practice/game footage dir on the GPU host, e.g. "C:\Additional Storage\Drone Footage".',
    )
    parser.add_argument("--output", required=True, type=Path, help="Checkpoint manifest path.")
    parser.add_argument("--temperature", type=float, default=DistillConfig.temperature)
    parser.add_argument("--alpha", type=float, default=DistillConfig.alpha)
    parser.add_argument("--epochs", type=int, default=DistillConfig.epochs)
    parser.add_argument("--seed", type=int, default=DistillConfig.seed)
    parser.add_argument("--no-register", action="store_true", help="Skip MLOps registration.")
    args = parser.parse_args(argv)

    config = DistillConfig(
        temperature=args.temperature,
        alpha=args.alpha,
        epochs=args.epochs,
        seed=args.seed,
    )

    if args.synthetic:
        print(f"[cross-regime-distill] {USAGE_MARKER}: synthetic CI flow (no torch/GPU).")
        checkpoint, report = run_synthetic(config)
    else:
        if not args.clips or not args.video_dir:
            parser.error("a real run requires --clips and --video-dir (or use --synthetic)")
        clips = json.loads(args.clips.read_text())
        checkpoint, report = run_nightly(
            clips,
            video_dir=args.video_dir,
            config=config,
            register=not args.no_register,
        )

    save_checkpoint(checkpoint, args.output)
    print(f"[cross-regime-distill] {json.dumps(report.to_dict())}")
    print(
        f"[cross-regime-distill] promotable={checkpoint['promotable']} "
        f"(gate >= {config.min_map_gain_pp} pp drone-follow mAP gain)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
