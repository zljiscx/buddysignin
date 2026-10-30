FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PORT=18321

# ca-certificates 用于 HTTPS 请求；时区设为上海，签到时间据此计算
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 无第三方依赖，仅标准库；配置/日志/token 都在 data/ 卷内
COPY app.py buddy_checkin.py buddy_auth.py ./
COPY core ./core
COPY web ./web
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data

EXPOSE 18321

# 无参数 = Web 管理台 + 定时调度；传参数透传执行（如 python buddy_auth.py）
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
