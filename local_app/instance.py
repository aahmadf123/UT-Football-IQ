"""Hold one OS-owned launcher lock per library without signaling its owner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


class InstanceLock:
    def __init__(self, data: Path) -> None:
        self.path = data / "runtime" / "launcher.lock"
        self.stream: TextIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="ascii")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                # Byte-range locks work even on a newly created empty file.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        # The lock, rather than the PID text, owns this install. A crash releases
        # it automatically, and reopening cannot kill a Windows process.
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        self.stream = stream
        return True

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
        # Keep the inode. Unlinking a lock that another launcher may already
        # have opened can give two launchers different files to lock.
