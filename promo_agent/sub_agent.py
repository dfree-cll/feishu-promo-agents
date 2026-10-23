# -*- coding: utf-8 -*-
"""
Sub-Agent「推广助手」——真正的 LLM Agent：
- 大脑：LLM + function-calling 循环（llm_client.run_tool_loop），控制流由模型决定；
- 手：write_doujia / write_dianfu / lookup_promo_table / notify_master /
      update_order_status / mark_paid / ask_master；
- 参数安全：LLM 生成的工具参数一律先经过 validators 白名单校验才落库；
- 结构化上报：模型按协议构造 SummaryEvent JSON 上交 Master（题目题眼）；
- 兜底：未配置 LLM 时保留原规则解析管道（ingest_text）。
"""
import json
import threading
from datetime import date as _date
from typing import List

from promo_agent import config
from promo_agent.agent_tools import SUB_TOOLS
from promo_agent.feishu_client import FeishuClient
from promo_agent.extractor import parse_any
from promo_agent.models import ApplicationRecord, SummaryEvent
from promo_agent.table_schema import PROMO_TABLE, PROMO_FIELDS, promo_record_fields

EVENT_BUS = config.STATE_DIR / "events.jsonl"      # Sub -> Master 的结构化事件通道
TODAY = _date.today().isoformat()

_EVENT_LOCK = threading.Lock()   # 多消息并发时保护事件总线文件追加

import threading

_MASTER_LOCK = threading.Lock()


def _run_master_async(client: FeishuClient):
    """Master 聚合/日报转后台线程执行：用户不等它，绿色日报卡片稍后自动出现。"""
    def _job():
        with _MASTER_LOCK:
            try:
                from promo_agent.master_agent import MasterAgent
                MasterAgent(client).run(send_report=True)
            except Exception as e:
                print(f"[Master-Agent] 后台汇总失败：{e}")
    threading.Thread(target=_job, daemon=True, name="master-summary").start()

SUB_SYSTEM_PROMPT = (
    "你是音乐宣推团队的「推广助手」（Sub-Agent），常驻公司【推广群】。\n"
    f"今天是 {TODAY}。\n"
    "员工会用各种口语化方式提交「抖加」或「垫付（付款）」申请，也可能询问歌曲推广情况，\n"
    "或反馈达人接单/打款的状态变化。\n"
    "你的工作准则：\n"
    "1. 每笔申请调用一次 submit_application（app_type 区分抖加/垫付），写入与上报自动完成；\n"
    "   多笔申请在同一轮并行多次调用本工具，能显著提速；\n"
    "2. 金额、博主名等关键信息缺失或有歧义时，先向对方追问确认，不要猜；\n"
    "   注意角色区分：抖加申请填 doujia_payer（支付人），垫付申请填 dianfu_payer（打款人），不要混填；\n"
    "3. 涉及总花费/接单/打款等汇总类问题，调用 ask_master 转交上级回答，不要自己估算；\n"
    "4. 员工反馈达人接单/拒接时调用 update_order_status；反馈钱已打款时调用 mark_paid；\n"
    "5. 写入前如担心重复，可先调用 lookup_promo_table 查重（名单内达人会带报价/抖音号）；\n"
    "6. 业务概念/流程不确定时调用 search_knowledge 检索知识库；\n"
    "7. 最终回复用简体中文，简洁汇报你做了什么、结果如何。"
)

try:
    from promo_agent import knowledge as _kb
    _KB_DIGEST = _kb.digest()
    if _KB_DIGEST:
        SUB_SYSTEM_PROMPT += (
            f"\n\n【团队知识库摘要】{_KB_DIGEST}。\n"
            "以上概念的细节可调用 search_knowledge 检索知识库后回答。")
except Exception:
    _KB_DIGEST = ""


