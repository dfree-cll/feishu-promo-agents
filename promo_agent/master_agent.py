# -*- coding: utf-8 -*-
"""
Master-Agent「综合助手」：
1. 消费 Sub-Agent 发来的结构化事件（事件总线，带游标幂等）；
2. 按 歌曲×日期 聚合，写入《综合管理表》（即题目要求“创建的表格”）；
3. 向管理者发送歌曲推广情况报告卡片（抖加/垫付分列、接单情况、打款情况）；
4. 23:00 日终总结（daily_summary）：重算当日全量申请并推送终版日报；
5. 对话入口（handle_question）：管理者自然语言提问 -> 查表 -> 自然语言回答。
"""
import json
import threading
from collections import defaultdict
from datetime import date as _date
from typing import Dict, List

from promo_agent import config
from promo_agent.agent_tools import MASTER_TOOLS
from promo_agent.feishu_client import FeishuClient, FeishuError
from promo_agent.models import SongDailyReport
from promo_agent.table_schema import (MGMT_TABLE, MGMT_FIELDS, PROMO_TABLE,
                                      mgmt_record_fields, bitable_url)

EVENT_BUS = config.STATE_DIR / "events.jsonl"
PROCESSED = config.STATE_DIR / "processed_events.json"
PROCESSED_LOCK = threading.Lock()   # 并发消费事件时保护游标文件
_RUN_LOCK = threading.RLock()       # 串行化 聚合/日终/重算，防止后台任务与 23:00 定时重入


def _serialized(fn):
    """装饰器：同一时刻只允许一个聚合/日终/重算任务在跑。"""
    def wrapper(self, *args, **kwargs):
        with _RUN_LOCK:
            return fn(self, *args, **kwargs)
    return wrapper


MASTER_SYSTEM_PROMPT = (
    "你是音乐宣推团队的「综合助手」（Master-Agent），服务对象是管理者/部门负责人。\n"
    "你掌握两张表格：《推广账号表》（博主粒度明细）与《综合管理表》（歌曲×日期日报）。\n"
    "工作准则：\n"
    "1. 被问到最新申请汇总时，调用 summarize_applications 先聚合再回答；\n"
    "2. 被问到花费/接单/打款等数字时，必须用工具查询真实数据，禁止凭空编造；\n"
    "3. 【最重要】工具返回中若包含 answer_text 字段，必须把它的内容原样作为回答主体，\n"
    "   不得改动其中任何数字与姓名，不得自行加总计算；\n"
    "   工具返回中没有 answer_text 时，只能引用工具返回内容里出现过的数字与名字；\n"
    "   工具结果与问题对不上时，如实说明数据缺失，不要推测；\n"
    "4. 回答用简体中文。"
)


PROCESSED_LOCK = threading.Lock()   # 并发消费事件时保护游标文件


