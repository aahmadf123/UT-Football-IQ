"""Per-install configuration; credentials never enter the public status file."""

from __future__ import annotations

import csv
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def protect_private_path(path: Path) -> None:
    """Restrict install secrets to the current OS account, including Windows ACLs."""
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = list(csv.reader(identity.stdout.splitlines()))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError(
            "Cannot determine the current Windows account for private library storage"
        )
    permission = "(OI)(CI)F" if path.is_dir() else "F"
    # /inheritance:r alone retains explicit grants on an existing folder.
    subprocess.run(["icacls", str(path), "/reset"], capture_output=True, check=True)
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{rows[0][1]}:{permission}",
        ],
        capture_output=True,
        check=True,
    )


def application_root() -> Path:
    configured = os.environ.get("FOOTBALL_IQ_BUNDLE_ROOT")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        for candidate in Path(sys.executable).resolve().parents:
            if (candidate / "native" / "postgres").is_dir():
                return candidate
        return Path(sys.executable).resolve().parents[1]
    return Path(__file__).resolve().parents[1]


def default_data_directory() -> Path:
    configured = os.environ.get("FOOTBALL_IQ_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "Football-IQ"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Football-IQ"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "football-iq"
    )


def atomic_json(path: Path, payload: object, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            if private:
                protect_private_path(temporary)
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_dotenv(path: Path) -> dict[str, str]:
    """Read the simple scalar configuration used by this repository."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.replace("_", "").isalnum():
            continue
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0:
                raise ValueError(f"Unterminated configuration value for {key}")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def available_port(preferred: int) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def initial_library_scaffold(entry: Path) -> bool:
    """Allow empty app folders and the GUI log created before configuration."""
    folders = {
        "logs",
        "runtime",
        "cache",
        "storage",
        "models",
        "checkpoints",
        "backups",
    }
    if entry.name not in folders or not entry.is_dir() or entry.is_symlink():
        return False
    for child in entry.rglob("*"):
        if child.is_symlink():
            return False
        if child.is_file() and not (
            entry.name == "logs" and child.name == "launcher.log"
        ):
            return False
    return True


@dataclass
class LocalConfig:
    root: Path
    data: Path
    api_port: int
    db_port: int
    secrets: dict[str, str]
    external_database: str | None = None

    @classmethod
    def load(
        cls,
        data: Path | None = None,
        api_port: int = 8000,
        external_database: str | None = None,
    ) -> LocalConfig:
        root = application_root()
        data = (data or default_data_directory()).expanduser().resolve()
        path = data / "config.json"
        if (
            data.is_dir()
            and not path.is_file()
            and any(not initial_library_scaffold(entry) for entry in data.iterdir())
        ):
            raise ValueError(
                "Choose an empty dedicated library folder. If this is an existing "
                "Football-IQ library, restore its config.json from your installation backup."
            )
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(data)
        if path.is_file():
            protect_private_path(path)
            stored = json.loads(path.read_text(encoding="utf-8"))
        else:
            stored = {
                "schema_version": 1,
                "installation_id": secrets.token_hex(16),
                "database_password": secrets.token_urlsafe(32),
                "secret_key": secrets.token_urlsafe(48),
                "worker_password": secrets.token_urlsafe(32),
                "admin_password": secrets.token_urlsafe(32),
                "launcher_key": secrets.token_urlsafe(32),
                "database_port": available_port(55432),
            }
            atomic_json(path, stored, private=True)
        if "launcher_key" not in stored:
            stored["launcher_key"] = secrets.token_urlsafe(32)
            atomic_json(path, stored, private=True)
        required = {
            "installation_id",
            "database_password",
            "secret_key",
            "worker_password",
            "admin_password",
        }
        if not required.issubset(stored):
            raise ValueError(
                "Local configuration is incomplete; "
                "restore config.json from your installation backup"
            )
        config = cls(
            root,
            data,
            available_port(api_port),
            int(stored["database_port"]),
            stored,
            external_database,
        )
        for name in (
            "storage",
            "models",
            "cache",
            "checkpoints",
            "runtime",
            "runtime/controls",
            "logs",
            "backups",
        ):
            (data / name).mkdir(parents=True, exist_ok=True)
        return config

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def executable(self, name: str) -> str | None:
        suffix = ".exe" if sys.platform == "win32" else ""
        for directory in (
            self.root / "native" / "bin",
            self.root / "native" / "postgres" / "bin",
        ):
            path = directory / (name + suffix)
            if path.is_file():
                return str(path)
        return shutil.which(name)

    def python(self, service: str) -> str:
        variable = (
            "WORKER" if service == "gpu-worker" else service.upper().replace("-", "_")
        )
        configured = os.environ.get(f"FOOTBALL_IQ_{variable}_PYTHON")
        if configured:
            return configured
        candidates = [self.root / service / ".venv", self.root / ".venv"]
        for directory in candidates:
            path = directory / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            if path.is_file():
                return str(path)
        return sys.executable

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in read_dotenv(self.root / ".env").items():
            env.setdefault(key, value)
        env.pop("LOCAL_APP_BOOTSTRAP_TOKEN", None)
        database = self.external_database or (
            f"postgresql+asyncpg://footiq:{self.secrets['database_password']}@127.0.0.1:{self.db_port}/footiq"
        )
        sync = database.replace("postgresql+asyncpg://", "postgresql://")
        env.update(
            {
                "ENVIRONMENT": "production",
                "DATABASE_URL": database,
                "DATABASE_SYNC_URL": sync,
                "SECRET_KEY": self.secrets["secret_key"],
                "FOOTBALL_IQ_DATA_DIR": str(self.data),
                "LOCAL_STORAGE_ROOT": str(self.data / "storage"),
                "STORAGE_BACKEND": "local",
                "PUBLIC_API_BASE_URL": self.api_url,
                "BACKEND_API_URL": self.api_url,
                "CORS_ORIGINS": self.api_url,
                "LOCAL_APP_STATIC_DIR": str(self.root / "frontend" / "out"),
                "LOCAL_APP_LAUNCHER_KEY": self.secrets["launcher_key"],
                "SEED_USERS_ON_STARTUP": "1",
                "SEED_ADMIN_EMAIL": "coach@footballiq.example.com",
                "SEED_ADMIN_PASSWORD": self.secrets["admin_password"],
                "SEED_WORKER_EMAIL": "worker@footballiq.example.com",
                "SEED_WORKER_PASSWORD": self.secrets["worker_password"],
                "WORKER_EMAIL": "worker@footballiq.example.com",
                "WORKER_PASSWORD": self.secrets["worker_password"],
                "WORKER_ID": f"local-{self.secrets['installation_id']}",
                "PIPELINE_DEVICE": "cpu",
                "PIPELINE_PROFILE": "quick",
                "PIPELINE_CHECKPOINT_ROOT": str(self.data / "checkpoints"),
                "MODEL_DETECT_PATH": str(self.data / "models" / "yolov8n.pt"),
                "GPU_WORKER_POLL_INTERVAL": "2",
                "GPU_WORKER_HEARTBEAT_INTERVAL": "15",
                "DEV_AUTOLOGIN": "0",
                "ENABLE_SAM3_NIGHTLY": "0",
                "ENABLE_BOTSORT_NIGHTLY": "0",
                "AUTO_TRAIN_ENABLED": "0",
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
            }
        )
        native = self.root / "native" / "bin"
        env["PATH"] = str(native) + os.pathsep + env.get("PATH", "")
        return env
