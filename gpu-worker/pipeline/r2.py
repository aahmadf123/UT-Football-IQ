"""R2 / storage helpers for the GPU worker pipeline (compatibility shim).

The implementation lives in :mod:`pipeline.storage`, which dispatches on the
reference scheme (``r2://bucket/key``, ``local://bucket/key``, ``file://``,
or a legacy bare key against ``R2_BUCKET_NAME``). This module keeps the
original ``pipeline.r2`` names so existing stages and test patches
(``patch("pipeline.r2.upload_file", ...)``) continue to work unchanged.
"""

from __future__ import annotations

from pipeline.storage import (
    R2_BUCKET,
    download_to_temp,
    upload_bytes,
    upload_file,
)

__all__ = ["R2_BUCKET", "download_to_temp", "upload_bytes", "upload_file"]