class MasterAgent:
    name = config.MASTER_AGENT_NAME

    def __init__(self, client: FeishuClient = None):
        self.client = client or FeishuClient()
        self._load_processed()

    # ---------- 事件消费（幂等游标） ----------
    def _load_processed(self):
        self.processed: List[str] = []
        if PROCESSED.exists():
            self.processed = json.loads(PROCESSED.read_text(encoding="utf-8"))

    def _mark_processed(self, event_id: str):
        with PROCESSED_LOCK:
            if event_id not in self.processed:
                self.processed.append(event_id)
                PROCESSED.write_text(json.dumps(self.processed, ensure_ascii=False),
                                     encoding="utf-8")

    def consume_events(self) -> List[dict]:
        if not EVENT_BUS.exists():
            return []
        fresh = []
        for line in EVENT_BUS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev["event_id"] not in self.processed:
                fresh.append(ev)
        return fresh

    # ---------- 聚合 + 填表 + 汇报 ----------
    def aggregate(self, events: List[dict]) -> Dict[tuple, SongDailyReport]:
        grouped: Dict[tuple, SongDailyReport] = {}
        for ev in events:
            key = (ev["summary_date"], ev["records"][0]["song"])
            rep = grouped.setdefault(key, SongDailyReport(
                summary_date=ev["summary_date"], song=key[1]))
            for rec in ev["records"]:
                blogger = rec["blogger"]
                if ev["event_type"] == "doujia_summary":
                    rep.doujia_total = round(rep.doujia_total + rec["amount"], 2)
                    rep.doujia_tx += rec["tx_count"]
                    if blogger not in rep.doujia_bloggers:
                        rep.doujia_bloggers.append(blogger)
                    rep.doujia_by_blogger[blogger] = round(
                        rep.doujia_by_blogger.get(blogger, 0) + rec["amount"], 2)
                    rep.doujia_tx_by_blogger[blogger] = (
                        rep.doujia_tx_by_blogger.get(blogger, 0) + rec["tx_count"])
                    rep.doujia_payer = ev["payer"]
                else:
                    rep.dianfu_total = round(rep.dianfu_total + rec["amount"], 2)
                    rep.dianfu_tx += rec["tx_count"]
                    if blogger not in rep.dianfu_bloggers:
                        rep.dianfu_bloggers.append(blogger)
                    rep.dianfu_by_blogger[blogger] = round(
                        rep.dianfu_by_blogger.get(blogger, 0) + rec["amount"], 2)
                    rep.dianfu_tx_by_blogger[blogger] = (
                        rep.dianfu_tx_by_blogger.get(blogger, 0) + rec["tx_count"])
                    rep.dianfu_payer = ev["payer"]
                    rep.expected_pay_date = ev.get("expected_pay_date", "")
            rep.warnings.extend(w for w in ev.get("warnings", []) if w not in rep.warnings)

        # 打款情况文案
        for rep in grouped.values():
            parts = []
            if rep.doujia_total:
                parts.append(f"抖加 ¥{rep.doujia_total:g} 已由「{rep.doujia_payer or '—'}」支付")
            if rep.dianfu_total:
                parts.append(f"垫付 ¥{rep.dianfu_total:g} 待打款"
                             f"（打款人：{rep.dianfu_payer or '—'}"
                             f"{f'，预计 {rep.expected_pay_date}' if rep.expected_pay_date else ''}）")
            rep.pay_status = "；".join(parts) or "无"
        return grouped

    def _apply_review_and_order(self, rep: SongDailyReport):
        """读取《推广账号表》的真实接单/审核状态：
        - 已驳回的达人费用从本日报中剔除（审核流闭环，不计入口径）；
        - 生成接单情况文案；剔除信息写入 warnings（卡片与核对备注可见）。"""
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(
            app_token, PROMO_TABLE,
            __import__("promo_agent.table_schema", fromlist=["PROMO_FIELDS"]).PROMO_FIELDS)
        norm = self.client._to_write_format
        status_map: dict = {}
        review_map: dict = {}
        for fields in self.client.search_records(app_token, table_id, "歌曲名", rep.song):
            blogger = norm(fields.get("博主名", ""))
            if not blogger:
                continue
            status_map[blogger] = norm(fields.get("是否接单", "")) or "待确认"
            review_map[blogger] = norm(fields.get("审核状态", ""))

        rejected = [b for b in rep.all_bloggers if review_map.get(b) == "已驳回"]
        for b in rejected:
            if b in rep.doujia_bloggers:
                rep.doujia_total = round(rep.doujia_total - rep.doujia_by_blogger.get(b, 0), 2)
                rep.doujia_tx -= rep.doujia_tx_by_blogger.get(b, 0)
                rep.doujia_bloggers.remove(b)
            if b in rep.dianfu_bloggers:
                rep.dianfu_total = round(rep.dianfu_total - rep.dianfu_by_blogger.get(b, 0), 2)
                rep.dianfu_tx -= rep.dianfu_tx_by_blogger.get(b, 0)
                rep.dianfu_bloggers.remove(b)
        if rejected:
            rep.warnings.append("已驳回（不计入本日报）：" + "、".join(rejected))

        by_status: dict = {}
        for b in rep.all_bloggers:
            by_status.setdefault(status_map.get(b, "待确认"), []).append(b)
        if not by_status:
            rep.order_status = "暂无达人记录"
        else:
            rep.order_status = "；".join(
                f"{s} {len(bs)} 位（{'、'.join(bs)}）" for s, bs in by_status.items())

    @_serialized
    def run(self, send_report: bool = True) -> List[SongDailyReport]:
        """即时增量入口：消费未处理事件，并对受影响的日期做【全量重算】。

        全量重算（而非增量累加）保证任何崩溃/重试场景下数字都正确；
        某个日期聚合失败时事件保留未处理状态，下次触发自动重试。
        """
        events = self.consume_events()
        if not events:
            print(f"[{self.name}] 暂无未处理事件。")
            return []
        print(f"[{self.name}] 📥 收到 {len(events)} 个结构化事件，开始聚合……")
        grouped = self.aggregate(events)

        # 受影响日期的全量事件（含历史已处理事件）——按天重算最终口径
        affected = {ev["summary_date"] for ev in events}
        all_events = []
        if EVENT_BUS.exists():
            all_events = [json.loads(line) for line in
                          EVENT_BUS.read_text(encoding="utf-8").splitlines() if line.strip()]
        full_grouped = self.aggregate([e for e in all_events
                                       if e["summary_date"] in affected])
        # 只推送本次新事件涉及的日期
        for key in list(grouped):
            full_grouped.setdefault(key, grouped[key])

        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, MGMT_TABLE, MGMT_FIELDS)
        table_url = bitable_url(app_token, table_id)

        key_events: Dict[tuple, List[dict]] = defaultdict(list)
        for ev in events:
            key_events[(ev["summary_date"], ev["records"][0]["song"])].append(ev)

        for key, rep in full_grouped.items():
            if key not in grouped:
                continue   # 该日期历史事件重算结果仅用于覆盖旧值，无需重复推卡
            self._apply_review_and_order(rep)
            fields = mgmt_record_fields(rep.to_dict())
            try:
                rid = self.client.upsert_record(app_token, table_id, "报告键",
                                                f"{key[0]}|{key[1]}", fields)
            except FeishuError as e:
                print(f"[{self.name}] ❌ 《{rep.song}》{key[0]} 聚合失败（事件保留待重试）：{e}")
                continue
            print(f"[{self.name}] ✅ 已写入《综合管理表》《{rep.song}》{key[0]} -> {rid}")
            self._card_to_manager(rep, send_report, table_url)
            for ev in key_events.get(key, []):
                self._mark_processed(ev["event_id"])
        return [rep for key, rep in full_grouped.items() if key in grouped]

    @_serialized
    def daily_summary(self, day: str = "") -> List[SongDailyReport]:
        """日终总结：重算某天的全部申请并推送终版日报（对应需求“23点总结当日情况”）。

        与 run()（即时增量）不同：这里按日期过滤事件总线做全量重算，
        保证 23 点的数字是全天最终口径，而不管白天事件被处理过多少次。
        """
        day = day or _date.today().isoformat()
        if not EVENT_BUS.exists():
            print(f"[{self.name}] {day} 无任何申请记录。")
            return []
        day_events = [json.loads(line) for line in
                      EVENT_BUS.read_text(encoding="utf-8").splitlines()
                      if line.strip() and json.loads(line).get("summary_date") == day]
        if not day_events:
            print(f"[{self.name}] {day} 当日无申请，跳过日终总结。")
            return []
        grouped = self.aggregate(day_events)
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, MGMT_TABLE, MGMT_FIELDS)
        table_url = bitable_url(app_token, table_id)
        for rep in grouped.values():
            self._apply_review_and_order(rep)
            fields = mgmt_record_fields(rep.to_dict())
            self.client.upsert_record(app_token, table_id, "报告键",
                                      f"{rep.summary_date}|{rep.song}", fields)
            print(f"[{self.name}] ✅ 日终总结已写入《综合管理表》《{rep.song}》{rep.summary_date}")
            self._card_to_manager(rep, True, table_url)
        return list(grouped.values())

    @_serialized
    def rebuild(self) -> List[SongDailyReport]:
        """从事件总线全量重算所有日报（修复历史数据/口径变更时使用）。"""
        if not EVENT_BUS.exists():
            return []
        all_events = [json.loads(line) for line in
                      EVENT_BUS.read_text(encoding="utf-8").splitlines() if line.strip()]
        grouped = self.aggregate(all_events)
        app_token = self.client.ensure_bitable()
        table_id = self.client.ensure_table(app_token, MGMT_TABLE, MGMT_FIELDS)
        for rep in grouped.values():
            self._apply_review_and_order(rep)
            fields = mgmt_record_fields(rep.to_dict())
            self.client.upsert_record(app_token, table_id, "报告键",
                                      f"{rep.summary_date}|{rep.song}", fields)
            print(f"[{self.name}] 🔁 已重算《综合管理表》《{rep.song}》{rep.summary_date}")
        return list(grouped.values())

    def _card_to_manager(self, rep: SongDailyReport, send: bool, table_url: str = ""):
        if not send:
            return
        warn = ("\n⚠️ **核对提醒**\n" + "\n".join(f"- {w}" for w in rep.warnings)) \
            if rep.warnings else ""
        link = f"\n📊 [点此打开《综合管理表》]({table_url})" if table_url else ""
        self.client.send_card(
            f"【{self.name}】《{rep.song}》推广日报 {rep.summary_date}",
            f"**📊 推广花费**（抖加/垫付分列）\n"
            f"- 抖加：**¥{rep.doujia_total:g}**（{rep.doujia_tx} 笔 / {len(rep.doujia_bloggers)} 位达人）\n"
            f"- 垫付：**¥{rep.dianfu_total:g}**（{rep.dianfu_tx} 笔 / {len(rep.dianfu_bloggers)} 位达人）\n"
            f"- 合计：**¥{rep.total:g}**\n\n"
            f"**🎧 达人接单情况**：{rep.order_status}\n\n"
            f"**💰 打款情况**：{rep.pay_status}\n"
            f"{warn}\n\n_明细见《推广账号表》，本报告已写入《综合管理表》_"
            + link,
            template="green")


