"""Switch browser without altering database state or sharing browser profiles."""
import json
import os
from pathlib import Path
import tempfile

from .config import Config
from .errors import SetupError
from .runtime import process_lock
from .windows_env import BROWSER_NAMES, browser_path


def switch_browser(path, channel):
    if channel not in ('chrome', 'msedge'):
        raise SetupError('请选择 Chrome 或 Edge。')
    path = Path(path).resolve()
    config = Config.load(path)
    with process_lock(config.state_dir):
        current = Config.load(path)
        if current.state_dir != config.state_dir:
            raise SetupError('配置目录已改变，请重新运行切换入口。')
        if not browser_path(channel):
            raise SetupError('未找到 %s，请先安装。原配置保持不变。' % BROWSER_NAMES[channel])
        if current.browser_channel == channel:
            print('当前已使用 %s，无需修改。' % BROWSER_NAMES[channel])
            return
        data = json.loads(path.read_text(encoding='utf-8'))
        data['browser_channel'] = channel
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.browser-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        print('已切换为 %s。采集进度、配额和暂停状态保留；首次使用此浏览器可能需要扫码。' % BROWSER_NAMES[channel])
        print('请先运行 02_check.cmd，再运行 04_login.cmd。')


def browser_menu(path):
    config = Config.load(path)
    print('当前浏览器：' + BROWSER_NAMES[config.browser_channel])
    for channel in ('msedge', 'chrome'):
        detected = browser_path(channel)
        print('%s：%s' % (BROWSER_NAMES[channel], detected or '未安装或未在标准位置找到'))
    print('1：Microsoft Edge；2：Google Chrome；0 或直接回车：取消')
    while True:
        try:
            answer = input('请选择：').strip()
        except EOFError:
            answer = ''
        if answer in ('', '0'):
            print('已取消，配置未修改。')
            return 0
        if answer in ('1', '2'):
            switch_browser(path, 'msedge' if answer == '1' else 'chrome')
            return 0
        print('请输入 1、2 或 0。')
