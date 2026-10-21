"""Release checks reject missing evidence, leakage and tampered artifacts.

All files created here are synthetic unit-test fixtures in temporary folders.
They never become production labels, model weights or accuracy reports.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_overhaul_release.py"
SPEC = importlib.util.spec_from_file_location("overhaul_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def artifact(self, name, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        (self.root / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}

    def source(self, sid, group, canonical="play-1", category="practice"):
        media = self.artifact(f"{sid}.mp4", b"unit-test-media-" + sid.encode())
        labels = self.artifact(f"{sid}.labels.json", {
            "evidence_kind": "human_reviewed", "reviewed": True,
            "reviewer_ids": ["fixture-reviewer"], "source_id": sid,
            "media_sha256": media["sha256"], "plays": [{
                "id": "p1", "canonical_play_id": canonical,
                "start_seconds": 0, "snap_seconds": 3, "end_seconds": 10,
                "double_labeled": True, "adjudicated": True,
                "identities": [{"target_id": "t1", "canonical_entity_id": "athlete-1",
                                "player_id": "player-1", "readable": True,
                                "team_resolved": True}],
            }],
        })
        return {"id": sid, "group_id": group, "category": category,
                "media": media, "labels": labels}

    @staticmethod
    def gate(result, name):
        return next(row for row in result["gates"] if row["name"] == name)

    def evaluation(self, task, source, fields):
        predicted = self.artifact(f"{task}.predictions.json", {"evidence_source": "real", "samples": [{"source_id": source["id"]}]})
        weights = self.artifact(f"{task}.weights", b"synthetic unit test weights")
        report = self.artifact(f"{task}.report.json", {"evidence_source": "real", "evaluator_version": "unit-test",
            "source_groups": [source["group_id"]], "label_sha256": [source["labels"]["sha256"]],
            "predictions_sha256": predicted["sha256"], "model_weights": [weights], **fields})
        return {"task": task, "predictions": predicted, "report": report}

    def evaluate_task(self, task, fields):
        source = self.source("measurement", "g")
        return MODULE.evaluate({"schema_version": 1, "sources": [source], "splits": {"holdout": ["g"]},
            "evaluations": [self.evaluation(task, source, fields)]}, self.root)

    def test_no_evidence_never_passes_or_manufactures_measurements(self):
        result = MODULE.evaluate({"schema_version": 1}, self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "not_ready")
        self.assertIsNone(self.gate(result, "spatial_accuracy")["measured"]["median_yards"])
        self.assertEqual(self.gate(result, "automatic_identity")["measured"]["accepted"], 0)
        self.assertTrue(all(not self.gate(result, f"{task}_evidence")["passed"] for task in MODULE.TASKS))

    def test_cli_missing_evidence_exits_one_and_writes_json(self):
        manifest = self.root / "manifest.json"
        manifest.write_text('{"schema_version":1}', encoding="utf-8")
        output = self.root / "result.json"
        process = subprocess.run([sys.executable, str(SCRIPT), str(manifest), "--output", str(output)],
                                 capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(output.read_text())["passed"])

    def test_output_cannot_overwrite_original_evidence(self):
        media = self.artifact("original.mp4", b"original unit test source")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "sources": [{"media": media}]}))
        original = (self.root / "original.mp4").read_bytes()
        process = subprocess.run([sys.executable, str(SCRIPT), str(manifest), "--output", str(self.root / "original.mp4")], capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertEqual((self.root / "original.mp4").read_bytes(), original)

    def test_hash_mismatch_rejects_labels_and_population(self):
        source = self.source("a", "group-a")
        (self.root / source["labels"]["path"]).write_text("tampered")
        result = MODULE.evaluate({"schema_version": 1, "sources": [source],
                                  "splits": {"holdout": ["group-a"]}}, self.root)
        self.assertFalse(self.gate(result, "evidence_integrity")["passed"])
        self.assertEqual(self.gate(result, "heldout_population")["measured"]["canonical_plays"], 0)

    def test_output_hardlink_cannot_mutate_original_evidence(self):
        media = self.artifact("original.mp4", b"original unit test source")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "sources": [{"media": media}]}))
        alias = self.root / "report.json"
        try:
            os.link(self.root / "original.mp4", alias)
        except OSError:
            self.skipTest("This filesystem does not support hard links")
        process = subprocess.run([sys.executable, str(SCRIPT), str(manifest), "--output", str(alias)], capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertEqual((self.root / "original.mp4").read_bytes(), b"original unit test source")

    def test_malformed_manifest_is_never_overwritten_by_error_output(self):
        manifest = self.root / "manifest.json"
        original = b'{"schema_version":'
        manifest.write_bytes(original)
        process = subprocess.run([sys.executable, str(SCRIPT), str(manifest), "--output", str(manifest)], capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(manifest.read_bytes(), original)

    def test_output_cannot_overwrite_model_referenced_inside_hashed_report(self):
        source = self.source("nested", "g")
        evaluation = self.evaluation("tracking", source, {})
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "sources": [source], "splits": {"holdout": ["g"]}, "evaluations": [evaluation]}))
        model = self.root / "tracking.weights"
        original = model.read_bytes()
        process = subprocess.run([sys.executable, str(SCRIPT), str(manifest), "--output", str(model)], capture_output=True, check=False)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(model.read_bytes(), original)

    def test_disjoint_group_names_do_not_hide_same_canonical_play_leakage(self):
        result = MODULE.evaluate({"schema_version": 1,
                                  "sources": [self.source("a", "train-group"),
                                              self.source("b", "heldout-group")],
                                  "splits": {"train": ["train-group"], "holdout": ["heldout-group"]}}, self.root)
        self.assertTrue(self.gate(result, "disjoint_source_groups")["passed"])
        self.assertFalse(self.gate(result, "no_media_or_play_leakage")["passed"])

    def test_alternate_recordings_do_not_inflate_canonical_population(self):
        result = MODULE.evaluate({"schema_version": 1,
                                  "sources": [self.source("a", "game-1"), self.source("b", "game-1")],
                                  "splits": {"holdout": ["game-1"]}}, self.root)
        self.assertEqual(self.gate(result, "heldout_population")["measured"]["canonical_plays"], 1)

    def test_identical_media_cannot_masquerade_as_independent_sources(self):
        sources = [self.source("a", "group-a"), self.source("b", "group-b")]
        sources[1]["media"] = sources[0]["media"]
        labels = json.loads((self.root / sources[1]["labels"]["path"]).read_text())
        labels["media_sha256"] = sources[0]["media"]["sha256"]
        sources[1]["labels"] = self.artifact("b.labels.json", labels)
        result = MODULE.evaluate({"schema_version": 1, "sources": sources,
            "splits": {"holdout": ["group-a", "group-b"]}}, self.root)
        self.assertFalse(self.gate(result, "evidence_integrity")["passed"])

    def test_synthetic_evaluation_is_rejected_even_with_valid_file_hashes(self):
        source = self.source("a", "game-1")
        predictions = self.artifact("predictions.json", {"evidence_source": "synthetic_test", "samples": []})
        report = self.artifact("report.json", {"evidence_source": "real", "evaluator_version": "test",
            "source_groups": ["game-1"], "label_sha256": [source["labels"]["sha256"]],
            "predictions_sha256": predictions["sha256"]})
        result = MODULE.evaluate({"schema_version": 1, "sources": [source],
            "splits": {"holdout": ["game-1"]}, "evaluations": [
                {"task": "segmentation", "report": report, "predictions": predictions}]}, self.root)
        self.assertFalse(self.gate(result, "segmentation_evidence")["passed"])

    def test_missing_or_bad_numbers_do_not_become_zero_errors(self):
        self.assertEqual(MODULE.numbers([0.01, float("nan")]), [])
        self.assertEqual(MODULE.numbers([-0.1, 0.01]), [])
        self.assertEqual(MODULE.numbers([True, 0.01]), [])
        self.assertEqual(MODULE.numbers([0.01, 0.02]), [0.01, 0.02])
        self.assertEqual(MODULE.numbers([10 ** 1000]), [])

    def test_nonfinite_json_evidence_is_invalid_even_when_hash_matches(self):
        ref = self.artifact("bad.json", b'{"error":NaN}')
        evidence = MODULE.Evidence(self.root)
        self.assertIsNone(evidence.read(ref, "bad evaluation"))
        self.assertEqual(len(evidence.errors), 1)

    def test_boolean_elapsed_time_cannot_pass_overnight_timing(self):
        fields = {"gpu_name": "unit-test GPU", "detailed_runs": [{"source_minutes": 90, "elapsed_minutes": True}]}
        self.assertFalse(self.gate(self.evaluate_task("gpu_timing", fields), "gpu_overnight")["passed"])
        fields["detailed_runs"][0]["elapsed_minutes"] = 480
        self.assertTrue(self.gate(self.evaluate_task("gpu_timing", fields), "gpu_overnight")["passed"])

    def test_boolean_precision_and_missing_tracking_measurements_fail(self):
        specialist = {"name": "contact", "accepted_precision": True, "recall": .5, "coverage": .5, "onset_absolute_errors_seconds": [.1]}
        self.assertFalse(self.gate(self.evaluate_task("interactions", {"specialists": [specialist]}), "interaction_precision")["passed"])
        specialist["accepted_precision"] = .99
        self.assertTrue(self.gate(self.evaluate_task("interactions", {"specialists": [specialist]}), "interaction_precision")["passed"])
        tracking = {"idf1": 95, "hota": 75, "visibility_coverage": None, "identity_switches": 1,
                    "fragmentation": 2, "visible_player_recall": .9, "localization_error": .1}
        self.assertFalse(self.gate(self.evaluate_task("tracking", {"categories": {"practice": tracking}}), "tracking_practice")["passed"])

    def test_boolean_component_version_is_not_deployment_identity(self):
        components = ("frontend", "edge", "backend", "worker", "database_schema", "models", "artifacts")
        checks = ("upload", "durable_queue", "gpu_claim", "worker_results", "signed_range_playback",
                  "normal_speed_playback", "evidence_drilldown", "analytics_query", "report_download", "correction_preservation")
        trace = {"component_versions": dict.fromkeys(components, True), "checks": dict.fromkeys(checks, True)}
        self.assertFalse(self.gate(self.evaluate_task("production_e2e", trace), "production_upload_to_result")["passed"])

    def test_boolean_evaluator_version_is_not_real_evaluation_lineage(self):
        result = self.evaluate_task("tracking", {"evaluator_version": True})
        self.assertFalse(self.gate(result, "tracking_evidence")["passed"])

    def test_identity_confidence_bound_requires_enough_measured_cases(self):
        self.assertLess(MODULE.wilson_lower(20, 20), .99)
        self.assertGreater(MODULE.wilson_lower(1990, 2000), .99)
        self.assertLess(MODULE.wilson_lower(1980, 2000), .99)
        self.assertEqual(MODULE.wilson_lower(0, 0), 0)

    def test_dense_tracks_require_actual_monotonic_points(self):
        self.assertFalse(MODULE.valid_dense_track({"target_id": "t", "points": ["fake", "fake"]}))
        points = [{"timestamp_seconds": t, "bbox": [0, 0, 2, 3]} for t in (0, .1)]
        self.assertTrue(MODULE.valid_dense_track({"target_id": "t", "points": points}))
        self.assertFalse(MODULE.valid_dense_track({"target_id": "t", "points": points[::-1]}))

    def dense_fixture(self):
        digest = "a" * 64
        index = self.artifact("native.json", {"evidence_kind": "source_frame_index", "source_id": "a", "media_sha256": digest,
            "width": 100, "height": 100, "start_seconds": 0, "end_seconds": .21, "frame_pts_seconds": [0, .1, .2]})
        coverage = {"evidence_kind": "human_reviewed_dense_coverage", "source_id": "a", "media_sha256": digest,
            "source_play_id": "p", "canonical_play_id": "canonical", "completed": True, "all_visible_players_annotated": True,
            "occlusion_offscreen_records_reviewed": True, "reviewer_ids": ["unit-test-staff"], "native_frame_index": index,
            "frames": [{"timestamp_seconds": stamp, "visible_target_ids": ["t"] if stamp != .1 else []} for stamp in (0, .1, .2)]}
        play = {"id": "p", "canonical_play_id": "canonical", "start_seconds": 0, "snap_seconds": 0, "end_seconds": .2,
            "dense_tracks": [{"target_id": "t", "points": [{"timestamp_seconds": stamp, "bbox": [1, 2, 20, 30], "shot_id": "shot-1"} for stamp in (0, .2)],
                              "visibility_events": [{"timestamp_seconds": .1, "visibility_state": "occluded", "shot_id": "shot-1"}]}]}
        return play, coverage, digest

    def test_sparse_track_points_do_not_establish_dense_coverage(self):
        play, coverage, digest = self.dense_fixture()
        evidence = MODULE.Evidence(self.root)
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, evidence))
        play["dense_coverage"] = self.artifact("coverage.json", coverage)
        self.assertTrue(MODULE.dense_coverage_valid(play, "a", digest, evidence))
        play["dense_tracks"][0]["visibility_events"] = []
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, evidence))
        play["dense_tracks"][0]["visibility_events"] = [{"timestamp_seconds": .05, "visibility_state": "occluded", "shot_id": "shot-1"}]
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, evidence))

    def test_dense_coverage_requires_every_native_frame_and_visible_box(self):
        play, coverage, digest = self.dense_fixture()
        coverage["frames"].pop(1)
        play["dense_coverage"] = self.artifact("incomplete.json", coverage)
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, MODULE.Evidence(self.root)))
        play, coverage, digest = self.dense_fixture()
        coverage["frames"][1]["visible_target_ids"] = ["t"]
        play["dense_coverage"] = self.artifact("missing-box.json", coverage)
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, MODULE.Evidence(self.root)))

    def test_dense_coverage_rejects_unattested_or_wrong_source(self):
        play, coverage, digest = self.dense_fixture()
        coverage["completed"] = False
        play["dense_coverage"] = self.artifact("unreviewed.json", coverage)
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", digest, MODULE.Evidence(self.root)))
        coverage["completed"] = True
        play["dense_coverage"] = self.artifact("reviewed.json", coverage)
        self.assertFalse(MODULE.dense_coverage_valid(play, "a", "b" * 64, MODULE.Evidence(self.root)))

    def test_conflicting_alternate_identity_prediction_is_not_hidden(self):
        sources = [self.source("a", "g"), self.source("b", "g")]
        predicted = self.artifact("identities.json", {"evidence_source": "real", "samples": [
            {"source_id": source["id"], "play_id": "p1", "target_id": "t1", "accepted": True,
             "player_id": "player-1" if source["id"] == "a" else "wrong-player"}
            for source in sources]})
        weights = self.artifact("fixture.weights", b"synthetic unit test weights")
        report = self.artifact("identity.report.json", {"evidence_source": "real", "evaluator_version": "unit-test",
            "source_groups": ["g"], "label_sha256": [source["labels"]["sha256"] for source in sources],
            "predictions_sha256": predicted["sha256"], "model_weights": [weights]})
        result = MODULE.evaluate({"schema_version": 1, "sources": sources, "splits": {"holdout": ["g"]},
            "evaluations": [{"task": "identity", "predictions": predicted, "report": report}]}, self.root)
        measurement = self.gate(result, "automatic_identity")["measured"]
        self.assertEqual(measurement["accepted"], 1)
        self.assertEqual(measurement["correct"], 0)
        self.assertEqual(measurement["readable_team_resolved_cases"], 1)


if __name__ == "__main__":
    unittest.main()
