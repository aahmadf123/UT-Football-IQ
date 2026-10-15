"""Assemble a native CPU app from tested service virtual environments.

Run on the target OS. Native PostgreSQL/pgvector and FFmpeg are explicit build
inputs, never downloaded from inside the staff application.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def freeze(
    python: Path, entry: Path, name: str, output: Path, *, extra: list[str]
) -> None:
    build = ROOT / "build" / "native" / name
    build.mkdir(parents=True, exist_ok=True)
    native_python_libraries: list[str] = []
    environment = os.environ.copy()
    import_paths = [ROOT]
    if name == "api":
        import_paths.append(ROOT / "backend")
    elif name == "worker":
        import_paths.append(ROOT / "gpu-worker")
    # CLI --collect-submodules executes while evaluating the generated spec,
    # before Analysis applies --paths. Make those packages importable there too.
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in import_paths)
    if sys.platform == "linux":
        # Some standalone Python distributions keep Tcl outside the system
        # loader path; Tk hooks collect scripts but cannot discover these libs.
        for pattern in ("libtcl*.so", "libtk*.so"):
            for library in (Path(sys.base_prefix) / "lib").glob(pattern):
                native_python_libraries.extend(
                    ["--add-binary", str(library) + os.pathsep + "."]
                )
    subprocess.run(
        [
            str(python),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            name,
            "--distpath",
            str(output),
            "--workpath",
            str(build / "work"),
            "--specpath",
            str(build),
            "--paths",
            str(ROOT),
            "--paths",
            str(ROOT / "backend"),
            *native_python_libraries,
            *extra,
            str(entry),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-python", type=Path, required=True)
    parser.add_argument("--worker-python", type=Path, required=True)
    parser.add_argument("--postgres-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument(
        "--native-lib",
        type=Path,
        help="Relocated shared libraries from prepare_native.py",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Football-IQ")
    args = parser.parse_args()
    suffix = ".exe" if os.name == "nt" else ""
    required = [
        ROOT / "frontend/out/index.html",
        args.postgres_root / "bin" / f"postgres{suffix}",
        args.postgres_root / "bin" / f"initdb{suffix}",
        args.ffmpeg,
        args.ffprobe,
    ]
    for path in required:
        if not path.is_file():
            raise SystemExit(f"Missing required build input: {path}")
    # A distribution without pgvector is incomplete and cannot initialize the schema.
    if not list(args.postgres_root.rglob("vector.control")):
        raise SystemExit(
            "Native PostgreSQL build input must include the pgvector extension"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    freeze(
        Path(sys.executable),
        ROOT / "local_app/__main__.py",
        "FootballIQ",
        output / "launcher",
        extra=[
            "--hidden-import",
            "local_app.config",
            "--hidden-import",
            "local_app.database",
            "--hidden-import",
            "local_app.supervisor",
            "--hidden-import",
            "local_app.connected",
            "--hidden-import",
            "local_app.reopen",
            "--hidden-import",
            "local_app.gui",
            "--hidden-import",
            "local_app.backup",
        ],
    )
    freeze(
        args.backend_python,
        ROOT / "local_app/service.py",
        "api",
        output / "services",
        extra=[
            "--collect-submodules",
            "app",
            "--collect-submodules",
            "alembic",
            "--collect-submodules",
            "sqlalchemy.dialects.postgresql",
            "--add-data",
            str(ROOT / "backend/app/data") + os.pathsep + "app/data",
            "--hidden-import",
            "uvicorn.logging",
            "--hidden-import",
            "uvicorn.loops.auto",
            "--hidden-import",
            "uvicorn.protocols.http.auto",
            "--hidden-import",
            "uvicorn.protocols.websockets.auto",
            "--hidden-import",
            "uvicorn.lifespan.on",
        ],
    )
    freeze(
        args.worker_python,
        ROOT / "local_app/service.py",
        "worker",
        output / "services",
        extra=[
            "--paths",
            str(ROOT / "gpu-worker"),
            "--add-data",
            subprocess.check_output(
                [
                    str(args.worker_python),
                    "-c",
                    "import pathlib,sysconfig; print(pathlib.Path(sysconfig.get_path('stdlib'))/'queue.py')",
                ],
                text=True,
            ).strip()
            + os.pathsep
            + "_stdlib",
            "--hidden-import",
            "_queue",
            "--collect-submodules",
            "pipeline",
            "--collect-submodules",
            "worker",
            "--collect-submodules",
            "queue",
            "--collect-submodules",
            "training",
            "--collect-submodules",
            "eval",
            "--collect-all",
            "ultralytics",
            "--collect-all",
            "torch",
            "--collect-all",
            "torchvision",
            "--collect-submodules",
            "ultralytics_thop",
        ],
    )
    shutil.copytree(ROOT / "frontend/out", output / "frontend/out", dirs_exist_ok=True)
    shutil.copytree(
        ROOT / "backend/migrations", output / "backend/migrations", dirs_exist_ok=True
    )
    shutil.copy2(ROOT / "backend/alembic.ini", output / "backend/alembic.ini")
    shutil.copytree(
        ROOT / "backend/app/data", output / "backend/app/data", dirs_exist_ok=True
    )
    for folder in ("pipeline", "worker", "queue", "training", "eval"):
        source = ROOT / "gpu-worker" / folder
        if source.is_dir():
            shutil.copytree(
                source,
                output / "gpu-worker" / folder,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pt", "*.pth", "*.onnx", "*.engine"
                ),
            )
    shutil.copy2(ROOT / "gpu-worker/__main__.py", output / "gpu-worker/__main__.py")
    packaged_postgres = output / "native/postgres"
    if packaged_postgres.exists():
        shutil.rmtree(packaged_postgres)
    shutil.copytree(args.postgres_root, packaged_postgres, symlinks=True)
    (output / "native/bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.ffmpeg, output / "native/bin" / args.ffmpeg.name)
    shutil.copy2(args.ffprobe, output / "native/bin" / args.ffprobe.name)
    if args.native_lib and args.native_lib.is_dir():
        shutil.copytree(args.native_lib, output / "native/lib", dirs_exist_ok=True)
    shutil.copy2(ROOT / "LICENSES.md", output / "LICENSES.md")
    # Preserve installed distribution notices rather than treating the inventory
    # document as a replacement for upstream licenses.
    notice_script = """
