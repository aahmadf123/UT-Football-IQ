"""Shared utility functions used across multiple routers."""

import uuid

from app.config import get_settings


def make_dataset_artifact_uri(model_scope: str) -> str:
    """Generate a deterministic-format object storage URI for a new training dataset snapshot."""
    settings = get_settings()
    return f"s3://{settings.s3_bucket_artifacts}/datasets/{model_scope}/{uuid.uuid4()}.jsonl"
