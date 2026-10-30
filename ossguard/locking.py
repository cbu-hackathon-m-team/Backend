import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()
