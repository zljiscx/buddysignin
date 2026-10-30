#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy 加油站每日签到。默认按环境自判：Windows 单次签到（配合计划任务）；Docker/NAS 常驻守护、到点签到。--once/--daemon 可显式覆盖。配置 data/buddy_config.json，日志/token 在 data/。"""

import sys
import json
import os
import base64
import time
import uuid
import datetime
import urllib.request
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(HERE, "data", "buddy_config.json")
CHECKIN_STATE_PATH = os.path.join(HERE, "data", "checkin_state.json")  # 当日签到成功记录
WEBHOOK_SEND = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

AUTH_REL_PATH = os.path.join(
    "AppData", "Local", "CodeBuddyExtension",
    "Data", "Public", "auth", "workbuddy-desktop.info",
)

# 对话（连续登录信号）：默认开启，每天一次真实提问
DEFAULT_CHAT = {
    "enabled": True,
    "model": "hy3",
    "prompt": "只回复一个好字",
    "timeout": 90,
}

MODEL_NAMES = {"hy3": "Hy3", "fast-model": "Fast"}

DEFAULT_CONFIG = {
    "api_bases": [
        "https://copilot.tencent.com",
        "https://www.codebuddy.cn",
    ],
    "webhook": "",
    "webhook_key": "",
    "log_file": "data/Log.txt",
    "timeout": 20,
    "follow_redirects": False,
    # 鉴权模式：auto（自动探测）| desktop（读本机登录态文件）| token（复用桌面端已签发 token，NAS 用）
    "auth_mode": "auto",
    "token_store": "data/token_store.json",
    # 签到时间（守护模式下生效），格式 "HH:MM"，可配多个互为兜底；默认早 7 点
    "times": ["08:10", "12:40", "20:30"],
    # 对话（连续登录记账信号）：enabled=false 可关闭
    "chat": dict(DEFAULT_CHAT),
    # 成长计划连续登录奖励（7/14/28 天档）自动领取：enabled=false 可关闭
    "growth": {"enabled": True},
    # 派猫猫旅行：每日派出 + 回来自动领取；location_id 留空则随机选地点
    "travel": {"enabled": True, "location_id": 0},
    # domain / username / uid 等都从登录态文件经 token_store.json 获取，不在此配置。
}

ENDPOINT_CHECKIN = "/v2/billing/meter/daily-checkin"
ENDPOINT_STATUS = "/v2/billing/meter/checkin-activity-status"
ENDPOINT_REFRESH = "/v2/plugin/auth/token/refresh"   # 插件网关续期（网关代持 client_secret，无需密钥）

ENDPOINT_CONV = "/v2/as/conversations/"
ENDPOINT_SESSION = "/console/as/conversations/%s/session"
ENDPOINT_REPORT = "/v2/report"
ENDPOINT_CHAT = "/v2/chat/completions"
CLOUD_STATE_PATH = os.path.join(HERE, "data", "cloud_state.json")  # 对话状态：会话复用 + 当日成功记录

ENDPOINT_GROWTH = "/v2/activity/growth"
ENDPOINT_TRAVEL = ENDPOINT_GROWTH + "/buddy/travel"
# 成长计划连登奖励三档：(tier 标识, /redeem/summary 状态字段前缀, 展示名, 解锁天数)
# tier 必须传档位标识字符串，传天数或档位名会被判 unknown tier。
GROWTH_TIERS = (
    ("7d", "starter", "入门", 7),
    ("14d", "advanced", "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)


# 配置加载
def resolve_auth_file(cfg):
    """登录态文件探测优先级：BUDDY_AUTH_FILE > 配置 auth_file > 用户主目录固定路径。
    返回第一个存在的路径；都不存在时返回最后候选（供报错使用）。"""
    candidates = []
    env = os.environ.get("BUDDY_AUTH_FILE")
    if env:
        candidates.append(env)
    if cfg.get("auth_file"):
        candidates.append(cfg["auth_file"])
    candidates.append(os.path.join(os.path.expanduser("~"), AUTH_REL_PATH))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[-1]


def load_config(data_dir=None, overrides=None):
    """加载配置。data_dir 指定账号数据目录（多账号时每个账号一个目录）。
    账号目录优先读自己的 buddy_config.json，缺失项回退到全局配置与默认值。"""
    data_dir = os.path.abspath(data_dir) if data_dir else DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    sources = []
    if os.path.abspath(data_dir) != os.path.abspath(DATA_DIR):
        sources.append(os.path.join(data_dir, "buddy_config.json"))
    sources.append(CONFIG_PATH)
    for src in sources:
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    cfg[k] = v
        except Exception as e:  # noqa
            print("[警告] 读取 %s 失败，使用默认配置：%s" % (src, e))
    if not os.path.exists(CONFIG_PATH):
        print("[提示] 未找到 %s，使用默认配置。" % CONFIG_PATH)
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v

    cfg["data_dir"] = data_dir
    # 多账号一律用各自目录里的 token，避免 auto 模式误读本机桌面登录态
    if (os.path.abspath(data_dir) != os.path.abspath(DATA_DIR)
            and (cfg.get("auth_mode") or "auto").lower() == "auto"):
        cfg["auth_mode"] = "token"

    # 环境变量覆盖（不含 auth_file，见 resolve_auth_file）
    if os.environ.get("BUDDY_API_BASES"):
        cfg["api_bases"] = [b.strip() for b in os.environ["BUDDY_API_BASES"].split(",") if b.strip()]
    if os.environ.get("BUDDY_LOG_FILE"):
        cfg["log_file"] = os.environ["BUDDY_LOG_FILE"]
    if os.environ.get("BUDDY_TIMEOUT"):
        try:
            cfg["timeout"] = int(os.environ["BUDDY_TIMEOUT"])
        except ValueError:
            pass
    if os.environ.get("BUDDY_FOLLOW_REDIRECTS"):
        cfg["follow_redirects"] = os.environ["BUDDY_FOLLOW_REDIRECTS"].lower() in ("1", "true", "yes")
    if os.environ.get("BUDDY_AUTH_MODE"):
        cfg["auth_mode"] = os.environ["BUDDY_AUTH_MODE"].lower()
    if os.environ.get("BUDDY_TOKEN_STORE"):
        cfg["token_store"] = os.environ["BUDDY_TOKEN_STORE"]
    if os.environ.get("BUDDY_DOMAIN"):
        cfg["domain"] = os.environ["BUDDY_DOMAIN"]
    if os.environ.get("BUDDY_TIMES"):
        cfg["times"] = [t.strip() for t in os.environ["BUDDY_TIMES"].split(",") if t.strip()]

    # 全局设置（Web 管理台「通知设置」写入 data/settings.json）：优先级最高，留空则回退下面的配置项
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), "r", encoding="utf-8") as f:
            st = json.load(f) or {}
        if isinstance(st, dict) and (st.get("webhook") or "").strip():
            hook = st["webhook"].strip()
            # 管理台允许只填机器人 key，这里统一补成完整地址
            cfg["webhook"] = hook if "://" in hook else WEBHOOK_SEND + hook
            cfg["webhook_key"] = ""
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa
        print("[警告] 读取 settings.json 失败，忽略：%s" % e)

    if os.path.abspath(data_dir) != os.path.abspath(DATA_DIR):
        # 多账号：日志与 token 一律落在账号自己的目录，互不干扰
        cfg["log_file"] = os.path.join(data_dir, "Log.txt")
        if cfg.get("token_store") in (None, "", "data/token_store.json"):
            cfg["token_store"] = os.path.join(data_dir, "token_store.json")
    elif not os.path.isabs(cfg["log_file"]):
        cfg["log_file"] = os.path.join(HERE, cfg["log_file"])

    cfg["auth_file"] = resolve_auth_file(cfg)

    # webhook 解析：webhook / webhook_key 二选一；兼容把完整地址误填到 key 的情况
    raw = (cfg.get("webhook") or "").strip()
    key = (cfg.get("webhook_key") or "").strip()
    if "此处填写" in raw or "此处填写" in key:
        print("[警告] 检测到 webhook/webhook_key 仍是模板占位符，已忽略（请修改 buddy_config.json 填入真实值）。")
        raw = ""
        key = ""
    if raw:
        cfg["_webhook_url"] = raw
    elif key:
        if "://" in key:
            print("[提示] webhook_key 看起来是完整地址，已按完整地址处理（建议改填 webhook 字段或仅填 key）。")
            cfg["_webhook_url"] = key
        else:
            cfg["_webhook_url"] = WEBHOOK_SEND + key
    else:
        cfg["_webhook_url"] = ""
    return cfg


