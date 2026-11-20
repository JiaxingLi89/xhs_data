import os
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapter import live_dependencies
from .errors import SetupError, StopRun


def scheduled_slot(now, config, store):
    local = datetime.fromtimestamp(now, ZoneInfo(config.timezone))
    # launchd may fire once on wake: reject a catch-up outside the first 15 minutes.
    if local.hour not in {10, 16} or local.minute >= 15:
        raise StopRun("outside_scheduled_window")
    store.reserve_slot(local.date().isoformat(), local.hour)


def create_schedule(config_path, config, install=False):
    if config.quota_mode == "rolling":
        raise SetupError("滚动配额模式由研究者手动开启批次，不创建或安装定时任务。")
    directory = config.root / "schedule"
    directory.mkdir(exist_ok=True, mode=0o700)
    # Prefer the prepared environment, never pin a missing Python executable.
    prepared = config.root / ".venv" / "bin" / "python"
    python = str(prepared if prepared.exists() else Path(sys.executable).absolute())
    label = "local.xhs-research." + __import__("hashlib").sha256(str(config.root).encode()).hexdigest()[:8]
    path = directory / (label + ".plist")
    payload = {"Label": label, "ProgramArguments": [python, "-m", "xhs_research", "--config", str(Path(config_path).resolve()), "run", "--scheduled"],
               "WorkingDirectory": str(config.root), "StartCalendarInterval": [{"Hour": 10, "Minute": 0}, {"Hour": 16, "Minute": 0}],
               "RunAtLoad": False, "KeepAlive": False, "ProcessType": "Background", "Umask": 0o077,
               "StandardOutPath": str(directory / "stdout.log"), "StandardErrorPath": str(directory / "stderr.log")}
    path.write_bytes(plistlib.dumps(payload))
    if install:
        if sys.platform != "darwin":
            raise SetupError("自动安装目前支持 macOS launchd。")
        if live_dependencies():
            raise SetupError("先使用实采虚拟环境完成 doctor 检查，再安装定时任务。")
        actual = datetime.now().astimezone().utcoffset()
        expected = datetime.now(ZoneInfo(config.timezone)).utcoffset()
        if actual != expected:
            raise SetupError("系统时区与配置时区不一致，请先统一；launchd使用系统时区。")
        result = subprocess.run(["launchctl", "bootstrap", "gui/%s" % os.getuid(), str(path)], capture_output=True, text=True)
        if result.returncode:
            raise SetupError("launchd安装失败或任务已加载；使用README中的卸载命令检查。")
    return path

