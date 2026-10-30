# -*- coding: utf-8 -*-
"""Web 管理台：账号增删改、token 导入、立即执行、日志查看。

仅用标准库 http.server，无第三方依赖。设置 BUDDY_ADMIN_PASSWORD 后启用 HTTP Basic 认证。
"""
import base64
import datetime
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import accounts, runner, settings
import buddy_checkin as bc

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(HERE, "web")
ADMIN_PASSWORD = os.environ.get("BUDDY_ADMIN_PASSWORD", "")


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
    def _authed(self):
        if not ADMIN_PASSWORD:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, pwd = base64.b64decode(hdr[6:]).decode().split(":", 1)
                return pwd == ADMIN_PASSWORD
            except Exception:
                return False
        return False

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

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
        if not self._authed():
            return self._send(401, "unauthorized", "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="Buddy"'})
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            p = os.path.join(WEB_DIR, "index.html")
            if not os.path.exists(p):
                return self._send(404, "web/index.html 不存在", "text/plain; charset=utf-8")
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json({"ok": True, "time": datetime.datetime.now().isoformat()})
        if path == "/api/accounts":
            return self._json({"accounts": [_profile(a) for a in accounts.list_accounts()]})
        if path == "/api/settings":
            cur = settings.load()
            return self._json({
                "webhook": cur.get("webhook") or "",
                "webhook_configured": bool(settings.webhook_url()),
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
        if not self._authed():
            return self._send(401, "unauthorized", "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="Buddy"'})
        path = urllib.parse.urlparse(self.path).path
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
            return self._send(401, "unauthorized", "text/plain; charset=utf-8")
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
            return self._send(401, "unauthorized", "text/plain; charset=utf-8")
        m = re.match(r"^/api/accounts/([^/]+)$", urllib.parse.urlparse(self.path).path)
        if not m:
            return self._json({"error": "not found"}, 404)
        aid = urllib.parse.unquote(m.group(1))
        if not accounts.delete_account(aid):
            return self._json({"error": "账号不存在"}, 404)
        return self._json({"ok": True})

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

    # ---------- 通知设置（全局） ----------
    def _save_settings(self):
        body = self._body()
        raw = (body.get("webhook") or "").strip()
        if raw and "://" in raw and not raw.startswith(("http://", "https://")):
            return self._json({"error": "请填 http/https 地址，或只填机器人 key"}, 400)
        if raw and not raw.startswith(("http://", "https://")) and len(raw) < 8:
            return self._json({"error": "机器人 key 看起来不完整"}, 400)
        settings.save({"webhook": raw})
        return self._json({"ok": True,
                           "webhook": settings.load().get("webhook", ""),
                           "webhook_configured": bool(settings.webhook_url())})

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
