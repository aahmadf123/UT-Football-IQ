"""Homography & capture-regime utilities for the Phase-CV pipeline."""

from pipeline.homography.field_template import (
    FieldTemplate,
    default_template,
)
from pipeline.homography.project import (
    apply_homography,
    apply_homography_many,
    ground_anchor,
    homography_from_flat,
)
from pipeline.homography.regime_detector import (
    CaptureRegimeDetector,
    RegimeDetectorAdapter,
    RegimeResult,
)

__all__ = [
    "CaptureRegimeDetector",
    "RegimeDetectorAdapter",
    "RegimeResult",
    "FieldTemplate",
    "default_template",
    "apply_homography",
    "apply_homography_many",
    "ground_anchor",
    "homography_from_flat",
]
