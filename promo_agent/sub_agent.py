# -*- coding: utf-8 -*-
"""
Sub-Agent「推广助手」——真正的 LLM Agent：
- 大脑：LLM + function-calling 循环（llm_client.run_tool_loop），控制流由模型决定；
- 手：submit_application（写台账 + 结构化上报一步完成）/ lookup_promo_table /
      update_order_status / mark_paid / review_application / rollback_last_change /
      recall_recent_applications / search_knowledge / ask_master；
- 参数安全：LLM 生成的工具参数一律先经过 validators 白名单校验才落库；
- 结构化上报：模型按协议构造 SummaryEvent JSON 上交 Master（题目题眼）；
- 兜底：未配置 LLM 时保留原规则解析管道（ingest_text）。
"""
import json
import threading
from typing import List, Optional

from promo_agent import config
from promo_agent import memory
from promo_agent.agent_tools import SUB_TOOLS
from promo_agent.feishu_client import FeishuClient
from promo_agent.extractor import parse_any
from promo_agent.models import ApplicationRecord, SummaryEvent, business_today
from promo_agent.table_schema import (PROMO_TABLE, PROMO_FIELDS,
                                      additive_amount_fields,
                                      promo_new_row_defaults, promo_record_fields)

EVENT_BUS = config.STATE_DIR / "events.jsonl"      # Sub -> Master 的结构化事件通道

_EVENT_LOCK = threading.Lock()   # 多消息并发时保护事件总线文件追加


def _event_exists(event_id: str) -> bool:
    """事件总线上是否已存在该幂等键。

    写台账**之前**必须先查：台账金额是累加的，一旦同一笔被处理两次就会翻倍，
    而日报按事件去重只算一次 → 两本账分叉。这里以事件总线为准做判重
    （事件是先算好 event_id 再上报的，天然就是"已应用"的凭据）。
    """
    if not event_id or not EVENT_BUS.exists():
        return False
    try:
        lines = EVENT_BUS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            if json.loads(line).get("event_id") == event_id:
                return True
        except json.JSONDecodeError:
            continue
    return False

# ---------------------------------------------------------------- 后台聚合：单例线程 + 去抖
# 原实现是「每笔申请起一个线程各跑一次全量重算」：突发多笔时会堆线程，
# 且 N 次 run() 各自重算同一批事件（O(n²)）。改为一个常驻工作线程，
# 申请只负责置位；线程等 0.4s 把这一波申请合并成一次聚合。
_SUMMARY_WAKE = threading.Event()
_SUMMARY_LOCK = threading.Lock()
_SUMMARY_WORKER = None
_DEBOUNCE_SECONDS = 0.4


def _summary_worker(client: FeishuClient):
    import time as _time
    while True:
        _SUMMARY_WAKE.wait()
        _SUMMARY_WAKE.clear()
        _time.sleep(_DEBOUNCE_SECONDS)      # 去抖窗口：让同一波申请先全部写进事件总线
        try:
            from promo_agent.master_agent import MasterAgent
            MasterAgent(client).run(send_report=True)
        except Exception as e:              # 后台任务不能把工作线程打死
            print(f"[Master-Agent] 后台汇总失败：{e}")


def _run_master_async(client: FeishuClient):
    """通知后台聚合（不阻塞回复）。工作线程单例常驻，多次调用合并成一次 run。"""
    global _SUMMARY_WORKER
    with _SUMMARY_LOCK:
        if _SUMMARY_WORKER is None or not _SUMMARY_WORKER.is_alive():
            _SUMMARY_WORKER = threading.Thread(target=_summary_worker, args=(client,),
                                               daemon=True, name="master-summary")
            _SUMMARY_WORKER.start()
    _SUMMARY_WAKE.set()

