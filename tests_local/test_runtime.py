"""Recovery tests for the launcher; no database or model downloads required."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_app.config import LocalConfig, atomic_json, read_dotenv
from local_app.supervisor import Service, Supervisor


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name)
        for name in ("logs", "runtime/controls", "cache", "storage"):
            (self.data / name).mkdir(parents=True)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_atomic_config_and_status_never_publish_credentials(self) -> None:
        config = LocalConfig.load(self.data)
        supervisor = Supervisor(self.data, config.api_url)
        supervisor.publish()
        status = (self.data / "runtime/status.json").read_text()
        for key in (
            "secret_key",
            "database_password",
            "worker_password",
            "admin_password",
        ):
            self.assertNotIn(config.secrets[key], status)
        if os.name != "nt":
            self.assertEqual(os.stat(self.data / "config.json").st_mode & 0o777, 0o600)
        self.assertEqual(list(self.data.glob(".*.tmp")), [])

    def test_standalone_does_not_implicitly_connect_to_hosted_database(self) -> None:
        with patch.dict(
            os.environ, {"DATABASE_URL": "postgresql+asyncpg://example.invalid/team"}
        ):
            config = LocalConfig.load(self.data)
            self.assertIsNone(config.external_database)
            self.assertIn("127.0.0.1", config.environment()["DATABASE_URL"])

    def test_shared_absolute_storage_and_cpu_configuration(self) -> None:
        config = LocalConfig.load(self.data)
        env = config.environment()
        self.assertEqual(
            env["LOCAL_STORAGE_ROOT"], str((self.data / "storage").resolve())
        )
        self.assertEqual(env["PIPELINE_DEVICE"], "cpu")
        self.assertEqual(env["AUTO_TRAIN_ENABLED"], "0")
        self.assertEqual(env["BACKEND_API_URL"], env["PUBLIC_API_BASE_URL"])

    def test_service_exit_recovers_and_restart_budget_is_bounded(self) -> None:
        supervisor = Supervisor(self.data, "http://127.0.0.1:1", restart_limit=2)
        service = Service(
            "worker",
            [sys.executable, "-c", "raise SystemExit(3)"],
            self.data,
            os.environ.copy(),
        )
        supervisor.add(service)
        try:
            self.assertTrue(supervisor.start(service))
            service.process.wait(timeout=5)
            supervisor.tick()
            self.assertEqual(service.state, "recovering")
            service.next_start = 0
            supervisor.tick()
            service.process.wait(timeout=5)
            supervisor.tick()
            service.next_start = 0
            supervisor.tick()
            self.assertEqual(service.state, "failed")
            self.assertEqual(len(service.starts), 2)
            self.assertIn("Restart limit", service.last_error)
        finally:
            supervisor.shutdown()

    def test_cache_repair_preserves_originals_and_models(self) -> None:
        cache = self.data / "cache/proxy.tmp"
        original = self.data / "storage/original.mp4"
        cache.write_bytes(b"disposable")
        original.write_bytes(b"film")
        supervisor = Supervisor(self.data, "http://127.0.0.1:1")
        atomic_json(
            self.data / "runtime/controls/cleanup.json", {"action": "clear-cache"}
        )
        supervisor.controls([], self.data, {})
        self.assertFalse(cache.exists())
        self.assertEqual(original.read_bytes(), b"film")
        self.assertFalse((self.data / "runtime/controls/cleanup.json").exists())

    def test_stop_cleans_up_a_running_process(self) -> None:
        supervisor = Supervisor(self.data, "http://127.0.0.1:1")
        service = Service(
            "worker",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            self.data,
            os.environ.copy(),
        )
        supervisor.add(service)
        supervisor.start(service)
        process = service.process
        supervisor.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertEqual(
            json.loads((self.data / "runtime/status.json").read_text())["state"],
            "stopped",
        )

    def test_dotenv_preserves_quoted_values_and_filters_inline_comments(self) -> None:
        path = self.data / ".env"
        path.write_text(
            'STORAGE_BACKEND=local # comment\nTEAM_NAME="Toledo # Rockets"\nexport VALUE=ok\n'
        )
        self.assertEqual(
            read_dotenv(path),
            {
                "STORAGE_BACKEND": "local",
                "TEAM_NAME": "Toledo # Rockets",
                "VALUE": "ok",
            },
        )


if __name__ == "__main__":
    unittest.main()
