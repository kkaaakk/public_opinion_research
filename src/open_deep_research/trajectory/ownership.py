"""Small OS-backed writer lease; a crashed process automatically releases it."""

from __future__ import annotations

import sys
from pathlib import Path


class WriterOwnedError(RuntimeError):
    """Report a session already owned by another live writer."""


class WriterLease:
    """Hold an exclusive byte/file lock for the session writer's lifecycle."""

    def __init__(self, path: Path):
        """Acquire ownership without overwriting or inspecting another writer's log."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        if path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise WriterOwnedError("Trajectory session has a live writer") from exc

    def close(self) -> None:
        """Release ownership; the small lock file is reusable after a crash."""
        self.handle.close()


def writer_active(path: Path) -> bool:
    """Probe ownership conservatively, never mistaking an unreadable lease for idle."""
    if not path.exists():
        return False
    try:
        lease = WriterLease(path)
    except OSError:
        return True
    except WriterOwnedError:
        return True
    lease.close()
    return False
