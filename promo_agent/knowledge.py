# -*- coding: utf-8 -*-
"""
知识库：加载 data/knowledge/*.md，按标题分块，提供轻量关键词检索。

- 零外部依赖（无向量库）：字符二元组 + 词组命中率评分，对术语类查询足够；
- 热更新：文件内容变化时自动重新加载（按 mtime 判断）；
- 用途：注入 Agent 系统提示词（digest）+ 作为 LLM 工具 search_knowledge 的后端。
"""
import re
import threading
from pathlib import Path
from typing import List, Dict

from promo_agent import config

KB_DIR = config.DATA_DIR / "knowledge"
_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {"mtime": None, "chunks": []}


def _load() -> List[Dict]:
    kb_dir = Path(KB_DIR)
    files = sorted(kb_dir.glob("*.md")) if kb_dir.exists() else []
    mtime = max((f.stat().st_mtime for f in files), default=0)
    with _LOCK:
        if _CACHE["mtime"] == mtime and _CACHE["chunks"]:
            return _CACHE["chunks"]
        chunks: List[Dict] = []
        for md in files:
            text = md.read_text(encoding="utf-8")
            # 按 "## " 标题分块；文件开头（# 主标题+导语）单独成块
            parts = re.split(r"\n(?=## )", "\n" + text)
            for part in parts:
                part = part.strip()
                if len(part) < 4:
                    continue
                first_line = next((l for l in part.splitlines()
                                   if l.strip() and not l.startswith("#")), "")
                chunks.append({
                    "title": first_line[:40] or md.stem,
                    "text": part[:600],
                    "source": md.stem,
                })
        _CACHE["mtime"], _CACHE["chunks"] = mtime, chunks
        return chunks


def _tokens(query: str) -> List[str]:
    """词组 + 中文字符二元组，兼顾英文词与中文术语匹配。"""
    q = query.strip()
    toks = [t for t in re.split(r"[\s，。？?！!、：:（）()]+", q) if len(t) >= 2]
    han = re.findall(r"[\u4e00-\u9fff]", q)
    toks += [han[i] + han[i + 1] for i in range(len(han) - 1)]
    return list(dict.fromkeys(toks))


def search(query: str, top_k: int = 3) -> List[Dict]:
    """按关键词命中率检索知识块，返回 [{title, text, source, score}]。"""
    chunks = _load()
    toks = _tokens(query)
    if not toks or not chunks:
        return []
    scored = []
    for c in chunks:
        hay = c["title"] + "\n" + c["text"]
        score = sum(hay.count(t) for t in toks)
        if c["title"] and any(t in c["title"] for t in toks):
            score += 3          # 标题命中加权
        if score > 0:
            scored.append({**c, "score": score})
    scored.sort(key=lambda x: -x["score"])
    return [{"title": c["title"], "text": c["text"], "source": c["source"]}
            for c in scored[:top_k]]


def digest(max_chars: int = 900) -> str:
    """知识库摘要（每个知识块的标题清单），用于注入系统提示词。"""
    chunks = _load()
    titles = list(dict.fromkeys(c["title"] for c in chunks))
    out = "；".join(titles)
    return out[:max_chars]
