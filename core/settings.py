# -*- coding: utf-8 -*-
"""全局设置：Web 管理台写入，所有账号共用。

data/settings.json
  {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"}

webhook 支持两种写法：完整机器人地址，或只填 key（自动拼接企业微信地址）。
留空 = 不推送。
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
PATH = os.path.join(DATA_DIR, "settings.json")

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

DEFAULTS = {"webhook": ""}


def load():
    d = dict(DEFAULTS)
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f) or {}
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if isinstance(raw.get(k), str):
                    d[k] = raw[k].strip()
    except Exception:
        pass
    return d


def save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    d = load()
    for k in DEFAULTS:
        if k in data:
            d[k] = (data.get(k) or "").strip()
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)
    return d


def webhook_url():
    """返回可直接 POST 的完整地址；未配置返回空串。"""
    raw = load().get("webhook") or ""
    if not raw:
        return ""
    if "://" in raw:
        return raw
    return WEBHOOK_PREFIX + raw
