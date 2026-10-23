# -*- coding: utf-8 -*-
"""核心纯逻辑回归测试（零网络、零飞书凭证）。

覆盖 4 类历史上真实踩过的坑：
1. 工具参数校验器产物 与 工具实现签名 不一致（submit_application 少了 submitted_at）；
2. listener 传了实现没有的参数（ingest_text 少了 msg_time）；
3. 申请写入把 update_order_status / mark_paid 回写的真实状态冲回默认值；
4. 聚合取 records[0]["song"] 当整组键，导致混歌事件的费用记错歌曲。
"""
import inspect

import pytest

from promo_agent import config
from promo_agent import feishu_client as fc
from promo_agent.extractor import parse_any, parse_summary
from promo_agent.master_agent import MasterAgent
from promo_agent.models import ApplicationRecord, SongDailyReport, SummaryEvent
from promo_agent.sub_agent import SubAgent, SubToolExecutor
from promo_agent.table_schema import (PROMO_NEW_ROW_DEFAULTS, promo_new_row_defaults,
                                      promo_record_fields)
from promo_agent import validators as v


# ------------------------------------------------------------------ 抽取 + 口径核对
def _sample(name: str) -> str:
    return (config.DATA_DIR / "samples" / name).read_text(encoding="utf-8")


def test_parse_doujia_sample_counts_and_totals():
    ev = parse_summary(_sample("sample_doujia.txt"))
    assert ev.event_type == "doujia_summary"
    assert ev.summary_date == "2026-09-02"
    assert len(ev.records) == 4
    assert ev.detail_total == 700          # 300 + 200 + 100 + 100
    assert ev.detail_count == 7            # 分 3 笔 + 分 2 笔 + 1 + 1
    assert ev.stated_total == 700
    assert ev.payer == "爆火音乐"
    assert ev.warnings == []               # 口径一致时不应告警


def test_parse_warns_on_total_mismatch():
    text = (
        "【抖加金额统计】今日已支付：¥999（2026-09-02）\n\n"
        "歌名：《你是我的仰望》- 博主名：初秋 - 金额：¥300\n"
    )
    ev = parse_summary(text)
    assert any("金额口径不一致" in w for w in ev.warnings)


# ------------------------------------------------------------------ 事件幂等
def test_event_id_is_deterministic_and_content_addressed():
    def make(amount: float) -> SummaryEvent:
        return SummaryEvent(
            event_type="doujia_summary", summary_date="2026-09-02", payer="爆火音乐",
            records=[ApplicationRecord(song="你是我的仰望", blogger="初秋",
                                       app_type="doujia", amount=amount, tx_count=1)],
        )

    assert make(300).event_id == make(300).event_id      # 同内容 -> 同 ID
    assert make(300).event_id != make(301).event_id      # 内容变了 -> ID 变


# ------------------------------------------------------------------ 签名一致性（防回归）
def test_ingest_text_accepts_msg_time():
    """listener 两个调用点都传 msg_time=，签名必须跟得上。"""
    assert "msg_time" in inspect.signature(SubAgent.ingest_text).parameters
    src = (config.PROJECT_ROOT / "promo_agent" / "listener.py").read_text(encoding="utf-8")
    assert "msg_time=msg_time" in src


def test_submit_application_accepts_all_validator_output():
    """validators 产出的每个键，工具实现都必须能接住（否则 TypeError 被吞成模型重试）。"""
    clean = v.validate_submit_args(
        {"app_type": "doujia", "song": "你是我的仰望", "blogger": "初秋",
         "amount": 300, "doujia_payer": "爆火音乐"},
        "2026-09-02",
    )
    assert "submitted_at" in clean
    # submit_application 实现位于 SubToolExecutor（工具执行器），不在 SubAgent 上
    accepted = set(inspect.signature(SubToolExecutor.submit_application).parameters)
    missing = set(clean) - accepted
    assert not missing, f"submit_application 缺少参数: {missing}"