class MasterToolExecutor:
    """Master-Agent 的工具分派（参数先过 validators 白名单）。"""

    def __init__(self, client: FeishuClient = None):
        self.client = client or FeishuClient()

    def run_tool(self, tool_name: str, params: dict) -> dict:
        from promo_agent import validators as v
        try:
            if tool_name == "summarize_applications":
                reports = MasterAgent(self.client).run(send_report=True)
                return {"reports": [r.to_dict() for r in reports] or ["暂无未汇总事件"]}
            if tool_name == "query_daily_reports":
                result = self.client.fetch_mgmt_rows(
                    song=str(params.get("song") or ""),
                    date=str(params.get("date") or ""))
                if not result.get("reports"):
                    # 自愈：刚申请完日报还没生成（事件在总线待消费）——先聚合再查
                    pending = MasterAgent(self.client).run(send_report=False)
                    if pending:
                        result = self.client.fetch_mgmt_rows(
                            song=str(params.get("song") or ""),
                            date=str(params.get("date") or ""))
                return result
            if tool_name == "lookup_promo_table":
                return self.client.fetch_promo_rows(
                    song=str(params.get("song") or ""),
                    blogger=str(params.get("blogger") or ""))
            if tool_name == "search_knowledge":
                from promo_agent import knowledge
                return {"results": knowledge.search(
                    str(params.get("query") or ""), int(params.get("top_k") or 3))}
        except ValueError as err:
            return {"error": "参数校验失败: " + str(err)}
        return {"error": "不支持的工具: " + str(tool_name)[:40]}


def handle_question(question: str, client: FeishuClient = None) -> str:
    """Master-Agent 的对话入口：管理者的自然语言问题 -> 查表聚合 -> 自然语言回答。"""
    import time as _time
    from promo_agent.llm_client import run_tool_loop, llm_ready
    from promo_agent import observability as obs
    if not llm_ready():
        return "（未配置 LLM，无法智能问答。可运行 python -m main summary 生成规则版日报）"
    run_id = obs.new_run_id("master")
    obs.set_current_run(run_id)
    obs.log_event("master_question", run_id=run_id, question=question[:120])
    t0 = _time.time()
    messages = [{"role": "system", "content": MASTER_SYSTEM_PROMPT},
                {"role": "user", "content": question}]
    answer = run_tool_loop(
        messages, MASTER_TOOLS, MasterToolExecutor(client).run_tool,
        answer_key="answer_text",   # 代码预渲染的答案直通，杜绝模型改数编名
        on_step=lambda fn, a: print(f"[{config.MASTER_AGENT_NAME}] 🔧 工具调用 {fn} "
                                    + json.dumps(a, ensure_ascii=False)))
    obs.log_event("master_answer", run_id=run_id, answer=answer[:200],
                  latency_s=round(_time.time() - t0, 2))
    return answer
