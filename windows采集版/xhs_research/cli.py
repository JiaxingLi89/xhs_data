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
from .runtime import Clock, local_day, process_lock, keep_awake
from .schedule import create_schedule, scheduled_slot
from .store import Store
from .windows_env import environment_issues, browser_path
from .browser_selection import browser_menu
from .promotion import classify_all
from .promotion_review import review_export, review_import, review_menu
from zoneinfo import ZoneInfoNotFoundError


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
    issues = environment_issues(config.browser_channel) + live_dependencies() + verify_snapshot(Path(__file__).resolve().parents[1])
    result = {"core_ready": True, "live_ready": not issues, "python": sys.version.split()[0],
              "stage": config.stage, "keywords": config.keywords, "target_notes": config.target_notes,
              "daily_notes": config.daily_notes, "daily_business_requests": config.daily_requests,
              "interval_seconds": config.request_interval_seconds,
              "browser_channel": config.browser_channel,
              "browser_executable": str(browser_path(config.browser_channel)) if browser_path(config.browser_channel) else None,
              "browser_profile": str(config.browser_profile), "issues": issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 2


def stop_message(reason):
    messages = {
        "daily_request_limit": "当天请求额度已用完；请次日双击 05_run.cmd 手动继续。当天重启不会重置额度。",
        "day_finished": "已到当日运行边界（或下一次请求将跨日）；请次日双击 05_run.cmd 手动继续。",
        "run_window_finished": "已到配置的单次运行时限；可手动启动继续，当天额度保留。",
        "daily_note_limit": "已到自定义每日篇数上限；请次日手动继续，或调整配置。",
        "per_run_note_limit": "已到自定义单次篇数上限；可手动启动继续，当天额度保留。",
        "pilot_complete_review_required": "试采已完成；请人工核对至少10篇，执行 promote 后再手动采集。",
        "pilot_review_required": "正式采集前需完成试采及至少10篇人工核对，再执行 promote。",
        "target_reached": "已达到累计目标篇数；如需更多，请修改 target_notes 后手动启动。",
        "candidates_exhausted": "当前关键词可用候选已处理完，搜索进度已保存；不会自动循环重搜。",
        "network_backoff": "网络退避尚未结束；请等待后手动启动。",
    }
    if reason.startswith("paused:"):
        return "异常暂停（%s）；请检查原因，冷却结束后执行 resume，再手动启动。" % reason[7:]
    return messages.get(reason, "停止原因：%s；进度和当天额度已保留。" % reason)


async def collect(config, store, command, scheduled=False):
    clock = Clock()
    gate = RequestGate(store, config, clock)
    adapter = MediaCrawlerAdapter(config, store, clock)
    session = SessionManager(adapter, gate, store, config, clock, notify)
    engine = Engine(config, store, session, clock)
    store.start_run(clock.now(), command)
    reason = 'completed'
    try:
        if store.get("synthetic_demo"):
            raise SetupError("模拟数据库不能用于实采，请使用独立的 data_dir。")
        reopened = store.prepare_unlimited_comments(config, clock.now())
        identity_jobs = store.prepare_identity_backfill(clock.now())
        if identity_jobs:
            print('已将%d篇旧笔记加入用户 ID 与 IP 属地补采队列；使用独立游标并计入现有额度。' % identity_jobs)
        if reopened:
            print("已将%d篇旧截断评论加入补采队列；从第一页重新翻页并去重。" % reopened)
        status = store.status(local_day(clock.now(), config.timezone), config)
        print("今日受控请求：已用%d / 上限%d，剩余%d。停止后需手动启动。" %
              (status["business_requests_today"], status["daily_request_limit"], status["requests_remaining"]))
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
        reason = str(exc)
        print("任务已保存：" + stop_message(str(exc)))
    except RiskBlocked as exc:
        reason = str(exc)
        store.pause(clock.now(), str(exc), clock.now() + exc.cooldown)
        notify("采集已暂停：" + str(exc) + "。请检查账号，冷却结束后使用 resume。")
    except NetworkFailure:
        reason = 'network_failure'
        store.set("network_until", max(store.get("network_until", 0), clock.now() + config.network_retry_seconds))
        print("网络或浏览器连接中断，已保存进度；不会因此重复触发登录。")
    except ProtocolError:
        reason = 'protocol_error'
        store.pause(clock.now(), "protocol_error")
        print("响应结构与预期不符，已暂停；请检查适配器，未将异常响应计为成功样本。")
    except (KeyboardInterrupt, asyncio.CancelledError):
        reason = 'user_interrupted'
        raise
    except BaseException as exc:
        reason = 'unexpected_error:' + type(exc).__name__
        raise
    finally:
        store.finish_run(clock.now(), reason)
        await session.close()


async def demo(config):
    from .demo import DemoAdapter, FakeClock
    directory = config.root / "demo_output" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory.mkdir(parents=True, mode=0o700)
    config = dataclasses.replace(config, root=directory, keywords=["模拟研究关键词"], stage="pilot",
                                 data_dir="data", export_dir="exports")
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
        print(json.dumps(store.status(local_day(clock.now(), config.timezone), config), ensure_ascii=False, indent=2))
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
    store.set("pilot_review_accepted", True)
    print("已记录人工核对，切换为正式阶段；当天已用额度继续计入。")


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="小红书低频研究采集：本地会话、全局配额、跨日续采")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次低频采集")
    run.add_argument("--scheduled", action="store_true", help="预留参数；Windows 独立版不提供定时运行")
    for name, help_text in [("login", "手动打开登录流程，成功后自动续采"), ("status", "查看任务与配额"),
                            ("resume", "人工检查后解除暂停，不清除未结束的冷却"),
                            ("export", "导出CSV、Excel和采样说明"), ("doctor", "检查环境，不访问小红书"),
                            ("demo", "执行隔离的离线模拟，不访问网络")]:
        sub.add_parser(name, help=help_text)
    sub.add_parser("browser", help="选择 Edge 或 Chrome，保留采集进度")
    sub.add_parser('classify', help='离线初筛已有帖子的商业推广线索')
    sub.add_parser('review-menu', help='商业推广初筛与人工复核菜单')
    review_out = sub.add_parser('review-export', help='导出疑似推广且未复核的帖子')
    review_out.add_argument('--all', action='store_true', help='导出全部已有正文的帖子')
    review_in = sub.add_parser('review-import', help='导入人工复核 CSV')
    review_in.add_argument('path', type=Path)
    schedule = sub.add_parser("schedule", help="本独立版不支持定时任务")
    schedule.add_argument("--install", action="store_true", help="依赖就绪后加载到用户launchd会话")
    promotion = sub.add_parser("promote", help="试采验收后手动切换正式阶段")
    promotion.add_argument("--reviewed-notes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.command == "browser":
            return browser_menu(args.config)
        if args.command == 'review-menu':
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
            problems = environment_issues(config.browser_channel) + live_dependencies() + verify_snapshot(Path(__file__).resolve().parents[1])
            if problems:
                raise SetupError(" ".join(problems))
        if args.command in {"status", "export"}:
            store = Store(config.state_dir)
            try:
                if args.command == "status":
                    print(json.dumps(store.status(local_day(Clock().now(), config.timezone), config), ensure_ascii=False, indent=2))
                else:
                    print(export_data(store, config, Clock().now(), store.get("synthetic_demo", False)))
            finally:
                store.close()
            return 0
        with process_lock(config.state_dir):
            # A browser switch may have completed after the initial config read.
            current = Config.load(args.config)
            if current.state_dir != config.state_dir:
                raise SetupError("配置目录已改变，请重新运行。")
            config = current
            store = Store(config.state_dir, migration_locked=True)
            try:
                if args.command == "resume":
                    store.resume(Clock().now())
                    print("暂停已解除；下次run继续。登录等待状态、采集进度和日配额均保留。")
                elif args.command == "promote":
                    promote(args.config.resolve(), config, store, args.reviewed_notes)
                elif args.command == 'classify':
                    result = classify_all(store, Clock().now())
                    print(json.dumps(result, ensure_ascii=False))
                    return 2 if result['pending'] else 0
                elif args.command == 'review-export':
                    print('复核 CSV：' + str(review_export(store, config, Clock().now(), args.all)))
                elif args.command == 'review-import':
                    print('已更新%d篇人工复核；未删除原帖。' % review_import(store, args.path, Clock().now()))
                else:
                    with keep_awake(notify):
                        asyncio.run(collect(config, store, args.command, getattr(args, "scheduled", False)))
            finally:
                store.close()
        return 0
    except StopRun as exc:
        print("任务未启动：" + stop_message(str(exc)))
        return 2 if args.command in {'browser', 'classify', 'review-export', 'review-import', 'review-menu'} else 0
    except ZoneInfoNotFoundError:
        print("缺少时区数据，请重新运行 01_install.cmd 安装 tzdata。", file=sys.stderr)
        return 2
    except CollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("文件操作失败，请检查权限及目录占用：" + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止，已提交的数据和配额保留。", file=sys.stderr)
        return 130
