# -*- coding: utf-8 -*-
"""
信息抽取器（规则兜底路径的“眼睛”）：
把【推广群】里的费用申请总结文本解析成结构化 ApplicationRecord。

- 内置规则解析：零外部依赖，直接吃题目物料的两种格式；
- 预留 LLM 抽取扩展点：规则不匹配时提示可接入大模型（见 README）；
- 一致性核对：汇总口径 vs 明细口径 对不上时生成 warning（数据质量护栏）。
"""
import re
from typing import List, Tuple

from promo_agent.models import ApplicationRecord, SummaryEvent, DOUJIA, DIANFU

# 抖加明细行：歌名：《X》- 博主名：Y - 金额：¥N（分 M 笔）
_RE_DOUJIA_LINE = re.compile(
    r"歌名[：:]\s*《(.+?)》\s*[-–—]\s*博主名[：:]\s*(.+?)\s*[-–—]\s*金额[：:]\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)"
    r"(?:[（(]\s*分\s*(\d+)\s*笔\s*[）)])?"
)
# 垫付明细行：申请为【达人：Y】垫付 ¥N（歌曲《X》）
_RE_DIANFU_LINE = re.compile(
    r"申请为\s*【达人[：:]\s*(.+?)】\s*垫付\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*[（(]歌曲《(.+?)》[）)]"
)
# 汇总头：【抖加金额统计】今日已支付：¥700（2026-09-02）
_RE_HEADER = re.compile(
    r"【(.+?)】.*?[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*[（(](\d{4}-\d{2}-\d{2})[）)]"
)
# 今日共 N 笔
_RE_COUNT = re.compile(r"今日共\s*(\d+)\s*笔")
_RE_DOUJIA_PAYER = re.compile(r"抖加支付人[：:]\s*(\S+)")
_RE_DIANFU_PAYER = re.compile(r"^打款人[：:]\s*(\S+)", re.M)
_RE_EXPECTED_PAY = re.compile(r"预计打款日期[：:]\s*(\d{2,4})[/\-年](\d{1,2})[/\-月](\d{1,2})")


def _to_amount(s: str) -> float:
    return float(s.replace(",", ""))


def _norm_expected_date(m: re.Match) -> str:
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _detect_type(text: str) -> str:
    if "申请为【达人" in text or "垫付" in text:
        return DIANFU
    return DOUJIA


def parse_summary(text: str, source: str = "推广群") -> SummaryEvent:
    """解析一段“抖加申请总结”或“垫付申请总结”文本 -> SummaryEvent。"""
    text = text.strip()
    app_type = _detect_type(text)
    warnings: List[str] = []

    header = _RE_HEADER.search(text)
    stated_total = _to_amount(header.group(2)) if header else 0.0
    summary_date = header.group(3) if header else ""

    records: List[ApplicationRecord] = []
    if app_type == DOUJIA:
        for m in _RE_DOUJIA_LINE.finditer(text):
            split_n = int(m.group(4)) if m.group(4) else 1
            records.append(ApplicationRecord(
                song=m.group(1).strip(), blogger=m.group(2).strip(), app_type=DOUJIA,
                amount=_to_amount(m.group(3)), tx_count=split_n,
                summary_date=summary_date, source=source, raw_line=m.group(0).strip(),
            ))
        payer_m = _RE_DOUJIA_PAYER.search(text)
        payer = payer_m.group(1).strip() if payer_m else ""
    else:
        for m in _RE_DIANFU_LINE.finditer(text):
            records.append(ApplicationRecord(
                song=m.group(3).strip(), blogger=m.group(1).strip(), app_type=DIANFU,
                amount=_to_amount(m.group(2)), tx_count=1,
                summary_date=summary_date, source=source, raw_line=m.group(0).strip(),
            ))
        payer_m = _RE_DIANFU_PAYER.search(text)
        payer = payer_m.group(1).strip() if payer_m else ""

    expected = ""
    em = _RE_EXPECTED_PAY.search(text)
    if em:
        expected = _norm_expected_date(em)

    count_m = _RE_COUNT.search(text)
    stated_count = int(count_m.group(1)) if count_m else 0

    for r in records:
        r.payer = payer
        r.expected_pay_date = expected

    # ---------- 一致性核对：文本汇总口径 vs 明细口径 ----------
    if header and records:
        detail_total, detail_count = sum(r.amount for r in records), sum(r.tx_count for r in records)
        if abs(detail_total - stated_total) > 0.01:
            warnings.append(
                f"金额口径不一致：汇总文本为 ¥{stated_total:g}，明细合计 ¥{detail_total:g}，"
                f"差异 ¥{stated_total - detail_total:g}，需人工核对（可能存在未列明细的申请）")
        if stated_count and detail_count != stated_count:
            warnings.append(
                f"笔数口径不一致：汇总文本为 {stated_count} 笔，明细合计 {detail_count} 笔，需人工核对")

    event_type = "doujia_summary" if app_type == DOUJIA else "dianfu_summary"
    return SummaryEvent(
        event_type=event_type, summary_date=summary_date, records=records,
        stated_total=stated_total, stated_count=stated_count, payer=payer,
        expected_pay_date=expected, source=source, warnings=warnings,
    )


def parse_any(text: str, source: str = "推广群") -> Tuple[List[SummaryEvent], List[str]]:
    """容错入口：自动切分文本中的多段申请总结；无法识别的行进入 errors。"""
    blocks = [b.strip() for b in re.split(r"\n\s*-{3,}\s*\n", text) if b.strip()]
    events, errors = [], []
    for b in (blocks or [text.strip()]):
        ev = parse_summary(b, source)
        if not ev.records:
            errors.append(f"未能识别的申请文本（扩展点：可接入 LLM 抽取）：\n{b[:120]}")
        else:
            events.append(ev)
    return events, errors
