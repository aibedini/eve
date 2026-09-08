"""Private, deployment-local paths for cross-process runtime state."""

import os
from pathlib import Path


def runtime_path(filename: str) -> str:
    if not filename or Path(filename).name != filename:
        raise ValueError('runtime filename must be a single path component')
    root = Path(os.environ.get('EVE_RUNTIME_DIR') or (Path.cwd() / 'instance' / 'runtime'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return str(root / filename)


def open_private_lock(filename: str, mode: str = 'a+'):
    path = runtime_path(filename)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, mode)
