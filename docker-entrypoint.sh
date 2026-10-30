#!/bin/sh
# 无参数：Web 管理台 + 多账号定时调度（单进程常驻）。
# 传参数时透传执行，例如 docker compose run --rm buddy python buddy_auth.py
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "[entrypoint] Buddy 签到管理台启动（Web + 调度，单进程）"
exec /usr/local/bin/python3 /app/app.py
