# -*- coding: utf-8 -*-
"""多账号执行与调度：逐个账号跑 buddy_checkin.run_once，互不影响。"""
import datetime
import json
import os
import threading
import time

import buddy_checkin as bc
from core import accounts

_lock = threading.Lock()
_state_lock = threading.Lock()
_last_run = {}          # account_id -> {time, ok, lines}
_account_locks = {}


def _alock(account_id):
    with _state_lock:
        return _account_locks.setdefault(account_id, threading.Lock())


def account_config(account):
    """构造某账号的运行配置：账号目录 + token 模式 + 账号名。"""
    aid = account["id"]
    overrides = {
        "auth_mode": "token",
        "account_id": aid,
        "account_name": account.get("name") or aid,
    }
    return bc.load_config(data_dir=accounts.account_dir(aid), overrides=overrides)


def run_account(account, manual=False):
    """执行一个账号的每日流程。返回 {ok, lines, time}。异常不外抛。"""
    aid = account["id"]
    lock = _alock(aid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "lines": ["⏳ 该账号正在执行中，跳过本次"], "time": _now()}
    try:
        cfg = account_config(account)
        ok = bc.run_once(cfg)
        state = accounts.read_state(aid, "checkin_state.json")
        lines = _summary_lines(state)
        res = {"ok": bool(ok), "lines": lines, "time": _now(), "manual": manual}
        with _state_lock:
            _last_run[aid] = res
        return res
    except Exception as e:
        res = {"ok": False, "lines": ["❌ 执行异常：%s" % str(e)[:200]], "time": _now()}
        with _state_lock:
            _last_run[aid] = res
        return res
    finally:
        lock.release()


def _summary_lines(state):
    out = []
    if state.get("last_checkin_date"):
        out.append("✅ 签到 %s（+%s，连登 %s 天）" % (
            state.get("last_checkin_time", "")[11:16] or state["last_checkin_date"],
            state.get("credit", "-"), state.get("streak_days", "-")))
    if state.get("growth_checked_date"):
        out.append("🎁 连登奖励已检查")
    if state.get("travel_done_date"):
        out.append("🐱 猫猫旅行已完成")
    if not out:
        out.append("暂无记录")
    return out


def run_all(manual=False):
    results = {}
    for a in accounts.list_accounts():
        if not a.get("enabled", True):
            continue
        results[a["id"]] = run_account(a, manual=manual)
        time.sleep(1)      # 账号之间稍作间隔，避免瞬时并发
    return results


def last_run(account_id=None):
    with _state_lock:
        if account_id:
            return _last_run.get(account_id)
        return dict(_last_run)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def scheduler_times(base_cfg):
    return bc.parse_times(base_cfg)


def scheduler_loop(stop_event, log=print):
    """常驻调度：每个时间点跑一轮所有账号，其余时间静默。"""
    base_cfg = bc.load_config()
    times = scheduler_times(base_cfg)
    log("调度启动 | 每日时间点=%s" % ", ".join("%02d:%02d" % t for t in times))
    while not stop_event.is_set():
        now = datetime.datetime.now()
        target = None
        for hm in times:
            cand = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if cand > now:
                target = cand
                break
        if target is None:
            target = now.replace(hour=times[0][0], minute=times[0][1], second=0,
                                 microsecond=0) + datetime.timedelta(days=1)
        wait = max(1.0, (target - now).total_seconds())
        log("下次执行 %s（%.0f 秒后）" % (target.strftime("%m-%d %H:%M"), wait))
        if stop_event.wait(wait):
            break
        try:
            run_all()
        except Exception as e:      # 调度线程不能死
            log("调度轮次异常：%s" % e)
        stop_event.wait(61)
