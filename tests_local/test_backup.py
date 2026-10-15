"""A failed snapshot must block migration without losing prior artifacts."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_app.backup import record_migration, snapshot, snapshot_before_migration
from local_app.config import LocalConfig


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.config = LocalConfig.load(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_failed_dump_preserves_existing_backups_and_removes_partial_bytes(
        self,
    ) -> None:
        prior = self.config.data / "backups/previous.dump"
        prior.write_bytes(b"previous good snapshot")
        with (
            patch.object(self.config, "executable", return_value="pg_dump"),
            patch(
                "local_app.backup.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "migration was not started"):
                snapshot(self.config)
        self.assertEqual(prior.read_bytes(), b"previous good snapshot")
        self.assertEqual(list((self.config.data / "backups").glob("*.tmp")), [])

    def test_owned_schema_changes_snapshot_before_migration_but_same_version_does_not(
        self,
    ) -> None:
        with (
            patch(
                "local_app.backup.sql",
                return_value=SimpleNamespace(returncode=0, stdout="alembic_version\n"),
            ),
            patch("local_app.backup.snapshot") as dump,
        ):
            snapshot_before_migration(self.config)
            dump.assert_called_once()
            record_migration(self.config)
            snapshot_before_migration(self.config)
            dump.assert_called_once()

    def test_external_database_has_no_implicit_native_backup(self) -> None:
        self.config.external_database = "postgresql+asyncpg://localhost/development"
        with (
            patch("local_app.backup.sql") as inspect,
            patch("local_app.backup.subprocess.run") as process,
        ):
            self.assertIsNone(snapshot(self.config))
            snapshot_before_migration(self.config)
            inspect.assert_not_called()
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
