"""Proposal metadata packaging guards; no originals, weights or reviewed labels."""

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "labeling_bundle", REPO / "scripts/build_labeling_bundle.py"
)
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


@unittest.skipUnless(
    shutil.which("node"), "Node.js is required for the actual viewer proposal parser"
)
class ProposalBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tool = self.root / "tools/football-labeler"
        self.tool.mkdir(parents=True)
        shutil.copyfile(
            REPO / "tools/football-labeler/connection.mjs", self.tool / "connection.mjs"
        )
        self.proposals = self.root / "incoming"
        self.proposals.mkdir()
        self.source = {
            "id": "protocol-source",
            "media_sha256": "a" * 64,
            "width": 100,
            "height": 80,
            "duration_seconds": 4,
        }
        self.catalog = {"sources": [self.source]}
        self.proposal = {
            "schema_version": 1,
            "evidence_kind": "detection_proposals",
            "source_id": self.source["id"],
            "media_sha256": self.source["media_sha256"],
            "width": 100,
            "height": 80,
            "provenance": {"source": "experimental", "review_status": "unreviewed"},
            "frames": [
                {
                    "timestamp_seconds": 1,
                    "detections": [
                        {
                            "proposal_id": "protocol-hint",
                            "actor_kind": "player",
                            "bbox": [1, 2, 20, 40],
                            "confidence": 0.5,
                        }
                    ],
                }
            ],
        }
        self.write()
        self.patcher = patch.object(bundle, "ROOT", self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def write(self):
        path = self.proposals / "protocol-source.json"
        path.write_text(json.dumps(self.proposal))
        self.inventory = {
            "evidence_kind": "local_detection_review_inventory",
            "rows": [
                {
                    "source_id": self.source["id"],
                    "media_sha256": self.source["media_sha256"],
                    "proposal_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ],
        }
        self.write_inventory()

    def write_inventory(self):
        (self.proposals / "inventory.json").write_text(json.dumps(self.inventory))

    def collect(self):
        return bundle.proposal_files(self.proposals, self.catalog)

    def test_actual_parser_accepts_only_source_bound_unreviewed_metadata(self):
        entries = self.collect()
        self.assertEqual(
            {name for _, name in entries},
            {
                "runs/practice-detection-review-20261021/inventory.json",
                "runs/practice-detection-review-20261021/protocol-source.json",
            },
        )

    def test_missing_inventory_refuses_partial_assignment(self):
        (self.proposals / "inventory.json").unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.collect()

    def test_missing_declared_source_refuses_partial_assignment(self):
        (self.proposals / "protocol-source.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing|Missing"):
            self.collect()

    def test_changed_source_hash_refused_even_with_updated_file_checksum(self):
        self.proposal["media_sha256"] = "b" * 64
        self.write()
        with self.assertRaisesRegex(ValueError, "parser|hash"):
            self.collect()

    def test_actual_parser_rejects_invalid_box_after_checksum_is_updated(self):
        self.proposal["frames"][0]["detections"][0]["bbox"] = [0, 0, 200, 40]
        self.write()
        with self.assertRaisesRegex(ValueError, "parser"):
            self.collect()

    def test_inventory_checksum_must_match_actual_proposal_bytes(self):
        self.inventory["rows"][0]["proposal_sha256"] = "0" * 64
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.collect()

    def test_arbitrary_sibling_media_or_weight_is_rejected(self):
        (self.proposals / "not-a-proposal.pt").write_bytes(
            b"not weights; protocol-only unsafe sibling"
        )
        with self.assertRaisesRegex(ValueError, "Unexpected|unexpected"):
            self.collect()

    def test_unsafe_source_path_is_rejected(self):
        self.inventory["rows"][0]["source_id"] = "../outside"
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "source|Source"):
            self.collect()

    def test_reviewed_payload_cannot_enter_unreviewed_hint_package(self):
        self.proposal["reviewed"] = True
        self.write()
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            self.collect()

    def test_duplicate_inventory_source_is_rejected(self):
        self.inventory["rows"].append(self.inventory["rows"][0])
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "unique|duplicate"):
            self.collect()

    def test_casefold_duplicate_source_cannot_alias_on_windows(self):
        other = dict(self.source, id="PROTOCOL-source")
        self.catalog["sources"].append(other)
        self.inventory["rows"].append(
            dict(self.inventory["rows"][0], source_id=other["id"])
        )
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "unique"):
            self.collect()

    def test_parser_and_archive_snapshot_use_identical_captured_bytes(self):
        path = self.proposals / "protocol-source.json"
        original = path.read_bytes()
        original_inventory = (self.proposals / "inventory.json").read_bytes()
        actual_run = bundle.subprocess.run

        def replace_after_capture(*args, **kwargs):
            path.write_text('{"changed_after_capture":true}')
            (self.proposals / "inventory.json").write_text("{}")
            return actual_run(*args, **kwargs)

        captured = {}
        with patch.object(bundle.subprocess, "run", side_effect=replace_after_capture):
            bundle.proposal_files(self.proposals, self.catalog, captured)
        self.assertEqual(captured[path], original)
        self.assertEqual(
            captured[self.proposals / "inventory.json"], original_inventory
        )
        self.assertNotEqual(path.read_bytes(), original)

    def test_source_symlink_is_rejected_even_if_target_is_inside_workspace(self):
        target = self.root / "same-bytes.json"
        source = self.proposals / "protocol-source.json"
        target.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(target)
        except OSError:
            self.skipTest("Symlinks unavailable on this host")
        with self.assertRaisesRegex(ValueError, "symlink|regular"):
            self.collect()
