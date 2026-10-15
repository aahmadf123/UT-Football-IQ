"""Private same-container processing must fail safely and preserve API access."""

from unittest.mock import MagicMock, patch

from scripts.hosted_runtime import main, migrate, processing_enabled, seed_worker, terminate


def test_cpu_processing_requires_private_service_account() -> None:
    assert not processing_enabled({})
    assert not processing_enabled({"HOSTED_PROCESSING_ENABLED": "1"})
    configured = {"WORKER_EMAIL": "worker@example.com", "WORKER_PASSWORD": "fixture-password"}
    assert processing_enabled(configured)
    assert not processing_enabled({**configured, "HOSTED_PROCESSING_ENABLED": "0"})


def test_worker_bootstrap_cannot_reset_or_seed_admin() -> None:
    environment = {
        "WORKER_EMAIL": "worker@example.com",
        "WORKER_PASSWORD": "fixture-password",
        "SEED_ADMIN_EMAIL": "admin@example.com",
        "SEED_ADMIN_PASSWORD": "fixture-admin",
        "SEED_RESET_PASSWORDS": "1",
    }
    with patch("scripts.hosted_runtime.subprocess.run") as run:
        run.return_value.returncode = 0
        assert seed_worker(environment)
    actual = run.call_args.kwargs["env"]
    assert actual["SEED_ADMIN_EMAIL"] == actual["SEED_ADMIN_PASSWORD"] == ""
    assert actual["SEED_RESET_PASSWORDS"] == "0"
    assert actual["SEED_WORKER_EMAIL"] == environment["WORKER_EMAIL"]


def test_worker_gets_graceful_exit_before_force_kill() -> None:
    process = MagicMock()
    process.poll.return_value = None
    terminate(process, grace=35)
    process.send_signal.assert_called_once()
    process.wait.assert_called_once_with(timeout=35)


def test_schema_migration_is_bounded_and_does_not_log_private_output() -> None:
    with patch("scripts.hosted_runtime.subprocess.run") as run:
        run.return_value.returncode = 1
        assert not migrate({"DATABASE_URL": "private-fixture"})
    assert run.call_args.args[0] == ["/opt/api/bin/alembic", "upgrade", "head"]
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["timeout"] == 120


def test_failed_migration_refuses_to_start_api_or_worker() -> None:
    with (
        patch("scripts.hosted_runtime.migrate", return_value=False),
        patch("scripts.hosted_runtime.subprocess.Popen") as spawn,
    ):
        assert main() == 1
    spawn.assert_not_called()
