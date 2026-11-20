import asyncio
import os
import errno
if os.name == "nt":
    import msvcrt
else:
    import fcntl
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import StopRun


@contextmanager
def keep_awake(progress=print):
    """Thread-scoped Windows sleep inhibition; never changes the power plan."""
    setter = None
    previous = 0
    if os.name == 'nt':
        try:
            import ctypes
            setter = ctypes.windll.kernel32.SetThreadExecutionState
            setter.argtypes = [ctypes.c_uint]
            setter.restype = ctypes.c_uint
            previous = setter(0x80000001)  # CONTINUOUS | SYSTEM_REQUIRED, not DISPLAY_REQUIRED
        except (OSError, AttributeError):
            previous = 0
        if previous:
            progress('采集期间已阻止自动睡眠，屏幕可以熄灭；主动睡眠或合盖仍可能中断。')
        else:
            progress('未能启用防自动睡眠，请在采集期间保持电脑唤醒。')
    try:
        yield bool(previous)
    finally:
        if previous:
            if not setter(previous | 0x80000000):
                progress('释放防睡眠请求失败；退出进程后 Windows 会自动释放。')


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
            if os.name == "nt":
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise StopRun("已有采集进程运行，请先结束原窗口中的任务。") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
