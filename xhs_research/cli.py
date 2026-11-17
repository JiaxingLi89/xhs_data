from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .adapter import MediaCrawlerAdapter, live_dependencies
from .config import Config
from .control import RequestGate, SessionManager
from .engine import Engine
from .errors import CollectorError, NetworkFailure, ProtocolError, RiskBlocked, SetupError, StopRun
from .export import export_data
from .runtime import Clock, local_day, process_lock
from .schedule import create_schedule, scheduled_slot
from .store import Store


def notify(message):
    print(message, flush=True)
    if sys.platform == "darwin":
        # argv is used, never interpolate a URL, cookie, or notification into shell code.
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "小红书研究采集"\nend run'
        try:
            subprocess.run(["osascript", "-e", script, message], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def verify_snapshot(root):
    lock_path = root / "upstream.lock.json"
    if not lock_path.is_file():
        return ["缺少 upstream.lock.json。"]
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        issues = []
        for filename, expected in lock["local_sha256"].items():
            path = root / filename
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                issues.append("适配器快照已变化：" + filename)
        return issues
    except (OSError, KeyError, ValueError):
        return ["上游快照记录无效。"]


def doctor(config):
    issues = live_dependencies() + verify_snapshot(Path(__file__).resolve().parents[1])
    if config.browser_channel == "chrome" and sys.platform == "darwin" and not Path("/Applications/Google Chrome.app").exists():
        issues.append("未在 /Applications 找到 Google Chrome。")
    result = {"core_ready": True, "live_ready": not issues, "python": sys.version.split()[0],
              "stage": config.stage, "keywords": config.keywords, "target_notes": config.target_notes,
              "daily_notes": config.daily_notes, "daily_business_requests": config.daily_requests,
              "interval_seconds": config.request_interval_seconds,
              "browser_profile": str(config.state_dir / "browser-profile"), "issues": issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 2


async def collect(config, store, command, scheduled=False):
    clock = Clock()
    gate = RequestGate(store, config, clock)
    adapter = MediaCrawlerAdapter(config, store, clock)
    session = SessionManager(adapter, gate, store, config, clock, notify)
    engine = Engine(config, store, session, clock)
    try:
        if store.get("synthetic_demo"):
            raise SetupError("模拟数据库不能用于实采，请使用独立的 data_dir。")
        if command == "run":
            engine.preflight()
        if scheduled:
            scheduled_slot(clock.now(), config, store)
        if command == "login":
            await session.ensure_ready(manual=True)
            await engine.run(already_ready=True)
        else:
            await engine.run()
    except StopRun as exc:
        print("任务已保存：" + str(exc))
    except RiskBlocked as exc:
        store.pause(clock.now(), str(exc), clock.now() + exc.cooldown)
        notify("采集已暂停：" + str(exc) + "。请检查账号，冷却结束后使用 resume。")
    except NetworkFailure:
        store.set("network_until", max(store.get("network_until", 0), clock.now() + config.network_retry_seconds))
        print("网络或浏览器连接中断，已保存进度；不会因此重复触发登录。")
    except ProtocolError:
        store.pause(clock.now(), "protocol_error")
        print("响应结构与预期不符，已暂停；请检查适配器，未将异常响应计为成功样本。")
    finally:
        await session.close()


async def demo(config):
    from .demo import DemoAdapter, FakeClock
    directory = config.root / "demo_output" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory.mkdir(parents=True, mode=0o700)
    config = dataclasses.replace(config, root=directory, keywords=["模拟研究关键词"], stage="pilot",
                                 data_dir="data", export_dir="exports")
    clock = FakeClock()
    with process_lock(config.state_dir):
        store = Store(config.state_dir)
        store.set("synthetic_demo", True)
        adapter = DemoAdapter(clock)
        gate = RequestGate(store, config, clock)
        session = SessionManager(adapter, gate, store, config, clock)
        try:
            await Engine(config, store, session, clock).run()
        except StopRun as exc:
            print("模拟任务结束：" + str(exc))
        finally:
            await session.close()
        result = export_data(store, config, clock.now(), demo=True)
        print(json.dumps(store.status(local_day(clock.now(), config.timezone)), ensure_ascii=False, indent=2))
        print("模拟数据导出：" + str(result))
        store.close()


def promote(path, config, store, reviewed):
    if store.complete_count() < config.pilot_notes or reviewed < 10:
        raise SetupError("需要完成50篇试采，并填写实际人工核对篇数（至少10篇）。")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(stage="formal", reviewed_notes=reviewed)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    store.event(Clock().now(), "pilot_review_recorded")
    print("已记录人工核对，切换为正式阶段；当天已用额度继续计入。")


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="小红书低频研究采集：本地会话、全局配额、跨日续采")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次低频采集")
    run.add_argument("--scheduled", action="store_true", help="由launchd调用，跳过错过的时段")
    for name, help_text in [("login", "手动打开登录流程，成功后自动续采"), ("status", "查看任务与配额"),
                            ("resume", "人工检查后解除暂停，不清除未结束的冷却"),
                            ("export", "导出CSV、Excel和采样说明"), ("doctor", "检查环境，不访问小红书"),
                            ("demo", "执行隔离的离线模拟，不访问网络")]:
        sub.add_parser(name, help=help_text)
    schedule = sub.add_parser("schedule", help="生成本地launchd配置")
    schedule.add_argument("--install", action="store_true", help="依赖就绪后加载到用户launchd会话")
    promotion = sub.add_parser("promote", help="试采验收后手动切换正式阶段")
    promotion.add_argument("--reviewed-notes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "demo":
            asyncio.run(demo(config))
            return 0
        if args.command == "schedule":
            print(create_schedule(args.config, config, args.install))
            return 0
        if args.command in {"run", "login"}:
            problems = live_dependencies() + verify_snapshot(Path(__file__).resolve().parents[1])
            if problems:
                raise SetupError(" ".join(problems))
        if args.command in {"status", "export"}:
            store = Store(config.state_dir)
            try:
                if args.command == "status":
                    print(json.dumps(store.status(local_day(Clock().now(), config.timezone)), ensure_ascii=False, indent=2))
                else:
                    print(export_data(store, config, Clock().now(), store.get("synthetic_demo", False)))
            finally:
                store.close()
            return 0
        with process_lock(config.state_dir):
            store = Store(config.state_dir)
            try:
                if args.command == "resume":
                    store.resume(Clock().now())
                    print("暂停已解除；下次run继续。登录等待状态、采集进度和日配额均保留。")
                elif args.command == "promote":
                    promote(args.config.resolve(), config, store, args.reviewed_notes)
                else:
                    asyncio.run(collect(config, store, args.command, getattr(args, "scheduled", False)))
            finally:
                store.close()
        return 0
    except StopRun as exc:
        print("任务未启动：" + str(exc))
        return 0
    except CollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止，已提交的数据和配额保留。", file=sys.stderr)
        return 130

