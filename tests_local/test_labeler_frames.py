"""Real ffmpeg fixtures verify frame clocks and safe index output."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class LabelerFrameTests(unittest.TestCase):
    def make_source(self, root, offset=0):
        media = root / "original.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30000/1001:d=2", "-c:v", "libx264", "-output_ts_offset", str(offset), str(media)], check=True, capture_output=True)
        catalog = root / "sources.json"
        catalog.write_text(json.dumps({"sources": [{"id": "fixture-only", "path": str(media), "bytes": media.stat().st_size, "duration_seconds": 10, "width": 320, "height": 180}]}))
        return media, catalog

    def index(self, catalog, output):
        return subprocess.run([sys.executable, str(ROOT / "tools/football-labeler/index_frames.py"), "--catalog", str(catalog), "--source", "fixture-only", "--start", "0", "--end", "2", "--output", str(output)], capture_output=True, text=True)

    def test_actual_fractional_pts_are_preserved_and_existing_outputs_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media, catalog = self.make_source(root)
            original = media.read_bytes()
            output = root / "index.json"
            result = self.index(catalog, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            index = json.loads(output.read_text())
            self.assertEqual(index["source_clock_origin_seconds"], 0)
            self.assertEqual(len(index["frame_pts_seconds"]), 60)
            self.assertAlmostEqual(index["frame_pts_seconds"][1], 1001 / 30000, places=6)
            saved = output.read_bytes()
            self.assertNotEqual(self.index(catalog, output).returncode, 0)
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(media.read_bytes(), original)

    def test_nonzero_clock_origin_refuses_unproven_browser_time_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog = self.make_source(root, offset=5)
            output = root / "index.json"
            result = self.index(catalog, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("zero-origin", result.stderr)
            self.assertFalse(output.exists())

    def test_catalog_dimensions_cannot_override_actual_source_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog = self.make_source(root)
            data = json.loads(catalog.read_text())
            data["sources"][0]["width"] = 999
            catalog.write_text(json.dumps(data))
            output = root / "index.json"
            result = self.index(catalog, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dimensions differ", result.stderr)
            self.assertFalse(output.exists())
