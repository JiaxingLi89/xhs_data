"""Install pinned live dependencies into the project .venv; no system Python changes."""
import argparse
import json
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheels", type=Path, help="从本地wheel目录安装，不访问网络")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        candidates = [shutil.which(name) for name in ["python3.14", "python3.13", "python3.12", "python3.11", "python3.10"]]
        candidates += [str(Path.home() / name / "bin/python") for name in ["miniconda3", "miniforge3", "anaconda3"]]
        for candidate in candidates:
            if not candidate or not Path(candidate).is_file():
                continue
            try:
                version = subprocess.check_output([candidate, "-c", "import sys; print(int(sys.version_info >= (3,10)))"], text=True, timeout=10).strip()
                if version == "1":
                    print("使用已有Python环境创建项目虚拟环境：" + candidate, flush=True)
                    return subprocess.run([candidate, str(Path(__file__).resolve()), *sys.argv[1:]]).returncode
            except (OSError, subprocess.SubprocessError):
                continue
        print("实采依赖需要 Python >= 3.10。请安装 Python 3.12 后运行：python3.12 scripts/setup_live.py", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    command = [str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-cache-dir",
               "--retries", "0", "--timeout", "15", "-r", str(root / "requirements-live.txt")]
    if args.wheels:
        command += ["--no-index", "--find-links", str(args.wheels.resolve())]
    else:
        command += ["--index-url", "https://pypi.org/simple"]
    installed = subprocess.run(command)
    if installed.returncode:
        print("依赖安装未完成，项目虚拟环境已保留。网络可用后重运行本脚本，或使用 --wheels 本地目录。", file=sys.stderr)
        return 2
    # Save a resolved dependency snapshot for later reproduction; never auto-upgrade it.
    freeze = subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True)
    (root / "requirements-resolved.txt").write_text(freeze, encoding="utf-8")
    subprocess.run([str(python), "-m", "xhs_research", "doctor"], cwd=root, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

