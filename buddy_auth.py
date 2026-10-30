#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面端 token 导出 / NAS 端导入助手（独立脚本，不依赖 buddy_checkin）。

Windows 上直接跑：读桌面端登录态 → 遇到新版 $wbEncrypted 加密信封时，调 WorkBuddy
客户端（Electron）子进程解密为明文 JWT → 写 data/token_store.json（永远是明文，
buddy_checkin 对 WorkBuddy 版本无感），并打印 transfer code。
NAS 上跑：无桌面端文件时，从 transfer code（BUDDY_TOKEN_BLOB 或交互粘贴）解码写入。

解密原理（参考 github.com/88lin/workbuddy-auto-signin）：
  新版桌面端把 accessToken/refreshToken/nickname 存成 {$wbEncrypted:1, envelope}，
  envelope 内是 AES-256-GCM（key = sha256(atRestSecretKey 字符串 utf8)，keyId =
  sha256(key)[:16]，nonce 12B / authTag 16B / AAD 固定构造）。atRestSecretKey 只在
  客户端进程内，通过 ELECTRON_RUN_AS_NODE=1 + WorkBuddy.exe -e <js> 子进程的
  process._linkedBinding('electron_browser_workbuddy_storage').loggerGet() 取得并在
  子进程内完成解密，密钥不落盘、不进入本进程。
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_REL_PATH = os.path.join("AppData", "Local", "CodeBuddyExtension",
                             "Data", "Public", "auth", "workbuddy-desktop.info")
TOKEN_STORE = os.path.join(HERE, "data", "token_store.json")

AUTH_HELPER_TIMEOUT = 15.0    # Electron 首次冷启动可能偏慢，比签到脚本放宽些
AUTH_INPUT_LIMIT = 65536
AUTH_OUTPUT_LIMIT = 65536

# 与客户端一致的解密助手：密钥只在子进程内存中出现，stdout 只回传明文。
# 仅支持 suite 1 / sym-v1；明文做长度校验，utf8 校验保证编码合法。
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const text = utf8(plaintext);
    if (!text.length || text.length > 32768) failure('INVALID_FORMAT');
    return text;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, text: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


class AuthError(Exception):
    """解密失败：只带固定原因码，不带任何敏感数据。"""


# 扫描时跳过的重量级目录，避免为了找一个 exe 把整盘翻穿
_SKIP_DIRS = {
    "windows", "winsxs", "system32", "syswow64", "sxs", "servicing",
    "$recycle.bin", "system volume information", "recovery", "drivers",
    "node_modules", ".git", ".svn", "cache", "temp", "tmp",
    "windowsapps", "installer", "packages",
}


def _is_client_exe(path):
    """真实客户端才带 resources 目录；排除更新器、卸载器等同名 exe。"""
    if not path or not os.path.isfile(path):
        return False
    return os.path.isdir(os.path.join(os.path.dirname(path), "resources"))