def log(cfg, msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line)
    try:
        with open(cfg["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# HTTP 工具
def make_opener(follow_redirects):
    if follow_redirects:
        return urllib.request.build_opener()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # 不跟随重定向，直接以 3xx 抛出交调用方判定
            raise urllib.error.HTTPError(newurl, code, msg, headers, fp)

    return urllib.request.build_opener(NoRedirect())


def http_post(url, token, body=b"{}", timeout=20, follow_redirects=False, user_id="", domain="", extra_headers=None):
    """返回 (status, body)；网络异常时 status=None。"""
    req = urllib.request.Request(url, data=body, method="POST")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if user_id:
        req.add_header("X-User-Id", user_id)
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-Domain", domain or "www.workbuddy.cn")
    req.add_header("User-Agent", "WorkBuddy/5.2.3")
    opener = make_opener(follow_redirects)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa
        return None, str(e)


def http_get(url, token, user_id="", domain="", timeout=20):
    """返回 (status, body)；网络异常时 status=None。"""
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if user_id:
        req.add_header("X-User-Id", user_id)
    req.add_header("X-Domain", domain or "www.workbuddy.cn")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy/5.2.3")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read(500).decode("utf-8", "replace") if e.fp else "")
    except Exception as e:  # noqa
        return None, str(e)[:200]


def call_endpoint(cfg, path, token, user_id="", domain="", extra_headers=None, body=b"{}"):
    """按 api_bases 顺序尝试；仅网络异常（status=None）才 fallback 到下一域名。"""
    last_err = None
    for base in cfg["api_bases"]:
        url = base.rstrip("/") + path
        if isinstance(body, str):
            body = body.encode("utf-8")
        st, rbody = http_post(url, token, body, cfg["timeout"], cfg["follow_redirects"], user_id, domain, extra_headers)
        if st is not None:
            return st, rbody, base
        last_err = rbody
        log(cfg, "WARN %s 不可达：%s" % (base, rbody))
    return None, last_err, None


# 业务
def _jwt_payload(token):
    """仅解析 JWT payload 段（不校验签名）；失败返回 {}。"""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def _jwt_exp(token):
    """从 JWT payload 解析 exp（秒，Unix 时间戳）；失败返回 None。"""
    return _jwt_payload(token).get("exp")


def _jwt_sub(token):
    """从 JWT payload 解析 sub（用户标识，须与 X-User-Id 一致）；失败返回空串。"""
    return _jwt_payload(token).get("sub") or ""


# ---- token 模式（NAS / 无桌面端）：复用桌面端已签发 token ----
def _token_store_path(cfg):
    p = cfg.get("token_store") or "token_store.json"
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def _load_token_store(cfg):
    p = _token_store_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_token_store(cfg, data):
    p = _token_store_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[警告] 写入 token 存储失败：%s" % e)


# ---- 当日签到成功记录：避免重复签到 / 重复推送 ----
def _state_path(cfg, name):
    return os.path.join(cfg.get("data_dir") or DATA_DIR, name)


def load_checkin_state(cfg):
    try:
        p = _state_path(cfg, "checkin_state.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_checkin_state(cfg, state):
    try:
        p = _state_path(cfg, "checkin_state.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[警告] 写入签到记录失败：%s" % e)


def _resolve_auth_mode(cfg):
    mode = (cfg.get("auth_mode") or "auto").lower()
    if mode in ("refresh", "token", "static"):
        return "token"
    if mode == "auto":
        # 桌面登录态文件存在 → desktop；否则 → token（直接复用桌面端 token）
        if os.path.exists(cfg.get("auth_file") or ""):
            return "desktop"
        return "token"
    return mode


def _cred_from_token(at, cfg, store=None):
    """token 模式凭据：优先用 token_store 中由登录态文件带出的 uid/昵称/domain（与桌面端一致），
    缺失时再回退从 JWT 推导；domain 最终回退到默认值。"""
    store = store or {}
    uid = store.get("uid") or _jwt_sub(at)
    nickname = store.get("username") or _jwt_payload(at).get("nickname") or _jwt_payload(at).get("name") or ""
    exp_ms = store.get("expires_at") or (_jwt_exp(at) or 0) * 1000
    domain = store.get("domain") or cfg.get("domain") or "www.workbuddy.cn"
    return at, exp_ms, nickname, "", uid, domain


def do_refresh(cfg, store, token, uid, domain):
    """调用插件网关续期。网关服务端代持 client_secret，只需 refresh_token 即可换取新 token。
    成功返回响应 data 字典，失败返回 None（不抛异常）。"""
    rt = store.get("refresh_token")
    if not rt:
        log(cfg, "REFRESH SKIP token_store 无 refresh_token，无法续期")
        return None
    extra = {
        "X-Refresh-Token": rt,
        "X-Auth-Refresh-Source": "plugin",
        "X-Product": "SaaS",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        st, body, _ = call_endpoint(cfg, ENDPOINT_REFRESH, token, uid, domain, extra)
        if st is None:
            log(cfg, "REFRESH FAIL 网络不可达：%s" % body)
            return None
        resp = json.loads(body)
        if st == 200 and resp.get("code") == 0:
            data = resp.get("data") or {}
            if data.get("accessToken"):
                return data
        log(cfg, "REFRESH FAIL code=%s msg=%s body=%s" % (resp.get("code"), resp.get("msg"), body[:200]))
    except Exception as e:  # noqa
        log(cfg, "REFRESH ERR %s" % e)
    return None


def maybe_refresh(cfg, cred):
    """token 模式下每日首次调用前续期（先续期再签到）。desktop 模式由客户端自行维护，不续期。
    成功则回存新 token/refresh_token 到 token_store 并返回新凭据；否则原样返回。"""
    if _resolve_auth_mode(cfg) != "token":
        return cred, ""
    store = _load_token_store(cfg)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if store.get("last_refresh_date") == today:
        log(cfg, "REFRESH SKIP 今日(%s)已续期" % today)
        return cred, ""

    data = do_refresh(cfg, store, cred[0], cred[4], cred[5])
    if not data:
        return cred, "⚠️ 自动续期失败（本次沿用旧 token）"

    now_ms = int(time.time() * 1000)
    store["access_token"] = data["accessToken"]
    if data.get("refreshToken"):          # refresh token 会轮换，必须回存否则链条断裂
        store["refresh_token"] = data["refreshToken"]
    if data.get("expiresIn"):
        store["expires_at"] = now_ms + int(data["expiresIn"]) * 1000
    if data.get("refreshExpiresIn"):
        store["refresh_expires_at"] = now_ms + int(data["refreshExpiresIn"]) * 1000
    if data.get("sessionState"):
        store["session_state"] = data["sessionState"]
    if data.get("domain"):
        store["domain"] = data["domain"]
    store["last_refresh_date"] = today
    store["last_refresh_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_token_store(cfg, store)
    exp_str = time.strftime("%Y-%m-%d", time.localtime(store.get("expires_at", 0) / 1000)) if store.get("expires_at") else "未知"
    log(cfg, "REFRESH OK 新 token 有效期至 %s" % exp_str)
    return _cred_from_token(store["access_token"], cfg, store), "🔑 已自动续期（有效期至 %s）" % exp_str


def load_credentials(cfg):
    """按 auth_mode 获取 (token, expires_at_ms, nickname, uin, uid, domain)。
    desktop 模式读本机 WorkBuddy 登录态文件；token 模式复用桌面端 token，并在签到前自动续期。"""
    mode = _resolve_auth_mode(cfg)
    if mode == "desktop":
        return load_token(cfg)
    # token 模式：直接复用桌面端已签发的 access_token
    store = _load_token_store(cfg)
    at = store.get("access_token")
    if at:
        return _cred_from_token(at, cfg, store)
    raise RuntimeError(
        "未找到可用 token。请在桌面端运行 buddy_auth.py 导出，并将 token 传入 NAS"
        "（写入 data/token_store.json，或用 BUDDY_TOKEN_BLOB 导入）。"
    )


def load_token(cfg):
    with open(cfg["auth_file"], encoding="utf-8") as f:
        d = json.load(f)
    auth = d.get("auth", {})
    account = d.get("account", {})
    token = auth.get("accessToken")
    # 新版桌面端把 accessToken 加密成 $wbEncrypted 信封（密钥在客户端进程内，本地解不开）。
    # 回退用 buddy_auth.py 导出的明文 token_store，对 WorkBuddy 版本无感。
    if isinstance(token, dict):
        store = _load_token_store(cfg)
        at = store.get("access_token")
        if isinstance(at, str) and at.count(".") == 2:
            return _cred_from_token(at, cfg, store)
        raise RuntimeError(
            "桌面端登录态是新版加密格式，且无可用明文 token_store。"
            "请先运行 buddy_auth.py 导出明文 token 到 data/token_store.json。")
    # 过期时间：优先用文件 expiresAt（毫秒），否则从 JWT exp 推导（秒→毫秒）
    exp = auth.get("expiresAt")
    if not exp and token:
        jwt_exp = _jwt_exp(token)
        if jwt_exp:
            exp = jwt_exp * 1000
    # X-User-Id 须与 Bearer token 的 sub 一致，直接由 token 解析，避免依赖 account.uid
    uid = _jwt_sub(token) if token else ""
    return (
        token,
        exp,
        account.get("nickname", ""),
        account.get("uin", ""),
        uid,
        auth.get("domain", ""),
    )


# ---- 失败通知去重（同一类失败每天只推一次，避免守护进程反复刷屏） ----
_notify_state = {}


def notify_once_per_day(cfg, msg, key):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    k = (today, cfg.get("data_dir") or HERE, key)
    if _notify_state.get(k):
        log(cfg, "SKIP 今日已通知同类失败，跳过推送：%s" % key)
        return
    _notify_state[k] = True
    push_wecom(cfg, msg)


def push_wecom(cfg, content):
    url = cfg.get("_webhook_url")
    if not url:
        log(cfg, "SKIP 未配置 webhook，跳过推送")
        return None, "no webhook"
    payload = json.dumps(
        {"msgtype": "text", "text": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    status, body = None, ""
    try:
        status, body = http_post(url, None, payload, cfg["timeout"], True)
    except Exception as e:      # 地址非法等异常不能让签到流程崩掉
        log(cfg, "WECOM FAIL 异常：%s" % e)
        return None, str(e)
    # status=None 表示网络异常（body 为异常信息）
    if status is None:
        log(cfg, "WECOM FAIL 网络异常：%s" % body)
        return status, body
    # 企业微信即便 key 失效也返回 HTTP 200，需检查响应体 errcode 判断真正成败
    try:
        resp = json.loads(body)
        errcode = resp.get("errcode")
        if errcode not in (0, None):
            log(cfg, "WECOM FAIL errcode=%s errmsg=%s" % (errcode, resp.get("errmsg")))
    except Exception:
        pass
    return status, body


def _pick(d, *keys):
    """按顺序返回第一个非 None 的字段值，兼容不同命名风格。"""
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return None


def fetch_status(cfg, token, uid, domain):
    """查询签到状态；成功返回 data 字典，失败返回 {}（best-effort，不抛异常）。"""
    try:
        st, sb, _ = call_endpoint(cfg, ENDPOINT_STATUS, token, uid, domain)
        if st == 200:
            return json.loads(sb).get("data", {}) or {}
        if st is None:
            log(cfg, "status 所有域名不可达：%s" % sb)
        else:
            log(cfg, "status 非预期状态码 %s：%s" % (st, sb[:200]))
    except Exception as e:  # noqa
        log(cfg, "status err: %s" % e)
    return {}


def append_status_summary(lines, status_info):
    """从 checkin-status 补充累计/连续/今日积分，使成功与幂等结果都展示完整签到信息。"""
    if not status_info:
        return
    today = _pick(status_info, "today_credit", "todayCredit")
    streak = _pick(status_info, "streak_days", "streakDays", "continuous_days", "continuousDays")
    total = _pick(status_info, "total_credits", "totalCredits")
    if today is not None:
        lines.append("今日积分：%s" % today)
    if streak is not None:
        lines.append("连续签到：%s 天" % streak)
    if total is not None:
        lines.append("累计积分：%s" % total)


def parse_times(cfg):
    """解析签到时间列表，返回排序去重后的 [(h, m), ...]；解析失败项忽略，空则回退 07:00。"""
    raw = cfg.get("times") or ["07:00"]
    out = []
    for t in raw:
        try:
            hh, mm = str(t).split(":")
            h, m = int(hh), int(mm)
            assert 0 <= h < 24 and 0 <= m < 60
            out.append((h, m))
        except Exception:
            log(cfg, "WARN 无法解析签到时间 %r，已忽略" % t)
    out = sorted(set(out))
    if not out:
        out = [(7, 0)]
    return out


def do_claim(cfg, token, uid, domain, max_retry=3, retry_gap=30):
    """执行一次签到调用，网络异常时按 max_retry/retry_gap 重试（接口幂等，重试安全）。
    返回 (claimed, result_lines, checkin_data)。"""
    result_lines = []
    claimed = False
    checkin_data = {}
    last_err = None
    for attempt in range(1, max_retry + 1):
        try:
            st, body, _ = call_endpoint(cfg, ENDPOINT_CHECKIN, token, uid, domain)
            if st is None:
                last_err = body
                log(cfg, "claim 第 %d/%d 次网络不可达：%s" % (attempt, max_retry, body))
                time.sleep(retry_gap)
                continue
            resp = json.loads(body)
            code = resp.get("code")
            msg = resp.get("msg", "")
            checkin_data = resp.get("data") or {}
            if code == 0:
                credit = _pick(checkin_data, "credit", "today_credit", "daily_credit")
                result_lines.append("✅ 签到成功")
                if credit is not None:
                    result_lines.append("本次获得积分：%s" % credit)
                claimed = True
            elif code == 10001 or "已签到" in msg:
                result_lines.append("✅ 今日已签到")
                claimed = True
            else:
                result_lines.append("❌ 签到异常 code=%s msg=%s" % (code, msg))
            return claimed, result_lines, checkin_data
        except Exception as e:
            last_err = str(e)
            log(cfg, "claim 第 %d/%d 次请求失败：%s" % (attempt, max_retry, e))
            time.sleep(retry_gap)
    result_lines.append("❌ 重试 %d 次仍失败：%s" % (max_retry, last_err))
    return claimed, result_lines, checkin_data


# ---- 对话（连续登录的真正记账信号）----
def load_chat_cfg(cfg):
    """chat 配置：默认 + 用户配置覆盖（空值不覆盖）。"""
    c = dict(DEFAULT_CHAT)
    c.update({k: v for k, v in (cfg.get("chat") or {}).items() if v not in (None, "")})
    return c


def load_cloud_state(cfg):
    """对话状态：conversationId（会话复用）+ 稳定设备标识 + 当日对话成功记录。"""
    try:
        with open(_state_path(cfg, "cloud_state.json"), encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    changed = False
    if not s.get("machineId"):
        s["machineId"] = str(uuid.uuid4())
        changed = True
    if not s.get("qimei36"):
        s["qimei36"] = uuid.uuid4().hex + "1a60f"
        changed = True
    if changed:
        save_cloud_state(cfg, s)
    return s


def save_cloud_state(cfg, state):
    try:
        p = _state_path(cfg, "cloud_state.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[警告] 写入对话状态失败：%s" % e)


def chat_done_today(cfg):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return load_cloud_state(cfg).get("last_chat_date") == today


def create_conversation(cfg, token, uid, domain):
    """在服务端创建真实会话，返回 (conversation_id, session_token, acp_link)。"""
    for base in cfg["api_bases"]:
        st, rbody = http_post(base.rstrip("/") + ENDPOINT_CONV, token, b"{}",
                              cfg["timeout"], cfg["follow_redirects"], uid, domain, None)
        if st is None:
            continue
        try:
            d = json.loads(rbody).get("data") or {}
            sess = d.get("session") or {}
            if d.get("id") and sess.get("link") and sess.get("token"):
                return d["id"], sess["token"], sess["link"]
        except Exception:
            pass
    return None, None, None


def resolve_conversation(cfg, token, uid, domain, state):
    """优先复用已保存的会话（避免每天新建一堆），拿不到才创建。返回 (cid, tok, link, reused)。"""
    old = state.get("conversationId")
    if old:
        for base in cfg["api_bases"]:
            st, raw = http_get(base.rstrip("/") + ENDPOINT_SESSION % old,
                               token, uid, domain, cfg["timeout"])
            if st != 200:
                continue
            try:
                d = json.loads(raw).get("data") or {}
                if d.get("token") and d.get("link"):
                    return old, d["token"], d["link"], True
            except Exception:
                pass
    cid, stok, link = create_conversation(cfg, token, uid, domain)
    if cid:
        state["conversationId"] = cid
        save_cloud_state(cfg, state)
    return cid, stok, link, False


def acp_prompt(link, session_token, conv_id, prompt, uid, domain, timeout):
    """ACP 发一条真实提问：bootstrap 取连接 id，再 session/prompt。返回 (ok, raw_sse)。"""
    import http.client
    import ssl
    from urllib.parse import urlparse

    u = urlparse(link)
    host, base_path = u.hostname, (u.path or "/acp")
    ctx = ssl.create_default_context()
    boot = json.dumps({
        "initialize": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"_meta": {"codebuddy.ai": {"cwd": "/workspace"}},
                                   "fs": {"readTextFile": False, "writeTextFile": False}}}},
        "sessionLoad": {"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {
            "sessionId": conv_id, "cwd": "/workspace", "mcpServers": [],
            "_meta": {"codebuddy.ai": {"round": 3}}}},
    }).encode()
    hdr = {"Authorization": "Bearer " + session_token, "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream", "X-User-Id": uid,
           "X-Domain": domain or "www.workbuddy.cn"}

    c1 = http.client.HTTPSConnection(host, 443, context=ctx, timeout=timeout)
    c1.request("POST", base_path + "/bootstrap", body=boot,
               headers=dict(hdr, **{"Content-Length": str(len(boot))}))
    r1 = c1.getresponse()
    conn_id = r1.getheader("Acp-Connection-Id")
    if not conn_id:
        c1.close()
        return False, "bootstrap 未返回 Acp-Connection-Id"

    p = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                    "params": {"sessionId": conv_id,
                               "prompt": [{"type": "text", "text": prompt}]}}).encode()
    c2 = http.client.HTTPSConnection(host, 443, context=ctx, timeout=timeout)
    c2.request("POST", base_path, body=p,
               headers=dict(hdr, **{"Content-Length": str(len(p)),
                                    "Acp-Connection-Id": conn_id}))
    r2 = c2.getresponse()
    if r2.status != 200:
        err = r2.read(300).decode("utf-8", "replace")
        c1.close()
        c2.close()
        return False, "HTTP %s %s" % (r2.status, err[:120])
    buf = b""
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            d = r2.read1(1024)
            if not d:
                break
            buf += d
            if b"stopReason" in buf or len(buf) > 30000:
                break
    except Exception:
        pass
    c1.close()
    c2.close()
    return True, buf.decode("utf-8", "replace")


def extract_acp_text(raw):
    """从 ACP 的 SSE 里抽取模型回复文本。"""
    import re
    parts = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    return "".join(p.encode().decode("unicode_escape", "replace") for p in parts).strip()


def build_chat_event(uid, username, model, prompt, state, conv_id=None):
    """照抄 CLI 真实 chat_request_send payload，仅随机化每次调用相关的 ID。"""
    now = int(time.time() * 1000)
    conv_id = conv_id or str(uuid.uuid4())
    req_id = uuid.uuid4().hex
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conv_id,
        "requestId": req_id,
        "inputLength": len(prompt),
        "requestModelId": model,
        "requestModelName": MODEL_NAMES.get(model, model),
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 500,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "presentAt": now - 100,
        "traceId": req_id,
        "rootRequestId": req_id,
        "parentConversationId": conv_id,
        "agentName": "cli",
        "agentType": "main",
        "timezone": "Asia/Shanghai",
        "qimei36": state["qimei36"],
        "userId": uid,
        "username": username or "",
        "userNickname": username or "",
        "product": "SaaS",
        "releaseDate": 1789036585355,
        "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
        "os": "win32",
        "arch": "x64",
        "osVersion": "10.0.19045",
        "cpuModel": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx  ",
        "cpuCores": 8,
        "memorySize": 18,
        "vcsType": "unknown",
        "vcsRepo": "",
        "vcsBranchName": "",
        "vcsRevId": "",
        "codebuddy.session_id": conv_id,
        "codebuddy.conversation_request_id": req_id,
        "extName": "workbuddy-desktop",
        "extVersion": "5.5.6",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "machineId": state["machineId"],
        "sessionId": str(uuid.uuid4()),
        "ideVersion": "5.5.6",
    }


def report_chat(cfg, token, uid, domain, ev):
    """上报 chat_request_send（连续登录的真正记账信号）。返回 (status, code, raw)。"""
    body = json.dumps([ev], ensure_ascii=False).encode("utf-8")
    extra = {"X-Product": "SaaS", "X-Requested-With": "WorkBuddy"}
    last = ""
    for base in cfg["api_bases"]:
        st, rbody = http_post(base.rstrip("/") + ENDPOINT_REPORT, token, body,
                              cfg["timeout"], cfg["follow_redirects"], uid, domain, extra)
        if st is not None:
            code = None
            try:
                code = json.loads(rbody).get("code")
            except Exception:
                pass
            return st, code, (rbody or "")[:200]
        last = str(rbody)
    return None, None, last


def run_chat_completions(cfg, token, uid, domain, prompt, model, timeout):
    """ACP 不可用时的回退：流式 chat/completions。返回 (ok, msg, elapsed)。"""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": True}, ensure_ascii=False).encode("utf-8")
    t0 = time.time()
    last = ""
    for base in cfg["api_bases"]:
        req = urllib.request.Request(base.rstrip("/") + ENDPOINT_CHAT, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("X-User-Id", uid)
        req.add_header("X-Domain", domain or "www.workbuddy.cn")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        req.add_header("User-Agent", "WorkBuddy/5.2.3")
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            last = str(e)[:200]
            continue
        parts = []
        try:
            for raw in r:
                s = raw.decode("utf-8", "replace").strip()
                if not s.startswith("data:"):
                    continue
                p = s[5:].strip()
                if p == "[DONE]":
                    break
                try:
                    d = json.loads(p)
                except Exception:
                    continue
                for ch in (d.get("choices") or []):
                    c = (ch.get("delta") or {}).get("content")
                    if c:
                        parts.append(c)
        finally:
            r.close()
        text = "".join(parts).strip()
        if text:
            return True, "模型=%s 回复：%s" % (model, text[:120]), time.time() - t0
        last = "无内容返回"
    return False, last, time.time() - t0


def do_chat(cfg, token, uid, username, domain, chat_cfg):
    """发一条真实提问（会话复用 + 上报 + ACP）。成功则写入当日记录。返回 (ok, 结果行)。"""
    prompt = chat_cfg.get("prompt") or DEFAULT_CHAT["prompt"]
    model = chat_cfg.get("model") or "hy3"
    timeout = int(chat_cfg.get("timeout") or 90)

    state = load_cloud_state(cfg)
    cid, stok, link, reused = resolve_conversation(cfg, token, uid, domain, state)

    # 发送时刻上报（带真实 conversationId；拿不到会话时用随机 id，记账可能不认）
    ev = build_chat_event(uid, username, model, prompt, state, cid)
    rst, rcode, _ = report_chat(cfg, token, uid, domain, ev)

    ok, detail = False, "未执行"
    if cid and stok and link:
        aok, ares = acp_prompt(link, stok, cid, prompt, uid, domain, timeout)
        if aok:
            text = extract_acp_text(ares)
            ok = True
            detail = ("模型=%s 回复：%s" % (model, text[:120])) if text else "ACP 已完成推理"
        else:
            log(cfg, "CHAT ACP 失败(%s)，回退 chat/completions" % ares[:120])
    if not ok:
        ok, detail, _ = run_chat_completions(cfg, token, uid, domain, prompt, model, timeout)

    if ok:
        state["last_chat_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
        state["last_chat_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_chat_model"] = model
        save_cloud_state(cfg, state)

    line = "✅ 对话成功（%s，会话%s）" % (detail, "复用" if reused else "新建")
    if not ok:
        line = "❌ 对话失败：%s" % detail
    if rst is None:
        line += " | ⚠️ 上报未送达"
    return ok, line


# ---- 成长计划：连续登录 7/14/28 天奖励自动领取 ----
def _dig(obj, key):
    """在可能被 data/result 包裹的响应里取字段。"""
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if isinstance(obj.get(k), dict):
                v = _dig(obj[k], key)
                if v is not None:
                    return v
    return None


def _growth_call(cfg, path, token, uid, domain, payload=None):
    """成长中心接口：payload 为 None 走 GET，否则 POST。返回 (status, dict)。"""
    result = (None, {})
    for base in cfg["api_bases"]:
        url = base.rstrip("/") + ENDPOINT_GROWTH + path
        if payload is None:
            st, body = http_get(url, token, uid, domain, cfg["timeout"])
        else:
            st, body = http_post(url, token,
                                 json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 cfg["timeout"], cfg["follow_redirects"], uid, domain,
                                 {"X-Product": "SaaS"})
        if st is None:
            result = (st, {})
            continue
        try:
            return st, json.loads(body)
        except Exception:
            return st, {}
    return result


def growth_checked_today(cfg):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return load_checkin_state(cfg).get("growth_checked_date") == today


def mark_growth_checked(cfg):
    state = load_checkin_state(cfg)
    state["growth_checked_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    save_checkin_state(cfg, state)


def claim_growth_rewards(cfg, token, uid, domain):
    """领取连登奖励（入门 7 天 / 进阶 14 天 / 巅峰 28 天）。

    服务端按档位幂等：已领为 claimed、未解锁为 locked，重复调用安全。
    状态以 /redeem/summary 为准，tier 标识以 /streak 的 tiers[] 为准。
    返回 (ok, 结果行)；ok=False 表示需要下次兜底重试。"""
    st, summary = _growth_call(cfg, "/redeem/summary", token, uid, domain)
    if st is None:
        return False, ["🎁 连登奖励：查询失败（网络不可达）"]
    if st in (401, 403):
        return False, ["🎁 连登奖励：查询被拒绝（HTTP %s）" % st]
    if not (200 <= st < 300):
        return False, ["🎁 连登奖励：状态查询失败（HTTP %s）" % st]

    got, errors, pending = [], [], 0
    for tier, status_key, label, days in GROWTH_TIERS:
        state = _dig(summary, status_key + "_status")
        if not state or state in ("claimed", "locked"):
            continue
        pending += 1
        code, resp = _growth_call(cfg, "/redeem", token, uid, domain,
                                  {"tier": tier, "client_token": "u-%s" % uuid.uuid4()})
        # 档位标识被判 unknown 时退回天数重试；这类 400 是参数校验拒绝，不会重复领取
        if code == 400 and "tier" in str(_dig(resp, "msg") or "").lower():
            code, resp = _growth_call(cfg, "/redeem", token, uid, domain,
                                      {"tier": days, "client_token": "u-%s" % uuid.uuid4()})
        if code is not None and 200 <= code < 300:
            bits = []
            for key, unit in (("credit_granted", "积分"), ("energy_granted", "能量"),
                              ("cards_granted", "补登卡"), ("chances_granted", "抽奖次数")):
                v = _dig(resp, key)
                if isinstance(v, int) and v:
                    bits.append("+%s %s" % (v, unit))
            got.append("🎁 连登奖励「%s %s天」已领取%s"
                       % (label, days, "（%s）" % " ".join(bits) if bits else ""))
        elif code == 403:
            # 连登天数不足：业务常态，不算失败
            continue
        else:
            errors.append("🎁 连登奖励「%s」领取失败（HTTP %s %s）"
                          % (label, code, _dig(resp, "msg") or ""))

    if got:
        return True, got + errors
    if errors:
        return False, errors
    # 没有可领取档位：静默，不污染每日推送
    return True, []


# ---- 成长计划：派猫猫旅行（每日 1 次，1~4 小时后回来自动领取）----
def travel_done_today(cfg):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return load_checkin_state(cfg).get("travel_done_date") == today


def mark_travel_done(cfg):
    state = load_checkin_state(cfg)
    state["travel_done_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    state["travel_done_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_checkin_state(cfg, state)


def _fmt_ts(ts):
    try:
        return time.strftime("%H:%M", time.localtime(int(ts)))
    except Exception:
        return "?"


def do_travel(cfg, token, uid, domain):
    """派猫猫旅行：已完成→领取，空闲且未达上限→派出，进行中→只记录。
    返回 (ok, 结果行)；ok=False 表示网络异常，下次兜底时间点重试。"""
    st, resp = _growth_call(cfg, "/buddy/travel/status", token, uid, domain)
    if st is None:
        return False, ["🐱 猫猫旅行：状态查询失败（网络不可达）"]
    if not (200 <= st < 300) or _dig(resp, "code") not in (None, 0):
        return False, ["🐱 猫猫旅行：状态查询失败（HTTP %s %s）" % (st, _dig(resp, "msg") or "")]

    state = _dig(resp, "state") or "idle"
    location = _dig(resp, "location") or {}
    arrive_at = _dig(resp, "arrive_at") or 0
    limit_reached = bool(_dig(resp, "daily_limit_reached"))

    # 1) 已返回：领取积分
    if state == "completed":
        code, r = _growth_call(cfg, "/buddy/travel/claim", token, uid, domain, {})
        if code is not None and 200 <= code < 300:
            credit = _dig(r, "reward_credit") or _dig(r, "credit") or 0
            name = location.get("name") or "旅行地"
            mark_travel_done(cfg)
            return True, ["🐱 猫猫旅行：%s 归来，已领取 %s 积分" % (name, credit)]
        if code == 403:
            # 已被领过（多实例并发或人工领取），视为今日完成
            mark_travel_done(cfg)
            return True, []
        return False, ["🐱 猫猫旅行：领取失败（HTTP %s %s）" % (code, _dig(r, "msg") or "")]

    # 2) 旅行中：记录预计归来时间，不重复派出
    if state == "in_progress":
        return True, ["🐱 猫猫旅行：在途（预计 %s 归来）" % _fmt_ts(arrive_at)] if arrive_at else []

    # 3) 空闲且今日未达上限：派出
    if not limit_reached:
        loc_id = int((cfg.get("travel") or {}).get("location_id") or 0)
        if not loc_id:
            code, conf = _growth_call(cfg, "/buddy/travel/config", token, uid, domain)
            locs = (_dig(conf, "locations") or []) if code and 200 <= code < 300 else []
            if locs:
                loc_id = locs[time.localtime().tm_mday % len(locs)].get("id") or 1
            else:
                loc_id = 1
        code, r = _growth_call(cfg, "/buddy/travel/depart", token, uid, domain,
                               {"location_id": loc_id})
        if code is not None and 200 <= code < 300:
            d = _dig(r, "location") or {}
            return True, ["🐱 猫猫旅行：已派往%s（%s 归来）"
                          % (d.get("name") or "旅行地", _fmt_ts(_dig(r, "arrive_at")))]
        if code == 403:
            # daily limit reached：今日名额已用尽
            mark_travel_done(cfg)
            return True, []
        return False, ["🐱 猫猫旅行：派出失败（HTTP %s %s）" % (code, _dig(r, "msg") or "")]

    # 4) 空闲且已达上限：今日已完成（可能已被人工派过并领完）
    mark_travel_done(cfg)
    return True, []


def run_once(cfg):
    """执行一次完整流程（取登录态→签到→对话→查状态→推送）。返回是否签到成功。
    签到与对话各按「当日首次成功」门控：已完成的不再重复，未完成的会在后续兜底时间点补做。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    chat_cfg = load_chat_cfg(cfg)
    chat_on = bool(chat_cfg.get("enabled", True))
    growth_on = bool((cfg.get("growth") or {}).get("enabled", True))
    travel_on = bool((cfg.get("travel") or {}).get("enabled", True))

    # 0) 当日已完成检查：签到、对话、连登奖励、猫猫旅行都完成才跳过；任一未完成则继续补做
    prev = load_checkin_state(cfg)
    signed_today = prev.get("last_checkin_date") == today and prev.get("status") == "success"
    chat_needed = chat_on and not chat_done_today(cfg)
    growth_needed = growth_on and not growth_checked_today(cfg)
    travel_needed = travel_on and not travel_done_today(cfg)
    if signed_today and not chat_needed and not growth_needed and not travel_needed:
        log(cfg, "SKIP 今日(%s)签到、对话、连登奖励、猫猫旅行均已完成，跳过。" % today)
        return True

    # 1) 获取登录态（desktop 模式读本机文件 / token 模式复用桌面端 token）
    try:
        token, expires_at, nickname, uin, uid, domain = load_credentials(cfg)
    except Exception as e:
        msg = "⚠️ Buddy加油站自动签到失败\n无法获取登录态（桌面模式读文件失败，或 token 未导入）：%s" % e
        notify_once_per_day(cfg, msg, "load_credentials")
        log(cfg, "FAIL load credentials: %s" % e)
        return False

    if not token:
        msg = "⚠️ Buddy加油站自动签到失败\n缺少 accessToken。桌面端请打开 WorkBuddy 刷新登录态；NAS 请运行 buddy_auth.py 导入 token（桌面端导出后写入 data/token_store.json）。"
        notify_once_per_day(cfg, msg, "no_token")
        log(cfg, "FAIL no token")
        return False

    # 2) 先续期再签到（token 模式每日一次；desktop 模式由客户端自行维护，跳过）
    (token, expires_at, nickname, uin, uid, domain), refresh_note = maybe_refresh(
        cfg, (token, expires_at, nickname, uin, uid, domain))

    # 3) token 过期检查
    if expires_at:
        try:
            if int(expires_at) < int(datetime.datetime.now().timestamp() * 1000):
                msg = "⚠️ Buddy加油站自动签到失败\naccessToken 已过期，请打开 WorkBuddy 客户端刷新登录态后重试。"
                notify_once_per_day(cfg, msg, "token_expired")
                log(cfg, "FAIL token expired")
                return False
        except Exception:
            pass

    # 4) 执行签到（服务端幂等，当天重复调用返回 code=10001，无需先查状态）
    status_info = {}
    if signed_today:
        claimed, result_lines, checkin_data = True, ["✅ 今日已签到"], {}
    else:
        claimed, result_lines, checkin_data = do_claim(cfg, token, uid, domain)

    # 5) 签到后查询状态：签到前查会拿到旧值（连续天数/累计积分尚未 +1）
    if claimed and not signed_today:
        status_info = fetch_status(cfg, token, uid, domain)
        # 状态查询失败时退化为签到响应自带的 streak_days
        append_status_summary(result_lines, status_info or checkin_data)

    # 5.1) 对话（连续登录记账信号）：当日首次成功后不再重复
    if chat_needed:
        try:
            chat_ok, chat_line = do_chat(cfg, token, uid, nickname, domain, chat_cfg)
            result_lines.append(chat_line)
        except Exception as e:
            log(cfg, "CHAT ERR %s" % e)
            result_lines.append("❌ 对话异常：%s" % str(e)[:120])

    # 5.2) 成长计划连登奖励（7/14/28 天档）：可领取才领，服务端按档位幂等
    if growth_needed:
        try:
            growth_ok, growth_lines = claim_growth_rewards(cfg, token, uid, domain)
            result_lines.extend(growth_lines)
            # 查询成功即记为已检查；网络/服务端异常则留待下个兜底时间点重试
            if growth_ok:
                mark_growth_checked(cfg)
        except Exception as e:
            log(cfg, "GROWTH ERR %s" % e)
            result_lines.append("🎁 连登奖励异常：%s" % str(e)[:120])

    # 5.3) 派猫猫旅行：空闲则派出，已返回则领取，在途只记录
    if travel_needed:
        try:
            travel_ok, travel_lines = do_travel(cfg, token, uid, domain)
            result_lines.extend(travel_lines)
        except Exception as e:
            log(cfg, "TRAVEL ERR %s" % e)
            result_lines.append("🐱 猫猫旅行异常：%s" % str(e)[:120])

    # 6) 组装企业微信纯文本消息
    md = "⛽ Buddy加油站 每日签到\n"
    md += "日期：%s\n" % today
    label = cfg.get("account_name") or nickname
    if label:
        md += "账号：%s\n" % label
    elif nickname:
        md += "账号：%s\n" % nickname
    if refresh_note:
        md += "%s\n" % refresh_note
    md += "\n" + "\n".join(result_lines) + "\n"

    status, body = push_wecom(cfg, md)
    if status is None:
        log(cfg, "RESULT: %s | wecom=None 原因=%s" % (" | ".join(result_lines), body))
    else:
        log(cfg, "RESULT: %s | wecom=%s" % (" | ".join(result_lines), status))

    # 7) 写入当日签到成功记录（仅签到成功时写；失败不写以便稍后重试）
    #    合并写入，避免覆盖同文件里的 growth_checked_date 等当日标记
    if claimed:
        state = load_checkin_state(cfg)
        state.update({
            "last_checkin_date": today,
            "last_checkin_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success",
            "credit": _pick(checkin_data, "credit", "today_credit", "daily_credit"),
            "streak_days": _pick(status_info or checkin_data, "streak_days", "streakDays", "continuous_days", "continuousDays"),
            "total_credits": _pick(status_info or checkin_data, "total_credits", "totalCredits"),
        })
        save_checkin_state(cfg, state)

    # 当日全部完成（签到 + 对话 + 旅行）才算 done，否则后续兜底时间点会继续补做
    return (claimed
            and (not chat_on or chat_done_today(cfg))
            and (not travel_on or travel_done_today(cfg)))


def _today_at(now, hm):
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def run_daemon(cfg):
    """常驻守护：读取 times 里的签到时间，到时间执行签到，其余时间静默 sleep。
    当天签到成功后睡到次日第一个时间点；任一时间点签到失败则继续等待下一个兜底时间。"""
    times = parse_times(cfg)
    tz = datetime.datetime.now().astimezone().tzinfo
    log(cfg, "DAEMON 启动 | 签到时间=%s | 时区=%s | restart=unless-stopped 即可长期保活" % (
        ", ".join("%02d:%02d" % t for t in times), tz))
    if not cfg.get("_webhook_url"):
        log(cfg, "WARN webhook 未配置，结果将不会推送到企业微信")

    last_claimed_date = None
    while True:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if last_claimed_date == today_str:
            # 今天已签到成功，睡到明天第一个时间点
            target = _today_at(now, times[0]) + datetime.timedelta(days=1)
        else:
            # 找今天尚未到达的第一个时间点
            target = None
            for hm in times:
                cand = _today_at(now, hm)
                if cand > now:
                    target = cand
                    break
            if target is None:
                # 今天所有时间点已过但尚未签到成功 -> 睡到明天第一个（兜底重试）
                target = _today_at(now, times[0]) + datetime.timedelta(days=1)

        delta = (target - now).total_seconds()
        if delta > 1:
            log(cfg, "WAIT 距下次签到 %s 还有 %.0f 秒（静默常驻）" % (target.strftime("%H:%M"), delta))
            # 分块 sleep，保持对时间与配置变化的响应；到点前 1 秒退出循环
            while (target - datetime.datetime.now()).total_seconds() > 1:
                time.sleep(min(60, (target - datetime.datetime.now()).total_seconds()))

        # 到点尝试签到
        claimed = run_once(cfg)
        if claimed:
            last_claimed_date = datetime.datetime.now().strftime("%Y-%m-%d")
        # 防止同一分钟内重复进入，稍候片刻
        time.sleep(61)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    if "--once" in argv:
        run_once(cfg)
        return
    if "--daemon" in argv:
        run_daemon(cfg)
        return
    # 未显式指定：按环境自动判定
    # Windows（桌面端有登录态文件，通常配合计划任务按点触发）→ 单次；Docker/NAS/Linux → 常驻守护
    if sys.platform == "win32":
        run_once(cfg)
    else:
        run_daemon(cfg)


if __name__ == "__main__":
    main()
