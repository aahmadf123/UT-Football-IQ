"""Object-storage helpers for the GPU worker pipeline (compatibility shim).

The implementation lives in :mod:`pipeline.storage`, which dispatches on the
reference scheme (``s3://bucket/key``, ``local://bucket/key``, ``file://``,
or a legacy bare key against ``S3_BUCKET_NAME``). This module keeps the
original ``pipeline.object_store`` names so existing stages and test patches
(``patch("pipeline.object_store.upload_file", ...)``) continue to work unchanged.
"""

from __future__ import annotations

from pipeline.storage import (
    S3_BUCKET,
    download_to_temp,
    upload_bytes,
    upload_file,
)

__all__ = ["S3_BUCKET", "download_to_temp", "upload_bytes", "upload_file"]
