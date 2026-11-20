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
from zoneinfo import ZoneInfo

from .adapter import MediaCrawlerAdapter, live_dependencies
from .config import Config
from .control import RequestGate, SessionManager
from .engine import Engine
from .errors import CollectorError, NetworkFailure, ProtocolError, RiskBlocked, SetupError, StopRun
from .export import export_data
from .runtime import Clock, local_day, process_lock
from .schedule import create_schedule, scheduled_slot
from .store import Store
from .promotion import classify_all
from .promotion_review import review_export, review_import, review_menu


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
              "quota_mode": config.quota_mode,
              "continuous_daily": config.continuous_daily,
              "daily_notes": config.daily_notes if config.quota_mode == "daily" else None,
              "daily_business_requests": config.daily_requests if config.quota_mode == "daily" else None,
              "notes_per_run": None if config.continuous_daily else config.notes_per_run,
              "requests_per_run": None if config.continuous_daily else config.requests_per_run,
              "rolling_requests_limit": config.rolling_requests_limit if config.quota_mode == "rolling" else None,
              "interval_seconds": config.request_interval_seconds,
              "browser_profile": str(config.state_dir / "browser-profile"), "issues": issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 2


def quota_report(store, config, now):
    report = store.quota_status(now, config)
    release = report["next_release_at"]
    report["next_release_local"] = (datetime.fromtimestamp(release, ZoneInfo(config.timezone)).isoformat()
                                    if release is not None else None)
    return report


def stop_description(reason):
    return {
        "per_run_note_limit": "本批笔记处理名额已用完",
        "per_run_request_limit": "本批请求额度已用完",
        "rolling_request_limit": "最近24小时请求额度已用完",
        "daily_note_limit": "当天笔记额度已用完",
        "daily_request_limit": "当天请求额度已用完",
        "no_pending_candidates": "没有可继续处理的候选笔记；当前搜索结果已耗尽",
        "pilot_complete_review_required": "试采目标已完成，请导出核对后手动切换正式阶段",
        "target_reached": "总采集目标已完成",
        "run_window_finished": "本次运行时间已结束",
    }.get(reason, reason)


async def collect(config, store, command, scheduled=False):
    clock = Clock()
    gate = RequestGate(store, config, clock)
    adapter = MediaCrawlerAdapter(config, store, clock)
    session = SessionManager(adapter, gate, store, config, clock, notify)
    engine = Engine(config, store, session, clock)
    initial_complete = store.complete_count()
    reason = "本批正常结束"
    print("本批开始（配额模式见 quota_mode，today 字段为自然日统计）：" + json.dumps(quota_report(store, config, clock.now()), ensure_ascii=False))
    try:
        if store.get("synthetic_demo"):
            raise SetupError("模拟数据库不能用于实采，请使用独立的 data_dir。")
        if scheduled and config.quota_mode == "rolling":
            raise StopRun("滚动配额模式仅支持手动开启批次；已有定时任务不会执行采集")
        store.prepare_identity_backfill(clock.now())
        engine.preflight()
        if scheduled:
            scheduled_slot(clock.now(), config, store)
        if command == "login":
            await session.ensure_ready(manual=True)
            await engine.run(already_ready=True)
        else:
            await engine.run()
        reason = "pilot_complete_review_required" if config.stage == "pilot" else "target_reached"
    except StopRun as exc:
        reason = str(exc)
        print("任务已保存：" + stop_description(reason))
    except RiskBlocked as exc:
        reason = "访问受限：" + str(exc)
        store.pause(clock.now(), str(exc), clock.now() + exc.cooldown)
        notify("采集已暂停：" + str(exc) + "。请检查账号，冷却结束后使用 resume。")
    except NetworkFailure:
        reason = "网络或浏览器连接中断"
        store.set("network_until", max(store.get("network_until", 0), clock.now() + config.network_retry_seconds))
        print("网络或浏览器连接中断，已保存进度；不会因此重复触发登录。")
    except ProtocolError as exc:
        reason = "响应结构异常：" + str(exc)
        print("错误诊断：" + json.dumps(exc.diagnostic, ensure_ascii=False))
        store.pause(clock.now(), "protocol_error")
        print("响应结构与预期不符，已暂停；请检查适配器，未将异常响应计为成功样本。")
    except BaseException:
        reason = "运行被中断或发生其他错误"
        raise
    finally:
        try:
            await session.close()
        finally:
            print("本批结束：处理笔记 %d 篇；新增完成 %d 篇；受控请求 %d 次；累计完成 %d 篇；原因：%s。" % (
                len(gate.run_notes), store.complete_count() - initial_complete, gate.run_requests,
                store.complete_count(), stop_description(reason)))
            print("当前配额与异常状态：" + json.dumps(quota_report(store, config, clock.now()), ensure_ascii=False))
            if config.continuous_daily:
                print("持续运行模式会自动等待每日额度；本次已因上述原因结束。审核、登录或异常处理后需手动运行；额度释放不会解除暂停。")
            else:
                print("额度释放时间不代表可以解除暂停；下一批需手动运行，并继续遵守等待与冷却。")


async def demo(config):
    from .demo import DemoAdapter, FakeClock
    directory = config.root / "demo_output" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory.mkdir(parents=True, mode=0o700)
    config = dataclasses.replace(config, root=directory, keywords=["模拟研究关键词"], stage="pilot",
                                 data_dir="data", export_dir="exports", continuous_daily=False)
    clock = FakeClock()
    with process_lock(config.state_dir):
        store = Store(config.state_dir, migration_locked=True)
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
    print("已记录人工核对，切换为正式阶段；历史请求继续计入当前配额，不会清零。")


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
    sub.add_parser('classify', help='离线初筛商业推广线索')
    sub.add_parser('review-menu', help='商业推广人工复核菜单')
    review_out = sub.add_parser('review-export', help='导出推广复核 CSV')
    review_out.add_argument('--all', action='store_true')
    review_in = sub.add_parser('review-import', help='导入人工复核 CSV')
    review_in.add_argument('path', type=Path)
    promotion = sub.add_parser("promote", help="试采验收后手动切换正式阶段")
    promotion.add_argument("--reviewed-notes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.command == "review-menu":
            return review_menu(args.config)
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
                    print(json.dumps(quota_report(store, config, Clock().now()), ensure_ascii=False, indent=2))
                else:
                    print(export_data(store, config, Clock().now(), store.get("synthetic_demo", False)))
            finally:
                store.close()
            return 0
        with process_lock(config.state_dir):
            store = Store(config.state_dir, migration_locked=True)
            try:
                if args.command == 'classify':
                    print(json.dumps(classify_all(store, Clock().now()), ensure_ascii=False))
                elif args.command == 'review-export':
                    print(review_export(store, config, Clock().now(), args.all))
                elif args.command == 'review-import':
                    print('已更新%d篇人工复核。' % review_import(store, args.path, Clock().now()))
                elif args.command == "resume":
                    store.resume(Clock().now())
                    print("暂停已解除；下次run继续。登录等待状态、采集进度和请求历史均保留。")
                elif args.command == "promote":
                    promote(args.config.resolve(), config, store, args.reviewed_notes)
                else:
                    asyncio.run(collect(config, store, args.command, getattr(args, "scheduled", False)))
            finally:
                store.close()
        return 0
    except StopRun as exc:
        print("任务未启动：" + str(exc))
        return 2 if args.command in {"classify", "review-export", "review-import", "review-menu"} else 0
    except CollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止，已提交的数据和配额保留。", file=sys.stderr)
        return 130

