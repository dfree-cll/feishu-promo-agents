# -*- coding: utf-8 -*-
"""
群消息监听（实时触发入口）：
通过飞书官方 SDK 的 WebSocket 长连接订阅 im.message.receive_v1 事件，
员工在群里 @机器人 发送申请内容 -> 自动触发 Sub-Agent -> 顺带跑一次 Master 汇总闭环。
无需公网服务器；群聊中只有 @机器人 的消息会推给机器人（飞书平台规则）。
进程内同时挂载 23:00 日终汇总定时任务；多条消息线程池并发处理。
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from promo_agent import config
from promo_agent.locks import file_lock
from promo_agent.extractor import parse_any
from promo_agent.feishu_client import FeishuClient
from promo_agent.master_agent import MasterAgent
from promo_agent.sub_agent import SubAgent

_MENTION_KEY = re.compile(r"@_user_\d+")     # 事件里 @人 的占位符
SEEN_FILE = config.STATE_DIR / "seen_events.json"
_UNKNOWN_HELP = (
    "没有从这条消息里识别出申请明细。\n"
    "支持的格式示例：\n"
    "抖加：歌名：《歌名》- 博主名：Y - 金额：¥100（分 2 笔）\n"
    "垫付：申请为【达人：Y】垫付 ¥500（歌曲《歌名》）\n"
    "或直接发送整段《抖加/垫付申请总结》文本。"
)


class SeenCache:
    """飞书事件为“至少一次”投递，同一条消息可能被推送多次；按事件ID去重。"""

    def __init__(self, cap: int = 1000):
        self.cap = cap
        self.keys = []
        if SEEN_FILE.exists():
            try:
                self.keys = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            except Exception:
                self.keys = []

    def first_time(self, key: str) -> bool:
        """判重：**锁内重读-判断-写**。

        飞书事件是"至少一次"投递，同一条消息可能被推送多次；而 listen 常驻进程
        与 CLI 命令可能同时收到事件，各自持有一份 keys 快照，只加 threading 锁
        并整文件覆盖会丢掉对方的记录 → 同一条消息被处理两次。
        """
        if not key:
            return False
        with file_lock(SEEN_FILE):
            keys = self.keys
            if SEEN_FILE.exists():
                try:
                    keys = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                except Exception:
                    keys = []
            if key in keys:
                self.keys = keys
                return False
            keys.append(key)
            if len(keys) > self.cap:
                keys = keys[-(self.cap // 2):]
            SEEN_FILE.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
            self.keys = keys
            return True


def _clean_text(raw: str, mention_names=None) -> str:
    """去掉 @机器人 的占位符/名字，返回纯申请文本。"""
    text = _MENTION_KEY.sub("", raw or "")
    for name in mention_names or []:
        text = text.replace("@" + name, "")
    text = text.strip()
    if text.startswith("@"):  # 兜底：事件未提供名字时按第一个空格切
        text = text.split(" ", 1)[1].strip() if " " in text else ""
    return text


def run():
    if config.FEISHU_MOCK:
        print("监听模式依赖真实飞书环境，请先把 .env 中 FEISHU_MOCK 改为 0。")
        return
    try:
        import lark_oapi as lark
    except ImportError:
        sys.exit("缺少官方 SDK，请先安装：pip install lark-oapi")

    sub, master, client = SubAgent(), MasterAgent(), FeishuClient()
    seen = SeenCache()
    # 多人同时发消息：每条消息一个工作线程并行处理（I/O 密集，线程池即可）
    pool = ThreadPoolExecutor(max_workers=config.LISTEN_WORKERS,
                              thread_name_prefix="msg")
    footer_cache: dict = {}

    def table_urls() -> tuple:
        """返回 (推广账号表URL, 综合管理表URL)，进程内缓存。"""
        if "urls" not in footer_cache:
            try:
                from promo_agent.table_schema import (PROMO_TABLE, MGMT_TABLE,
                                                      PROMO_FIELDS, MGMT_FIELDS, bitable_url)
                app = client.ensure_bitable()
                tid_p = client.ensure_table(app, PROMO_TABLE, PROMO_FIELDS)
                tid_m = client.ensure_table(app, MGMT_TABLE, MGMT_FIELDS)
                footer_cache["urls"] = (bitable_url(app, tid_p), bitable_url(app, tid_m))
            except Exception as e:
                print(f"[警告] 生成表格链接失败：{e}")
                footer_cache["urls"] = ("", "")
        return footer_cache["urls"]

    def table_footer() -> str:
        promo_url, mgmt_url = table_urls()
        if not promo_url:
            return ""
        return (f"\n\n📊 [打开《推广账号表》]({promo_url})"
                f" ｜ 📈 [打开《综合管理表》]({mgmt_url})")

    def _process(text: str, chat_id: str, sender: str = "", msg_time: str = "",
                 message_id: str = ""):
        try:
            print(f"\n[监听] 开始处理（提交时间 {msg_time or '未知'}）：{text[:80]}")

            from promo_agent.llm_client import llm_ready
            if llm_ready():
                # 混合路由：标准物料格式 -> 规则快速路径（0 次 LLM，2-3 秒）；
                # 口语化/状态类消息 -> Agent 路径（LLM 理解 + 工具循环）。
                fast_events, _ = parse_any(text)
                if fast_events:
                    print("[路由] 命中标准格式，走规则快速路径")
                    sub.ingest_text(text, table_url=table_urls()[0], msg_time=msg_time,
                                    message_id=message_id)
                    master.run()
                    return
                # Agent 路径：先秒回“处理中”回执卡（带提交时间），完成后原地更新为结果
                ack_id = client.send_card("⏳ 收到，正在处理…",
                                          f"**提交时间**：{msg_time or '—'}\n"
                                          f"消息：{text[:80]}",
                                          template="grey", to_chat_id=chat_id)
                reply = sub.handle_text(text, chat_id, sender, msg_time)
                markdown = (reply + table_footer()
                            + (f"\n\n🕘 提交时间：{msg_time}" if msg_time else ""))
                done = False
                if ack_id:
                    try:
                        done = client.update_card(ack_id, "【推广助手】回复", markdown,
                                                  template="blue")
                    except Exception as e:      # noqa: BLE001
                        if not getattr(e, "retryable", True):
                            # 更新超时：卡片很可能已更新成功，再补发会在群里出现两张结果卡
                            print(f"[监听] 回执卡更新超时（可能已成功），跳过补发：{e}")
                            return
                        done = False
                if not done:
                    client.send_card("【推广助手】回复", markdown,
                                     template="blue", to_chat_id=chat_id)
                return
            # 兜底路径：规则解析（未配置 LLM Key 时）
            events = sub.ingest_text(text, table_url=table_urls()[0], msg_time=msg_time,
                                     message_id=message_id)
            if not events:
                client.send_card("未识别的申请消息", _UNKNOWN_HELP + table_footer(),
                                 template="grey", to_chat_id=chat_id)
                return
            master.run()   # 演示友好：即时闭环（正式由 23:00 定时任务执行）
        except Exception as e:  # 不让单条消息异常杀死长连接
            print(f"[监听] 处理消息出错：{e}")

    def on_message(data):
        try:
            from datetime import datetime as _dt
            from promo_agent import observability as obs
            msg = data.event.message
            ev_key = getattr(getattr(data, "header", None), "event_id", "") \
                or getattr(msg, "message_id", "")
            if not seen.first_time(ev_key):
                print(f"[监听] 跳过重复推送的事件 {ev_key}")
                return
            if msg.message_type != "text":
                return
            names = [m.name for m in (msg.mentions or []) if getattr(m, "name", None)]
            text = _clean_text(json.loads(msg.content).get("text", ""), names)
            if not text:
                return
            # 消息采集时间：优先用飞书事件里的发送时间（防止员工忘写时间）
            msg_time = ""
            try:
                ct = str(getattr(msg, "create_time", "") or "")
                if ct.isdigit():
                    msg_time = _dt.fromtimestamp(int(ct) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                pass
            if not msg_time:
                msg_time = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            sender = ""
            try:
                sender = data.event.sender.sender_id.open_id
            except AttributeError:
                pass
            obs.observe_message()
            print(f"\n[监听] 收到群消息（提交时间 {msg_time}）：{text[:80]}")
            pool.submit(_process, text, msg.chat_id, sender, msg_time,
                        getattr(msg, "message_id", "") or "")   # 并发处理，不阻塞下一条消息
        except Exception as e:
            print(f"[监听] 事件分发出错：{e}")

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    ws = lark.ws.Client(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET,
                        event_handler=handler, log_level=lark.LogLevel.WARNING)

    # 同一进程内挂一个 23:00 日终汇总定时器（与群消息监听共用生命周期）
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler()
        sched.add_job(lambda: MasterAgent().daily_summary(), "cron",
                      hour=23, minute=0, id="daily_summary")
        sched.start()
        print(f"[{config.MASTER_AGENT_NAME}] 已挂载 23:00 日终汇总定时任务。")
    except ImportError:
        print("[警告] 未安装 APScheduler，跳过 23:00 定时汇总（pip install APScheduler）")

    print(f"[{config.SUB_AGENT_NAME}] 长连接监听已启动。"
          f"在群里 @机器人 发送申请内容即可触发全链路（Ctrl+C 退出）。")
    try:
        # 断线自愈：ws.start() 正常返回或抛异常都意味着长连接已经断了
        # （实测会出现 keepalive ping timeout 导致 receive loop exit）。
        # 常驻服务 + 23:00 日终定时器都挂在本进程里，不能让进程静默退出。
        while True:
            try:
                ws.start()
                print("[监听] 长连接已断开，5 秒后重连…")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[监听] 长连接异常：{e}；5 秒后重连…")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n监听已停止。")
