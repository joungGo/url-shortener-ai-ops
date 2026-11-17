"""Local progress probes and one-process-per-role locks. Never reads credentials."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time

from ai_ops.incidents import _atomic_write, _private_directory

MAX_PROGRESS_AGE = 30
ROLES = ('ingress', 'relay')


def _file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('slack_process_file_rejected')


def _directory(store, role, *, create=False):
    if role not in ROLES:
        raise ValueError('slack_process_role_rejected')
    directory = Path(store) / 'slack-process'
    if create:
        _private_directory(directory)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('slack_process_directory_rejected')
    return directory


class Progress:
    def __init__(self, directory, role):
        self.path = directory / (role + '.json')

    def tick(self):
        _atomic_write(self.path, json.dumps({'pid': os.getpid(), 'monotonic': time.monotonic()}).encode())


@contextmanager
def process_lock(store, role):
    directory = _directory(store, role, create=True)
    fd = os.open(directory / (role + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    locked = False
    progress = Progress(directory, role)
    try:
        _file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        progress.path.unlink(missing_ok=True)  # no stale readiness after a restart
        yield progress
    finally:
        if locked:
            progress.path.unlink(missing_ok=True)
        os.close(fd)


def check_process(store, role):
    directory = _directory(store, role)
    fd = os.open(directory / (role + '.json'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _file(fd)
        raw = os.read(fd, 1025)
        if len(raw) > 1024:
            raise ValueError('slack_process_state_rejected')
        value = json.loads(raw)
    finally:
        os.close(fd)
    if (not isinstance(value, dict) or set(value) != {'pid', 'monotonic'}
            or type(value['pid']) is not int or value['pid'] <= 0
            or type(value['monotonic']) not in (float, int)
            or not math.isfinite(value['monotonic'])
            or not 0 <= time.monotonic() - value['monotonic'] <= MAX_PROGRESS_AGE):
        raise ValueError('slack_process_state_rejected')
    os.kill(value['pid'], 0)
    fd = os.open(directory / (role + '.lock'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise ValueError('slack_process_not_running')
    finally:
        os.close(fd)
