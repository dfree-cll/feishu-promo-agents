# -*- coding: utf-8 -*-
"""
CLI 入口：
  python main.py demo       一键演示：物料数据 -> Sub-Agent 入表 -> 通知 Master -> 汇总入表 -> 管理者报告
  python main.py agent-demo Agent 路径演示：口语化消息 -> LLM 理解 -> 工具调用 -> 回复（需 LLM Key）
  python main.py listen     监听群消息：员工 @机器人 发申请 -> 自动跑全链路（真实模式）
  python main.py ingest --file path [--text "..."]   摄入任意申请总结文本
  python main.py summary    23点日终总结（当日全量重算 + 终版日报）
  python main.py schedule   以定时任务方式常驻：每天 23:00 自动汇总
  python main.py entrance   向群里发“表格入口”卡片（可置顶）
  python main.py seed-roster 预置物料达人名单
  python main.py rebuild    从事件总线全量重算日报
  python main.py reset      清空表格记录与事件状态（全新测试）
  python main.py setup      真实飞书模式：自动建多维表格 + 两张表，并列出机器人所在群
  python main.py check      检查凭证/权限/连通性
"""
import argparse
import sys
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from promo_agent import config
from promo_agent.feishu_client import FeishuClient, FeishuError
from promo_agent.master_agent import MasterAgent
from promo_agent.sub_agent import SubAgent


def cmd_demo(args):
    print(f"\n▶▶ 模式：{'离线模拟（FEISHU_MOCK=1）' if config.FEISHU_MOCK else '真实飞书环境'}")
    print("▶▶ 场景：员工在【推广群】提交了两份申请总结（抖加 + 垫付），晚上由 Agent 链路自动处理\n"
          "▶▶ 本命令走规则解析兜底路径（确定性）；口语化自由输入请用 agent-demo 或 listen\n")

    sub, master = SubAgent(), MasterAgent()

    doujia = (config.DATA_DIR / "samples" / "sample_doujia.txt").read_text(encoding="utf-8")
    dianfu = (config.DATA_DIR / "samples" / "sample_dianfu.txt").read_text(encoding="utf-8")

    print("─" * 62)
    print("STEP 1-3  Sub-Agent「推广助手」：抽取 → 填《推广账号表》→ 结构化通知")
    print("─" * 62)
    sub.ingest_text(doujia)
    sub.ingest_text(dianfu)

    print("\n" + "─" * 62)
    print("STEP 4-5  Master-Agent「综合助手」：聚合 → 填《综合管理表》→ 向管理者报告")
    print("─" * 62)
    master.run()

    print("\n▶▶ 演示完成。表格与消息存档见 state/ 目录；真实模式下请到飞书查看多维表格与群卡片。")


def cmd_agent_demo(args):
    """Agent 路径演示：口语化消息 -> LLM 理解 -> 工具调用 -> 回复（需配置 LLM Key）。"""
    from promo_agent.llm_client import llm_ready
    if not llm_ready():
        sys.exit("未配置 LLM。请在 .env 填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
                 "（支持 GLM / DeepSeek / Kimi 等 OpenAI 兼容服务）。")
    from promo_agent.sub_agent import handle_text_wrap
    samples = [
        "给《你是我的仰望》找的「山里的孩子」上260抖加，分2笔，走爆火音乐",
        "垫付申请：达人「葵花夫妇」500块，歌曲《你是我的仰望》，林老师打款，预计2026-10-25",
        "《你是我的仰望》现在总共花了多少钱？达人接单情况怎么样？",
    ]
    for s in samples:
        print("\n" + "=" * 62)
        print("群成员：", s)
        reply = handle_text_wrap(s)
        print("推广助手：", reply)
    print("\n（提示）以上回复同时已写表/推送日报卡片，去飞书查看。")


def cmd_ingest(args):
    text = open(args.file, encoding="utf-8").read() if args.file else args.text
    if not text:
        sys.exit("请用 --file 或 --text 提供申请文本")
    SubAgent().ingest_text(text)
    print("\n（提示）Master-Agent 汇总：python main.py summary")


def cmd_summary(args):
    """日终总结：重算当日全部申请并推送终版日报（对应需求“23点总结当日情况”）。"""
    from datetime import date as _date
    day = _date.today().isoformat()
    reports = MasterAgent().daily_summary(day)
    if not reports:
        print(f"{day} 当日无申请记录。")


def cmd_listen(args):
    from promo_agent.listener import run as listen_run
    listen_run()


