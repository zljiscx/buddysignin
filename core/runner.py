# -*- coding: utf-8 -*-
"""多账号执行与调度：逐个账号跑 buddy_checkin.run_once，互不影响。"""
import datetime
import json
import os
import threading
import time

import buddy_checkin as bc
from core import accounts, settings

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


def scheduler_times():
    """当前生效的自动执行时间点（每次调用都重新读设置，管理台改完即时生效）。"""
    return settings.times()


SCHED_PATH = os.path.join(bc.DATA_DIR, "scheduler_state.json")


def _sched_load():
    try:
        with open(SCHED_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _sched_save(d):
    try:
        os.makedirs(bc.DATA_DIR, exist_ok=True)
        tmp = SCHED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCHED_PATH)
    except Exception:
        pass


def _tick(stop_event, log):
    """检查一次时间点：当天该跑且没跑过的点就立刻跑一轮（含启动补跑）。"""
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    st = _sched_load()
    if st.get("date") != today:
        st = {"date": today, "done": []}
    done = [s for s in (st.get("done") or []) if isinstance(s, str)]

    slots = ["%02d:%02d" % t for t in scheduler_times()]
    hhmm = now.strftime("%H:%M")
    pending = [s for s in slots if s <= hhmm and s not in done]

    if not pending:
        stop_event.wait(30)     # 30 秒轮询：设置改动、系统时间漂移都能跟上
        return

    label = "、".join(pending)
    log("[调度] 到点 %s，开始执行" % label)
    t0 = time.time()
    try:
        run_all()
    except Exception as e:      # 单轮异常不能让调度线程死掉
        log("[调度] 本轮异常：%s" % str(e)[:200])

    st["date"] = today
    st["done"] = sorted(set(done) | set(pending))
    st["last_slot"] = label
    st["last_run"] = _now()
    _sched_save(st)
    log("[调度] 本轮完成，用时 %.0f 秒；时间点 %s 记为当天已执行" % (time.time() - t0, label))


def scheduler_loop(stop_event, log=print):
    """常驻调度：轮询时间点，每个点当天只跑一次；已完成的步骤由 run_once 内部幂等跳过。

    与「定时启动」并存也安全：两种方式最终都落到 run_once 的当日门控上，
    谁先成功就算完成，另一个只做补漏。
    """
    log("[调度] 启动 | 每日时间点=%s（来自管理台设置，改动即时生效）"
        % ", ".join("%02d:%02d" % t for t in scheduler_times()))
    while not stop_event.is_set():
        try:
            _tick(stop_event, log)
        except Exception as e:
            log("[调度] 循环异常：%s" % str(e)[:200])
            stop_event.wait(30)
