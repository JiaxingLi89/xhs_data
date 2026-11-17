import asyncio
import fcntl
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import StopRun


class Clock:
    def now(self):
        return time.time()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def local_day(timestamp, timezone="Asia/Shanghai"):
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date().isoformat()


def next_midnight(timestamp, timezone):
    here = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    return (here.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


@contextmanager
def process_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    fd = os.open(str(directory / "worker.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StopRun("another_process_is_running") from exc
        yield
    finally:
        os.close(fd)