# ------------------------------------------------------------------ 状态回写不被冲掉
def test_promo_record_fields_does_not_write_execution_state():
    """是否接单/是否打款 属执行态字段，不能由申请写入覆盖。"""
    fields = promo_record_fields({
        "row_key": "你是我的仰望|初秋", "song": "你是我的仰望", "blogger": "初秋",
        "app_type": "doujia", "amount": 300, "tx_count": 3, "payer": "爆火音乐",
        "summary_date": "2026-09-02", "submitted_at": "2026-09-02 10:00:00",
        "expected_pay_date": "", "source": "推广群",
    })
    assert "是否接单" not in fields
    assert "是否打款" not in fields
    assert fields["提交时间"] == "2026-09-02 10:00:00"
    # 申请刚提交时达人是否接单尚未确认，默认必须是「待确认」（0.7.3 修正）
    assert PROMO_NEW_ROW_DEFAULTS["是否接单"] == "待确认"
    # 抖加不涉及打款，默认值应按类型区分
    assert promo_new_row_defaults("doujia") == {"是否接单": "待确认"}
    assert promo_new_row_defaults("dianfu") == {"是否接单": "待确认", "是否打款": "待打款"}


def test_upsert_defaults_only_apply_on_create(monkeypatch):
    """新建行才写默认状态；已存在的行不能被后续申请把真实状态冲回去。"""
    store = {"bitables": {}, "messages": []}
    monkeypatch.setattr(fc, "_mock_load", lambda: store)
    monkeypatch.setattr(fc, "_mock_save", lambda s: None)

    c = fc.FeishuClient(mock=True)
    app = c.ensure_bitable()
    tid = c.ensure_table(app, "推广账号表", [])
    defaults = {"是否接单": "已接单", "是否打款": "待打款"}

    rid = c.upsert_record(app, tid, "行键", "A|初秋",
                          {"行键": "A|初秋", "抖加金额": 100}, create_defaults=defaults)
    records = store["bitables"][app]["tables"][tid]["records"]
    assert records[rid]["是否接单"] == "已接单"       # 新建：补默认值

    # 达人反馈拒接 -> 回写真实状态
    c.upsert_record(app, tid, "行键", "A|初秋", {"行键": "A|初秋", "是否接单": "已拒绝"})
    # 之后又有新申请写入同一行
    c.upsert_record(app, tid, "行键", "A|初秋",
                    {"行键": "A|初秋", "抖加金额": 200}, create_defaults=defaults)
    assert records[rid]["是否接单"] == "已拒绝"       # 关键：不能被冲回已接单
    assert records[rid]["抖加金额"] == 200


# ------------------------------------------------------------------ 聚合正确性
def _agent() -> MasterAgent:
    """不连飞书地构造 MasterAgent（aggregate / _apply_review_and_order 都是纯逻辑）。"""
    return MasterAgent.__new__(MasterAgent)


def test_aggregate_splits_multi_song_event():
    events = [{
        "event_type": "doujia_summary", "summary_date": "2026-09-02",
        "payer": "爆火音乐",
        "records": [
            {"song": "歌A", "blogger": "初秋", "amount": 100, "tx_count": 1},
            {"song": "歌B", "blogger": "艳红", "amount": 200, "tx_count": 2},
        ],
    }]
    grouped = _agent().aggregate(events)
    assert set(grouped) == {("2026-09-02", "歌A"), ("2026-09-02", "歌B")}
    assert grouped[("2026-09-02", "歌A")].doujia_total == 100
    assert grouped[("2026-09-02", "歌B")].doujia_total == 200


def test_explode_by_song_is_idempotent():
    events = [{
        "event_type": "doujia_summary", "summary_date": "2026-09-02", "payer": "p",
        "records": [{"song": "歌A", "blogger": "a", "amount": 1, "tx_count": 1},
                    {"song": "歌B", "blogger": "b", "amount": 2, "tx_count": 1}],
    }]
    once = MasterAgent._explode_by_song(events)
    assert len(once) == 2
    assert MasterAgent._explode_by_song(once) == once


