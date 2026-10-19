"""Download the public yard-number dataset for local training.

Source: Roboflow Universe "Football Field Yard Marker"
(football25-ai-field-session-4kghd/football-field-yard-marker-h0mlg),
CC BY 4.0. Classes name each painted yard number by row and side of
midfield: ``n``/``f`` = near/far row, ``l``/``r`` = left/right of the 50,
``1``-``4`` = 10-40, ``5`` = the 50.

Uses Roboflow's export REST API directly (no SDK, so the worker's pinned
packages stay untouched). Needs ``ROBOFLOW_API_KEY`` in the environment or
in the repo's gitignored ``.env``; the key is never printed.

Usage:
    python -m training.field.download [--version 30] [--out roboflow-data/field_numbers]
"""

from __future__ import annotations

import argparse
import io
import os
import zipfile
from pathlib import Path

WORKSPACE = "football25-ai-field-session-4kghd"
PROJECT = "football-field-yard-marker-h0mlg"
DEFAULT_VERSION = 30
API = "https://api.roboflow.com"


def _api_key() -> str:
    key = os.environ.get("ROBOFLOW_API_KEY", "").strip()
    if key:
        return key
    for env_file in (Path(".env"), Path("..") / ".env"):
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("ROBOFLOW_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("ROBOFLOW_API_KEY is not set (add it to the gitignored .env).")


def download(version: int, out: Path, export_format: str = "yolov11") -> Path:
    import time

    import httpx

    key = _api_key()
    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            # Roboflow builds the export on first request (HTTP 202 + progress).
            for _ in range(120):
                meta = client.get(
                    f"{API}/{WORKSPACE}/{PROJECT}/{version}/{export_format}",
                    params={"api_key": key},
                )
                meta.raise_for_status()
                body = meta.json()
                if "export" in body:
                    break
                print(f"export progress {float(body.get('progress', 0)):.0%}", flush=True)
                time.sleep(10)
            else:
                raise SystemExit("Roboflow export did not finish in 20 minutes; try again.")
            link = body["export"]["link"]
            archive = client.get(link, timeout=600)
            archive.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Roboflow request failed with HTTP {exc.response.status_code}.") from None
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        zipped.extractall(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, default=DEFAULT_VERSION)
    parser.add_argument("--out", type=Path, default=Path("roboflow-data/field_numbers"))
    args = parser.parse_args()
    location = download(args.version, args.out)
    print(f"Dataset downloaded to: {location}")
    print(f"Train with: python -m training.field.train --data {location / 'data.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