class SubAgent:
    name = config.SUB_AGENT_NAME

    def __init__(self, client: FeishuClient = None):
        self.client = client or FeishuClient()

    # ---------- Agent 路径：LLM 工具循环 ----------
    def handle_text(self, text: str, chat_id: str = "") -> str:
        """任意口语化消息 -> 会话记忆 + LLM 理解 -> 自主调用工具 -> 自然语言回复。"""
        import time as _time
        from promo_agent.llm_client import run_tool_loop, llm_ready
        from promo_agent import memory, observability as obs
        if not llm_ready():
            return ("（未配置 LLM Key，Agent 路径不可用。请在 .env 填写 "
                    "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，或先用规则兜底：python -m main demo）")
        run_id = obs.new_run_id("group" if chat_id else "cli")
        obs.set_current_run(run_id)
        obs.log_event("agent_run_start", run_id=run_id, message=text[:120])
        t0 = _time.time()
        history = memory.get_recent_turns(chat_id, 6)
        if history:
            obs.log_event("memory_context", run_id=run_id, turns=len(history))
        messages = [{"role": "system", "content": SUB_SYSTEM_PROMPT}]
        messages += history
        messages.append({"role": "user", "content": text})
        reply = run_tool_loop(
            messages, SUB_TOOLS, SubToolExecutor(self).run_tool,
            answer_key="answer_text",   # 确认/转交类回复由代码直出，省一轮组稿
            on_step=lambda fn, a: print(f"[{self.name}] 🔧 工具调用 {fn} "
                                        + json.dumps(a, ensure_ascii=False)))
        memory.add_chat_turn(chat_id, "user", text)
        memory.add_chat_turn(chat_id, "assistant", reply)
        elapsed = _time.time() - t0
        obs.observe_agent_run(run_id, elapsed)
        obs.log_event("agent_reply", run_id=run_id, reply=reply[:200])
        print(f"[{self.name}] ⏱ 本条消息处理用时 {elapsed:.1f}s（run_id={run_id}）")
        return reply

    # ---------- 事件上报（工具 notify_master 的底层实现）----------
    def notify_master(self, ev: SummaryEvent):
        with _EVENT_LOCK:
            with open(EVENT_BUS, "a", encoding="utf-8") as f:
                f.write(json.dumps(json.loads(ev.to_json()), ensure_ascii=False) + "\n")
        print(f"[{self.name}] 📨 已向 Master-Agent 发送结构化事件 {ev.event_id} "
              f"(事件总线: {EVENT_BUS.name})")

    # ---------- 兜底路径：规则解析（离线/无 LLM 时可用）----------
    def ingest_text(self, text: str, notify_master: bool = True, table_url: str = "") -> List[SummaryEvent]:
        events, errors = parse_any(text)
        for err in errors:
            print(f"[{self.name}] ⚠️ {err}")

        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(
            app_token, PROMO_TABLE, PROMO_FIELDS)

        for ev in events:
            written = 0
            for rec in ev.records:
                fields = promo_record_fields(rec.to_dict())
                rid = self.client.upsert_record(
                    app_token, table_id, "行键", rec.row_key, fields)
                written += 1
                print(f"[{self.name}] ✅ 已写入《推广账号表》 {rec.blogger} "
                      f"({rec.amount:g}元/{rec.tx_count}笔) -> {rid}")
            self._card_to_group(ev, written, table_url)
            if notify_master:
                self.notify_master(ev)

        return events

    def _card_to_group(self, ev: SummaryEvent, written: int, table_url: str = ""):
        label = "抖加" if ev.event_type == "doujia_summary" else "垫付"
        role_label = "支付人" if ev.event_type == "doujia_summary" else "打款人"
        lines = [f"- {r.blogger}：¥{r.amount:g}" + (f"（分 {r.tx_count} 笔）" if r.tx_count > 1 else "")
                 for r in ev.records]
        warn = ("\n⚠️ **核对提醒**\n" + "\n".join(f"- {w}" for w in ev.warnings)) if ev.warnings else ""
        link = f"\n\n📊 [点此打开《推广账号表》]({table_url})" if table_url else ""
        self.client.send_card(
            f"【{self.name}】{label}申请已入表 {ev.summary_date}",
            f"**歌曲**：《{ev.records[0].song}》　**{role_label}**：{ev.payer or '—'}\n\n"
            f"**明细（已写入《推广账号表》{written} 行）**\n" + "\n".join(lines) +
            f"\n\n**明细口径**：¥{ev.detail_total:g} / {ev.detail_count} 笔（合并 {ev.merged_count} 位博主）"
            + warn + link,
            template="blue" if label == "抖加" else "orange")