class _FakeFeishu:
    """只实现 _apply_review_and_order 需要的三个方法。"""

    def __init__(self, rows):
        self._rows = rows

    def ensure_bitable(self):
        return "app"

    def ensure_table(self, *args, **kwargs):
        return "tbl"

    def search_records(self, *args, **kwargs):
        return self._rows

    @staticmethod
    def _to_write_format(value):
        return value


def _rejected_report() -> SongDailyReport:
    return SongDailyReport(
        summary_date="2026-09-02", song="歌A",
        doujia_total=300, doujia_tx=2, doujia_bloggers=["张三", "李四"],
        doujia_by_blogger={"张三": 100, "李四": 200},
        doujia_tx_by_blogger={"张三": 1, "李四": 1},
    )


def test_rejected_blogger_is_excluded_from_report():
    agent = _agent()
    agent.client = _FakeFeishu([
        {"博主名": "张三", "是否接单": "已接单", "审核状态": "已驳回"},
        {"博主名": "李四", "是否接单": "已接单", "审核状态": "已批准"},
    ])
    rep = _rejected_report()
    agent._apply_review_and_order(rep)
    assert rep.doujia_total == 200
    assert rep.doujia_bloggers == ["李四"]
    assert any("已驳回" in w for w in rep.warnings)


def test_apply_review_and_order_is_idempotent():
    agent = _agent()
    agent.client = _FakeFeishu([
        {"博主名": "张三", "是否接单": "已接单", "审核状态": "已驳回"},
        {"博主名": "李四", "是否接单": "已接单", "审核状态": "已批准"},
    ])
    rep = _rejected_report()
    agent._apply_review_and_order(rep)
    agent._apply_review_and_order(rep)   # 重复调用不应再次扣减
    assert rep.doujia_total == 200
    assert rep.doujia_tx == 1
    assert sum(1 for w in rep.warnings if "已驳回" in w) == 1


# ------------------------------------------------------------------ 参数校验
def test_validator_rejects_role_mixup_and_bad_amount():
    with pytest.raises(ValueError):
        v.validate_submit_args(
            {"app_type": "doujia", "song": "s", "blogger": "b", "amount": 100,
             "doujia_payer": "爆火音乐", "dianfu_payer": "林老师"}, "2026-09-02")
    with pytest.raises(ValueError):
        v.clean_amount(0)
    with pytest.raises(ValueError):
        v.validate_order_args({"status": "随便填"})


def test_validator_clamps_tx_count():
    assert v.clean_tx_count(0) == 1
    assert v.clean_tx_count(1000) == 99

# ------------------------------------------------------------------ 文案与金额一致（0.7.3 回归）
def test_pay_status_recomputed_after_rejection():
    """驳回剔除之后「打款情况」文案必须跟着重算。

    实测 bug：抖加总花费扣到 260，文案仍写「抖加 ¥660」（差的是被驳回的 400），
    同一张日报卡片里数字自相矛盾。
    """
    agent = _agent()
    agent.client = _FakeFeishu([
        {"博主名": "张三", "是否接单": "待确认", "审核状态": "已驳回"},
        {"博主名": "李四", "是否接单": "待确认", "审核状态": "已批准"},
    ])
    rep = _rejected_report()
    MasterAgent._build_pay_status(rep)      # 剔除前：文案含 ¥300
    assert "¥300" in rep.pay_status

    agent._apply_review_and_order(rep)      # 剔除被驳回的张三
    MasterAgent._build_pay_status(rep)      # 剔除后必须重算文案
    assert rep.doujia_total == 200
    assert "¥200" in rep.pay_status
    assert "¥300" not in rep.pay_status, "旧金额仍出现在打款文案里"


