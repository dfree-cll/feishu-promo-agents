# -*- coding: utf-8 -*-
"""
可观测模块：结构化运行日志（JSONL）+ 指标聚合 + 错误留存 + run 上下文。

- 每次 Agent 处理产生一个 run_id，链路上的每个环节（收到消息/LLM 调用/工具调用/
  回复/后台任务）都落一条结构化日志到 state/obs_runs.jsonl；
- 指标（消息数/LLM 调用数与均耗时/工具调用与错误/卡片数）滚动快照到 state/metrics.json，
  可用 `python -m main metrics` 查看；
- `python -m main trace <run_id>` 可回放单次处理的完整步骤时间线。

线程安全：JSONL 追加与指标更新均带锁；run 上下文用 threading.local 支持并发处理。
"""
import json
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import List
from typing import Optional

from promo_agent import config

RUNS_FILE = config.STATE_DIR / "obs_runs.jsonl"
ERRORS_FILE = config.STATE_DIR / "errors.jsonl"
METRICS_FILE = config.STATE_DIR / "metrics.json"

_LOCK = threading.Lock()
_CTX = threading.local()          # 当前线程绑定的 run_id

_METRICS = {
    "messages": 0,          # 收到的群消息数
    "agent_runs": 0,        # Agent 处理次数
    "llm_calls": 0,         # LLM 调用次数
    "llm_latency_s": 0.0,   # LLM 累计耗时
    "tool_calls": 0,        # 工具调用次数
    "tool_errors": 0,       # 工具执行失败次数
    "cards_sent": 0,        # 发出的消息卡片数
    "errors": 0,            # 错误总数
}


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _append(path, obj: dict):
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def set_current_run(run_id: str):
    _CTX.run_id = run_id


def current_run() -> str:
    return getattr(_CTX, "run_id", "")


def new_run_id(source: str = "cli") -> str:
    return f"run_{datetime.now():%Y%m%d%H%M%S}_{source}_{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- 事件与指标
def log_event(event: str, run_id: str = "", **fields):
    obj = {"ts": _now(), "run_id": run_id or current_run(), "event": event, **fields}
    _append(RUNS_FILE, obj)


def log_error(where: str, error: str, run_id: str = ""):
    _METRICS["errors"] += 1
    obj = {"ts": _now(), "run_id": run_id or current_run(), "where": where,
           "error": str(error)[:300]}
    _append(ERRORS_FILE, obj)
    log_event("error", **obj)


def observe_llm(latency_s: float, tool_calls: int = 0):
    _METRICS["llm_calls"] += 1
    _METRICS["llm_latency_s"] = round(_METRICS["llm_latency_s"] + latency_s, 3)
    log_event("llm_call", latency_s=round(latency_s, 2), tool_calls=tool_calls)


def observe_tool(name: str, latency_s: float, ok: bool = True):
    _METRICS["tool_calls"] += 1
    if not ok:
        _METRICS["tool_errors"] += 1
    log_event("tool_call", tool=name, latency_s=round(latency_s, 2), ok=ok)


def observe_card():
    _METRICS["cards_sent"] += 1


def observe_message():
    _METRICS["messages"] += 1
    log_event("message_received")


def observe_agent_run(run_id: str, latency_s: float, source: str = ""):
    _METRICS["agent_runs"] += 1
    log_event("agent_run_end", run_id=run_id, latency_s=round(latency_s, 2), source=source)


def _persist_metrics():
    m = dict(_METRICS)
    m["llm_avg_latency_s"] = round(m["llm_latency_s"] / m["llm_calls"], 2) if m["llm_calls"] else 0
    m["tool_error_rate"] = (round(m["tool_errors"] / m["tool_calls"], 3)
                            if m["tool_calls"] else 0)
    m["updated_at"] = _now()
    METRICS_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def snapshot() -> dict:
    """从结构化日志推导指标（跨进程/重启有效），并落盘快照。"""
    m = {k: 0 for k in _METRICS}
    m["llm_latency_s"] = 0.0
    if RUNS_FILE.exists():
        with _LOCK:
            lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = o.get("event")
            if ev == "message_received":
                m["messages"] += 1
            elif ev == "agent_reply":
                m["agent_runs"] += 1
            elif ev == "llm_call":
                m["llm_calls"] += 1
                m["llm_latency_s"] += float(o.get("latency_s") or 0)
            elif ev == "tool_call":
                m["tool_calls"] += 1
                if not o.get("ok", True):
                    m["tool_errors"] += 1
            elif ev == "error":
                m["errors"] += 1
    m["llm_latency_s"] = round(m["llm_latency_s"], 2)
    with _LOCK:
        for k, v in m.items():
            _METRICS[k] = v
        return _persist_metrics()


def render_metrics() -> str:
    m = snapshot()
    return (
        f"消息总数:        {m['messages']}\n"
        f"Agent 处理次数:  {m['agent_runs']}\n"
        f"LLM 调用:        {m['llm_calls']} 次，平均 {m['llm_avg_latency_s']}s\n"
        f"工具调用:        {m['tool_calls']} 次（失败 {m['tool_errors']}，"
        f"失败率 {m['tool_error_rate']:.1%}）\n"
        f"消息卡片:        {m['cards_sent']} 张\n"
        f"错误:            {m['errors']} 次\n"
        f"更新时间:        {m['updated_at']}")


def tail_runs(n: int = 20) -> List[dict]:
    if not RUNS_FILE.exists():
        return []
    with _LOCK:
        lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-n * 8:]:   # 粗略多读，保证凑够 n 个 run
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def get_run(run_id: str) -> List[dict]:
    if not RUNS_FILE.exists():
        return []
    with _LOCK:
        lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
    trace = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("run_id") == run_id:
            trace.append(obj)
    return trace


def reset():
    with _LOCK:
        for f in (RUNS_FILE, ERRORS_FILE, METRICS_FILE):
            if f.exists():
                f.unlink()
        for k in _METRICS:
            _METRICS[k] = 0