def _registry_candidates():
    """注册表线索：App Paths（安装时写入）与卸载项的 InstallLocation/DisplayIcon。
    用户换目录重装后旧硬编码路径必然失效，注册表是唯一权威来源。"""
    out = []
    try:
        import winreg
    except ImportError:
        return out

    def qv(key, name):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    locations = [(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE"),
                 (winreg.HKEY_CURRENT_USER, "SOFTWARE"),
                 (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node")]
    for hive, prefix in locations:
        for app in ("WorkBuddy.exe", "CodeBuddy.exe"):
            try:
                with winreg.OpenKey(hive, prefix + "\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + app) as k:
                    v = qv(k, "")
                    if v:
                        out.append(str(v).strip('"'))
            except OSError:
                pass
        for uninstall in ("Microsoft\\Windows\\CurrentVersion\\Uninstall",
                          "WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"):
            try:
                with winreg.OpenKey(hive, prefix + "\\" + uninstall) as root_key:
                    count = winreg.QueryInfoKey(root_key)[0]
                    for i in range(count):
                        try:
                            with winreg.OpenKey(root_key, winreg.EnumKey(root_key, i)) as sub:
                                display = str(qv(sub, "DisplayName") or "")
                                if "workbuddy" not in display.lower():
                                    continue
                                loc = qv(sub, "InstallLocation")
                                icon = qv(sub, "DisplayIcon")
                                if loc:
                                    out.append(os.path.join(str(loc).strip('"'), "WorkBuddy.exe"))
                                if icon:
                                    out.append(str(icon).strip('"').split(",")[0])
                        except OSError:
                            continue
            except OSError:
                pass
    return out


def _drive_roots():
    """只扫本地固定盘，跳过可移动盘和网络盘（既慢又不可能装客户端）。"""
    roots = []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for i in range(26):
            if not (mask & (1 << i)):
                continue
            root = chr(65 + i) + ":\\"
            if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                roots.append(root)
    except Exception:
        pass
    return roots or [os.environ.get("SystemDrive", "C:") + "\\"]


def _scan_drives(max_depth=3, budget=8.0):
    """有界扫描：深度与总耗时都封顶，找到就返回。默认深度足够覆盖
    D:\\.Agent\\WorkBuddy、C:\\Program Files\\WorkBuddy、D:\\Apps\\WorkBuddy 这类布局。"""
    started = time.time()
    for root in _drive_roots():
        stack = [(root, 0)]
        while stack:
            if time.time() - started > budget:
                return None
            current, depth = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_file():
                        if entry.name.lower() == "workbuddy.exe" and _is_client_exe(entry.path):
                            return entry.path
                    elif entry.is_dir() and depth < max_depth:
                        if entry.name.lower() not in _SKIP_DIRS:
                            stack.append((entry.path, depth + 1))
                except OSError:
                    continue
    return None


def find_workbuddy_runtime():
    """不写死安装路径：环境变量 → 注册表 → 常见目录 → 有界扫描。"""
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path):
            raise AuthError("WORKBUDDY_EXE 不是可用的客户端路径：%s" % path)
        return path

    candidates = _registry_candidates()
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(os.path.join(base, "WorkBuddy", "WorkBuddy.exe"))
    candidates.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))

    for path in candidates:
        if _is_client_exe(path):
            return os.path.abspath(path)

    found = _scan_drives()
    if found:
        return os.path.abspath(found)
    raise AuthError("未找到 WorkBuddy 客户端（WorkBuddy.exe）。请确认已安装，"
                    "或用 WORKBUDDY_EXE 环境变量指定其完整路径。")


def run_auth_helper(request, timeout=AUTH_HELPER_TIMEOUT):
    """起一个 WorkBuddy Electron 子进程执行解密助手。密钥不出客户端进程。"""
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthError("凭据过大，格式异常")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.Popen([find_workbuddy_runtime(), "-e", AUTH_HELPER_JS],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise AuthError("无法启动客户端子进程：%s" % e)
    out = {}

    def read_pipe(name, pipe):
        try:
            captured = pipe.read(AUTH_OUTPUT_LIMIT + 1)
            out[name] = captured[:AUTH_OUTPUT_LIMIT]
        except OSError:
            pass

    def write_pipe():
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass

    workers = [threading.Thread(target=read_pipe, args=("stdout", proc.stdout), daemon=True),
               threading.Thread(target=read_pipe, args=("stderr", proc.stderr), daemon=True),
               threading.Thread(target=write_pipe, daemon=True)]
    for w in workers:
        w.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AuthError("客户端解密超时")
    for w in workers:
        w.join(timeout=2)
    try:
        reply = json.loads(out.get("stdout", b"").decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        raise AuthError("客户端解密助手返回异常（stderr: %s）"
                        % out.get("stderr", b"")[:200].decode("utf-8", "replace"))
    if not isinstance(reply, dict) or reply.get("version") != 1:
        raise AuthError("客户端解密助手协议异常")
    if reply.get("ok") is not True:
        raise AuthError("解密失败：%s（客户端版本可能与凭据不匹配，或需先登录桌面端）"
                        % reply.get("reason", "UNKNOWN"))
    return reply


def wb_decrypt(value):
    """解一个 $wbEncrypted 信封（dict），返回明文字符串。"""
    return run_auth_helper({"operation": "decrypt", "value": value})["text"]


def is_envelope(v):
    return isinstance(v, dict) and v.get("$wbEncrypted") == 1 and isinstance(v.get("envelope"), str)


def ensure_plaintext(v, field):
    """字段可能是明文字符串或加密信封；信封则解密。解不出给明确报错。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if is_envelope(v):
        try:
            return wb_decrypt(v)
        except AuthError as e:
            raise AuthError("解密 %s 失败：%s" % (field, e))
    raise AuthError("%s 是无法识别的类型 %s，请打开 WorkBuddy 桌面端刷新登录态后重试"
                    % (field, type(v).__name__))


# ---------- token_store 读写（独立实现，行为与 buddy_checkin 中的版本一致） ----------

def _save_token_store(store):
    os.makedirs(os.path.dirname(TOKEN_STORE), exist_ok=True)
    with open(TOKEN_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    _register_account(store)


def _register_account(store):
    """同时登记到多账号目录 data/accounts/<uid>/，Web 管理台可直接看到。"""
    uid = store.get("uid")
    at = store.get("access_token")
    if not uid or not isinstance(at, str):
        return
    root = os.path.dirname(TOKEN_STORE)
    acc_dir = os.path.join(root, "accounts", uid)
    os.makedirs(acc_dir, exist_ok=True)
    with open(os.path.join(acc_dir, "token_store.json"), "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    idx_path = os.path.join(root, "accounts.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = {"accounts": []}
    if not isinstance(idx.get("accounts"), list):
        idx["accounts"] = []
    for a in idx["accounts"]:
        if a.get("id") == uid:
            a["name"] = store.get("username") or a.get("name") or uid
            break
    else:
        idx["accounts"].append({
            "id": uid,
            "name": store.get("username") or uid,
            "enabled": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def _exp_ms(token):
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        p = json.loads(base64.urlsafe_b64decode(seg))
        return int(p.get("exp", 0)) * 1000
    except Exception:
        return 0


def _b64_encode(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64_decode(s):
    s = "".join(str(s).strip().strip('"').split())
    if not s:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    except Exception:
        return None


def _save(at, rt, domain="www.workbuddy.cn", uid="", username="", expires_at=0):
    store = {
        "access_token": at,
        "domain": domain,
        "uid": uid,
        "username": username,
    }
    if rt:
        store["refresh_token"] = rt
    exp = expires_at or _exp_ms(at)
    if exp:
        store["expires_at"] = exp
    store["exported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_token_store(store)
    return store.get("expires_at", 0)


def _read_desktop():
    info_path = os.path.join(os.path.expanduser("~"), AUTH_REL_PATH)
    env_file = os.environ.get("BUDDY_AUTH_FILE")
    if env_file and os.path.exists(env_file):
        info_path = env_file
    if not os.path.exists(info_path):
        print("未检测到桌面端登录态文件（%s），进入导入模式。" % info_path)
        return None
    with open(info_path, encoding="utf-8") as f:
        d = json.load(f)
    auth = d.get("auth") or {}
    account = d.get("account") or {}
    return {
        "access_token": auth.get("accessToken"),
        "refresh_token": auth.get("refreshToken"),
        "domain": auth.get("domain") or (account.get("sso") or {}).get("domain") or "www.workbuddy.cn",
        "uid": account.get("uid") or "",
        "username": account.get("nickname") or "",
        "expires_at": auth.get("expiresAt") or 0,
        "path": info_path,
    }


def main():
    sess = _read_desktop()

    # ---------- 导出模式（桌面端） ----------
    if sess:
        raw_at, raw_rt, raw_name = sess["access_token"], sess["refresh_token"], sess["username"]
        encrypted = any(is_envelope(v) for v in (raw_at, raw_rt, raw_name) if v is not None)
        if encrypted:
            print("检测到新版登录态加密（$wbEncrypted 信封），调用 WorkBuddy 客户端解密…")
        at = ensure_plaintext(raw_at, "accessToken")
        rt = ensure_plaintext(raw_rt, "refreshToken") if raw_rt else ""
        username = ensure_plaintext(raw_name, "nickname") if raw_name and is_envelope(raw_name) else (raw_name or "")
        if not at or at.count(".") != 2:
            print("❌ 解密后的 accessToken 不是合法 JWT（前缀: %s…），请先在 WorkBuddy 桌面端登录后再运行本工具。"
                  % (at or "")[:16])
            sys.exit(1)
        saved_exp = _save(at, rt, sess["domain"], sess["uid"], username, sess["expires_at"])
        print("✅ 已从桌面端登录态写入：%s" % TOKEN_STORE)
        print("accessToken 过期时间：%s（过期后需重新运行本工具重搬）"
              % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_exp / 1000)) if saved_exp else "未知"))
        print("已随登录态写入：domain=%s  uid=%s  username=%s" % (sess["domain"], sess["uid"], username))
        blob = _b64_encode({
            "access_token": at,
            "refresh_token": rt,
            "domain": sess["domain"],
            "uid": sess["uid"],
            "username": username,
            "expires_at": saved_exp,
        })
        print("")
        print("=== 复制到 NAS 的 transfer code（一行，用 BUDDY_TOKEN_BLOB 传入即可）===")
        print(blob)
        print("")
        print("NAS 导入示例：")
        print('  docker compose run --rm -e BUDDY_TOKEN_BLOB="%s" buddy-checkin python buddy_auth.py' % blob)
        return

    # ---------- 导入模式（NAS / 无桌面端） ----------
    blob = (os.environ.get("BUDDY_TOKEN_BLOB") or "").strip()
    if not blob:
        print("请把 Windows 上 buddy_auth.py 打印的 transfer code 粘贴进来（或设 BUDDY_TOKEN_BLOB 环境变量）：")
        try:
            blob = input("transfer code > ").strip()
        except EOFError:
            print("\n非交互环境请用 -e BUDDY_TOKEN_BLOB=... 传入")
            sys.exit(1)

    dec = _b64_decode(blob)
    if isinstance(dec, dict) and dec.get("access_token"):
        at = dec["access_token"]
        saved_exp = _save(at, dec.get("refresh_token") or "",
                          dec.get("domain") or "www.workbuddy.cn",
                          dec.get("uid") or "", dec.get("username") or "",
                          dec.get("expires_at") or 0)
        print("✅ 已从 transfer code 写入：%s" % TOKEN_STORE)
        print("accessToken 过期时间：%s"
              % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_exp / 1000)) if saved_exp else "未知"))
        print("已写入：domain=%s  uid=%s  username=%s"
              % (dec.get("domain"), dec.get("uid"), dec.get("username")))
        return

    at = blob.strip().strip('"')
    if at and at.count(".") == 2:
        _save(at, "", "www.workbuddy.cn", "", "", 0)
        print("✅ 已从原始 accessToken 写入：%s" % TOKEN_STORE)
        return

    print("❌ 无法识别输入（既非 transfer code 也非 accessToken），已退出")
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except AuthError as e:
        print("❌ %s" % e)
        sys.exit(2)