def test_card_metric_survives_snapshot(monkeypatch, tmp_path):
    """卡片数必须能由结构化日志重建（原来只加内存计数，快照重建即归零）。"""
    from promo_agent import observability as obs

    monkeypatch.setattr(obs, "RUNS_FILE", tmp_path / "runs.jsonl")
    monkeypatch.setattr(obs, "ERRORS_FILE", tmp_path / "errors.jsonl")
    monkeypatch.setattr(obs, "METRICS_FILE", tmp_path / "metrics.json")
    obs.reset()
    obs.observe_card()
    obs.observe_card()
    assert obs.snapshot()["cards_sent"] == 2


def test_file_lock_is_reentrant_safe_across_threads(tmp_path):
    """跨进程锁模块可用：同一进程内多线程串行进入临界区，计数不丢。"""
    import threading
    from promo_agent.locks import file_lock

    target = tmp_path / "counter.txt"
    target.write_text("0", encoding="utf-8")
    errors = []

    def worker():
        try:
            for _ in range(20):
                with file_lock(target, timeout=5):
                    v = int(target.read_text(encoding="utf-8"))
                    target.write_text(str(v + 1), encoding="utf-8")
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert int(target.read_text(encoding="utf-8")) == 80

# ------------------------------------------------------------------ P1：去抖 / 去重锁 / 观测归档
def test_run_master_async_debounces(monkeypatch):
    """一波多笔申请只触发一次后台聚合（原来每笔一个线程、各跑一次全量重算）。"""
    import time as _t

    from promo_agent import master_agent, sub_agent

    calls = []

    class _FakeMaster:
        def __init__(self, client=None):
            pass

        def run(self, send_report=True):
            calls.append(1)
            return []

    monkeypatch.setattr(master_agent, "MasterAgent", _FakeMaster)
    sub_agent._SUMMARY_WORKER = None
    sub_agent._SUMMARY_WAKE.clear()

    for _ in range(5):                     # 模拟同一波 5 笔申请
        sub_agent._run_master_async(None)
    _t.sleep(1.2)                          # 去抖窗口 0.4s + 执行时间

    assert len(calls) == 1, f"应合并为 1 次聚合，实际 {len(calls)} 次"


def test_seen_cache_dedupes_across_instances(monkeypatch, tmp_path):
    """两个 SeenCache 实例（模拟两个进程）不能重复消费同一个 event_id。"""
    from promo_agent import listener

    monkeypatch.setattr(listener, "SEEN_FILE", tmp_path / "seen.json")
    a = listener.SeenCache()
    assert a.first_time("evt-1") is True
    assert a.first_time("evt-1") is False

    b = listener.SeenCache()               # 另一个实例（等价于另一个进程）
    assert b.first_time("evt-1") is False  # 必须能看到 a 写下的去重记录
    assert a.first_time("evt-2") is True


def test_obs_runs_rotated_when_too_large(monkeypatch, tmp_path):
    """obs_runs.jsonl 超过阈值要滚动归档，不能无限增长。"""
    from promo_agent import observability as obs

    runs = tmp_path / "obs_runs.jsonl"
    monkeypatch.setattr(obs, "RUNS_FILE", runs)
    monkeypatch.setattr(obs, "MAX_RUNS_BYTES", 300)

    for i in range(60):
        obs.log_event("probe", i=i)

    archives = list(tmp_path.glob("obs_runs-*.jsonl"))
    assert archives, "超过阈值后应产生归档文件"
    assert runs.exists() and runs.stat().st_size <= 2 * 300