def cmd_schedule(args):
    from apscheduler.schedulers.blocking import BlockingScheduler
    sched = BlockingScheduler()

    def job():
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] ⏰ 23:00 日终汇总任务触发")
        MasterAgent().daily_summary()

    sched.add_job(job, "cron", hour=23, minute=0, id="daily_summary")
    print(f"[{config.MASTER_AGENT_NAME}] 定时任务已启动：每天 23:00 自动汇总（Ctrl+C 退出）")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n定时任务已停止。")


def cmd_entrance(args):
    """向群里发一张“表格入口”卡片，可置顶作为固定入口。"""
    from promo_agent.table_schema import (PROMO_TABLE, MGMT_TABLE,
                                          PROMO_FIELDS, MGMT_FIELDS, bitable_url)
    c = FeishuClient()
    app = c.ensure_bitable()
    tid_p = c.ensure_table(app, PROMO_TABLE, PROMO_FIELDS)
    tid_m = c.ensure_table(app, MGMT_TABLE, MGMT_FIELDS)
    c.send_card(
        "📊 推广费用管理 · 表格入口",
        "**《推广账号表》**（Sub-Agent「推广助手」维护，博主粒度）\n"
        f"👉 [点击打开《推广账号表》]({bitable_url(app, tid_p)})\n\n"
        "**《综合管理表》**（Master-Agent「综合助手」维护，歌曲日报粒度）\n"
        f"👉 [点击打开《综合管理表》]({bitable_url(app, tid_m)})\n\n"
        "_提示：长按本条消息选择「置顶」，之后随时从群顶部打开两张表_",
        template="indigo")
    print("✅ 入口卡片已发送到群里。")


def cmd_seed_roster(args):
    """把物料《推广账号表》的达人名单预置入库（幂等：已存在则只补主档字段）。"""
    import json as _json
    from promo_agent.table_schema import PROMO_TABLE, PROMO_FIELDS
    roster = _json.loads((config.DATA_DIR / "roster.json").read_text(encoding="utf-8"))
    c = FeishuClient()
    app = c.ensure_bitable()
    tid = c.ensure_table(app, PROMO_TABLE, PROMO_FIELDS)
    for item in roster:
        song = "你是我的仰望"
        key = f"{song}|{item['博主名']}"
        fields = {
            "行键": key, "歌曲名": song,
            "博主名": item["博主名"], "账号类型": item["账号类型"],
            "抖音号": str(item["抖音号"]), "报价": item["报价"],
            "是否接单": item["是否接单"], "来源": "物料名单预置",
        }
        if item.get("发布日期"):
            fields["发布日期"] = item["发布日期"]
        rid = c.upsert_record(app, tid, "行键", key, fields)
        print(f"✅ {item['账号类型']} {item['博主名']}（报价 ¥{item['报价']:g}，{item['是否接单']}）-> {rid}")
    print(f"\n✅ 名单预置完成，共 {len(roster)} 位达人。")


def cmd_reset(args):
    """清空两张表的记录 + 重置事件总线/去重缓存/记忆，准备全新测试（表格与字段保留）。"""
    for fname in ("events.jsonl", "processed_events.json", "seen_events.json",
                  "mock_feishu.json", "memory_chat.json", "memory_ops.json"):
        p = config.STATE_DIR / fname
        if p.exists():
            p.unlink()
            print(f"已清除 state/{fname}")
    c = FeishuClient()
    from promo_agent.table_schema import PROMO_TABLE, MGMT_TABLE, PROMO_FIELDS, MGMT_FIELDS
    app = c.ensure_bitable()
    for name, fields in ((PROMO_TABLE, PROMO_FIELDS), (MGMT_TABLE, MGMT_FIELDS)):
        tid = c.ensure_table(app, name, fields)
        n = c.clear_records(app, tid)
        print(f"已清空《{name}》{n} 条记录")
    print("✅ 重置完成：表格已空、事件总线与去重缓存已清零，可以开始全新测试。")


def cmd_rebuild(args):
    """从事件总线全量重算《综合管理表》（修复历史数据）。"""
    reports = MasterAgent().rebuild()
    print(f"✅ 已重算 {len(reports)} 条日报。")


def cmd_metrics(args):
    """查看可观测指标（消息数/LLM 调用/工具调用/错误率）。"""
    from promo_agent import observability as obs
    print(obs.render_metrics())


