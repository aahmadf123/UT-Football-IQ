"""Synthetic temporary-file regressions for safe staff labeling preparation."""

import copy
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_overhaul_labeling.py"
SPEC = importlib.util.spec_from_file_location("overhaul_labeling", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LabelingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.media = self.root / "original.mp4"
        self.media.write_bytes(b"synthetic temporary test media" * 100000)
        self.inventory = {"observed_date": "2026-10-21", "items": [
            {"library": "practice", "name": "Dji 20260416110000 0257 D.mp4", "path": str(self.media),
             "bytes": self.media.stat().st_size, "probe": {"format": {"duration": "10"}}}]}
        self.inventory["items"][0]["probe"]["streams"] = [{"codec_type": "video", "width": 100, "height": 80,
                                                          "r_frame_rate": "30000/1001", "avg_frame_rate": "299/10"}]
        self.catalog = MODULE.make_catalog(self.inventory)
        self.source = self.catalog["sources"][0]
        self.source.update(group_status="confirmed", category_status="confirmed")
        self.primary = MODULE.draft_for(self.source)
        self.primary.update(reviewer_id="staff-one", source_context_confirmed=True)
        self.primary["plays"] = [{"id": "play-one", "canonical_play_id": "session-play-one",
                                  "start_seconds": 0, "snap_seconds": 2, "end_seconds": 8,
                                  "double_labeled": False, "adjudicated": False,
                                  "dense_tracks": [{"target_id": "target-one", "points": [
                                      {"timestamp_seconds": 2, "bbox": [1, 2, 3, 4], "visibility_state": "visible"},
                                      {"timestamp_seconds": 2.1, "bbox": [2, 2, 4, 4], "visibility_state": "visible"}],
                                      "visibility_events": [{"timestamp_seconds": 3, "visibility_state": "offscreen"}]}],
                                  "identities": [], "events": [{"event_type": "snap", "timestamp_seconds": 2}]}]

    def json_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_catalog_ids_survive_library_move_and_order_change(self):
        moved = copy.deepcopy(self.inventory)
        moved["items"][0]["path"] = "D:/Staff Library/moved.mp4"
        self.assertEqual(MODULE.make_catalog(moved)["sources"][0]["id"], self.source["id"])
        self.assertEqual(self.source["group_id"], "practice-2026-04-16")
        self.assertIsNone(self.source["media_sha256"])

    def test_same_date_practice_extracts_share_proposed_group_and_games_stay_unknown(self):
        inventory = copy.deepcopy(self.inventory)
        other = copy.deepcopy(inventory["items"][0])
        other["name"] = "Dji 20260416110100 0258 D.mp4"
        game = copy.deepcopy(other)
        game.update(library="game", name="26 01 FULL GAME.mp4")
        inventory["items"].extend([other, game])
        catalog = MODULE.make_catalog(inventory)
        self.assertEqual(len({s["group_id"] for s in catalog["sources"] if s["library"] == "practice"}), 1)
        self.assertIsNone(next(s["category"] for s in catalog["sources"] if s["library"] == "game"))
        self.assertTrue(all(s["group_status"] == "proposed" for s in catalog["sources"]))

    def test_prepare_writes_empty_unreviewed_templates_and_unassigned_review_passes(self):
        inventory = self.json_file("inventory.json", self.inventory)
        output = self.root / "package"
        readiness = MODULE.prepare(inventory, output)
        draft = MODULE.read_json(next((output / "drafts").iterdir()))
        self.assertFalse(draft["reviewed"])
        self.assertEqual(draft["plays"], [])
        self.assertEqual(draft["reviewer_id"], "")
        coverage = MODULE.read_json(output / "dense-coverage-draft.json")
        self.assertEqual(coverage["evidence_kind"], "dense_coverage_draft")
        self.assertFalse(coverage["all_visible_players_annotated"])
        self.assertEqual(coverage["frames"], [])
        self.assertEqual(readiness["confirmed_groups"], 0)
        with (output / "review-assignments.csv").open(encoding="utf-8-sig", newline="") as stream:
            assignments = list(csv.DictReader(stream))
        self.assertEqual({r["review_pass"] for r in assignments}, set(MODULE.PASSES))
        self.assertTrue(all(not r["reviewer_id"] and r["status"] == "unassigned" for r in assignments))
        before = (output / "sources.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            MODULE.prepare(inventory, output)
        self.assertEqual((output / "sources.json").read_bytes(), before)

    def test_stream_hash_matches_bytes_and_rejects_changed_source_size(self):
        expected = hashlib.sha256(self.media.read_bytes()).hexdigest()
        self.assertEqual(MODULE.stream_hash(self.media, self.media.stat().st_size), expected)
        with self.assertRaisesRegex(ValueError, "size changed"):
            MODULE.stream_hash(self.media, 1)
        hashed = MODULE.hash_catalog(self.catalog, [self.source["id"]])
        self.assertEqual(hashed["sources"][0]["media_sha256"], expected)
        self.assertIsNone(self.source["media_sha256"])

    def test_freeze_requires_review_attestation_and_confirmed_context(self):
        with self.assertRaisesRegex(ValueError, "attest-reviewed"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=False)
        self.source["category_status"] = "proposed"
        with self.assertRaisesRegex(ValueError, "confirm source"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)

    def test_frozen_primary_labels_bind_actual_bytes_without_double_review_claim(self):
        packet, entry = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(packet["evidence_kind"], "human_reviewed")
        self.assertEqual(packet["reviewer_ids"], ["staff-one"])
        self.assertEqual(packet["media_sha256"], hashlib.sha256(self.media.read_bytes()).hexdigest())
        self.assertEqual(entry["media"]["sha256"], packet["media_sha256"])
        self.assertFalse(packet["plays"][0]["double_labeled"])
        self.assertEqual(packet["plays"][0]["dense_tracks"][0]["visibility_events"][0]["visibility_state"], "offscreen")
        self.assertEqual(self.primary["evidence_kind"], "annotation_draft")

    def test_catalog_keeps_actual_dimensions_and_rational_rates(self):
        self.assertEqual((self.source["width"], self.source["height"]), (100, 80))
        self.assertEqual(self.source["r_frame_rate"], "30000/1001")
        self.assertEqual(self.source["avg_frame_rate"], "299/10")

    def test_multiple_windows_keep_one_canonical_play_and_all_double_review_occurrences(self):
        alternate = copy.deepcopy(self.primary["plays"][0])
        alternate.update(id="angle-two", start_seconds=8, snap_seconds=8.2, end_seconds=9.5, dense_tracks=[], events=[])
        self.primary["plays"].append(alternate)
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(len(packet["plays"]), 2)
        self.assertEqual(len({p["canonical_play_id"] for p in packet["plays"]}), 1)
        secondary, adjudication = copy.deepcopy(self.primary), copy.deepcopy(self.primary)
        secondary.update(review_pass="secondary", reviewer_id="staff-two")
        adjudication.update(review_pass="adjudication", reviewer_id="staff-three")
        secondary["plays"].pop()
        with self.assertRaisesRegex(ValueError, "all source-local occurrences"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True, secondary=secondary,
                          adjudication=adjudication, double_plays=["session-play-one"], attest_adjudicated=True)

    def test_identity_requires_real_target_and_explicit_known_evidence(self):
        valid = {"target_id": "target-one", "canonical_entity_id": "athlete-case-one", "player_id": "roster-one",
                 "readable": True, "team_resolved": True}
        for invalid in ({**valid, "target_id": "absent"}, {**valid, "readable": None},
                        {**valid, "canonical_entity_id": None}, {**valid, "team_resolved": False}):
            draft = copy.deepcopy(self.primary)
            draft["plays"][0]["identities"] = [invalid]
            with self.assertRaises(ValueError):
                MODULE.freeze(self.catalog, draft, attest_reviewed=True)
        self.primary["plays"][0]["identities"] = [valid]
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(packet["plays"][0]["identities"], [valid])

    def test_duplicate_targets_and_out_of_picture_boxes_are_rejected(self):
        draft = copy.deepcopy(self.primary)
        draft["plays"][0]["dense_tracks"].append(copy.deepcopy(draft["plays"][0]["dense_tracks"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate target"):
            MODULE.freeze(self.catalog, draft, attest_reviewed=True)
        draft = copy.deepcopy(self.primary)
        draft["plays"][0]["dense_tracks"][0]["points"][0]["bbox"] = [1, 2, 101, 4]
        with self.assertRaisesRegex(ValueError, "pixel box"):
            MODULE.freeze(self.catalog, draft, attest_reviewed=True)

    def test_unknown_identity_fields_remain_unknown_without_fabricated_case_ids(self):
        unknown = {"target_id": "target-one", "canonical_entity_id": None, "player_id": None,
                   "readable": False, "team_resolved": False}
        self.primary["plays"][0]["identities"] = [unknown]
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(packet["plays"][0]["identities"], [unknown])

    def test_one_visible_frame_is_valid_sparse_evidence_but_empty_track_is_rejected(self):
        self.primary["plays"][0]["dense_tracks"][0]["points"] = self.primary["plays"][0]["dense_tracks"][0]["points"][:1]
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(len(packet["plays"][0]["dense_tracks"][0]["points"]), 1)
        self.assertNotIn("dense_coverage", packet["plays"][0])
        self.primary["plays"][0]["dense_tracks"][0]["points"] = []
        with self.assertRaisesRegex(ValueError, "one visible observation"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)

    def test_no_snap_or_explicit_noncountable_sequences_are_retained_outside_normal_plays(self):
        no_snap = copy.deepcopy(self.primary["plays"][0])
        no_snap.update(id="unknown-sequence", canonical_play_id="unknown-sequence", snap_seconds=None)
        noncountable = copy.deepcopy(self.primary["plays"][0])
        noncountable.update(id="staff-sequence", canonical_play_id="staff-sequence", canonical_count_eligible=False,
                            sequence_reason="staff_confirmed_aborted_rep")
        self.primary["plays"].extend([no_snap, noncountable])
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(len(packet["plays"]), 1)
        self.assertEqual(len(packet["sequences"]), 2)
        self.assertEqual(packet["sequences"][0]["sequence_reason"], "no_observed_snap")
        self.assertEqual(packet["sequences"][1]["sequence_reason"], "staff_confirmed_aborted_rep")
        self.assertTrue(all(p["canonical_count_eligible"] is False for p in packet["sequences"]))
        self.assertNotIn("sequence_reason", self.primary["plays"][1])
        self.primary["plays"] = [no_snap]
        packet, _ = MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        self.assertEqual(packet["plays"], [])
        self.assertEqual(len(packet["sequences"]), 1)

    def test_single_draft_flags_cannot_manufacture_adjudication(self):
        self.primary["plays"][0]["adjudicated"] = True
        with self.assertRaisesRegex(ValueError, "checkbox"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True)
        with self.assertRaisesRegex(ValueError, "independent secondary"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True, double_plays=["session-play-one"])

    def test_double_review_needs_distinct_staff_and_explicit_adjudication(self):
        secondary, adjudication = copy.deepcopy(self.primary), copy.deepcopy(self.primary)
        secondary["review_pass"] = "secondary"
        adjudication.update(review_pass="adjudication", reviewer_id="staff-adjudicator")
        kwargs = {"attest_reviewed": True, "secondary": secondary, "adjudication": adjudication,
                  "attest_adjudicated": True, "double_plays": ["session-play-one"]}
        with self.assertRaisesRegex(ValueError, "distinct"):
            MODULE.freeze(self.catalog, self.primary, **kwargs)
        secondary["reviewer_id"] = "staff-two"
        adjudication["plays"][0]["snap_seconds"] = 2.2
        packet, _entry = MODULE.freeze(self.catalog, self.primary, **kwargs)
        self.assertTrue(packet["plays"][0]["double_labeled"])
        self.assertTrue(packet["plays"][0]["adjudicated"])
        self.assertEqual(packet["plays"][0]["snap_seconds"], 2.2)
        self.assertEqual(len(packet["reviewer_ids"]), 3)

    def test_missing_secondary_canonical_play_cannot_count_as_double_labeled(self):
        secondary, adjudication = copy.deepcopy(self.primary), copy.deepcopy(self.primary)
        secondary.update(review_pass="secondary", reviewer_id="staff-two")
        adjudication.update(review_pass="adjudication", reviewer_id="staff-three")
        secondary["plays"][0]["canonical_play_id"] = "different-play"
        with self.assertRaisesRegex(ValueError, "Every double-labeled"):
            MODULE.freeze(self.catalog, self.primary, attest_reviewed=True, secondary=secondary,
                          adjudication=adjudication, double_plays=["session-play-one"], attest_adjudicated=True)

    def test_bad_source_time_hash_or_fabricated_loss_box_is_refused(self):
        for mutate in (
            lambda d: d.update(media_sha256="0" * 64),
            lambda d: d["plays"][0].update(snap_seconds=float("nan")),
            lambda d: d["plays"][0].update(end_seconds=100),
            lambda d: d["plays"][0]["dense_tracks"][0]["points"][0].update(visibility_state="offscreen"),
        ):
            draft = copy.deepcopy(self.primary)
            mutate(draft)
            with self.assertRaises(ValueError):
                MODULE.freeze(self.catalog, draft, attest_reviewed=True)

    def test_cli_refuses_original_alias_and_never_overwrites_staff_file(self):
        catalog = self.json_file("catalog.json", self.catalog)
        alias = self.root / "source-alias.json"
        try:
            os.link(self.media, alias)
        except OSError:
            self.skipTest("Hardlinks unsupported on this filesystem")
        before = self.media.read_bytes()
        process = subprocess.run([sys.executable, str(SCRIPT), "hash", "--catalog", str(catalog),
                                  "--output", str(alias)], capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(self.media.read_bytes(), before)

    def test_cli_freeze_outputs_hash_bound_release_manifest_entry(self):
        catalog = self.json_file("catalog.json", self.catalog)
        draft = self.json_file("primary.json", self.primary)
        output, entry = self.root / "frozen.json", self.root / "entry.json"
        process = subprocess.run([sys.executable, str(SCRIPT), "freeze", "--catalog", str(catalog),
                                  "--draft", str(draft), "--attest-reviewed", "--output", str(output),
                                  "--entry-output", str(entry)], capture_output=True, check=False)
        self.assertEqual(process.returncode, 0, process.stdout)
        reference = MODULE.read_json(entry)
        self.assertEqual(reference["labels"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(reference["media"]["sha256"], hashlib.sha256(self.media.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
