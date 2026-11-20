"""Prepare an isolated Windows Python 3.12 environment."""
import argparse
import glob
import os
from pathlib import Path
import subprocess
import sys
import venv


MIN_VERSION = (3, 10)
TARGET_VERSION = (3, 12)

_PROBE = (
    "import sys,struct;"
    "print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P')))"
)

# 记录本次探测到的解释器版本，仅用于失败时给出可读提示。
_DETECTED = []


def _probe_version(command):
    try:
        result = subprocess.run(command + ["-c", _PROBE], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    fields = (result.stdout or '').split()
    if len(fields) != 2:
        return None
    try:
        major, minor = [int(part) for part in fields[0].split('.')]
        bits = int(fields[1])
    except ValueError:
        return None
    if bits != 8:
        return None
    return (major, minor)


def _candidate_paths():
    """Python 装好但没进 PATH 时，常见安装位置的回退探测。"""
    patterns = []
    for base in (os.environ.get('LOCALAPPDATA'), os.environ.get('ProgramFiles'), os.environ.get('ProgramW6432')):
        if not base:
            continue
        patterns.append(os.path.join(base, 'Programs', 'Python', 'Python3*', 'python.exe'))
        patterns.append(os.path.join(base, 'Python3*', 'python.exe'))
    patterns.append(r'C:\Python3*\python.exe')
    for base in (os.environ.get('USERPROFILE'), os.environ.get('ProgramData'), os.environ.get('LOCALAPPDATA')):
        if not base:
            continue
        for dist in ('anaconda3', 'miniconda3', 'miniforge3', 'mambaforge'):
            patterns.append(os.path.join(base, dist, 'python.exe'))
            patterns.append(os.path.join(base, dist, 'envs', '*', 'python.exe'))

    paths = []
    for pattern in patterns:
        try:
            paths.extend(glob.glob(pattern))
        except OSError:
            continue
    return [Path(item) for item in paths if Path(item).is_file()]


def find_python():
    """返回可用的 64 位 Python 3.12 启动命令；找不到时返回 None。"""
    del _DETECTED[:]
    candidates = [["py", "-3.12"], ["py", "-3"], [sys.executable]]
    candidates += [["python3.12"], ["python3"], ["python"]]
    candidates += [[str(path)] for path in _candidate_paths()]

    best = None
    for command in candidates:
        version = _probe_version(command)
        if version is None:
            continue
        _DETECTED.append('%d.%d' % version)
        if version < MIN_VERSION:
            continue
        if version == TARGET_VERSION:
            return command  # 优先 3.12
        if best is None or version > best[0]:
            best = (version, command)
    # 只有 3.10/3.11：本包的静态检查仍要求 3.12，这里返回 None 并给出提示。
    return None


SOURCES = {
    'tuna': 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple',
    'pypi': 'https://pypi.org/simple',
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--wheels', type=Path)
    source.add_argument('--source', choices=SOURCES)
    parser.add_argument('--menu', action='store_true', help='交互选择安装源')
    parser.add_argument('--selected', action='store_true', help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def choose_source():
    print('安装源：1 清华镜像（默认） / 2 PyPI 官方源 / 0 取消')
    while True:
        answer = input('请选择 [1]：').strip()
        if answer in ('', '1'):
            return 'tuna'
        if answer == '2':
            return 'pypi'
        if answer == '0':
            return None
        print('请输入 1、2 或 0。')


def pip_command(python, root, args):
    command = [str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check',
               'install', '--retries', '0', '--timeout', '30', '-r', str(root / 'requirements-live.txt')]
    if args.wheels:
        return command + ['--no-index', '--find-links', str(args.wheels.resolve())]
    return command + ['--index-url', SOURCES[args.source or 'tuna']]


def install(root, args):
    if args.wheels:
        if not args.wheels.is_dir():
            print('离线依赖目录不存在。')
            return 2
        print('安装来源：本地目录 ' + str(args.wheels.resolve()))
    else:
        if args.menu and args.source is None:
            args.source = choose_source()
            if args.source is None:
                print('已取消安装。')
                return 0
        print('安装来源：' + SOURCES[args.source or 'tuna'])
    environment = root / '.venv'
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / 'Scripts' / 'python.exe'
    if subprocess.call(pip_command(python, root, args), cwd=root):
        print('依赖安装失败，未自动换源或重试。请保留上述错误；联网安装可重新双击 01_install.cmd 选择另一来源，或使用 --source tuna / --source pypi。')
        return 2
    freeze = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], text=True)
    (root / 'requirements-resolved.txt').write_text(freeze, encoding='utf-8')
    print('依赖安装完成，开始环境检查。')
    result = subprocess.call([str(python), '-m', 'xhs_research', 'doctor'], cwd=root)
    if result:
        print('依赖已安装，但环境检查未通过。若是浏览器问题，请运行 08_browser.cmd 选择已安装的 Edge 或 Chrome，再运行 02_check.cmd；无需重复下载依赖。')
    return result


def main(argv=None):
    args = parse_args(argv)
    if os.name != 'nt':
        print('此安装入口仅用于 Windows 11 及以上系统。')
        return 2
    if sys.getwindowsversion().build < 22000:
        print('本包目标为 Windows 11；Windows 10 尚未验证。')
        return 2
    if not args.selected:
        command = find_python()
        if not command:
            if _DETECTED:
                print('检测到的 Python：%s；本包需要 64 位 Python 3.12。' % '、'.join(sorted(set(_DETECTED))))
            print('请先安装 64 位 Python 3.12：https://www.python.org/downloads/windows/')
            print('安装时请勾选 “Add python.exe to PATH” 和 “Python Launcher”，装完重新双击 01_install.cmd。')
            return 2
        return subprocess.call(command + [str(Path(__file__).resolve()), '--selected'] + (sys.argv[1:] if argv is None else argv))
    return install(Path(__file__).resolve().parents[1], args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print('已取消安装。')
        raise SystemExit(130)
    except (OSError, subprocess.SubprocessError) as exc:
        print('安装未完成：%s。请保留错误信息并检查 Python 安装。' % exc)
        raise SystemExit(2)
