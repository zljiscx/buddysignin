# -*- coding: utf-8 -*-
"""全局设置：Web 管理台写入，所有账号共用。

data/settings.json
  {
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    "times": [
      {"time": "08:10", "enabled": true},
      {"time": "12:40", "enabled": false}
    ],
    "admin_password": ""
  }

webhook 支持两种写法：完整机器人地址，或只填 key（自动拼接企业微信地址）；留空 = 不推送。
times 是每天的自动执行时间点，每条带 enabled 开关；禁用的保留在列表里但不参与调度。
  兼容旧格式：元素是字符串时视为启用（"08:10"）。
admin_password 留空 = 使用默认密码（admin），可在管理台修改。
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
PATH = os.path.join(DATA_DIR, "settings.json")

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

DEFAULT_TIMES = ["08:10", "12:40", "20:30"]


def _read():
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def norm_slot(x):
    """把一条时间点归一成 {"time": "HH:MM", "enabled": bool}；非法返回 None。"""
    enabled = True
    if isinstance(x, dict):
        t = str(x.get("time") or "").strip()
        if x.get("enabled") is False:
            enabled = False
    else:
        t = str(x or "").strip()
    try:
        hh, mm = t.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
    except Exception:
        return None
    return {"time": "%02d:%02d" % (h, m), "enabled": enabled}


def norm_slots(items):
    """归一整个列表：去非法、去重（先出现的优先）、按时间排序。"""
    out, seen = [], set()
    for x in items or []:
        s = norm_slot(x)
        if not s or s["time"] in seen:
            continue
        seen.add(s["time"])
        out.append(s)
    return sorted(out, key=lambda s: s["time"])


def load():
    raw = _read()
    d = {"webhook": "", "admin_password": "", "times": list(DEFAULT_TIMES)}
    for k in ("webhook", "admin_password"):
        v = raw.get(k)
        if isinstance(v, str):
            d[k] = v.strip()
    t = raw.get("times")
    if isinstance(t, list) and t:
        d["times"] = t
    return d


def save(data):
    """按键合并保存；未出现的键保持原值。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    d = load()

    if "webhook" in data:
        d["webhook"] = (data.get("webhook") or "").strip()
    if "admin_password" in data:
        d["admin_password"] = (data.get("admin_password") or "").strip()
    if "times" in data:
        slots = norm_slots(data.get("times"))
        if slots:
            d["times"] = slots

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


def slots():
    """全部时间点（含禁用的），[{"time": "HH:MM", "enabled": bool}, ...]。"""
    out = norm_slots(load().get("times"))
    return out or norm_slots(DEFAULT_TIMES)


def enabled_times():
    """启用中的时间点字符串列表。"""
    return [s["time"] for s in slots() if s["enabled"]]


def times():
    """启用中的时间点 [(h, m), ...]；全部禁用时返回空列表。"""
    out = []
    for s in slots():
        if not s["enabled"]:
            continue
        h, m = s["time"].split(":")
        out.append((int(h), int(m)))
    return out


def next_run(now=None):
    """下一次自动执行的时间点（datetime）；没有启用中的时间点返回 None。"""
    import datetime
    now = now or datetime.datetime.now()
    ts = times()
    if not ts:
        return None
    for h, m in ts:
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand > now:
            return cand
    h, m = ts[0]
    return now.replace(hour=h, minute=m, second=0, microsecond=0) + datetime.timedelta(days=1)
