# -*- coding: utf-8 -*-
"""两张多维表格的字段定义（与题目物料《推广账号表》字段对齐）。"""

BITABLE_NAME = "推广费用管理"
PROMO_TABLE = "推广账号表"     # Sub-Agent 写：博主粒度
MGMT_TABLE = "综合管理表"      # Master-Agent 写：歌曲 × 日期 粒度

# 飞书字段类型：1 文本 / 2 数字 / 3 单选 / 5 日期
T_TEXT, T_NUMBER, T_SELECT, T_DATE = 1, 2, 3, 5

PROMO_FIELDS = [
    {"field_name": "行键",     "type": T_TEXT},   # 歌曲|博主，幂等 upsert 用
    {"field_name": "歌曲名",   "type": T_TEXT},
    {"field_name": "博主名",   "type": T_TEXT},
    {"field_name": "账号类型", "type": T_SELECT, "property": {"options": [
        {"name": "音乐号"}, {"name": "剧情演绎"}, {"name": "沙雕动画"}, {"name": "陪听号"}]}},
    {"field_name": "抖音号",   "type": T_TEXT},
    {"field_name": "报价",     "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "发布日期", "type": T_TEXT},   # 物料口径如 4.9 / 4.10
    {"field_name": "抖加金额", "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "抖加笔数", "type": T_NUMBER, "property": {"formatter": "0"}},
    {"field_name": "抖加支付人", "type": T_TEXT},
    {"field_name": "垫付金额", "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "垫付笔数", "type": T_NUMBER, "property": {"formatter": "0"}},
    {"field_name": "是否打款", "type": T_SELECT, "property": {"options": [{"name": "已打款"}, {"name": "待打款"}]}},
    {"field_name": "打款人",   "type": T_TEXT},
    {"field_name": "预计打款日期", "type": T_DATE, "property": {"date_formatter": "yyyy/MM/dd", "auto_fill": False}},
    {"field_name": "实际打款日期", "type": T_DATE, "property": {"date_formatter": "yyyy/MM/dd", "auto_fill": False}},
    {"field_name": "是否接单", "type": T_SELECT, "property": {"options": [{"name": "已接单"}, {"name": "待确认"}, {"name": "已拒绝"}]}},
    {"field_name": "审核状态", "type": T_SELECT, "property": {"options": [{"name": "待审核"}, {"name": "已批准"}, {"name": "已驳回"}]}},
    {"field_name": "审核备注", "type": T_TEXT},
    {"field_name": "提交时间", "type": T_TEXT},   # 群消息采集时间（防止员工忘写时间）
    {"field_name": "申请日期", "type": T_DATE, "property": {"date_formatter": "yyyy/MM/dd", "auto_fill": False}},
    {"field_name": "来源",     "type": T_TEXT},
]

MGMT_FIELDS = [
    {"field_name": "报告键",   "type": T_TEXT},   # 日期|歌曲，幂等 upsert 用
    {"field_name": "统计日期", "type": T_DATE, "property": {"date_formatter": "yyyy/MM/dd", "auto_fill": False}},
    {"field_name": "歌曲名",   "type": T_TEXT},
    {"field_name": "抖加总花费", "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "垫付总花费", "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "推广总花费", "type": T_NUMBER, "property": {"formatter": "0.00"}},
    {"field_name": "参与达人数", "type": T_NUMBER, "property": {"formatter": "0"}},
    {"field_name": "抖加笔数", "type": T_NUMBER, "property": {"formatter": "0"}},
    {"field_name": "垫付笔数", "type": T_NUMBER, "property": {"formatter": "0"}},
    {"field_name": "达人接单情况", "type": T_TEXT},
    {"field_name": "打款情况", "type": T_TEXT},
    {"field_name": "抖加支付人", "type": T_TEXT},
    {"field_name": "垫付打款人", "type": T_TEXT},
    {"field_name": "核对备注", "type": T_TEXT},
]


def _ts(date_str: str):
    """'2026-09-02' -> 毫秒时间戳；空值返回 None。"""
    if not date_str:
        return None
    import datetime as _dt
    try:
        return int(_dt.datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)
    except ValueError:
        return None


def bitable_url(app_token: str, table_id: str) -> str:
    """多维表格指定数据表的直达链接（短域对所有租户通用，会自动跳转）。"""
    return f"https://www.feishu.cn/base/{app_token}?table={table_id}"


def promo_record_fields(rec_dict: dict) -> dict:
    """ApplicationRecord -> 推广账号表 行字段（按类型只更新自己一侧的列）。

    注：报价/抖音号/账号类型/发布日期 属于投放前预置的主档信息，
    由名单预置（seed_roster）维护，申请写入不覆盖。
    注：是否接单/是否打款 是执行态字段，由状态回写工具维护；
    新建行时的初始值见 PROMO_NEW_ROW_DEFAULTS，不在此处写。
    """
    f = {
        "行键": rec_dict["row_key"],
        "歌曲名": rec_dict["song"],
        "博主名": rec_dict["blogger"],
        "申请日期": _ts(rec_dict["summary_date"]),
        "提交时间": rec_dict.get("submitted_at", ""),
        "来源": rec_dict.get("source", ""),
        "审核状态": "待审核",
    }
    if rec_dict["app_type"] == "doujia":
        f.update({
            "抖加金额": rec_dict["amount"],
            "抖加笔数": rec_dict["tx_count"],
            "抖加支付人": rec_dict["payer"],
        })
    else:
        f.update({
            "垫付金额": rec_dict["amount"],
            "打款人": rec_dict["payer"],
            "预计打款日期": _ts(rec_dict["expected_pay_date"]),
        })
    return {k: v for k, v in f.items() if v is not None}


# 仅在“新建行”时写入的默认值（不参与后续 upsert 合并）。
# 放进这里而不是 promo_record_fields，是为了避免重复提交申请时把
# update_order_status / mark_paid 回写的真实状态冲回默认值。
# 新建行的初始执行态：接单默认「待确认」——申请刚提交时达人是否接单尚未确认，
# 写「已接单」等于系统替达人签收（实测日报里出现“已接单 5 位”而员工并未反馈）。
# 真实接单状态由 update_order_status 回写。
PROMO_NEW_ROW_DEFAULTS = {"是否接单": "待确认", "是否打款": "待打款"}


def promo_new_row_defaults(app_type: str) -> dict:
    """新建行的初始执行态：接单「待确认」；只有垫付申请才涉及「打款」。"""
    defaults = {"是否接单": "待确认"}
    if app_type == "dianfu":
        defaults["是否打款"] = "待打款"
    return defaults


def mgmt_record_fields(report: dict) -> dict:
    """SongDailyReport -> 综合管理表 行字段。"""
    f = {
        "报告键": f"{report['summary_date']}|{report['song']}",
        "统计日期": _ts(report["summary_date"]),
        "歌曲名": report["song"],
        "抖加总花费": report["doujia_total"],
        "垫付总花费": report["dianfu_total"],
        "推广总花费": report["total"],
        "参与达人数": len(report["all_bloggers"]),
        "抖加笔数": report["doujia_tx"],
        "垫付笔数": report["dianfu_tx"],
        "达人接单情况": report.get("order_status", "待确认"),
        "打款情况": report["pay_status"],
        "抖加支付人": report["doujia_payer"],
        "垫付打款人": report["dianfu_payer"],
        "核对备注": "\n".join(report["warnings"]) if report["warnings"] else "汇总口径与明细核对一致",
    }
    return f