# ------------------------------------------------------------------ 台账累加（0.7.5）
def test_upsert_accumulates_amount_columns(monkeypatch):
    """同一博主的多次申请必须累加到同一行（台账是博主粒度总账），不能被后一笔覆盖。"""
    store = {"bitables": {}, "messages": []}
    monkeypatch.setattr(fc, "_mock_load", lambda: store)
    monkeypatch.setattr(fc, "_mock_save", lambda s: None)

    c = fc.FeishuClient(mock=True)
    app = c.ensure_bitable()
    tid = c.ensure_table(app, "推广账号表", [])
    doujia = ("抖加金额", "抖加笔数")

    rid = c.upsert_record(app, tid, "行键", "歌A|初秋",
                          {"行键": "歌A|初秋", "抖加金额": 300, "抖加笔数": 3},
                          additive_fields=doujia)
    c.upsert_record(app, tid, "行键", "歌A|初秋",
                    {"行键": "歌A|初秋", "抖加金额": 200, "抖加笔数": 2},
                    additive_fields=doujia)

    rec = store["bitables"][app]["tables"][tid]["records"][rid]
    assert rec["抖加金额"] == 500          # 300 + 200，不能被覆盖成 200
    assert rec["抖加笔数"] == 5

    # 垫付列写同一行：两块金额互不影响（抖加仍 500）
    c.upsert_record(app, tid, "行键", "歌A|初秋",
                    {"行键": "歌A|初秋", "垫付金额": 950, "垫付笔数": 1},
                    additive_fields=("垫付金额", "垫付笔数"))
    rec = store["bitables"][app]["tables"][tid]["records"][rid]
    assert rec["抖加金额"] == 500 and rec["垫付金额"] == 950

    # 主档字段（报价）不属于累加列，仍是覆盖语义
    c.upsert_record(app, tid, "行键", "歌A|初秋", {"行键": "歌A|初秋", "报价": 400})
    c.upsert_record(app, tid, "行键", "歌A|初秋", {"行键": "歌A|初秋", "报价": 450})
    rec = store["bitables"][app]["tables"][tid]["records"][rid]
    assert rec["报价"] == 450

# ------------------------------------------------------------------ 混合消息（0.7.6）
def test_parse_combined_message_without_separator():
    """抖加段 + 垫付段直接连写（无 --- 分隔）也要各自识别，且不产生跨类型假告警。"""
    text = (
        "【抖加金额统计】今日已支付：¥1500（2026-10-25）\n\n"
        "歌名：《合并测试》 - 博主名：初秋 - 金额：¥800（分 2 笔）\n"
        "歌名：《合并测试》 - 博主名：艳红 - 金额：¥700（分 1 笔）\n\n"
        "抖加支付人：爆火音乐\n\n"
        "【垫付统计】今日已垫付：¥1200（2026-10-25）\n\n"
        "明细如下：\n"
        "申请为【达人：初秋】垫付 ¥1000（歌曲《合并测试》）\n"
        "申请为【达人：艳红】垫付 ¥200（歌曲《合并测试》）\n\n"
        "今日共 2 笔垫付申请。\n\n"
        "打款人：林老师\n"
        "预计打款日期：26/10/27"
    )
    events, errors = parse_any(text)
    assert not errors
    assert len(events) == 2, [e.event_type for e in events]

    dj = next(e for e in events if e.event_type == "doujia_summary")
    df = next(e for e in events if e.event_type == "dianfu_summary")
    assert dj.detail_total == 1500 and len(dj.records) == 2
    assert df.detail_total == 1200 and len(df.records) == 2
    assert dj.payer == "爆火音乐" and df.payer == "林老师"
    assert df.expected_pay_date == "2026-10-27"
    # 关键：不能出现"拿抖加汇总头比垫付明细"的假告警
    assert dj.warnings == [] and df.warnings == []


def test_parse_combined_message_with_separator_still_works():
    """带 --- 分隔的老写法不能因为这次改造而回归。"""
    text = (
        "【抖加金额统计】今日已支付：¥300（2026-10-26）\n"
        "歌名：《A》 - 博主名：初秋 - 金额：¥300（分 1 笔）\n"
        "抖加支付人：爆火音乐\n"
        "---\n"
        "【垫付统计】今日已垫付：¥500（2026-10-26）\n"
        "申请为【达人：艳红】垫付 ¥500（歌曲《A》）\n"
        "打款人：林老师"
    )
    events, errors = parse_any(text)
    assert not errors
    assert len(events) == 2
    assert {e.event_type for e in events} == {"doujia_summary", "dianfu_summary"}
    dj = next(e for e in events if e.event_type == "doujia_summary")
    df = next(e for e in events if e.event_type == "dianfu_summary")
    assert dj.detail_total == 300 and df.detail_total == 500
    assert dj.warnings == [] and df.warnings == []

