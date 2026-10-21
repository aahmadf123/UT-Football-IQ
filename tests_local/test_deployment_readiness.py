"""Behavioral release gates; these fixtures are not football evaluation data."""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deployment_readiness.py"


class DeploymentReadinessTests(unittest.TestCase):
    @staticmethod
    def history(tags):
        return {"schema_version": 2, "verified_images": [{"tag": tag, "digest": "sha256:" + tag[0] * 64} for tag in tags]}

    @staticmethod
    def plan(module, inventory, tag, history):
        return module.retention_plan(inventory, tag, history, "sha256:" + "a" * 64)

    def module(self):
        self.assertTrue(SCRIPT.exists(), "Release verification/retention implementation is missing")
        spec = importlib.util.spec_from_file_location("deployment_readiness", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_requires_matching_build_and_database_readiness(self):
        module = self.module()
        sha = "a" * 40
        self.assertTrue(module.is_ready({"git_sha": sha}, {"status": "ready", "checks": {"database": "ok"}}, sha))
        for version, ready in [({"git_sha": "b" * 40}, {"status": "ready", "checks": {"database": "ok"}}),
                               ({"git_sha": sha}, {"status": "ready", "checks": {"database": "unavailable"}}),
                               ({"git_sha": sha}, {"status": "ready"}),
                               ({"git_sha": sha}, {"status": "not_ready", "checks": {"database": "ok"}})]:
            self.assertFalse(module.is_ready(version, ready, sha))

    def test_retains_current_and_two_previously_verified_images(self):
        module = self.module()
        inventory = [{"name": "footiq-api-edge-backendcontainer", "tags": ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"]},
                     {"name": "another-app", "tags": ["eeeeeeee"]}]
        result = self.plan(module, inventory, "aaaaaaaa", self.history(["bbbbbbbb", "cccccccc", "dddddddd"]))
        self.assertEqual([image["tag"] for image in result["verified_images"]], ["aaaaaaaa", "bbbbbbbb", "cccccccc"])
        self.assertEqual(result["delete_tags"], ["dddddddd"])

    def test_bootstrap_and_missing_protected_tags_never_delete_rollback(self):
        module = self.module()
        inventory = [{"name": "footiq-api-edge-backendcontainer", "tags": ["aaaaaaaa", "bbbbbbbb", "dddddddd"]}]
        self.assertEqual(self.plan(module, inventory, "aaaaaaaa", None)["delete_tags"], [])
        self.assertEqual(self.plan(module, inventory, "aaaaaaaa", self.history(["bbbbbbbb", "cccccccc"]))["delete_tags"], [])

    def test_repeated_image_does_not_displace_known_good_predecessor(self):
        module = self.module()
        inventory = [{"name": "footiq-api-edge-backendcontainer", "tags": ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"]}]
        result = self.plan(module, inventory, "aaaaaaaa", self.history(["aaaaaaaa", "bbbbbbbb", "cccccccc"]))
        self.assertEqual([image["tag"] for image in result["verified_images"]], ["aaaaaaaa", "bbbbbbbb", "cccccccc"])
        self.assertEqual(result["delete_tags"], ["dddddddd"])

    def test_conflicting_tag_digest_history_cannot_delete_another_image(self):
        module = self.module()
        history = self.history(["bbbbbbbb", "cccccccc"])
        history["verified_images"][1]["tag"] = "bbbbbbbb"
        inventory = [{"name": "footiq-api-edge-backendcontainer", "tags": ["aaaaaaaa", "bbbbbbbb", "dddddddd"]}]
        with self.assertRaises(ValueError):
            self.plan(module, inventory, "aaaaaaaa", history)

    def test_aliases_of_one_digest_are_one_generation(self):
        module = self.module()
        history = self.history(["bbbbbbbb", "cccccccc"])
        history["verified_images"][1]["digest"] = history["verified_images"][0]["digest"]
        inventory = [{"name": "footiq-api-edge-backendcontainer", "tags": ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"]}]
        result = self.plan(module, inventory, "aaaaaaaa", history)
        self.assertEqual(len(result["verified_images"]), 2)
        self.assertEqual(result["delete_tags"], [])

    def test_corrupt_history_and_unsafe_tags_fail_closed(self):
        module = self.module()
        for history in [{}, {"schema_version": 1, "verified_tags": "aaaaaaaa"},
                        {"schema_version": 1, "verified_tags": ["bad;command"]}]:
            with self.assertRaises(ValueError):
                self.plan(module, [], "aaaaaaaa", history)
        with self.assertRaises(ValueError):
            self.plan(module, [], "aaaaaaaa;rm", None)

    def test_poll_survives_rollout_timeout_and_resets_steady_build(self):
        module = self.module()
        sha = "a" * 40
        ready = {"status": "ready", "checks": {"database": "ok"}}
        sequence = iter([TimeoutError(), ({"git_sha": sha}, ready),
                         ({"git_sha": "b" * 40}, ready),
                         ({"git_sha": sha}, ready), ({"git_sha": sha}, ready)])
        now = [0.0]

        def probe(*_):
            value = next(sequence)
            if isinstance(value, Exception):
                raise value
            return value

        def sleep(seconds):
            now[0] += seconds

        result = module.wait_until_ready("http://localhost", sha, timeout_seconds=10, interval_seconds=1,
                                         steady_checks=2, probe=probe, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result["git_sha"], sha)
        self.assertEqual(result["consecutive_checks"], 2)
        self.assertEqual(now[0], 4)

    def test_deadline_exits_without_writing_success_evidence(self):
        module = self.module()
        now = [0.0]
        with self.assertRaises(TimeoutError):
            module.wait_until_ready("http://localhost", "a" * 40, timeout_seconds=2, interval_seconds=1,
                                    probe=lambda *_: ({}, {}), clock=lambda: now[0],
                                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))


if __name__ == "__main__":
    unittest.main()
