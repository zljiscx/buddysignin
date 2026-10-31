# -*- coding: utf-8 -*-
"""Web 管理台：账号增删改、token 导入、立即执行、日志查看。

仅用标准库 http.server，无第三方依赖。访问任何页面会跳到只填密码的登录页，
登录成功发一个签名 Cookie（默认 7 天）。密码默认 admin，可在管理台「设置」里改。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import accounts, runner, settings
import buddy_checkin as bc

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(HERE, "web")


SESSION_COOKIE = "buddy_admin"
SESSION_DAYS = 7
KEY_PATH = os.path.join(settings.DATA_DIR, "session.key")
DEFAULT_PASSWORD = "admin"


def admin_password():
    """管理密码：环境变量 > 管理台设置 > 默认 admin。永远非空，默认就要登录。"""
    env = (os.environ.get("BUDDY_ADMIN_PASSWORD") or "").strip()
    if env:
        return env
    return (settings.load().get("admin_password") or "").strip() or DEFAULT_PASSWORD


def password_is_default():
    return admin_password() == DEFAULT_PASSWORD


def _session_key():
    """Cookie 签名密钥，首次自动生成到 data/session.key。"""
    try:
        with open(KEY_PATH, "rb") as f:
            k = f.read().strip()
        if len(k) >= 32:
            return k
    except Exception:
        pass
    k = secrets.token_hex(32).encode()
    try:
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        with open(KEY_PATH, "wb") as f:
            f.write(k)
        os.chmod(KEY_PATH, 0o600)
    except Exception:
        pass
    return k


def _sign(exp, pwd):
    # 密码参与签名：改了密码，所有旧 Cookie 立即失效
    msg = ("%s:%s" % (exp, hashlib.sha256(pwd.encode()).hexdigest())).encode()
    return hmac.new(_session_key(), msg, hashlib.sha256).hexdigest()[:40]


def make_session():
    exp = int(time.time()) + SESSION_DAYS * 86400
    return "%d.%s" % (exp, _sign(exp, admin_password()))


def check_session(tok):
    pwd = admin_password()
    if not tok or not pwd:
        return False
    try:
        exp_s, sig = tok.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sign(exp_s, pwd))


def _jwt(token):
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def _parse_token_blob(raw):
    """接受明文 JWT 或 buddy_auth 的 transfer code，返回 token_store 字典。"""
    raw = (raw or "").strip().strip('"')
    if raw.count(".") == 2:
        return {"access_token": raw}
    try:
        s = "".join(raw.split())
        obj = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
        if isinstance(obj, dict) and obj.get("access_token"):
            return obj
    except Exception:
        pass
    return None


def _build_store(blob, name=""):
    store = dict(blob)
    at = store.get("access_token")
    if not isinstance(at, str) or at.count(".") != 2:
        raise ValueError("access_token 不是合法 JWT")
    payload = _jwt(at)
    store.setdefault("uid", payload.get("sub") or "")
    store.setdefault("username", payload.get("nickname") or payload.get("name") or name or "")
    store.setdefault("domain", "www.workbuddy.cn")
    if not store.get("expires_at") and payload.get("exp"):
        store["expires_at"] = int(payload["exp"]) * 1000
    store["exported_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not store.get("uid"):
        raise ValueError("token 中缺少用户标识（sub）")
    return store


def _profile(account):
    aid = account["id"]
    store = accounts.read_state(aid, "token_store.json")
    state = accounts.read_state(aid, "checkin_state.json")
    cloud = accounts.read_state(aid, "cloud_state.json")
    exp = store.get("expires_at") or 0
    return {
        "id": aid,
        "name": account.get("name") or aid,
        "enabled": bool(account.get("enabled", True)),
        "username": store.get("username") or "",
        "domain": store.get("domain") or "",
        "token_expires_at": exp,
        "token_days_left": (int((exp - datetime.datetime.now().timestamp() * 1000) // 86400000)
                            if exp else None),
        "token_refreshed_at": store.get("last_refresh_date") or "",
        "token_refreshable": bool(store.get("refresh_token")),
        "today": datetime.datetime.now().strftime("%Y-%m-%d"),
        "signed_today": state.get("last_checkin_date") == datetime.datetime.now().strftime("%Y-%m-%d"),
        "checkin": state,
        "chat_done_today": cloud.get("last_chat_date") == datetime.datetime.now().strftime("%Y-%m-%d"),
        "travel_done_today": state.get("travel_done_date") == datetime.datetime.now().strftime("%Y-%m-%d"),
        "last_run": runner.last_run(aid),
    }


def _tail(path, lines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200 * 400))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception as e:
        return "（无日志：%s）" % e


class Handler(BaseHTTPRequestHandler):
    server_version = "BuddyAdmin"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return urllib.parse.unquote(v)
        return ""

    def _authed(self):
        pwd = admin_password()
        if check_session(self._cookie(SESSION_COOKIE)):
            return True
        # 兼容命令行 / 脚本里的 Basic（浏览器不会走到这里，页面用登录页）
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, passwd = base64.b64decode(hdr[6:]).decode().split(":", 1)
                return hmac.compare_digest(passwd, pwd)
            except Exception:
                return False
        return False

    def _deny(self):
        """页面请求跳登录页，接口请求回 401 JSON（不弹浏览器原生对话框）。"""
        if urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return self._json({"error": "未登录或登录已过期", "login": True}, 401)
        return self._send(302, "", "text/plain; charset=utf-8", {"Location": "/login"})

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(min(n, 1 << 20)).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/login":
            return self._login_page()
        if path == "/api/health":
            return self._json({"ok": True, "time": datetime.datetime.now().isoformat()})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._authed():
            return self._deny()
        if path in ("/", "/index.html"):
            p = os.path.join(WEB_DIR, "index.html")
            if not os.path.exists(p):
                return self._send(404, "web/index.html 不存在", "text/plain; charset=utf-8")
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/accounts":
            nr = settings.next_run()
            return self._json({
                "accounts": [_profile(a) for a in accounts.list_accounts()],
                "times": settings.enabled_times(),
                "next_run": nr.strftime("%m-%d %H:%M") if nr else "",
            })
        if path == "/api/settings":
            cur = settings.load()
            nr = settings.next_run()
            return self._json({
                "webhook": cur.get("webhook") or "",
                "webhook_configured": bool(settings.webhook_url()),
                "times": settings.enabled_times(),
                "slots": settings.slots(),
                "password_default": password_is_default(),
                "password_from_env": bool((os.environ.get("BUDDY_ADMIN_PASSWORD") or "").strip()),
                "next_run": nr.strftime("%Y-%m-%d %H:%M") if nr else "",
            })
        m = re.match(r"^/api/accounts/([^/]+)/(logs|run)$", path)
        if m:
            aid = urllib.parse.unquote(m.group(1))
            if not accounts.get_account(aid):
                return self._json({"error": "账号不存在"}, 404)
            if m.group(2) == "logs":
                q = urllib.parse.parse_qs(u.query)
                n = min(int((q.get("lines") or ["200"])[0] or 200), 2000)
                return self._json({"log": _tail(os.path.join(accounts.account_dir(aid), "Log.txt"), n)})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._logout()
        if not self._authed():
            return self._deny()
        if path == "/api/accounts":
            return self._create()
        if path == "/api/settings":
            return self._save_settings()
        if path == "/api/settings/test":
            return self._test_push()
        m = re.match(r"^/api/accounts/([^/]+)/run$", path)
        if m:
            aid = urllib.parse.unquote(m.group(1))
            a = accounts.get_account(aid)
            if not a:
                return self._json({"error": "账号不存在"}, 404)
            res = runner.run_account(a, manual=True)
            return self._json(res)
        return self._json({"error": "not found"}, 404)

    def do_PATCH(self):
        if not self._authed():
            return self._deny()
        m = re.match(r"^/api/accounts/([^/]+)$", urllib.parse.urlparse(self.path).path)
        if not m:
            return self._json({"error": "not found"}, 404)
        aid = urllib.parse.unquote(m.group(1))
        a = accounts.get_account(aid)
        if not a:
            return self._json({"error": "账号不存在"}, 404)
        body = self._body()
        accounts.upsert_account(aid, name=body.get("name") or a.get("name") or aid,
                                enabled=body.get("enabled", a.get("enabled", True)))
        return self._json({"ok": True, "account": accounts.get_account(aid)})

    def do_DELETE(self):
        if not self._authed():
            return self._deny()
        m = re.match(r"^/api/accounts/([^/]+)$", urllib.parse.urlparse(self.path).path)
        if not m:
            return self._json({"error": "not found"}, 404)
        aid = urllib.parse.unquote(m.group(1))
        if not accounts.delete_account(aid):
            return self._json({"error": "账号不存在"}, 404)
        return self._json({"ok": True})

    # ---------- 登录 ----------
    def _login_page(self, err="", code=200):
        p = os.path.join(WEB_DIR, "login.html")
        if not os.path.exists(p):
            return self._send(404, "web/login.html 不存在", "text/plain; charset=utf-8")
        hint = ("首次使用：默认密码 <b>admin</b>，登录后请在「设置」里改掉。"
                if password_is_default() else "")
        with open(p, encoding="utf-8") as f:
            html = (f.read().replace("{{ERR}}", err)
                    .replace("{{SHOW_ERR}}", "block" if err else "none")
                    .replace("{{SHOW_HINT}}", "block" if hint else "none")
                    .replace("{{HINT}}", hint))
        return self._send(code, html, "text/html; charset=utf-8")

    def _login(self):
        pwd = admin_password()
        given = (self._body().get("password") or "").strip()
        if not given:
            return self._login_page("请输入密码", 200)
        if not hmac.compare_digest(given.encode(), pwd.encode()):
            time.sleep(0.6)  # 简单拖慢暴力尝试
            return self._login_page("密码不正确", 200)
        ck = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            SESSION_COOKIE, make_session(), SESSION_DAYS * 86400)
        return self._json({"ok": True}, extra={"Set-Cookie": ck})

    def _logout(self):
        return self._json({"ok": True},
                          extra={"Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                                               % SESSION_COOKIE})

    def _create(self):
        body = self._body()
        raw = body.get("token") or body.get("token_blob") or ""
        blob = _parse_token_blob(raw)
        if not blob:
            return self._json({"error": "无法识别 token（需明文 JWT 或 transfer code）"}, 400)
        try:
            store = _build_store(blob, body.get("name") or "")
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        aid = store["uid"]
        accounts.upsert_account(aid, name=body.get("name") or store.get("username") or aid,
                                enabled=True)
        accounts.write_state(aid, "token_store.json", store)
        return self._json({"ok": True, "id": aid, "account": _profile(accounts.get_account(aid))})

    # ---------- 设置（全局） ----------
    def _save_settings(self):
        body = self._body()
        out = {}

        if "webhook" in body:
            raw = (body.get("webhook") or "").strip()
            if raw and "://" in raw and not raw.startswith(("http://", "https://")):
                return self._json({"error": "推送地址请填 http/https，或只填机器人 key"}, 400)
            if raw and not raw.startswith(("http://", "https://")) and len(raw) < 8:
                return self._json({"error": "机器人 key 看起来不完整"}, 400)
            out["webhook"] = raw

        key = "slots" if "slots" in body else ("times" if "times" in body else "")
        if key:
            raw = body.get(key)
            items = raw if isinstance(raw, list) else str(raw or "").replace(",", "\n").splitlines()
            clean, bad = [], []
            for it in items:
                s = settings.norm_slot(it)
                if s:
                    clean.append(s)
                elif str(it).strip():
                    bad.append(str(it).strip())
            if bad:
                return self._json({"error": "时间格式不对（应为 HH:MM）：%s" % "、".join(bad[:3])}, 400)
            clean = settings.norm_slots(clean)
            if not clean:
                return self._json({"error": "至少保留一个时间点"}, 400)
            if len(clean) > 12:
                return self._json({"error": "时间点最多 12 个"}, 400)
            out["times"] = clean

        changed_pwd = False
        if "password" in body:
            pwd = (body.get("password") or "").strip()
            if pwd:
                if len(pwd) < 4:
                    return self._json({"error": "密码至少 4 位"}, 400)
                if (os.environ.get("BUDDY_ADMIN_PASSWORD") or "").strip():
                    return self._json(
                        {"error": "当前密码由环境变量 BUDDY_ADMIN_PASSWORD 指定，改这里无效"}, 400)
                changed_pwd = pwd != admin_password()
                out["admin_password"] = pwd

        if out:
            settings.save(out)

        cur = settings.load()
        nr = settings.next_run()
        return self._json({
            "ok": True,
            "webhook": cur.get("webhook", ""),
            "webhook_configured": bool(settings.webhook_url()),
            "times": settings.enabled_times(),
            "slots": settings.slots(),
            "password_default": password_is_default(),
            "password_changed": changed_pwd,
            "next_run": nr.strftime("%Y-%m-%d %H:%M") if nr else "",
        })

    def _test_push(self):
        if not settings.webhook_url():
            return self._json({"error": "尚未配置推送地址"}, 400)
        cfg = bc.load_config()
        status, body = bc.push_wecom(cfg, "✅ Buddy 签到管理台：推送测试成功")
        ok = status == 200
        msg = ""
        try:
            r = json.loads(body)
            if r.get("errcode") not in (0, None):
                ok, msg = False, "errcode=%s %s" % (r.get("errcode"), r.get("errmsg"))
        except Exception:
            msg = str(body)[:200]
        return self._json({"ok": ok, "status": status, "message": msg})


def serve(host="0.0.0.0", port=18321):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print("[web] 管理台已启动：http://%s:%s" % (host, port))
    httpd.serve_forever()