# ------------------------------------------------------------------ 重试语义（0.7.7）
class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"code": 0, "data": {"ok": True}}

    def json(self):
        return self._body


class _FakeHTTP:
    """记录调用次数的假 HTTP 会话。"""

    def __init__(self, exc=None, resp=None):
        self.calls = 0
        self.exc = exc
        self.resp = resp or _FakeResp()

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.resp


def _client_with(fake):
    c = fc.FeishuClient(mock=True)
    c._http = fake
    c._tenant_token = lambda: "fake-token"
    return c


def test_api_write_does_not_retry_on_read_timeout():
    """写操作读超时：服务端可能已执行，必须只尝试一次（否则重复建记录/重复发卡片）。"""
    import requests

    fake = _FakeHTTP(exc=requests.Timeout("read timed out"))
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("POST", "/open-apis/im/v1/messages", {"x": 1})
    assert fake.calls == 1


def test_api_read_retries_on_timeout(monkeypatch):
    """读操作无副作用，超时应重试满 3 次。"""
    import requests

    monkeypatch.setattr("time.sleep", lambda *_: None)
    fake = _FakeHTTP(exc=requests.Timeout("read timed out"))
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("GET", "/open-apis/bitable/v1/apps/x/tables")
    assert fake.calls == 3


def test_api_write_retries_on_connection_error(monkeypatch):
    """连接失败说明请求没送达，写操作可安全重试。"""
    import requests

    monkeypatch.setattr("time.sleep", lambda *_: None)
    fake = _FakeHTTP(exc=requests.ConnectionError("connection refused"))
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("POST", "/open-apis/bitable/v1/apps", {})
    assert fake.calls == 3


def test_api_write_does_not_retry_on_5xx():
    """写操作遇 5xx：可能已执行，不自动重试。"""
    fake = _FakeHTTP(resp=_FakeResp(status=500, body={"code": 1254005, "msg": "internal"}))
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("PUT", "/open-apis/bitable/v1/x", {"fields": {}})
    assert fake.calls == 1


