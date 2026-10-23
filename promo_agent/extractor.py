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
# 汇总头：【抖加金额统计】今日已支付：¥700（2026-09-02）/【垫付统计】今日已垫付：¥2595（…）
# 限定标题必须含"抖加/垫付"，避免误匹配明细行里的【达人：X】等
_RE_HEADER = re.compile(
    r"【([^】]{0,40}(?:抖加|垫付)[^】]{0,30})】[^\n]*?[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*[（(](\d{4}-\d{2}-\d{2})[）)]"
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
    """无汇总头时的兜底判型（保留旧行为）。"""
    if "申请为【达人" in text or "垫付" in text:
        return DIANFU
    return DOUJIA


def _header_type(header_text: str) -> str:
    """汇总头判型：【…垫付…】→ 垫付；其余（含"抖加"）→ 抖加。"""
    return DIANFU if "垫付" in header_text else DOUJIA


def _split_blocks(text: str) -> List[str]:
    """把一段消息切成「单类型申请块」。

    切分依据（从强到弱）：
    1. 显式分隔线 `---`
    2. **汇总头出现的位置**（【抖加金额统计】… /【垫付统计】…）

    以前只认 `---`，群里把抖加段和垫付段直接连写时会整段被判成垫付：
    抖加明细静默丢弃，而且拿抖加的汇总头去比垫付的明细，产生一条
    "金额口径不一致 ¥300" 的**假告警**。按汇总头切块后不再依赖分隔符。
    """
    blocks: List[str] = []
    for chunk in re.split(r"\n\s*-{3,}\s*\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        heads = list(_RE_HEADER.finditer(chunk))
        if len(heads) <= 1:
            blocks.append(chunk)
            continue
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(chunk)
            part = chunk[m.start():end].strip()
            if part:
                blocks.append(part)
    return blocks


def parse_summary(text: str, source: str = "推广群") -> SummaryEvent:
    """解析一段“抖加申请总结”或“垫付申请总结”文本 -> SummaryEvent。"""
    text = text.strip()
    warnings: List[str] = []

    header = _RE_HEADER.search(text)
    # 类型判定：优先用本块的汇总头（块已被 _split_blocks 切成单类型），
    # 没有汇总头时按明细行形态推断，最后才退回旧的"含垫付即垫付"兜底。
    if header:
        app_type = _header_type(header.group(1))
    else:
        has_dianfu_line = _RE_DIANFU_LINE.search(text) is not None
        has_doujia_line = _RE_DOUJIA_LINE.search(text) is not None
        if has_dianfu_line and not has_doujia_line:
            app_type = DIANFU
        elif has_doujia_line and not has_dianfu_line:
            app_type = DOUJIA
        else:
            app_type = _detect_type(text)

    # 极端情况（一个汇总头却混了另一类明细）不能静默丢数据，必须告警
    if app_type == DIANFU and _RE_DOUJIA_LINE.search(text):
        warnings.append("本段含抖加明细但汇总头为垫付：本次仅处理垫付，"
                        "请用 --- 分隔或分开发送")
    if app_type == DOUJIA and _RE_DIANFU_LINE.search(text):
        warnings.append("本段含垫付明细但汇总头为抖加：本次仅处理抖加，"
                        "请用 --- 分隔或分开发送")

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
    """容错入口：自动切分多段（分隔线或汇总头）申请总结，无法识别的段进入 errors。"""
    blocks = _split_blocks(text) or [text.strip()]
    events, errors, seen = [], [], set()
    for b in blocks:
        ev = parse_summary(b, source)
        if not ev.records:
            errors.append(f"未能识别的申请文本（扩展点：可接入 LLM 抽取）：\n{b[:120]}")
            continue
        if ev.event_id in seen:      # 同一内容被重复切到时只保留一个事件
            continue
        seen.add(ev.event_id)
        events.append(ev)
    return events, errors
