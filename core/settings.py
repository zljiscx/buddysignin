# -*- coding: utf-8 -*-
"""全局设置：Web 管理台写入，所有账号共用。

data/settings.json
  {
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    "times": ["08:10", "12:40", "20:30"],
    "admin_password": ""
  }

webhook 支持两种写法：完整机器人地址，或只填 key（自动拼接企业微信地址）；留空 = 不推送。
times 是每天的自动执行时间点，可多个；同一天内每个时间点只触发一次，已完成的步骤会自动跳过。
admin_password 留空 = 使用默认密码（admin），可在管理台修改。
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
PATH = os.path.join(DATA_DIR, "settings.json")

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

DEFAULT_TIMES = ["08:10", "12:40", "20:30"]

DEFAULTS = {
    "webhook": "",
    "times": list(DEFAULT_TIMES),
    "admin_password": "",
}


def load():
    d = dict(DEFAULTS)
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return d
    if not isinstance(raw, dict):
        return d
    for k, dv in DEFAULTS.items():
        v = raw.get(k)
        if isinstance(dv, list):
            if isinstance(v, list):
                d[k] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str):
            d[k] = v.strip()
    return d


def save(data):
    """按键合并保存；未出现的键保持原值。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    d = load()
    for k, dv in DEFAULTS.items():
        if k not in data:
            continue
        v = data.get(k)
        if isinstance(dv, list):
            d[k] = [str(x).strip() for x in (v or []) if str(x).strip()]
        else:
            d[k] = (v or "").strip() if isinstance(v, str) else ""
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


def times():
    """返回校验过的时间点列表 [(h, m), ...]，已排序去重；全部非法时退回默认。"""
    out = []
    for t in load().get("times") or DEFAULT_TIMES:
        try:
            hh, mm = str(t).split(":")
            h, m = int(hh), int(mm)
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
        except Exception:
            continue
    return sorted(set(out)) or _parse(DEFAULT_TIMES)


def _parse(items):
    out = []
    for t in items:
        hh, mm = t.split(":")
        out.append((int(hh), int(mm)))
    return sorted(set(out))


def next_run(now=None):
    """下一次自动执行的时间点（datetime）；无可用时间点返回 None。"""
    import datetime
    now = now or datetime.datetime.now()
    for h, m in times():
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand > now:
            return cand
    h, m = times()[0]
    return now.replace(hour=h, minute=m, second=0, microsecond=0) + datetime.timedelta(days=1)
