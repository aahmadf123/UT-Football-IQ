"""Copy and relocate native PostgreSQL/pgvector and FFmpeg build inputs."""

from __future__ import annotations

import argparse
import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path


def relocate_macos(native: Path) -> None:
    libraries = native / "lib"
    libraries.mkdir(exist_ok=True)
    magics = {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
    pending = []
    for path in native.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in magics:
                    pending.append(path)
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        result = subprocess.run(
            ["otool", "-L", str(path)], capture_output=True, text=True
        )
        if result.returncode:
            continue
        for line in result.stdout.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if not dependency.startswith(("/opt/homebrew/", "/usr/local/")):
                continue
            source = Path(dependency)
            if not source.is_file():
                raise RuntimeError(f"Missing native dependency: {source.name}")
            destination = libraries / source.name
            if not destination.is_file():
                shutil.copy2(source.resolve(), destination)
                destination.chmod(0o755)
                pending.append(destination)
            relative = os.path.relpath(destination, path.parent)
            subprocess.run(
                [
                    "install_name_tool",
                    "-change",
                    dependency,
                    "@loader_path/" + relative,
                    str(path),
                ],
                check=True,
            )
        identity = subprocess.run(
            ["otool", "-D", str(path)], capture_output=True, text=True
        )
        if identity.returncode == 0 and len(identity.stdout.splitlines()) > 1:
            subprocess.run(
                ["install_name_tool", "-id", "@loader_path/" + path.name, str(path)],
                check=True,
            )
        # Ad-hoc signing is needed after changing Mach-O load commands. Release
        # signing/notarization can replace it without changing the runtime.
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(path)],
            check=True,
            capture_output=True,
        )
        relocated = subprocess.run(
            ["otool", "-L", str(path)], capture_output=True, text=True, check=True
        )
        if any(
            line.strip().startswith(("/opt/homebrew/", "/usr/local/"))
            for line in relocated.stdout.splitlines()[1:]
        ):
            raise RuntimeError(
                f"Native library still depends on the build machine: {path.name}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    native = args.output.resolve()
    (native / "bin").mkdir(parents=True, exist_ok=True)
    pg_config = (
        args.postgres_root
        / "bin"
        / ("pg_config.exe" if os.name == "nt" else "pg_config")
    )
    compiled = subprocess.run(
        [str(pg_config), "--bindir", "--pkglibdir", "--sharedir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    if sys.platform == "linux":
        configuration = subprocess.run(
            [str(pg_config), "--configure"], capture_output=True, text=True, check=True
        ).stdout
        options = dict(
            item[2:].split("=", 1)
            for item in shlex.split(configuration)
            if item.startswith("--") and "=" in item
        )
        original_bin = options.get("bindir", "")
        if "/lib/postgresql/" in original_bin:
            # pg_config already relocates --bindir to its donor location. Its
            # configure arguments retain the original Debian compiled layout.
            compiled = [
                original_bin,
                str(Path(original_bin).parent / "lib"),
                options["datadir"],
            ]
    compiled_prefix = Path(os.path.commonpath(compiled))
    relative_bin, relative_lib, relative_share = [
        Path(path).relative_to(compiled_prefix) for path in compiled
    ]
    # Debian installs its binaries under lib/postgresql/<version>/bin rather
    # than prefix/bin. Preserve that compiled relative layout so PostgreSQL's
    # own relocation logic finds both catalog bootstrap files and extensions.
    debian_layout = sys.platform == "linux" and relative_bin != Path("bin")
    destinations = (
        {"bin": relative_bin, "lib": relative_lib, "share": relative_share}
        if debian_layout
        else {folder: Path(folder) for folder in ("bin", "lib", "share")}
    )
    for folder in ("bin", "lib", "share"):
        source = args.postgres_root / folder
        if not source.is_dir():
            raise SystemExit(f"PostgreSQL input is missing {folder}")
        shutil.copytree(
            source,
            native / "postgres" / destinations[folder],
            dirs_exist_ok=True,
            symlinks=False,
        )
    if debian_layout:
        aliases = native / "postgres/bin"
        aliases.mkdir(parents=True, exist_ok=True)
        for source in (native / "postgres" / relative_bin).iterdir():
            alias = aliases / source.name
            if not alias.exists():
                alias.symlink_to(os.path.relpath(source, aliases))
    for binary in (args.ffmpeg, args.ffprobe):
        shutil.copy2(binary.resolve(), native / "bin" / binary.name)
    for name in (
        "COPYRIGHT",
        "LICENSE",
        "LICENSE.txt",
        "COPYING",
        "THIRD-PARTY-NOTICES.md",
    ):
        source = args.postgres_root / name
        if source.is_file():
            shutil.copy2(source, native / "postgres" / name)
    if not list((native / "postgres" / destinations["share"]).rglob("vector.control")):
        raise SystemExit("Build pgvector against this PostgreSQL installation first")
    if sys.platform == "darwin":
        relocate_macos(native)
    version = subprocess.run(
        [
            str(
                args.postgres_root
                / "bin"
                / ("pg_config.exe" if os.name == "nt" else "pg_config")
            ),
            "--version",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    ffmpeg_version = subprocess.run(
        [str(args.ffmpeg), "-version"], capture_output=True, text=True, check=True
    ).stdout
    ffmpeg_license = subprocess.run(
        [str(args.ffmpeg), "-L"], capture_output=True, text=True, check=True
    ).stdout
    (native / "postgres/THIRD-PARTY-NOTICES.md").write_text(
        "# Native build inputs\n\n"
        + version
        + "\n\nPostgreSQL: https://www.postgresql.org/about/licence/\npgvector: https://github.com/pgvector/pgvector/blob/v0.8.1/LICENSE\n\nFFmpeg build:\n```\n"
        + ffmpeg_version
        + "```\n\n"
        + ffmpeg_license
        + "\nExact vendor sources and additional dependency notices must accompany any redistributed release.\n",
        encoding="utf-8",
    )
    print("Native dependencies prepared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
