"""Serialize local lifecycle operations without locking unrelated projects."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from functools import wraps

from pg_stand.errors import PreconditionError

_local = threading.local()


@contextmanager
def project_operation(project):
    # Keep this inode outside storage and credentials, including during cleanup.
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    path = project / ".pg_stand-operation.lock"
    held = getattr(_local, "held", {})
    if path in held:
        yield
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreconditionError("another lifecycle operation owns this stand project") from None
        held[path] = fd
        _local.held = held
        try:
            yield
        finally:
            del held[path]
    finally:
        os.close(fd)


def serialized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with project_operation(self.config.project_directory):
            return method(self, *args, **kwargs)

    return run
