# -*- coding: utf-8 -*-
"""工具参数校验白名单（纯函数）。

LLM 生成的工具参数不可直接信任：落库前必须经过这里的
类型转换、字段清洗、长度与取值范围检查。
"""


def clean_text(args: dict, key: str, max_len: int = 50) -> str:
    value = "".join(str(args.get(key) or "").split())
    if not value:
        raise ValueError("字段 " + key + " 不能为空")
    if len(value) > max_len:
        raise ValueError("字段 " + key + " 超长（>" + str(max_len) + "）")
    return value


def clean_date(args: dict, key: str, default: str, today: str) -> str:
    from datetime import datetime
    raw = str(args.get(key) or default).strip()
    if not raw:
        return today
    datetime.strptime(raw, "%Y-%m-%d")   # 不合法会抛 ValueError
    return raw


def clean_amount(value, max_amount: float = 10_000_000.0) -> float:
    amount = float(value or 0)
    if not 0 < amount <= max_amount:
        raise ValueError("金额超出合理范围: " + str(amount))
    return amount


def clean_tx_count(value) -> int:
    return max(1, min(int(value or 1), 99))


ORDER_STATUSES = ("已接单", "待确认", "已拒绝")


def validate_order_args(args: dict) -> dict:
    status = str(args.get("status") or "").strip()
    if status not in ORDER_STATUSES:
        raise ValueError("status 必须是 已接单/待确认/已拒绝 之一")
    return {"status": status}


REVIEW_DECISIONS = ("已批准", "已驳回")
REVIEWABLE_FIELDS = ("审核状态", "是否接单", "是否打款")


def validate_review_args(args: dict) -> dict:
    decision = str(args.get("decision") or "").strip()
    if decision not in REVIEW_DECISIONS:
        raise ValueError("decision 必须是 已批准/已驳回 之一")
    return {"decision": decision,
            "reason": str(args.get("reason") or "").strip()[:100]}


def validate_rollback_args(args: dict) -> dict:
    field = str(args.get("field") or "").strip()
    if field and field not in REVIEWABLE_FIELDS:
        raise ValueError("field 必须是 审核状态/是否接单/是否打款 之一")
    return {"field": field}


def validate_paid_args(args: dict, today: str) -> dict:
    return {"paid_date": clean_date(args, "paid_date", today, today)}


def validate_write_args(args: dict, today: str) -> dict:
    return {
        "song": clean_text(args, "song"),
        "blogger": clean_text(args, "blogger"),
        "amount": clean_amount(args.get("amount")),
        "tx_count": clean_tx_count(args.get("tx_count")),
        "payer": clean_text(args, "payer"),
        "date": clean_date(args, "date", today, today),
        "expected_pay_date": clean_date(args, "expected_pay_date", "", today)
        if args.get("expected_pay_date") else "",
    }


def validate_submit_args(args: dict, today: str) -> dict:
    """submit_application 工具参数：按申请类型区分支付人/打款人 + 写入字段 + 口径核对字段。"""
    app_type = str(args.get("app_type") or "").strip()
    if app_type not in ("doujia", "dianfu"):
        raise ValueError("app_type 必须是 doujia 或 dianfu")
    merged = dict(args)
    if app_type == "doujia":
        # 抖加申请：必须有「抖加支付人」，不填打款人
        merged["payer"] = clean_text(args, "doujia_payer")
        if args.get("dianfu_payer"):
            raise ValueError("抖加申请不应填写 dianfu_payer（打款人），请使用 doujia_payer（支付人）")
    else:
        # 垫付申请：必须有「打款人」，不填支付人
        merged["payer"] = clean_text(args, "dianfu_payer")
        if args.get("doujia_payer"):
            raise ValueError("垫付申请不应填写 doujia_payer（支付人），请使用 dianfu_payer（打款人）")
    clean = validate_write_args(merged, today)
    clean["app_type"] = app_type
    clean["submitted_at"] = str(args.get("submitted_at") or "")[:32]   # 系统注入的采集时间
    clean["stated_total"] = float(args.get("stated_total") or 0)
    clean["stated_count"] = int(args.get("stated_count") or 0)
    return clean


def validate_notify_args(args: dict, today: str) -> dict:
    event_type = str(args.get("event_type") or "")
    if event_type not in ("doujia_summary", "dianfu_summary"):
        raise ValueError("event_type 必须是 doujia_summary 或 dianfu_summary")
    records = []
    for r in (args.get("records") or [])[:50]:
        records.append({
            "song": clean_text(r, "song"),
            "blogger": clean_text(r, "blogger"),
            "amount": clean_amount(r.get("amount")),
            "tx_count": clean_tx_count(r.get("tx_count")),
        })
    if not records:
        raise ValueError("records 不能为空")
    return {
        "event_type": event_type,
        "summary_date": clean_date(args, "summary_date", today, today),
        "records": records,
        "payer": clean_text(args, "payer"),
        "expected_pay_date": clean_date(args, "expected_pay_date", "", today)
        if args.get("expected_pay_date") else "",
        "stated_total": float(args.get("stated_total") or 0),
        "stated_count": int(args.get("stated_count") or 0),
    }