class SubToolExecutor:
    """LLM 工具调用入口：参数先过 validators 白名单，再以显式命名参数分派。"""

    def __init__(self, agent: SubAgent):
        self.agent = agent
        self.client = agent.client

    def run_tool(self, tool_name: str, params: dict) -> dict:
        from promo_agent import validators as v
        try:
            if tool_name == "submit_application":
                return self.submit_application(**v.validate_submit_args(params, TODAY))
            if tool_name == "lookup_promo_table":
                return self.client.fetch_promo_rows(
                    song=str(params.get("song") or ""),
                    blogger=str(params.get("blogger") or ""))
            if tool_name == "update_order_status":
                out = self.update_order_status(
                    song=v.clean_text(params, "song"),
                    blogger=v.clean_text(params, "blogger"),
                    status=v.validate_order_args(params)["status"])
                if out.get("status") == "ok":
                    song, blogger = out["row_key"].split("|")
                    out["answer_text"] = f"已将《{song}》的「{blogger}」接单状态更新为：{out['order_status']}。"
                return out
            if tool_name == "mark_paid":
                clean = v.validate_paid_args(params, TODAY)
                out = self.mark_paid(
                    song=v.clean_text(params, "song"),
                    blogger=v.clean_text(params, "blogger"),
                    paid_date=clean["paid_date"])
                if out.get("status") == "ok":
                    song, blogger = out["row_key"].split("|")
                    out["answer_text"] = (f"已将《{song}》的「{blogger}」标记为已打款"
                                          f"（实际打款日期：{out['paid_date']}）。")
                return out
            if tool_name == "ask_master":
                from promo_agent.master_agent import handle_question
                reply = handle_question(v.clean_text(params, "question", 500))
                return {"master_reply": reply, "answer_text": reply}
            if tool_name == "search_knowledge":
                from promo_agent import knowledge
                return {"results": knowledge.search(
                    str(params.get("query") or ""), int(params.get("top_k") or 3))}
            if tool_name == "recall_recent_applications":
                from promo_agent import memory
                return {"recent_applications": memory.recent_applications(
                    max(1, min(int(params.get("n") or 5), 20)))}
        except ValueError as err:
            return {"error": "参数校验失败: " + str(err)}
        return {"error": "不支持的工具: " + str(tool_name)[:40]}

    def write_promo(self, app_type: str, song: str, blogger: str, amount: float,
                    tx_count: int, pay_date: str, payer: str,
                    expected_pay_date: str = "") -> dict:
        rec = ApplicationRecord(song=song, blogger=blogger, app_type=app_type,
                                amount=amount, tx_count=tx_count,
                                summary_date=pay_date, payer=payer,
                                expected_pay_date=expected_pay_date)
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid = self.client.upsert_record(app_token, table_id, "行键",
                                        rec.row_key, promo_record_fields(rec.to_dict()))
        return {"status": "ok", "type": app_type, "row_key": rec.row_key,
                "amount": rec.amount, "tx_count": rec.tx_count, "record_id": rid}

    def update_order_status(self, song: str, blogger: str, status: str) -> dict:
        key = song + "|" + blogger
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid, _old = self.client.find_record(app_token, table_id, "行键", key)
        if rid is None:
            return {"error": "《推广账号表》中没有 " + key + " 的申请记录，请先提交申请"}
        self.client.upsert_record(app_token, table_id, "行键", key,
                                  {"行键": key, "是否接单": status})
        return {"status": "ok", "row_key": key, "order_status": status}

    def mark_paid(self, song: str, blogger: str, paid_date: str) -> dict:
        key = song + "|" + blogger
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid, _old = self.client.find_record(app_token, table_id, "行键", key)
        if rid is None:
            return {"error": "《推广账号表》中没有 " + key + " 的申请记录，请先提交申请"}
        from datetime import datetime as _dt
        ts = int(_dt.strptime(paid_date, "%Y-%m-%d").timestamp() * 1000)
        self.client.upsert_record(app_token, table_id, "行键", key,
                                  {"行键": key, "是否打款": "已打款", "实际打款日期": ts})
        return {"status": "ok", "row_key": key, "paid": True, "paid_date": paid_date}

    def submit_application(self, app_type: str, song: str, blogger: str, amount: float,
                           tx_count: int, payer: str, date: str,
                           expected_pay_date: str = "", stated_total: float = 0.0,
                           stated_count: int = 0) -> dict:
        """一笔申请一步完成：写入《推广账号表》+ 结构化事件上报 Master（后台聚合）。"""
        rec = ApplicationRecord(song=song, blogger=blogger, app_type=app_type,
                                amount=amount, tx_count=tx_count,
                                summary_date=date, payer=payer,
                                expected_pay_date=expected_pay_date)
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid = self.client.upsert_record(app_token, table_id, "行键",
                                        rec.row_key, promo_record_fields(rec.to_dict()))
        label = "抖加" if app_type == "doujia" else "垫付"
        role_label = "支付人" if app_type == "doujia" else "打款人"
        ev = SummaryEvent(
            event_type=app_type + "_summary", summary_date=date,
            records=[rec], stated_total=stated_total, stated_count=stated_count,
            payer=payer, expected_pay_date=expected_pay_date,
        )
        self.agent.notify_master(ev)
        _run_master_async(self.client)   # 聚合/日报转后台，不阻塞回复
        # 写入操作记忆：支持“刚才那笔”类召回与后续修改
        from promo_agent import memory
        memory.add_application({
            "song": song, "blogger": blogger, "app_type": app_type,
            "amount": amount, "tx_count": tx_count, "payer": payer,
            "date": date, "record_id": rid,
        })
        return {"status": "ok", "type": app_type, "row_key": rec.row_key,
                "amount": rec.amount, "tx_count": rec.tx_count, "record_id": rid,
                "answer_text": (f"已为《{song}》的「{blogger}」记录{label}申请"
                                f"¥{amount:g}（{tx_count} 笔），{role_label}：{payer}，"
                                f"并已上报 Master-Agent 汇总，推广日报稍后推送。")}


def handle_text_wrap(text: str, chat_id: str = "") -> str:
    """模块级便捷入口：默认客户端跑一轮 Sub-Agent。"""
    return SubAgent().handle_text(text, chat_id)
