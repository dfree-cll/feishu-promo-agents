# -*- coding: utf-8 -*-
"""
记忆系统：
- 会话记忆 ChatMemory：每个群的近期对话轮次（注入 LLM 上下文，支持"再上一笔"类指代）；
- 操作记忆 OpMemory：最近写入的费用申请（支持"刚才那笔"的召回）。

均持久化到 state/，带容量上限与线程锁；重启不丢失。
"""
import json
from datetime import datetime
from typing import List, Dict, Optional

from promo_agent import config
from promo_agent.locks import file_lock

CHAT_FILE = config.STATE_DIR / "memory_chat.json"
OPS_FILE = config.STATE_DIR / "memory_ops.json"
STATUS_HISTORY_FILE = config.STATE_DIR / "status_history.json"

MAX_TURNS_PER_CHAT = 20
MAX_OPS = 50
MAX_STATUS_HISTORY = 200


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


# ---------------------------------------------------------------- 会话记忆
def add_chat_turn(chat_id: str, role: str, content: str):
    chat_id = chat_id or "default"
    with file_lock(CHAT_FILE):
        store = _load(CHAT_FILE, {})
        turns = store.get(chat_id, [])
        turns.append({"ts": _now(), "role": role, "content": content[:800]})
        store[chat_id] = turns[-MAX_TURNS_PER_CHAT:]
        CHAT_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def get_recent_turns(chat_id: str, k: int = 6) -> List[Dict[str, str]]:
    """最近 k 轮对话，按时间正序返回 [{role, content}]（可直接拼进 LLM messages）。"""
    chat_id = chat_id or "default"
    with file_lock(CHAT_FILE):
        store = _load(CHAT_FILE, {})
        turns = store.get(chat_id, [])[-k:]
    return [{"role": t["role"], "content": t["content"]} for t in turns]


def clear_chat(chat_id: str):
    with file_lock(CHAT_FILE):
        store = _load(CHAT_FILE, {})
        store.pop(chat_id or "default", None)
        CHAT_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=1),
                             encoding="utf-8")


# ---------------------------------------------------------------- 状态变更历史（回滚依据）
def add_status_change(row_key: str, field: str, old, new):
    """记录一次状态字段变更；回滚 = 把 old 写回。"""
    with file_lock(STATUS_HISTORY_FILE):
        hist = _load(STATUS_HISTORY_FILE, [])
        hist.append({"ts": _now(), "row_key": row_key, "field": field,
                     "old": old, "new": new})
        STATUS_HISTORY_FILE.write_text(
            json.dumps(hist[-MAX_STATUS_HISTORY:], ensure_ascii=False, indent=1),
            encoding="utf-8")


def last_status_change(row_key: str, field: str = "") -> Optional[Dict]:
    """最近一次该行（可限字段）的状态变更；没有返回 None。"""
    with file_lock(STATUS_HISTORY_FILE):
        hist = _load(STATUS_HISTORY_FILE, [])
    for entry in reversed(hist):
        if entry.get("row_key") != row_key:
            continue
        if field and entry.get("field") != field:
            continue
        return entry
    return None


# ---------------------------------------------------------------- 操作记忆
def add_application(entry: Dict):
    """记录一笔写入成功的申请：{song, blogger, app_type, amount, tx_count, payer, record_id}。"""
    with file_lock(OPS_FILE):
        ops = _load(OPS_FILE, [])
        ops.append({"ts": _now(), **entry})
        OPS_FILE.write_text(json.dumps(ops[-MAX_OPS:], ensure_ascii=False, indent=1),
                            encoding="utf-8")


def recent_applications(n: int = 5) -> List[Dict]:
    with file_lock(OPS_FILE):
        ops = _load(OPS_FILE, [])
    return ops[-n:]


def find_application(blogger: str = "", song: str = "") -> Optional[Dict]:
    """按博主/歌曲从最近的申请往前找，返回最新一条匹配。"""
    blogger = (blogger or "").strip()
    song = (song or "").strip()
    with file_lock(OPS_FILE):
        ops = _load(OPS_FILE, [])
    for op in reversed(ops):
        if blogger and op.get("blogger") != blogger:
            continue
        if song and op.get("song") != song:
            continue
        return op
    return None
