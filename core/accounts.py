# -*- coding: utf-8 -*-
"""多账号存储：账号列表与每个账号的数据目录管理。

data/accounts.json        账号索引
data/accounts/<uid>/      账号数据（token_store / checkin_state / cloud_state / Log.txt）
"""
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
INDEX_PATH = os.path.join(DATA_DIR, "accounts.json")


def _load_index():
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("accounts"), list):
                return d
    except Exception:
        pass
    return {"accounts": []}


def _save_index(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_PATH)


def account_dir(account_id):
    return os.path.join(DATA_DIR, "accounts", account_id)


def list_accounts():
    return _load_index()["accounts"]


def get_account(account_id):
    for a in list_accounts():
        if a.get("id") == account_id:
            return a
    return None


def upsert_account(account_id, name="", enabled=True):
    d = _load_index()
    for a in d["accounts"]:
        if a.get("id") == account_id:
            if name:
                a["name"] = name
            a["enabled"] = bool(enabled)
            _save_index(d)
            os.makedirs(account_dir(account_id), exist_ok=True)
            return a
    a = {"id": account_id, "name": name or account_id, "enabled": bool(enabled),
         "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d["accounts"].append(a)
    _save_index(d)
    os.makedirs(account_dir(account_id), exist_ok=True)
    return a


def delete_account(account_id):
    d = _load_index()
    before = len(d["accounts"])
    d["accounts"] = [a for a in d["accounts"] if a.get("id") != account_id]
    if len(d["accounts"]) == before:
        return False
    _save_index(d)
    p = account_dir(account_id)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    return True


def read_state(account_id, name, default=None):
    p = os.path.join(account_dir(account_id), name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_state(account_id, name, data):
    d = account_dir(account_id)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def migrate_legacy():
    """把旧的单账号 data/*.json 迁移成 accounts/<uid>/，只执行一次。"""
    if list_accounts():
        return None
    src = os.path.join(DATA_DIR, "token_store.json")
    if not os.path.exists(src):
        return None
    try:
        with open(src, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        return None
    at = store.get("access_token")
    if not isinstance(at, str) or at.count(".") != 2:
        return None
    uid = store.get("uid") or ""
    if not uid:
        return None
    dst = account_dir(uid)
    os.makedirs(dst, exist_ok=True)
    for name in ("token_store.json", "checkin_state.json", "cloud_state.json"):
        s = os.path.join(DATA_DIR, name)
        if os.path.exists(s):
            shutil.move(s, os.path.join(dst, name))
    if os.path.exists(os.path.join(DATA_DIR, "Log.txt")):
        shutil.move(os.path.join(DATA_DIR, "Log.txt"), os.path.join(dst, "Log.txt"))
    upsert_account(uid, store.get("username") or uid, True)
    return uid
