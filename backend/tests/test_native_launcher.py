"""Native launcher locks and clean stop controls require no console or database."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Backend-only pytest runs also need the launcher package from the repository.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_app.config import LocalConfig  # noqa: E402
from local_app.connected import run_connected  # noqa: E402
from local_app.database import sql, start_database  # noqa: E402
from local_app.instance import InstanceLock  # noqa: E402
from local_app.supervisor import Service, Supervisor  # noqa: E402


def test_instance_lock_releases_after_exit_without_signaling_owner(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(
        os, "kill", lambda *_: pytest.fail("A launcher must never signal a lock owner")
    )
    first = InstanceLock(tmp_path)
    second = InstanceLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    assert first.path.is_file()  # keep the inode safe for simultaneous openers


def test_crashed_launcher_releases_os_lock(tmp_path):
    script = (
        "from pathlib import Path; from local_app.instance import InstanceLock; import sys; "
        "lock=InstanceLock(Path(sys.argv[1])); assert lock.acquire(); "
        "print('locked', flush=True); sys.stdin.read(1)"
    )
    child = subprocess.Popen(  # noqa: S603 - fixed Python test code, no shell or user input
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        owned = InstanceLock(tmp_path)
        assert not owned.acquire()
        child.kill()
        child.wait(timeout=5)
        assert owned.acquire()
        owned.release()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_connected_agent_cannot_overlap_a_local_library(tmp_path, capsys):
    config = LocalConfig.load(tmp_path)
    lock = InstanceLock(tmp_path)
    assert lock.acquire()
    try:
        assert run_connected(config, "https://team.example", None, True, True) == 1
        assert "already has a running launcher" in capsys.readouterr().err
    finally:
        lock.release()


@pytest.mark.parametrize(
    "url", ["ftp://localhost", "https://user:secret@team.example", "https://team.example?token=x"]
)
def test_connected_agent_rejects_unsafe_api_address_before_starting(tmp_path, capsys, url):
    assert run_connected(LocalConfig.load(tmp_path), url, None, True, True) == 1
    assert "HTTPS team API URL" in capsys.readouterr().err
    lock = InstanceLock(tmp_path)
    assert lock.acquire()
    lock.release()


def test_owned_database_uses_scram_private_socket_and_locale_independent_rows(
    tmp_path, monkeypatch
):
    config = LocalConfig.load(tmp_path)
    monkeypatch.setattr(config, "executable", lambda name: str(tmp_path / name))
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr("local_app.database.subprocess.run", run)
    supervisor = Supervisor(tmp_path, config.api_url)
    monkeypatch.setattr(supervisor, "start", lambda _service: True)
    start_database(config, supervisor)
    init = next(command for command in commands if command[0].endswith("initdb"))
    assert init[init.index("--auth-local") + 1] == "scram-sha-256"
    scalar = next(command for command in commands if command[0].endswith("psql"))
    assert "--tuples-only" in scalar and "--no-align" in scalar
    assert not any("CREATE DATABASE" in str(command) for command in commands)
    service = supervisor.services["database"]
    assert service.stop_command and "fast" in service.stop_command
    if sys.platform != "win32":
        sockets = Path(service.command[service.command.index("-k") + 1])
        assert sockets.is_dir() and sockets.stat().st_mode & 0o777 == 0o700


def test_console_free_service_stops_via_private_control_file(tmp_path, monkeypatch):
    import os

    config = LocalConfig.load(tmp_path)
    script = (
        "import os,time; from pathlib import Path; "
        "path=Path(os.environ['LOCAL_APP_STOP_FILE']); "
        "exec('while not path.exists():\\n time.sleep(0.02)')"
    )
    service = Service("worker", [sys.executable, "-c", script], ROOT, os.environ.copy())
    supervisor = Supervisor(tmp_path, config.api_url)
    supervisor.add(service)
    assert supervisor.start(service)
    process = service.process
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_: pytest.fail("Clean service stop needs no console signal"),
        raising=False,
    )
    try:
        supervisor.stop(service)
        assert process.returncode == 0
        assert Path(service.environment["LOCAL_APP_STOP_FILE"]).is_file()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_api_and_worker_keep_separate_stop_files_when_launched_from_shared_environment(tmp_path):
    import os

    config = LocalConfig.load(tmp_path)
    shared = os.environ.copy()
    script = (
        "import os,time; from pathlib import Path; "
        "path=Path(os.environ['LOCAL_APP_STOP_FILE']); "
        "exec('while not path.exists():\\n time.sleep(0.02)')"
    )
    api = Service("api", [sys.executable, "-c", script], ROOT, shared)
    worker = Service("worker", [sys.executable, "-c", script], ROOT, shared)
    supervisor = Supervisor(tmp_path, config.api_url)
    supervisor.add(api)
    supervisor.add(worker)
    try:
        assert supervisor.start(api)
        assert supervisor.start(worker)
        processes = [api.process, worker.process]
        assert api.environment["LOCAL_APP_STOP_FILE"] != worker.environment["LOCAL_APP_STOP_FILE"]
        assert "LOCAL_APP_STOP_FILE" not in shared
        supervisor.shutdown()
        assert [process.returncode for process in processes] == [0, 0]
    finally:
        for service in (api, worker):
            if service.process and service.process.poll() is None:
                service.process.kill()
                service.process.wait(timeout=5)


def test_psql_has_no_user_startup_script_or_localized_table_format(tmp_path, monkeypatch):
    config = LocalConfig.load(tmp_path)
    monkeypatch.setattr(config, "executable", lambda _name: "psql")
    calls = []
    monkeypatch.setattr(
        "local_app.database.subprocess.run",
        lambda command, **_kwargs: (
            calls.append(command) or SimpleNamespace(returncode=0, stdout="1\n")
        ),
    )
    assert sql(config, "SELECT 1").stdout.strip() == "1"
    assert {"--no-psqlrc", "--tuples-only", "--no-align"}.issubset(calls[0])


def test_windows_private_path_replaces_preexisting_explicit_acl(tmp_path, monkeypatch):
    from local_app.config import protect_private_path

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='"user","S-1-5-21-123"\n')

    monkeypatch.setattr("local_app.config.subprocess.run", run)
    with monkeypatch.context() as windows:
        windows.setattr("local_app.config.os.name", "nt")
        protect_private_path(tmp_path)
    assert calls[1] == ["icacls", str(tmp_path), "/reset"]
    assert calls[2] == [
        "icacls",
        str(tmp_path),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-123:(OI)(CI)F",
    ]


def test_library_folder_never_privatises_an_unrelated_nonempty_directory(tmp_path):
    (tmp_path / "unrelated-document.txt").write_text("keep", encoding="utf-8")
    original_mode = tmp_path.stat().st_mode
    with pytest.raises(ValueError, match="empty dedicated library folder"):
        LocalConfig.load(tmp_path)
    assert tmp_path.stat().st_mode == original_mode
    assert not (tmp_path / "config.json").exists()


def test_library_initialisation_allows_only_empty_app_scaffolding(tmp_path):
    for folder in ("logs", "runtime/controls", "cache", "storage"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    config = LocalConfig.load(tmp_path)
    assert config.data == tmp_path
    assert (tmp_path / "config.json").is_file()