SUB_SYSTEM_PROMPT = (
    "你是音乐宣推团队的「推广助手」（Sub-Agent），常驻公司【推广群】。\n"
    "截账规则：每天 23:00 日终截账，之后提交的申请计入下一业务日"
    "（当前业务日以系统提示为准）。\n"
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
    def handle_text(self, text: str, chat_id: str = "", sender: str = "",
                    msg_time: str = "") -> str:
        """任意口语化消息 -> 会话记忆 + LLM 理解 -> 自主调用工具 -> 自然语言回复。

        msg_time：消息采集时间（飞书事件里的发送时间），用于业务日归属与台账留痕。
        """
        import time as _time
        from datetime import datetime as _dt
        from promo_agent.llm_client import run_tool_loop, llm_ready
        from promo_agent import memory, observability as obs
        if not llm_ready():
            return ("（未配置 LLM Key，Agent 路径不可用。请在 .env 填写 "
                    "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，或先用规则兜底：python -m main demo）")
        run_id = obs.new_run_id("group" if chat_id else "cli")
        obs.set_current_run(run_id)
        obs.log_event("agent_run_start", run_id=run_id, message=text[:120], sender=sender)
        t0 = _time.time()
        try:
            biz_day = business_today(_dt.fromisoformat(msg_time)) if msg_time \
                else business_today()
        except ValueError:
            biz_day = business_today()
        history = memory.get_recent_turns(chat_id, 6)
        if history:
            obs.log_event("memory_context", run_id=run_id, turns=len(history))
        messages = [{"role": "system",
                     "content": SUB_SYSTEM_PROMPT + f"\n【当前业务日】{biz_day}"
                               "（23:00 后提交的申请计入下一业务日）"}]
        messages += history
        messages.append({"role": "user", "content": text})
        executor = SubToolExecutor(self).bind(chat_id=chat_id, sender=sender,
                                              msg_time=msg_time)
        reply = run_tool_loop(
            messages, SUB_TOOLS, executor.run_tool,
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
    def ingest_text(self, text: str, notify_master: bool = True, table_url: str = "",
                    msg_time: str = "", message_id: str = "") -> List[SummaryEvent]:
        """规则路径摄入。message_id 用于派生消息级幂等键：同一条消息重复投递只算一次。"""
        events, errors = parse_any(text, message_id=message_id)
        for err in errors:
            print(f"[{self.name}] ⚠️ {err}")

        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(
            app_token, PROMO_TABLE, PROMO_FIELDS)

        today = business_today()
        for ev in events:
            # 规则路径兜底：总结文本没写日期时按当前业务日归属，
            # 否则 summary_date 为空会让这些申请永远进不了任何一天的日报。
            if not ev.summary_date:
                ev.summary_date = today
                ev.__post_init__()   # 日期变了 —— 按新内容重算确定性 event_id
            if _event_exists(ev.event_id):
                # 幂等：这段申请已经处理过（消息重推 / 同一条消息被重复分发），
                # 不能再写台账——台账是累加语义，重复写会翻倍。
                print(f"[{self.name}] ♻️ 该申请已处理过（幂等键 {ev.event_id}），跳过重复写入")
                continue
            written = 0
            for rec in ev.records:
                rec.summary_date = rec.summary_date or ev.summary_date
                if msg_time:
                    rec.submitted_at = msg_time   # 群消息采集时间落库（提交时间列）
                fields = promo_record_fields(rec.to_dict())
                if not config.REVIEW_ENABLED:
                    fields["审核状态"] = "已批准"
                rid = self.client.upsert_record(
                    app_token, table_id, "行键", rec.row_key, fields,
                    create_defaults=promo_new_row_defaults(rec.app_type),
                    additive_fields=additive_amount_fields(rec.app_type))
                from promo_agent import memory
                memory.add_status_change(rec.row_key, "审核状态", "", fields["审核状态"])
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
    """LLM 工具调用入口：参数先过 validators 白名单，再以显式命名参数分派。

    bind(chat_id, sender)：chat_id 用于审批卡回发；sender 用于审核/回滚类
    管理者指令的身份校验（配合 config.APPROVER_OPEN_IDS）。
    """

    def __init__(self, agent: SubAgent):
        self.agent = agent
        self.client = agent.client
        self.chat_id = ""
        self.sender = ""
        self.msg_time = ""

    def bind(self, chat_id: str = "", sender: str = "", msg_time: str = "") -> "SubToolExecutor":
        self.chat_id = chat_id or self.chat_id
        self.sender = sender or self.sender
        self.msg_time = msg_time or self.msg_time
        return self

    def _require_manager(self) -> Optional[dict]:
        """审核/回滚属管理者指令：配置了 APPROVER_OPEN_IDS 时校验发送者身份。

        fail-closed：白名单非空但取不到发送者身份（如事件解析失败）时一律拒绝，
        避免“解析不到 open_id 就放行”导致的越权。白名单留空 = 演示模式，不校验。
        """
        if config.APPROVER_OPEN_IDS and (not self.sender or self.sender not in config.APPROVER_OPEN_IDS):
            return {"error": "只有管理者可以执行审核/回滚操作。"}
        return None

    def run_tool(self, tool_name: str, params: dict) -> dict:
        from promo_agent import validators as v
        try:
            if tool_name == "submit_application":
                params = {**params, "submitted_at": self.msg_time}   # 采集时间由系统注入
                clean = v.validate_submit_args(params, business_today())
                return self.submit_application(**clean)
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
                clean = v.validate_paid_args(params, business_today())
                out = self.mark_paid(
                    song=v.clean_text(params, "song"),
                    blogger=v.clean_text(params, "blogger"),
                    paid_date=clean["paid_date"])
                if out.get("status") == "ok":
                    song, blogger = out["row_key"].split("|")
                    out["answer_text"] = (f"已将《{song}》的「{blogger}」标记为已打款"
                                          f"（实际打款日期：{out['paid_date']}）。")
                return out
            if tool_name == "review_application":
                denied = self._require_manager()
                if denied:
                    return denied
                clean = v.validate_review_args(params)
                return self.review_application(
                    song=v.clean_text(params, "song"),
                    blogger=v.clean_text(params, "blogger"),
                    decision=clean["decision"], reason=clean["reason"])
            if tool_name == "rollback_last_change":
                denied = self._require_manager()
                if denied:
                    return denied
                clean = v.validate_rollback_args(params)
                return self.rollback_last_change(
                    song=v.clean_text(params, "song"),
                    blogger=v.clean_text(params, "blogger"),
                    field=clean["field"])
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
                                        rec.row_key, promo_record_fields(rec.to_dict()),
                                        create_defaults=promo_new_row_defaults(rec.app_type),
                                        additive_fields=additive_amount_fields(rec.app_type))
        return {"status": "ok", "type": app_type, "row_key": rec.row_key,
                "amount": rec.amount, "tx_count": rec.tx_count, "record_id": rid}

    def update_order_status(self, song: str, blogger: str, status: str) -> dict:
        key = song + "|" + blogger
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid, old = self.client.find_record(app_token, table_id, "行键", key)
        if rid is None:
            return {"error": "《推广账号表》中没有 " + key + " 的申请记录，请先提交申请"}
        old_status = self.client._to_write_format((old or {}).get("是否接单", "")) or "待确认"
        self.client.upsert_record(app_token, table_id, "行键", key,
                                  {"行键": key, "是否接单": status})
        from promo_agent import memory
        memory.add_status_change(key, "是否接单", old_status, status)
        return {"status": "ok", "row_key": key, "order_status": status}

    def mark_paid(self, song: str, blogger: str, paid_date: str) -> dict:
        key = song + "|" + blogger
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid, old = self.client.find_record(app_token, table_id, "行键", key)
        if rid is None:
            return {"error": "《推广账号表》中没有 " + key + " 的申请记录，请先提交申请"}
        from datetime import datetime as _dt
        ts = int(_dt.strptime(paid_date, "%Y-%m-%d").timestamp() * 1000)
        old_pay = self.client._to_write_format((old or {}).get("是否打款", "")) or "待打款"
        self.client.upsert_record(app_token, table_id, "行键", key,
                                  {"行键": key, "是否打款": "已打款", "实际打款日期": ts})
        from promo_agent import memory
        memory.add_status_change(key, "是否打款", old_pay, "已打款")
        return {"status": "ok", "row_key": key, "paid": True, "paid_date": paid_date}

    def submit_application(self, app_type: str, song: str, blogger: str, amount: float,
                           tx_count: int, payer: str, date: str,
                           expected_pay_date: str = "", stated_total: float = 0.0,
                           stated_count: int = 0, submitted_at: str = "") -> dict:
        """一笔申请一步完成：写入《推广账号表》+ 结构化事件上报 Master（后台聚合）。"""
        rec = ApplicationRecord(song=song, blogger=blogger, app_type=app_type,
                                amount=amount, tx_count=tx_count,
                                summary_date=date, payer=payer, submitted_at=submitted_at,
                                expected_pay_date=expected_pay_date)
        # 先构造事件拿到幂等键（内容哈希）：模型若因错误重试同一次提交，
        # 内容一致 → event_id 一致 → 这里能拦住，避免台账重复累加。
        ev = SummaryEvent(
            event_type=app_type + "_summary", summary_date=date,
            records=[rec], stated_total=stated_total, stated_count=stated_count,
            payer=payer, expected_pay_date=expected_pay_date,
        )
        if _event_exists(ev.event_id):
            return {
                "status": "duplicate", "duplicate": True, "row_key": rec.row_key,
                "answer_text": (f"《{song}》的「{blogger}」这笔 ¥{amount:g} 申请之前已经记录过了，"
                                f"本次没有重复写入（幂等）。"),
            }
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        fields = promo_record_fields(rec.to_dict())
        review_on = config.REVIEW_ENABLED
        fields["审核状态"] = "待审核" if review_on else "已批准"
        rid = self.client.upsert_record(app_token, table_id, "行键",
                                        rec.row_key, fields,
                                        create_defaults=promo_new_row_defaults(rec.app_type),
                                        additive_fields=additive_amount_fields(rec.app_type))
        memory.add_status_change(rec.row_key, "审核状态", "", fields["审核状态"])
        label = "抖加" if app_type == "doujia" else "垫付"
        role_label = "支付人" if app_type == "doujia" else "打款人"
        self.agent.notify_master(ev)
        _run_master_async(self.client)   # 聚合/日报转后台，不阻塞回复
        # 审批请求卡：仅人工审核流开启时发送
        if review_on:
            self.client.send_card(
                f"🔔 待审核：{label}申请「{blogger}」",
                f"**歌曲**：《{song}》　**博主**：{blogger}\n"
                f"**金额**：¥{amount:g}（{tx_count} 笔）　**{role_label}**：{payer}\n"
                + (f"**预计打款日期**：{expected_pay_date}\n" if expected_pay_date else "")
                + "\n管理者请回复：**同意 " + blogger + "** 或 **驳回 " + blogger + " [原因]**",
                template="yellow", to_chat_id=self.chat_id)
        # 写入操作记忆：支持“刚才那笔”类召回与后续修改
        memory.add_application({
            "song": song, "blogger": blogger, "app_type": app_type,
            "amount": amount, "tx_count": tx_count, "payer": payer,
            "date": date, "record_id": rid,
        })
        review_note = ("申请已进入待审核状态，等管理者确认后正式生效。"
                       if review_on else "申请已直接生效（人工审核流已关闭）。")
        return {"status": "ok", "review": fields["审核状态"], "type": app_type,
                "row_key": rec.row_key,
                "amount": rec.amount, "tx_count": rec.tx_count, "record_id": rid,
                "answer_text": (f"已为《{song}》的「{blogger}」记录{label}申请"
                                f"¥{amount:g}（{tx_count} 笔），{role_label}：{payer}。"
                                f"{review_note}")}

    def review_application(self, song: str, blogger: str, decision: str,
                           reason: str = "") -> dict:
        key = song + "|" + blogger
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        rid, old = self.client.find_record(app_token, table_id, "行键", key)
        if rid is None:
            return {"error": "《推广账号表》中没有 " + key + " 的申请记录"}
        old_status = self.client._to_write_format((old or {}).get("审核状态", "")) or "待审核"
        fields = {"行键": key, "审核状态": decision}
        if reason:
            fields["审核备注"] = reason
        self.client.upsert_record(app_token, table_id, "行键", key, fields)
        memory.add_status_change(key, "审核状态", old_status, decision)
        template = "green" if decision == "已批准" else "red"
        self.client.send_card(
            f"【审核结果】{key}",
            f"「{blogger}」的申请已由管理者**{decision}**"
            + (f"（原因：{reason}）" if reason else ""),
            template=template, to_chat_id=self.chat_id)
        return {"status": "ok", "row_key": key, "review": decision,
                "previous": old_status}

    def rollback_last_change(self, song: str, blogger: str, field: str = "") -> dict:
        key = song + "|" + blogger
        entry = memory.last_status_change(key, field)
        if not entry:
            return {"error": "没有找到 " + key + " 可回滚的状态变更记录"}
        field_name = entry["field"]
        restore = entry["old"]
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        self.client.upsert_record(app_token, table_id, "行键", key,
                                  {"行键": key, field_name: restore})
        memory.add_status_change(key, field_name, entry["new"],
                                 f"（回滚自 {entry['ts']}）{restore}")
        return {"status": "ok", "row_key": key, "field": field_name,
                "restored": restore, "changed_at": entry["ts"]}


def handle_text_wrap(text: str, chat_id: str = "") -> str:
    """模块级便捷入口：默认客户端跑一轮 Sub-Agent。"""
    return SubAgent().handle_text(text, chat_id)