import importlib.metadata as m, json, pathlib, shutil, sys
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
versions = {}
for dist in m.distributions():
    name = dist.metadata['Name']; versions[name] = dist.version
    for relative in dist.files or []:
        if any(part.lower().startswith(('license', 'copying', 'copyright', 'notice')) for part in relative.parts):
            source = pathlib.Path(dist.locate_file(relative))
            if source.is_file():
                target = out / name / pathlib.Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
(out / 'versions.json').write_text(json.dumps(versions, indent=2))
"""
    for name, python in (
        ("api", args.backend_python),
        ("worker", args.worker_python),
        ("launcher", Path(sys.executable)),
    ):
        subprocess.run(
            [str(python), "-c", notice_script, str(output / "licenses" / name)],
            check=True,
        )
    for name in (
        "COPYRIGHT",
        "LICENSE",
        "LICENSE.txt",
        "COPYING",
        "THIRD-PARTY-NOTICES.md",
    ):
        source = args.postgres_root / name
        if source.is_file():
            destination = output / "licenses/native" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    atomic_manifest = {
        "format": 1,
        "platform": sys.platform,
        "architecture": __import__("platform").machine(),
        "models_included": False,
        "native_postgres": str(args.postgres_root.name),
    }
    (output / "build-info.json").write_text(
        json.dumps(atomic_manifest, indent=2), encoding="utf-8"
    )
    if os.name == "nt":
        (output / "Start Football-IQ.cmd").write_text(
            '@echo off\nset "FOOTBALL_IQ_BUNDLE_ROOT=%~dp0"\n"%~dp0launcher\\FootballIQ\\FootballIQ.exe" gui\n',
            encoding="utf-8",
        )
    else:
        launcher = output / "Start Football-IQ.command"
        launcher.write_text(
            '#!/bin/sh\napp_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexport FOOTBALL_IQ_BUNDLE_ROOT="$app_root"\nexec "$app_root/launcher/FootballIQ/FootballIQ" gui\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    print(f"Native CPU distribution assembled: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
