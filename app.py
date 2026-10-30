#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy 签到管理台主程序：Web 管理页 + 多账号定时调度。

    python app.py                  # Web(18321) + 调度，常驻
    python app.py --once           # 立即执行一轮所有账号后退出（Windows 计划任务用）
    python app.py --no-web         # 只跑调度，不开 Web
    python app.py --web-only       # 只开 Web，不自动调度
    python app.py --port 9000      # 指定端口
"""
import argparse
import os
import signal
import threading

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path=None):
    """读取项目根目录 .env（不覆盖已有环境变量），让本地运行与 docker compose 行为一致。"""
    path = path or os.path.join(HERE, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_dotenv()

from core import accounts, runner, webui      # noqa: E402  (.env 需先加载)


def main():
    p = argparse.ArgumentParser(description="Buddy 签到管理台")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 18321))
    p.add_argument("--host", default=os.environ.get("BUDDY_HOST") or "0.0.0.0")
    p.add_argument("--once", action="store_true", help="执行一轮后退出")
    p.add_argument("--no-web", action="store_true", help="不启动 Web")
    p.add_argument("--web-only", action="store_true", help="不启动定时调度")
    args = p.parse_args()

    migrated = accounts.migrate_legacy()
    if migrated:
        print("[init] 已将旧的单账号数据迁移为账号 %s" % migrated)

    if args.once:
        res = runner.run_all()
        for aid, r in res.items():
            print("%s: %s" % (aid, " | ".join(r.get("lines") or [])))
        return

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if not args.web_only:
        threading.Thread(target=runner.scheduler_loop, args=(stop, print),
                         daemon=True, name="scheduler").start()

    if args.no_web:
        print("[init] 仅调度模式，Ctrl+C 退出")
        stop.wait()
        return

    threading.Thread(target=lambda: (stop.wait(), os._exit(0)),
                     daemon=True, name="shutdown").start()
    print("[init] 账号数：%d" % len(accounts.list_accounts()))
    webui.serve(args.host, args.port)


if __name__ == "__main__":
    main()
