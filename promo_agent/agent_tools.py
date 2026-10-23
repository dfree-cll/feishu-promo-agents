# -*- coding: utf-8 -*-
"""
Agent 工具清单（纯数据，无执行逻辑）。

每个工具的 JSON Schema 定义了 LLM 可以调用的能力边界；执行器见
sub_agent.SubToolExecutor 与 master_agent.MasterToolExecutor。
"""
SUBMIT_APPLICATION = {
    "name": "submit_application",
    "description": "提交一笔费用申请（抖加 doujia 或垫付 dianfu）：写入《推广账号表》并自动以"
                   "结构化事件上报 Master-Agent，一步完成。一笔申请调用一次；"
                   "多笔申请可在同一轮并行多次调用本工具。",
    "parameters": {"type": "object", "properties": {
        "app_type": {"type": "string", "enum": ["doujia", "dianfu"],
                     "description": "申请类型：doujia=抖加，dianfu=垫付/付款"},
        "song": {"type": "string", "description": "歌曲名，不带书名号"},
        "blogger": {"type": "string", "description": "博主/达人昵称"},
        "amount": {"type": "number", "description": "金额（元）"},
        "tx_count": {"type": "integer", "description": "分几笔支付，默认1"},
        "doujia_payer": {"type": "string",
                         "description": "【仅抖加申请】抖加支付人，如：爆火音乐"},
        "dianfu_payer": {"type": "string",
                         "description": "【仅垫付申请】打款人（把合作费打给博主的人），如：林老师"},
        "expected_pay_date": {"type": "string", "description": "垫付专用：预计打款日期 YYYY-MM-DD"},
        "date": {"type": "string", "description": "申请日期 YYYY-MM-DD，默认今天"},
        "stated_total": {"type": "number", "description": "申请者声称的总额（如有），用于口径核对"},
        "stated_count": {"type": "integer", "description": "申请者声称的笔数（如有）"},
    }, "required": ["app_type", "song", "blogger", "amount"]},
}
LOOKUP = {
    "name": "lookup_promo_table",
    "description": "查询《推广账号表》已有记录（含报价/抖音号/接单/打款），"
                   "可用于写入前查重或回答明细问题。",
    "parameters": {"type": "object", "properties": {
        "song": {"type": "string", "description": "按歌曲名筛选（可选）"},
        "blogger": {"type": "string", "description": "按博主名筛选（可选）"},
    }},
}
UPDATE_ORDER = {
    "name": "update_order_status",
    "description": "更新某博主对某歌曲推广单的「接单状态」（员工在群里反馈达人接单/拒接时调用）。",
    "parameters": {"type": "object", "properties": {
        "song": {"type": "string", "description": "歌曲名"},
        "blogger": {"type": "string", "description": "博主/达人昵称"},
        "status": {"type": "string", "enum": ["已接单", "待确认", "已拒绝"],
                   "description": "最新接单状态"},
    }, "required": ["song", "blogger", "status"]},
}
MARK_PAID = {
    "name": "mark_paid",
    "description": "标记某博主某笔费用已实际打款（员工反馈“钱已打了”时调用），"
                   "会更新「是否打款」为已打款并记录实际打款日期。",
    "parameters": {"type": "object", "properties": {
        "song": {"type": "string", "description": "歌曲名"},
        "blogger": {"type": "string", "description": "博主/达人昵称"},
        "paid_date": {"type": "string", "description": "实际打款日期 YYYY-MM-DD，默认今天"},
    }, "required": ["song", "blogger"]},
}
ASK_MASTER = {
    "name": "ask_master",
    "description": "把问题转交上级 Master-Agent「综合助手」回答。适用于：歌曲总花费、"
                   "达人接单情况、打款情况、汇总日报等需要聚合多行数据的问题。",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "description": "要转达的问题"},
    }, "required": ["question"]},
}
SUMMARIZE = {
    "name": "summarize_applications",
    "description": "把事件总线上未处理的申请聚合，写入《综合管理表》，返回每个歌曲的推广日报。",
    "parameters": {"type": "object", "properties": {}},
}
QUERY_REPORTS = {
    "name": "query_daily_reports",
    "description": "查询《综合管理表》里已有的歌曲推广日报（歌曲维度聚合，含花费/接单/打款），"
                   "支持按日期过滤。totals 与明细由系统精确计算。",
    "parameters": {"type": "object", "properties": {
        "song": {"type": "string", "description": "按歌曲名筛选（可选）"},
        "date": {"type": "string", "description": "按统计日期筛选 YYYY-MM-DD（可选，"
                 "问“某天花了多少”时必传，否则返回全口径合计）"},
    }},
}

SEARCH_KB = {
    "name": "search_knowledge",
    "description": "检索团队知识库（业务术语/流程/FAQ）。对业务概念、角色区分、流程规则"
                   "不确定时调用，例如“抖加和垫付的区别”“预计打款日期格式”。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "要检索的问题或关键词"},
        "top_k": {"type": "integer", "description": "返回条数，默认3"},
    }, "required": ["query"]},
}
RECENT_APPS = {
    "name": "recall_recent_applications",
    "description": "回忆最近写入的费用申请（短期操作记忆）。当用户说“刚才那笔”"
                   "“最近一笔”这类指代，或需要确认刚刚提交的内容时调用。",
    "parameters": {"type": "object", "properties": {
        "n": {"type": "integer", "description": "返回条数，默认5"},
    }},
}

SUB_TOOLS = [SUBMIT_APPLICATION, LOOKUP, UPDATE_ORDER, MARK_PAID,
             RECENT_APPS, SEARCH_KB, ASK_MASTER]
MASTER_TOOLS = [SUMMARIZE, QUERY_REPORTS, LOOKUP, SEARCH_KB]
