# -*- coding: utf-8 -*-
"""
LLM 客户端与通用 tool-calling 循环（OpenAI 兼容协议，requests 直连，无额外依赖）。

Agent 的“大脑”：模型看到工具清单 -> 自己决定调用哪个工具与参数 -> 拿到工具结果继续思考
-> 直到给出最终自然语言回答。这就是 Agent 与脚本的本质区别：控制流由模型决定。
"""
import json
import threading
import time
from typing import Callable, List, Optional

import requests

from promo_agent import config
from promo_agent import observability as obs

_HTTP = requests.Session()   # 连接复用，降低每轮调用的握手开销
_LLM_SEM = threading.Semaphore(max(1, config.MAX_CONCURRENT_LLM))   # 并发上限，防突发限流


class LLMError(RuntimeError):
    pass


def llm_ready() -> bool:
    return bool(config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL)


def chat(messages: List[dict], tools: Optional[list] = None, temperature: float = 0.2) -> dict:
    payload = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "auto"
    t0 = time.time()
    with _LLM_SEM:   # 高并发下限流保护：超过上限的调用排队等待
        r = _HTTP.post(
            config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise LLMError(f"LLM 返回非 JSON（{r.status_code}）：{r.text[:200]}")
    if r.status_code != 200 or "choices" not in data:
        raise LLMError(f"LLM 调用失败（{r.status_code}）：{json.dumps(data, ensure_ascii=False)[:300]}")
    msg = data["choices"][0]["message"]
    obs.observe_llm(time.time() - t0, len(msg.get("tool_calls") or []))
    return msg


def run_tool_loop(messages: List[dict], tools: List[dict],
                  executor: Callable[[str, dict], dict],
                  max_turns: int = 8,
                  on_step: Optional[Callable[[str, dict], None]] = None,
                  answer_key: Optional[str] = None) -> str:
    """
    通用 Agent 循环：模型 -> tool_calls -> 执行 -> 结果回填 -> 模型 … 直到产出最终回答。
    executor(tool_name, args) 由上层提供，把工具名映射到具体函数。
    answer_key：若工具结果中带该字段（如代码预渲染的 answer_text），
    则跳过模型续写、直接作为最终回答——数字与姓名由代码保证，模型只负责决策。
    """
    messages = list(messages)
    for _ in range(max_turns):
        msg = chat(messages, tools)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or ""
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_step:
                on_step(fn, args)
            t0 = time.time()
            try:
                result = executor(fn, args)
                ok = not (isinstance(result, dict) and result.get("error"))
            except Exception as e:  # 工具报错也回填给模型，让它自己决定重试或追问
                result = {"error": f"{type(e).__name__}: {e}"}
                ok = False
            obs.observe_tool(fn, time.time() - t0, ok)
            if answer_key and isinstance(result, dict) and result.get(answer_key):
                return str(result[answer_key])
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
    return "（已达到最大思考轮数仍未完成，请稍后重试或简化你的请求）"
