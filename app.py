#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy 签到管理台主程序：Web 管理页 + 多账号定时调度。

两种运行方式，可任选其一，也可以并存（都靠每日状态门控保证幂等）：

  常驻（推荐，容器就是这个）：
    python app.py                  # Web(18321) + 到点自动执行
    python app.py --no-web         # 只跑调度，不开 Web
    python app.py --web-only       # 只开 Web，不自动调度

  定时启动（Windows 计划任务 / cron / docker exec）：
    python app.py --once           # 立刻跑一轮所有账号，然后退出

端口：--port 或环境变量 PORT，默认 18321。
"""
import argparse
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import accounts, runner, webui      # noqa: E402


def main():
    p = argparse.ArgumentParser(description="Buddy 签到管理台")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT") or os.environ.get("BUDDY_PORT") or 18321))
    p.add_argument("--host", default=os.environ.get("BUDDY_HOST") or "0.0.0.0")
    p.add_argument("--once", action="store_true", help="执行一轮后退出（定时启动用）")
    p.add_argument("--no-web", action="store_true", help="不启动 Web")
    p.add_argument("--web-only", action="store_true", help="不启动定时调度")
    args = p.parse_args()

    migrated = accounts.migrate_legacy()
    if migrated:
        print("[init] 已将旧的单账号数据迁移为账号 %s" % migrated)

    if args.once:
        res = runner.run_all()
        if not res:
            print("[once] 没有启用的账号")
            return 0
        all_ok = True
        for aid, r in res.items():
            ok = bool(r.get("ok"))
            all_ok = all_ok and ok
            print("[once] %s %s：%s" % ("OK " if ok else "FAIL", aid,
                                        " | ".join(r.get("lines") or [])))
        return 0 if all_ok else 1

    stop = threading.Event()
    signal_handlers(stop)

    if not args.web_only:
        threading.Thread(target=runner.scheduler_loop, args=(stop, print),
                         daemon=True, name="scheduler").start()

    if args.no_web:
        print("[init] 仅调度模式（无 Web），Ctrl+C 退出")
        stop.wait()
        return 0

    threading.Thread(target=lambda: (stop.wait(), os._exit(0)),
                     daemon=True, name="shutdown").start()
    print("[init] 账号数：%d | 时间点：%s" % (
        len(accounts.list_accounts()),
        ", ".join("%02d:%02d" % t for t in runner.scheduler_times())))
    webui.serve(args.host, args.port)
    return 0


def signal_handlers(stop):
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
