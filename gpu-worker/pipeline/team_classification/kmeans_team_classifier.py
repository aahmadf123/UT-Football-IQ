"""Bounded k=3 CIELab team classifier.

Issue #130 asks for a lightweight visual split among offense, defense, and
officials using existing tracklet bounding boxes.  This module deliberately
keeps the implementation model-free: bounded crop sampling, deterministic
k-means in CIELab, locked first-frame centroids, EMA drift updates, a helmet
color tiebreaker, and a stripe guard for officials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

TEAM_OFFENSE = "offense"
TEAM_DEFENSE = "defense"
TEAM_OFFICIAL = "official"
TEAM_UNKNOWN = "unknown"

DEFAULT_ALPHA = 0.95
DEFAULT_HELMET_TIE_THRESHOLD = 10.0
DEFAULT_STRIPE_THRESHOLD = 0.5
MIN_CLUSTER_SAMPLES = 3


@dataclass(frozen=True)
class TrackletColorSample:
    """Visual color summary for a single tracklet on one frame."""

    tracklet_id: str
    frame_number: int
    jersey_lab: np.ndarray
    helmet_lab: np.ndarray | None
    stripe_score: float
    pixel_count: int


@dataclass(frozen=True)
class ClassificationResult:
    """Team classification for one tracklet sample."""

    tracklet_id: str
    frame_number: int
    label: str
    confidence: float
    cluster_index: int | None
    stripe_score: float
    reason: str


@dataclass(frozen=True)
class FrameClassification:
    """All classifications emitted for a frame."""

    frame_number: int
    results: list[ClassificationResult]


class KMeansTeamClassifier:
    """Deterministic k=3 CIELab classifier with locked EMA centroids."""

    def __init__(
        self,
        *,
        alpha: float = DEFAULT_ALPHA,
        helmet_tie_threshold: float = DEFAULT_HELMET_TIE_THRESHOLD,
        stripe_threshold: float = DEFAULT_STRIPE_THRESHOLD,
        max_iterations: int = 25,
    ) -> None:
        self.alpha = min(max(alpha, 0.0), 1.0)
        self.helmet_tie_threshold = helmet_tie_threshold
        self.stripe_threshold = stripe_threshold
        self.max_iterations = max_iterations
        self.centroids: np.ndarray | None = None
        self.helmet_centroids: np.ndarray | None = None
        self.cluster_labels: dict[int, str] = {}

    @property
    def fitted(self) -> bool:
        return self.centroids is not None

    def fit_initial_frame(
        self,
        samples: list[TrackletColorSample],
        grass_lab: np.ndarray,
    ) -> FrameClassification:
        """Fit k=3 centroids on the first viable frame and classify it."""
        usable = _usable_samples(samples)
        if len(usable) < MIN_CLUSTER_SAMPLES:
            return FrameClassification(
                frame_number=_frame_number(samples),
                results=[_unknown_result(s, "insufficient_initial_samples") for s in samples],
            )

        features = np.vstack([s.jersey_lab for s in usable])
        self.centroids, assignments = _kmeans_deterministic(
            features,
            k=3,
            max_iterations=self.max_iterations,
        )
        self.helmet_centroids = _helmet_centroids(usable, assignments, self.centroids)
        self.cluster_labels = _assign_cluster_labels(
            self.centroids,
            grass_lab,
            usable,
            assignments,
            self.stripe_threshold,
        )
        return self.classify_frame(samples, update=False)

    def classify_frame(
        self,
        samples: list[TrackletColorSample],
        *,
        update: bool = True,
    ) -> FrameClassification:
        """Classify one frame and optionally update centroids via EMA."""
        if self.centroids is None:
            return FrameClassification(
                frame_number=_frame_number(samples),
                results=[_unknown_result(s, "classifier_not_fitted") for s in samples],
            )

        results: list[ClassificationResult] = []
        assigned_samples: list[tuple[TrackletColorSample, int]] = []
        for sample in samples:
            result = self._classify_sample(sample)
            results.append(result)
            if result.cluster_index is not None:
                assigned_samples.append((sample, result.cluster_index))

        if update:
            self._ema_update(assigned_samples)

        return FrameClassification(frame_number=_frame_number(samples), results=results)

    def _classify_sample(self, sample: TrackletColorSample) -> ClassificationResult:
        if self.centroids is None:
            return _unknown_result(sample, "classifier_not_fitted")

        if sample.stripe_score >= self.stripe_threshold:
            official_cluster = _cluster_for_label(self.cluster_labels, TEAM_OFFICIAL)
            return ClassificationResult(
                tracklet_id=sample.tracklet_id,
                frame_number=sample.frame_number,
                label=TEAM_OFFICIAL,
                confidence=min(0.98, 0.7 + sample.stripe_score),
                cluster_index=official_cluster,
                stripe_score=sample.stripe_score,
                reason="stripe_guard",
            )

        distances = np.linalg.norm(self.centroids - sample.jersey_lab, axis=1)
        order = np.argsort(distances)
        cluster_index = int(order[0])
        reason = "jersey_lab_nearest"

        if (
            len(order) > 1
            and distances[order[1]] - distances[order[0]] < self.helmet_tie_threshold
            and sample.helmet_lab is not None
            and self.helmet_centroids is not None
        ):
            blended = distances.copy()
            helmet_distances = np.linalg.norm(
                self.helmet_centroids - sample.helmet_lab,
                axis=1,
            )
            blended = 0.7 * blended + 0.3 * helmet_distances
            cluster_index = int(np.argmin(blended))
            distances = blended
            reason = "helmet_tiebreak"

        label = self.cluster_labels.get(cluster_index, TEAM_UNKNOWN)
        confidence = _distance_confidence(np.sort(distances))
        return ClassificationResult(
            tracklet_id=sample.tracklet_id,
            frame_number=sample.frame_number,
            label=label,
            confidence=confidence,
            cluster_index=cluster_index,
            stripe_score=sample.stripe_score,
            reason=reason,
        )

    def _ema_update(
        self,
        assigned_samples: list[tuple[TrackletColorSample, int]],
    ) -> None:
        if self.centroids is None or not assigned_samples:
            return

        for cluster_index in range(self.centroids.shape[0]):
            cluster_samples = [
                sample
                for sample, assigned in assigned_samples
                if assigned == cluster_index and sample.pixel_count > 0
            ]
            if not cluster_samples:
                continue
            jersey_mean = np.mean([s.jersey_lab for s in cluster_samples], axis=0)
            self.centroids[cluster_index] = (
                self.alpha * self.centroids[cluster_index] + (1.0 - self.alpha) * jersey_mean
            )
            helmet_values = [s.helmet_lab for s in cluster_samples if s.helmet_lab is not None]
            if helmet_values and self.helmet_centroids is not None:
                helmet_mean = np.mean(helmet_values, axis=0)
                self.helmet_centroids[cluster_index] = (
                    self.alpha * self.helmet_centroids[cluster_index]
                    + (1.0 - self.alpha) * helmet_mean
                )


def classify_tracklet_frames(
    frames_by_number: dict[int, np.ndarray],
    tracklets: list[dict[str, Any]],
    *,
    initial_frame: int | None = None,
    classifier: KMeansTeamClassifier | None = None,
) -> list[FrameClassification]:
    """Classify tracklets across bounded sampled frames.

    The first frame with at least three usable tracklet samples initializes
    centroids.  Later frames use nearest-centroid assignment plus EMA updates.
    Pass ``classifier`` to read its fitted centroids afterwards.
    """
    if not frames_by_number:
        return []

    frame_numbers = sorted(frames_by_number)
    if initial_frame is not None:
        if initial_frame in frames_by_number:
            frame_numbers = [
                initial_frame,
                *[f for f in frame_numbers if f != initial_frame],
            ]
        else:
            after = [f for f in frame_numbers if f >= initial_frame]
            before = [f for f in frame_numbers if f < initial_frame]
            frame_numbers = [*after, *before]

    classifier = classifier or KMeansTeamClassifier()
    frame_results: list[FrameClassification] = []
    for frame_number in frame_numbers:
        frame = frames_by_number[frame_number]
        samples = _samples_for_frame(tracklets, frame_number, frame)
        if not samples:
            continue
        grass_lab = estimate_grass_lab(frame)
        if not classifier.fitted:
            classification = classifier.fit_initial_frame(samples, grass_lab)
            if classifier.fitted:
                frame_results.append(classification)
            continue
        frame_results.append(classifier.classify_frame(samples))

    return frame_results


def extract_tracklet_sample(
    frame_bgr: np.ndarray,
    bbox: list[float],
    *,
    tracklet_id: str,
    frame_number: int,
    max_pixels: int = 512,
) -> TrackletColorSample | None:
    """Extract bounded jersey and helmet CIELab summaries from a bbox."""
    x1, y1, x2, y2 = _clamp_bbox(bbox, frame_bgr.shape)
    if x2 <= x1 or y2 <= y1:
        return None

    width = x2 - x1
    height = y2 - y1
    jersey = frame_bgr[
        y1 + int(0.28 * height) : y1 + int(0.72 * height),
        x1 + int(0.18 * width) : x1 + int(0.82 * width),
    ]
    helmet = frame_bgr[
        y1 : y1 + max(1, int(0.22 * height)),
        x1 + int(0.25 * width) : x1 + int(0.75 * width),
    ]

    jersey_pixels = _non_grass_pixels(jersey, max_pixels=max_pixels)
    if len(jersey_pixels) == 0:
        jersey_pixels = _bounded_pixels(jersey, max_pixels=max_pixels)
    if len(jersey_pixels) == 0:
        return None

    helmet_pixels = _non_grass_pixels(helmet, max_pixels=max_pixels // 2)
    helmet_lab = _mean_lab(helmet_pixels) if len(helmet_pixels) > 0 else None
    return TrackletColorSample(
        tracklet_id=tracklet_id,
        frame_number=frame_number,
        jersey_lab=_mean_lab(jersey_pixels),
        helmet_lab=helmet_lab,
        stripe_score=detect_stripe_score(jersey),
        pixel_count=int(len(jersey_pixels)),
    )


def estimate_grass_lab(frame_bgr: np.ndarray) -> np.ndarray:
    """Estimate grass color in CIELab, with a football-field green fallback."""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95) & (hsv[:, :, 1] >= 35) & (hsv[:, :, 2] >= 25)
    pixels = frame_bgr[mask]
    if len(pixels) == 0:
        pixels = np.array([[40, 120, 40]], dtype=np.uint8)
    if len(pixels) > 2048:
        step = max(1, len(pixels) // 2048)
        pixels = pixels[::step][:2048]
    return _mean_lab(pixels)


#: Below this torso size stripes can't be resolved (players from a drone are
#: ~20-40 px tall); the detector abstains instead of guessing.
MIN_STRIPE_CROP_HEIGHT = 24
MIN_STRIPE_CROP_WIDTH = 12


def detect_stripe_score(crop_bgr: np.ndarray) -> float:
    """Score in [0, 1] for an official's vertical black-and-white stripes.

    A referee shirt is a regular left-to-right light/dark alternation (a few
    stripes across the torso) that repeats down every row. A jersey
    number or logo is not periodic and not consistent across rows. The score
    is the share of the column-intensity spectrum in that stripe band,
    weighted by how consistently the rows share the pattern. Crops too small
    to resolve stripes score 0 (abstain): the earlier Gabor version saturated
    to 1.0 on every small textured crop, labelling a whole drone clip as
    officials.
    """
    if crop_bgr.size == 0:
        return 0.0
    height, width = crop_bgr.shape[:2]
    if height < MIN_STRIPE_CROP_HEIGHT or width < MIN_STRIPE_CROP_WIDTH:
        return 0.0
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    if float(np.std(gray)) / 255.0 < 0.08:
        return 0.0

    profile = gray.mean(axis=0)
    profile = profile - profile.mean()
    power = np.abs(np.fft.rfft(profile)) ** 2
    total = float(power[1:].sum())
    if total <= 0.0:
        return 0.0
    # 2-8 cycles across the cropped torso: ~2-inch NCAA stripes on the
    # central two-thirds of a chest.
    band = float(power[2:9].sum()) / total

    # Stripes are evenly spaced; a number ("11") is two bars on uneven
    # background. Require >= 3 interior light/dark runs of similar width.
    binary = profile > 0
    edges = np.flatnonzero(np.diff(binary.astype(np.int8))) + 1
    runs = np.diff(edges)
    if len(runs) < 3 or float(np.std(runs) / (np.mean(runs) + 1e-6)) > 0.5:
        return 0.0
    # Officials' stripes are about half black, half white; a white number on
    # a solid jersey is thin bars on wide background.
    light = [int(r) for r, start in zip(runs, edges[:-1], strict=False) if binary[start]]
    dark = [int(r) for r, start in zip(runs, edges[:-1], strict=False) if not binary[start]]
    if light and dark:
        duty = float(np.mean(light) / np.mean(dark))
        if not 0.6 <= duty <= 1.67:
            return 0.0

    rows = gray - gray.mean(axis=1, keepdims=True)
    reference = profile / (np.linalg.norm(profile) + 1e-6)
    norms = np.linalg.norm(rows, axis=1) + 1e-6
    consistency = float(np.clip(np.mean((rows @ reference) / norms), 0.0, 1.0))
    return float(np.clip(band * consistency * 1.5, 0.0, 1.0))


def _samples_for_frame(
    tracklets: list[dict[str, Any]],
    frame_number: int,
    frame_bgr: np.ndarray,
) -> list[TrackletColorSample]:
    samples: list[TrackletColorSample] = []
    for tracklet in tracklets:
        point = _point_at_frame(tracklet, frame_number)
        if point is None:
            continue
        bbox = point.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        tracklet_id = str(tracklet.get("id") or tracklet.get("tracklet_id") or "")
        if not tracklet_id:
            continue
        sample = extract_tracklet_sample(
            frame_bgr,
            bbox,
            tracklet_id=tracklet_id,
            frame_number=frame_number,
        )
        if sample is not None:
            samples.append(sample)
    return samples


def _point_at_frame(tracklet: dict[str, Any], frame_number: int) -> dict[str, Any] | None:
    for point in tracklet.get("track_points", []):
        if int(point.get("frame_number", -1)) == frame_number:
            return point
    return None


def _clamp_bbox(bbox: list[float], shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    height, width = shape[:2]
    x1 = max(0, min(width - 1, int(round(bbox[0]))))
    y1 = max(0, min(height - 1, int(round(bbox[1]))))
    x2 = max(0, min(width, int(round(bbox[2]))))
    y2 = max(0, min(height, int(round(bbox[3]))))
    return x1, y1, x2, y2


def _non_grass_pixels(crop_bgr: np.ndarray, *, max_pixels: int) -> np.ndarray:
    pixels = _bounded_pixels(crop_bgr, max_pixels=max_pixels * 2)
    if len(pixels) == 0:
        return pixels
    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    grass_mask = (hsv[:, 0] >= 35) & (hsv[:, 0] <= 95) & (hsv[:, 1] >= 35) & (hsv[:, 2] >= 25)
    visible_mask = hsv[:, 2] >= 20
    pixels = pixels[(~grass_mask) & visible_mask]
    if len(pixels) > max_pixels:
        step = max(1, len(pixels) // max_pixels)
        pixels = pixels[::step][:max_pixels]
    return pixels


def _bounded_pixels(crop_bgr: np.ndarray, *, max_pixels: int) -> np.ndarray:
    if crop_bgr.size == 0:
        return np.empty((0, 3), dtype=np.uint8)
    pixels = crop_bgr.reshape(-1, 3)
    if len(pixels) <= max_pixels:
        return pixels
    step = max(1, len(pixels) // max_pixels)
    return pixels[::step][:max_pixels]


def _mean_lab(bgr_pixels: np.ndarray) -> np.ndarray:
    pixels = bgr_pixels.astype(np.uint8).reshape(-1, 1, 3)
    lab = cv2.cvtColor(pixels, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    return np.mean(lab, axis=0)


def _usable_samples(samples: list[TrackletColorSample]) -> list[TrackletColorSample]:
    return [sample for sample in samples if sample.pixel_count > 0]


def _kmeans_deterministic(
    features: np.ndarray,
    *,
    k: int,
    max_iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
    centroids = _initial_centroids(features, k)
    assignments = np.zeros(features.shape[0], dtype=np.int64)
    for _ in range(max_iterations):
        distances = np.linalg.norm(features[:, None, :] - centroids[None, :, :], axis=2)
        next_assignments = np.argmin(distances, axis=1)
        if np.array_equal(next_assignments, assignments):
            break
        assignments = next_assignments
        for cluster_index in range(k):
            members = features[assignments == cluster_index]
            if len(members) > 0:
                centroids[cluster_index] = np.mean(members, axis=0)
    return centroids, assignments


def _initial_centroids(features: np.ndarray, k: int) -> np.ndarray:
    """Deterministic farthest-point initialization."""
    centroids = [features[int(np.argmin(features[:, 0]))]]
    while len(centroids) < k:
        existing = np.vstack(centroids)
        distances = np.min(
            np.linalg.norm(features[:, None, :] - existing[None, :, :], axis=2),
            axis=1,
        )
        centroids.append(features[int(np.argmax(distances))])
    return np.vstack(centroids).astype(np.float32)


def _helmet_centroids(
    samples: list[TrackletColorSample],
    assignments: np.ndarray,
    jersey_centroids: np.ndarray,
) -> np.ndarray:
    helmet_centroids = jersey_centroids.copy()
    for cluster_index in range(jersey_centroids.shape[0]):
        helmets = [
            sample.helmet_lab
            for sample, assigned in zip(samples, assignments, strict=False)
            if assigned == cluster_index and sample.helmet_lab is not None
        ]
        if helmets:
            helmet_centroids[cluster_index] = np.mean(helmets, axis=0)
    return helmet_centroids


def _assign_cluster_labels(
    centroids: np.ndarray,
    grass_lab: np.ndarray,
    samples: list[TrackletColorSample],
    assignments: np.ndarray,
    stripe_threshold: float,
) -> dict[int, str]:
    distances = np.linalg.norm(centroids - grass_lab, axis=1)
    clusters = set(range(centroids.shape[0]))
    labels: dict[int, str] = {}

    stripe_scores = []
    for cluster_index in clusters:
        scores = [
            sample.stripe_score
            for sample, assigned in zip(samples, assignments, strict=False)
            if assigned == cluster_index
        ]
        stripe_scores.append((float(np.mean(scores)) if scores else 0.0, cluster_index))

    best_stripe_score, best_stripe_cluster = max(stripe_scores)
    if best_stripe_score >= stripe_threshold:
        labels[best_stripe_cluster] = TEAM_OFFICIAL
        clusters.remove(best_stripe_cluster)

    by_far_from_grass = [int(i) for i in np.argsort(-distances) if int(i) in clusters]
    if len(by_far_from_grass) == 3:
        # No striped cluster: there may be no officials at all (practice).
        # The two most-populated clusters are the teams; the smallest is a
        # coach, a different-colored QB jersey or noise -- unknown, never
        # "official" (the closest-to-grass rule mislabelled a whole team).
        sizes = {c: int(np.sum(assignments == c)) for c in by_far_from_grass}
        smallest = min(by_far_from_grass, key=lambda c: sizes[c])
        labels[smallest] = TEAM_UNKNOWN
        by_far_from_grass = [c for c in by_far_from_grass if c != smallest]

    if by_far_from_grass:
        labels[by_far_from_grass[0]] = TEAM_OFFENSE
    if len(by_far_from_grass) > 1:
        labels[by_far_from_grass[1]] = TEAM_DEFENSE
    for cluster_index in clusters:
        labels.setdefault(cluster_index, TEAM_UNKNOWN)
    return {
        cluster_index: labels.get(cluster_index, TEAM_UNKNOWN)
        for cluster_index in range(centroids.shape[0])
    }


def _cluster_for_label(cluster_labels: dict[int, str], label: str) -> int | None:
    for cluster_index, cluster_label in cluster_labels.items():
        if cluster_label == label:
            return cluster_index
    return None


def _distance_confidence(sorted_distances: np.ndarray) -> float:
    if len(sorted_distances) < 2:
        return 0.5
    margin = float(sorted_distances[1] - sorted_distances[0])
    confidence = 0.5 + min(0.45, margin / 80.0)
    return round(confidence, 3)


def _unknown_result(sample: TrackletColorSample, reason: str) -> ClassificationResult:
    return ClassificationResult(
        tracklet_id=sample.tracklet_id,
        frame_number=sample.frame_number,
        label=TEAM_UNKNOWN,
        confidence=0.0,
        cluster_index=None,
        stripe_score=sample.stripe_score,
        reason=reason,
    )


def _frame_number(samples: list[TrackletColorSample]) -> int:
    return samples[0].frame_number if samples else -1
