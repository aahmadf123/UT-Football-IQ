"""Disposable video/HTTP fixture for tools/football-labeler/browser.test.mjs only."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="footiq-labeler-browser-protocol-"))
repository = Path(__file__).resolve().parents[1]
bundle = root / "staff-package.zip"
subprocess.run(
    [
        sys.executable,
        str(repository / "scripts/build_labeling_bundle.py"),
        "--output",
        str(bundle),
    ],
    check=True,
    capture_output=True,
)
extracted = root / "extracted-package"
with zipfile.ZipFile(bundle) as archive:
    assert "tools/football-labeler/start-labeler.cmd" in archive.namelist()
    assert "server.py" in archive.read("START-HERE.txt").decode()
    archive.extractall(extracted)
media = root / "protocol-only.mp4"
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=10",
        "-t",
        "4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(media),
    ],
    check=True,
)
source = {
    "id": "protocol-source",
    "filename": media.name,
    "path": str(media),
    "bytes": media.stat().st_size,
    "width": 320,
    "height": 180,
    "duration_seconds": 4,
    "nominal_fps": 10,
    "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
    "category": "practice",
    "group_id": "protocol-only",
    "category_status": "proposed",
    "group_status": "proposed",
}
catalog = root / "sources.json"
catalog.write_text(json.dumps({"schema_version": 1, "sources": [source]}))
proposals = root / "proposals"
proposals.mkdir()
(proposals / "protocol-source.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "evidence_kind": "detection_proposals",
            "source_id": source["id"],
            "media_sha256": source["media_sha256"],
            "width": 320,
            "height": 180,
            "provenance": {"source": "synthetic_protocol_only"},
            "frames": [
                {
                    "timestamp_seconds": 1,
                    "detections": [
                        {
                            "proposal_id": "protocol-hint-1",
                            "bbox": [20, 20, 60, 120],
                            "actor_kind": "referee",
                        }
                    ],
                }
            ],
        }
    )
)
spec = importlib.util.spec_from_file_location(
    "labeler_server",
    extracted / "tools/football-labeler/server.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
server = module.create_server(catalog, root / "saved", 0, proposals)
print(
    json.dumps(
        {
            "url": f"http://127.0.0.1:{server.server_port}",
            "root": str(root),
            "source": source,
        }
    ),
    flush=True,
)
try:
    server.serve_forever()
finally:
    server.server_close()