def cmd_trace(args):
    """按 run_id 回放一次 Agent 处理的完整步骤时间线。"""
    from promo_agent import observability as obs
    run_id = args.run_id
    if run_id == "last":
        runs = [o for o in obs.tail_runs(200) if o.get("event") == "agent_run_start"]
        if not runs:
            sys.exit("暂无运行记录（state/obs_runs.jsonl 为空）。")
        run_id = runs[-1]["run_id"]
        print(f"（最近一次运行：{run_id}）")
    trace = obs.get_run(run_id)
    if not trace:
        sys.exit(f"未找到 run_id={run_id} 的运行记录。")
    print(f"Run {run_id} 共 {len(trace)} 步：")
    for o in trace:
        extra = " ".join(f"{k}={v}" for k, v in o.items()
                         if k not in ("ts", "run_id", "event") and v != "")
        print(f"  [{o['ts'][11:23]}] {o['event']}  {extra[:140]}")


def cmd_knowledge(args):
    """检索知识库（调试用）。"""
    from promo_agent import knowledge
    hits = knowledge.search(args.query, args.top_k)
    if not hits:
        print("没有命中的知识条目。")
        return
    for h in hits:
        print(f"\n【{h['title']}】(来源: {h['source']})\n{h['text']}")


def cmd_setup(args):
    if config.FEISHU_MOCK:
        sys.exit("当前是离线模拟模式。请先把 .env 中 FEISHU_MOCK 改为 0，并填入 APP_ID/APP_SECRET。")
    tables = FeishuClient().ensure_all_tables()
    print("✅ 多维表格就绪：")
    for name, tid in tables.items():
        print(f"   - {name}: {tid}")
    print("\n机器人所在的群（把目标群 chat_id 填入 .env 的 FEISHU_CHAT_ID）：")
    for c in FeishuClient().list_chats():
        print(f"   - {c.get('name')}: {c.get('chat_id')}")


def cmd_check(args):
    client = FeishuClient()
    if config.FEISHU_MOCK:
        print("当前为离线模拟模式（FEISHU_MOCK=1），不访问真实飞书。")
        client.ensure_all_tables()
        print("✅ 模拟存储读写正常：", config.STATE_DIR / "mock_feishu.json")
        return
    try:
        client.ensure_all_tables()
        print("✅ 凭证有效，多维表格可读写。")
        chats = client.list_chats()
        print(f"✅ 机器人在 {len(chats)} 个群中：")
        for c in chats:
            print(f"   - {c.get('name')}: {c.get('chat_id')}")
    except FeishuError as e:
        sys.exit(f"❌ 检查失败：{e}")


def main():
    ap = argparse.ArgumentParser(description="飞书推广费用双 Agent 系统")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="一键演示全流程（规则兜底路径）").set_defaults(func=cmd_demo)
    sub.add_parser("agent-demo", help="Agent 路径演示（需配置 LLM Key）").set_defaults(func=cmd_agent_demo)
    p = sub.add_parser("ingest", help="摄入申请总结文本")
    p.add_argument("--file"); p.add_argument("--text")
    p.set_defaults(func=cmd_ingest)
    sub.add_parser("summary", help="Master 日终总结（当日全量）").set_defaults(func=cmd_summary)
    sub.add_parser("listen", help="监听群消息（@机器人 实时触发）").set_defaults(func=cmd_listen)
    sub.add_parser("schedule", help="常驻定时（每天23:00）").set_defaults(func=cmd_schedule)
    sub.add_parser("entrance", help="发送表格入口卡片（可置顶）").set_defaults(func=cmd_entrance)
    sub.add_parser("seed-roster", help="预置物料达人名单").set_defaults(func=cmd_seed_roster)
    sub.add_parser("rebuild", help="从事件总线全量重算日报").set_defaults(func=cmd_rebuild)
    sub.add_parser("reset", help="清空表格记录与事件状态（全新测试）").set_defaults(func=cmd_reset)
    sub.add_parser("setup", help="真实模式一键建表").set_defaults(func=cmd_setup)
    sub.add_parser("check", help="连通性自检").set_defaults(func=cmd_check)
    sub.add_parser("metrics", help="查看可观测指标").set_defaults(func=cmd_metrics)
    p = sub.add_parser("trace", help="按 run_id 回放运行时间线（last=最近一次）")
    p.add_argument("run_id", nargs="?", default="last")
    p.set_defaults(func=cmd_trace)
    p = sub.add_parser("kb", help="检索知识库（调试用）")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=3)
    p.set_defaults(func=cmd_knowledge)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
