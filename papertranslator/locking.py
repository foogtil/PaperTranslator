"""Process-wide task ownership, released by the OS even after a forced stop."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading

_guard = threading.RLock()
_held = {}
save_guard = threading.RLock()


def identity(path):
    return os.path.normcase(str(Path(path).resolve()))


@contextmanager
def job_lock(source):
    # Reentrant across HTTP threads of this process, exclusive across apps.
    from .settings import data_dir
    key = hashlib.sha256(identity(source).encode('utf-8')).hexdigest()
    lock_path = data_dir() / 'locks' / (key + '.lock')
    name = str(lock_path.resolve())
    with _guard:
        if name in _held:
            _held[name][1] += 1
        else:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, 'a+b')
            try:
                if not lock_path.stat().st_size:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise ValueError('另一个程序窗口正在处理此文献，请等待该任务结束后再继续。') from None
            _held[name] = [handle, 1]
    try:
        yield
    finally:
        with _guard:
            _held[name][1] -= 1
            if not _held[name][1]:
                handle = _held.pop(name)[0]
                # Closing the sole handle releases the OS lock. Keep the inode
                # in place so another process never locks a different file.
                handle.close()
