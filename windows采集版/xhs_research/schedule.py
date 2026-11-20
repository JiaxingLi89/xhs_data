"""Scheduling is intentionally disabled in the manual Windows package."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .errors import SetupError, StopRun

def scheduled_slot(now, config, store):
    local = datetime.fromtimestamp(now, ZoneInfo(config.timezone))
    # launchd may fire once on wake: reject a catch-up outside the first 15 minutes.
    if local.hour not in {10, 16} or local.minute >= 15:
        raise StopRun("outside_scheduled_window")
    store.reserve_slot(local.date().isoformat(), local.hour)


def create_schedule(config_path, config, install=False):
    raise SetupError("Windows 独立版仅支持手动运行，不提供定时任务。")
