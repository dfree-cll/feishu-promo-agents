# -*- coding: utf-8 -*-
"""数据模型：申请明细、Sub→Master 结构化事件、聚合报告。"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List

DOUJIA = "doujia"      # 抖加申请
DIANFU = "dianfu"      # 垫付/付款申请
TYPE_LABEL = {DOUJIA: "抖加", DIANFU: "垫付"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ApplicationRecord:
    """一笔（合并后的）费用申请明细 —— Sub-Agent 的抽取产物。"""
    song: str
    blogger: str
    app_type: str              # DOUJIA / DIANFU
    amount: float
    tx_count: int = 1          # 分几笔
    summary_date: str = ""     # 申请汇总日期 2026-09-02
    payer: str = ""            # 抖加：支付人；垫付：打款人
    expected_pay_date: str = ""  # 垫付专用：预计打款日期
    source: str = "推广群"
    raw_line: str = ""

    @property
    def row_key(self) -> str:
        """推广账号表 一行 = 歌曲 × 博主（抖加/垫付字段合并在同一行）。"""
        return f"{self.song}|{self.blogger}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["row_key"] = self.row_key
        return d


@dataclass
class SummaryEvent:
    """Sub-Agent → Master-Agent 的结构化事件（题眼：结构化通知）。"""
    event_type: str                     # doujia_summary / dianfu_summary
    summary_date: str
    records: List[ApplicationRecord] = field(default_factory=list)
    stated_total: float = 0.0           # 汇总文本口径的总额
    stated_count: int = 0               # 汇总文本口径的笔数
    payer: str = ""                     # 抖加支付人 / 打款人
    expected_pay_date: str = ""
    source: str = "推广群"
    event_id: str = ""
    produced_by: str = "sub_agent_promotion"
    produced_at: str = field(default_factory=now_iso)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        # 事件 ID 由内容确定性生成：同一份申请总结重复摄入不会产生新事件（端到端幂等）
        import hashlib
        payload = json.dumps({
            "t": self.event_type, "d": self.summary_date,
            "r": sorted([[r.song, r.blogger, r.amount, r.tx_count] for r in self.records]),
            "tot": self.stated_total, "payer": self.payer,
        }, ensure_ascii=False, sort_keys=True)
        self.event_id = "evt_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    @property
    def detail_total(self) -> float:
        return round(sum(r.amount for r in self.records), 2)

    @property
    def detail_count(self) -> int:
        return sum(r.tx_count for r in self.records)

    @property
    def merged_count(self) -> int:
        return len(self.records)

    def to_json(self, indent: int = 2) -> str:
        d = asdict(self)
        d["detail_total"] = self.detail_total
        d["detail_count"] = self.detail_count
        d["merged_count"] = self.merged_count
        return json.dumps(d, ensure_ascii=False, indent=indent)


@dataclass
class SongDailyReport:
    """Master-Agent 输出：按 歌曲 × 日期 聚合的推广日报。"""
    summary_date: str
    song: str
    doujia_total: float = 0.0
    dianfu_total: float = 0.0
    doujia_tx: int = 0
    dianfu_tx: int = 0
    doujia_bloggers: List[str] = field(default_factory=list)
    dianfu_bloggers: List[str] = field(default_factory=list)
    doujia_payer: str = ""
    dianfu_payer: str = ""
    expected_pay_date: str = ""
    pay_status: str = ""       # 聚合时生成
    order_status: str = ""     # 聚合时从《推广账号表》读取真实接单状态生成
    warnings: List[str] = field(default_factory=list)
    updated_at: str = field(default_factory=now_iso)

    @property
    def total(self) -> float:
        return round(self.doujia_total + self.dianfu_total, 2)

    @property
    def all_bloggers(self) -> List[str]:
        seen = list(dict.fromkeys(self.doujia_bloggers + self.dianfu_bloggers))
        return seen

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        d["all_bloggers"] = self.all_bloggers
        return d
