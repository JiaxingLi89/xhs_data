"""Read-only Windows environment diagnostics."""
import os
from pathlib import Path
import struct
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BROWSER_NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "chromium": "Chromium"}


def browser_path(channel):
    relative = {"chrome": "Google/Chrome/Application/chrome.exe",
                "msedge": "Microsoft/Edge/Application/msedge.exe"}.get(channel)
    if relative is None:
        return None
    for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(key)
        if base:
            candidate = Path(base) / relative
            if candidate.is_file():
                return candidate
    return None


def chrome_path():
    return browser_path("chrome")


def environment_issues(channel='chrome'):
    issues = []
    if sys.platform != 'win32':
        issues.append('此独立版的实采目标为 Windows 11；当前并非 Windows。')
    elif sys.getwindowsversion().build < 22000:
        issues.append('Windows 10 尚未验证，本包要求 Windows 11 及以上。')
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        issues.append('请使用 64 位 Python 3.12 安装项目环境。')
    try:
        ZoneInfo('Asia/Shanghai')
    except ZoneInfoNotFoundError:
        issues.append('缺少时区数据，请重新运行安装入口安装 tzdata。')
    if channel in ('chrome', 'msedge') and not browser_path(channel):
        issues.append('未在标准位置找到 %s，请安装或运行 08_browser.cmd 切换浏览器。' % BROWSER_NAMES[channel])
    return issues