def test_api_rate_limit_retries_even_for_write(monkeypatch):
    """限流是服务端明确拒绝（未执行），写操作也可以安全重试。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    fake = _FakeHTTP(resp=_FakeResp(status=200, body={"code": 99991400, "msg": "rate limit"}))
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("POST", "/open-apis/im/v1/messages", {})
    assert fake.calls == 3

# ------------------------------------------------------------------ 0.7.8：幂等键与防御
def test_event_id_uses_message_id_when_available():
    """同一条消息重推 → 同 event_id；两条消息发同样文本 → 不同 event_id（都要算）。"""
    text = ("【抖加金额统计】今日已支付：¥300（2026-10-27）\n"
            "歌名：《幂等测试》 - 博主名：初秋 - 金额：¥300（分 1 笔）\n"
            "抖加支付人：爆火音乐")

    e1, _ = parse_any(text, message_id="om_msg_A")
    e2, _ = parse_any(text, message_id="om_msg_A")      # 同一条消息被重推
    e3, _ = parse_any(text, message_id="om_msg_B")      # 另一条消息发同样内容

    assert e1[0].event_id == e2[0].event_id, "同一消息重推必须幂等"
    assert e1[0].event_id != e3[0].event_id, "两条消息各发一次应视为两次提交"


def test_event_id_falls_back_to_content_hash_without_message_id():
    """无消息上下文（CLI ingest / 单测）仍按内容哈希，保持旧行为。"""
    text = ("【垫付统计】今日已垫付：¥500（2026-10-27）\n"
            "申请为【达人：艳红】垫付 ¥500（歌曲《X》）\n"
            "打款人：林老师")
    a, _ = parse_any(text)
    b, _ = parse_any(text)
    assert a[0].event_id == b[0].event_id


def test_aggregate_skips_event_with_empty_records():
    """records 为空的历史脏事件不能把聚合打挂（events.jsonl 可被手工编辑）。"""
    agent = _agent()
    grouped = agent.aggregate([
        {"event_type": "doujia_summary", "summary_date": "2026-10-27",
         "payer": "p", "records": []},
        {"event_type": "doujia_summary", "summary_date": "2026-10-27",
         "payer": "p", "records": [{"song": "S", "blogger": "b",
                                    "amount": 100, "tx_count": 1}]},
    ])
    assert set(grouped) == {("2026-10-27", "S")}
    assert grouped[("2026-10-27", "S")].doujia_total == 100


def test_api_non_json_response_raises_feishu_error(monkeypatch):
    """网关 502 返回 HTML 时要抛 FeishuError，而不是裸 JSONDecodeError。"""
    class _HtmlResp:
        status_code = 502
        text = "<html>502 Bad Gateway</html>"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr("time.sleep", lambda *_: None)
    fake = _FakeHTTP(resp=_HtmlResp())
    c = _client_with(fake)
    with pytest.raises(fc.FeishuError):
        c._api("GET", "/open-apis/x")
    assert fake.calls == 3          # 读操作遇 5xx 仍重试


def test_nonretryable_write_error_stops_tool_loop(monkeypatch):
    """「写可能已执行」的错误必须直接终止工具循环，不能把控制权交回模型（否则重复记账）。"""
    from promo_agent import llm_client

    calls = {"chat": 0}

    def fake_chat(messages, tools=None, temperature=0.2):
        calls["chat"] += 1
        return {"content": "", "tool_calls": [{
            "id": "call_1",
            "function": {"name": "submit_application", "arguments": "{}"},
        }]}

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    def executor(fn, args):
        raise fc.FeishuError("写超时", retryable=False,
                             answer_text="这笔可能已提交，请人工核对台账，不要重复提交。")

    out = llm_client.run_tool_loop([{"role": "user", "content": "提交"}], [], executor)
    assert "人工核对" in out
    assert calls["chat"] == 1       # 只调一次模型就终止，没有给它重试机会

# ------------------------------------------------------------------ 0.7.9：日期校验与 --date
def test_expected_pay_date_before_summary_date_warns():
    """物料第三个坑：预计打款日期早于申请日期（钱还没申请就先"预计打款"）要告警。"""
    text = ("【垫付统计】今日已垫付：¥950（2026-09-03）\n"
            "申请为【达人：初秋】垫付 ¥950（歌曲《你是我的仰望》）\n"
            "打款人：林老师\n"
            "预计打款日期：26/08/31")
    ev = parse_summary(text)
    assert ev.expected_pay_date == "2026-08-31"
    assert any("日期口径异常" in w for w in ev.warnings)


def test_expected_pay_date_after_summary_date_is_silent():
    """预计打款日期晚于申请日期是正常业务，不应告警。"""
    text = ("【垫付统计】今日已垫付：¥950（2026-09-03）\n"
            "申请为【达人：初秋】垫付 ¥950（歌曲《A》）\n"
            "打款人：林老师\n"
            "预计打款日期：26/09/10")
    ev = parse_summary(text)
    assert not any("日期口径异常" in w for w in ev.warnings)


def test_cmd_summary_accepts_date(monkeypatch):
    """`summary --date` 必须真的把指定日期传给 daily_summary（物料跨天演示靠它）。"""
    from types import SimpleNamespace

    import main as cli

    captured = {}

    class _FakeMaster:
        def daily_summary(self, day):
            captured["day"] = day
            return []

    monkeypatch.setattr(cli, "MasterAgent", _FakeMaster)
    cli.cmd_summary(SimpleNamespace(date="2026-09-02"))
    assert captured["day"] == "2026-09-02"
