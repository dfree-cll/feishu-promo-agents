# -*- coding: utf-8 -*-
"""
飞书开放平台客户端（多维表格 + 消息）。

- FEISHU_MOCK=1：离线模拟模式，全部读写落在 state/mock_feishu.json，无需任何凭证即可跑通全流程；
- FEISHU_MOCK=0：真实模式，走飞书开放平台 HTTP API（需要自建应用 app_id/secret + 权限）。
"""
import json
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests

from promo_agent import config
from promo_agent.table_schema import (BITABLE_NAME, PROMO_TABLE, MGMT_TABLE,
                                      PROMO_FIELDS, MGMT_FIELDS)

TOKEN_CACHE = config.STATE_DIR / "token_cache.json"
MOCK_STORE = config.STATE_DIR / "mock_feishu.json"


class FeishuError(RuntimeError):
    pass


# ---------------------------------------------------------------- mock 存储
def _mock_load() -> dict:
    if MOCK_STORE.exists():
        return json.loads(MOCK_STORE.read_text(encoding="utf-8"))
    return {"bitables": {}, "messages": []}


def _mock_save(store: dict):
    MOCK_STORE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


class FeishuClient:
    def __init__(self, mock: Optional[bool] = None):
        self.mock = config.FEISHU_MOCK if mock is None else mock
        self._token: Optional[str] = None
        self._token_expire_at = 0
        self._http = requests.Session()          # 连接复用，省去每次 TLS 握手
        self._bitable_token: Optional[str] = None
        self._table_cache: Dict[str, str] = {}   # 表名 -> table_id 进程内缓存
        self._fields_synced: set = set()         # 已做过字段同步的表（每进程一次）

    # ================================================== 基础
    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 300:
            return self._token
        if TOKEN_CACHE.exists():
            c = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if c.get("token") and time.time() < c.get("expire_at", 0) - 300:
                self._token, self._token_expire_at = c["token"], c["expire_at"]
                return self._token
        r = self._http.post(
            f"{config.FEISHU_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
            timeout=15)
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 tenant_access_token 失败：{data}")
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200))
        TOKEN_CACHE.write_text(json.dumps(
            {"token": self._token, "expire_at": self._token_expire_at}), encoding="utf-8")
        return self._token

    def _api(self, method: str, path: str, payload: Optional[dict] = None,
             params: Optional[dict] = None) -> dict:
        """带重试的 API 调用：网络超时/连接错误/限流时按 1s/2s/4s 退避重试。"""
        last_err = None
        for attempt in range(3):
            try:
                r = self._http.request(
                    method, f"{config.FEISHU_BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {self._tenant_token()}",
                             "Content-Type": "application/json; charset=utf-8"},
                    json=payload, params=params, timeout=30)
            except requests.RequestException as e:   # 超时/连接失败：退避重试
                last_err = e
                time.sleep(2 ** attempt)
                continue
            data = r.json()
            if data.get("code") == 0:
                return data.get("data", {})
            last_err = FeishuError(f"{method} {path} 失败：{data}")
            # 限流/服务端错误才重试，参数类错误直接抛出
            if data.get("code") in (99991400, 99991401) or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise last_err
        raise FeishuError(f"{method} {path} 重试 3 次仍失败：{last_err}")

    # ================================================== 多维表格
    def ensure_bitable(self) -> str:
        """返回 bitable app_token；真实模式下为空则自动创建（进程内缓存）。"""
        if self.mock:
            return "mock_bitable"
        if self._bitable_token:
            return self._bitable_token
        if config.FEISHU_BITABLE_APP_TOKEN:
            self._bitable_token = config.FEISHU_BITABLE_APP_TOKEN
            return self._bitable_token
        data = self._api("POST", "/open-apis/bitable/v1/apps", {"name": BITABLE_NAME})
        token = data["app"]["app_token"]
        self._share_to_chat_owner(token)
        # 回写 .env，避免重复建表
        env_path = config.PROJECT_ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        lines = [l for l in lines if not l.startswith("FEISHU_BITABLE_APP_TOKEN=")]
        lines.append(f"FEISHU_BITABLE_APP_TOKEN={token}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config.FEISHU_BITABLE_APP_TOKEN = token
        self._bitable_token = token
        return token

    def _share_to_chat_owner(self, app_token: str):
        """应用自建的表格对用户不可见，自动把群主加为完全访问协作者（失败不阻塞）。"""
        if not config.FEISHU_CHAT_ID:
            return
        try:
            chat = self._api("GET", f"/open-apis/im/v1/chats/{config.FEISHU_CHAT_ID}")
            owner = chat.get("owner_id")
            if owner:
                self._api("POST", f"/open-apis/drive/v1/permissions/{app_token}/members",
                          {"member_type": "openid", "member_id": owner, "perm": "full_access"},
                          params={"type": "bitable", "need_notification": "true"})
                print("[提示] 已把多维表格共享给群主（完全访问）")
        except FeishuError as e:
            print(f"[警告] 自动共享表格失败（可手动在文档里添加协作者）：{e}")

    def _sync_missing_fields(self, app_token: str, table_id: str, wanted: List[dict]):
        """已有表格缺新字段时（如后续新增的“实际打款日期”），自动补齐。"""
        try:
            existing = {f["field_name"] for f in self._api(
                "GET", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                params={"page_size": 100}).get("items", [])}
        except FeishuError as e:
            print(f"[警告] 读取表字段失败，跳过字段同步：{e}")
            return
        for f in wanted:
            if f["field_name"] in existing:
                continue
            body = {"field_name": f["field_name"], "type": f["type"]}
            if f.get("property"):
                body["property"] = f["property"]
            try:
                self._api("POST", f"/open-apis/bitable/v1/apps/{app_token}"
                                  f"/tables/{table_id}/fields", body)
                print(f"[字段同步] 已补充字段：{f['field_name']}")
            except FeishuError as e:
                print(f"[警告] 补充字段 {f['field_name']} 失败：{e}")

    def ensure_table(self, app_token: str, name: str, fields: List[dict]) -> str:
        """按名字找表，没有则建表（带字段）；已有表自动补缺失字段。返回 table_id。

        结果进程内缓存：表/字段结构极少变化，避免每次操作都重复“列表+查字段”。
        """
        if self.mock:
            store = _mock_load()
            bt = store["bitables"].setdefault(app_token, {"tables": {}, "url": ""})
            table_id = f"tbl_{name}"
            if table_id not in bt["tables"]:
                bt["tables"][table_id] = {"name": name, "fields": fields, "records": {}}
                _mock_save(store)
            return table_id
        if name in self._table_cache:
            return self._table_cache[name]
        tables = self._api("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables",
                           params={"page_size": 50}).get("items", [])
        for t in tables:
            if t["name"] == name:
                self._table_cache[name] = t["table_id"]
                if t["table_id"] not in self._fields_synced:
                    self._sync_missing_fields(app_token, t["table_id"], fields)
                    self._fields_synced.add(t["table_id"])
                return t["table_id"]
        data = self._api("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables",
                         {"table": {"name": name, "fields": fields}})
        self._table_cache[name] = data["table_id"]
        self._fields_synced.add(data["table_id"])   # 新建的表字段即建即全
        return data["table_id"]

    def ensure_all_tables(self) -> Dict[str, str]:
        app_token = self.ensure_bitable()
        return {
            PROMO_TABLE: self.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS),
            MGMT_TABLE: self.ensure_table(app_token, MGMT_TABLE, MGMT_FIELDS),
        }

    def find_record(self, app_token: str, table_id: str, key_field: str,
                    key_value: str) -> tuple:
        """按唯一键字段查记录；返回 (record_id, fields)，找不到返回 (None, None)。"""
        if self.mock:
            store = _mock_load()
            records = store["bitables"][app_token]["tables"][table_id]["records"]
            for rid, rec in records.items():
                if rec.get(key_field) == key_value:
                    return rid, rec
            return None, None
        data = self._api(
            "POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            {"filter": {"conjunction": "and",
                        "conditions": [{"field_name": key_field, "operator": "is",
                                        "value": [key_value]}]}},
            params={"page_size": 5})
        items = data.get("items") or []
        if not items:
            return None, None
        return items[0]["record_id"], items[0].get("fields") or {}

    def search_records(self, app_token: str, table_id: str, field_name: str,
                       value: str) -> List[dict]:
        """按字段值查多条记录，返回字段字典列表。"""
        if self.mock:
            store = _mock_load()
            records = store["bitables"][app_token]["tables"][table_id]["records"]
            return [rec for rec in records.values() if rec.get(field_name) == value]
        data = self._api(
            "POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            {"filter": {"conjunction": "and",
                        "conditions": [{"field_name": field_name, "operator": "is",
                                        "value": [value]}]}},
            params={"page_size": 100})
        return [it.get("fields", {}) for it in (data.get("items") or [])]

    def search_all_records(self, app_token: str, table_id: str,
                           page_size: int = 100) -> List[dict]:
        """拉取表内全部记录（上限 page_size 条）。"""
        if self.mock:
            store = _mock_load()
            records = store["bitables"][app_token]["tables"][table_id]["records"]
            return list(records.values())
        data = self._api(
            "POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            {}, params={"page_size": page_size})
        return [it.get("fields", {}) for it in (data.get("items") or [])]

    def clear_records(self, app_token: str, table_id: str) -> int:
        """清空表中所有记录（保留表与字段定义），返回删除条数。"""
        if self.mock:
            store = _mock_load()
            records = store["bitables"][app_token]["tables"][table_id]["records"]
            count = len(records)
            records.clear()
            _mock_save(store)
            return count
        total, page_token = 0, ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._api("POST",
                             f"/open-apis/bitable/v1/apps/{app_token}"
                             f"/tables/{table_id}/records/search", {}, params=params)
            ids = [it["record_id"] for it in (data.get("items") or [])]
            if ids:
                self._api("POST", f"/open-apis/bitable/v1/apps/{app_token}"
                                  f"/tables/{table_id}/records/batch_delete",
                          {"records": ids})
                total += len(ids)
            page_token = data.get("page_token") or ""
            if not data.get("has_more"):
                break
        return total

    @staticmethod
    def _to_write_format(v):
        """把读取接口返回的字段值规范化为写入格式（文本分段 -> 字符串）。"""
        if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
            return "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
        return v

    def upsert_record(self, app_token: str, table_id: str, key_field: str,
                      key_value: str, fields: dict) -> str:
        """按唯一键 upsert：已存在则「合并字段后整体写回」，避免覆盖另一侧（抖加/垫付）的列。"""
        record_id, old_fields = self.find_record(app_token, table_id, key_field, key_value)
        old_fields = {k: self._to_write_format(v) for k, v in (old_fields or {}).items()}
        merged = {**old_fields, **fields}
        if self.mock:
            store = _mock_load()
            records = store["bitables"][app_token]["tables"][table_id]["records"]
            if record_id is None:
                record_id = f"rec_{len(records) + 1:04d}"
            records[record_id] = merged
            _mock_save(store)
            return record_id
        if record_id:
            self._api("PUT", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}"
                             f"/records/{record_id}", {"fields": merged})
        else:
            data = self._api("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                                     f"{table_id}/records", {"fields": merged})
            record_id = data["record"]["record_id"]
        return record_id

    # ================================================== 查询视图（Agent 工具用）
    def fetch_promo_rows(self, song: str = "", blogger: str = "") -> dict:
        """按歌曲/博主查《推广账号表》明细行（含主档字段）。"""
        app_token = self.ensure_bitable()
        table_id = self.ensure_table(app_token, PROMO_TABLE, PROMO_FIELDS)
        if song:
            rows = self.search_records(app_token, table_id, "歌曲名",
                                       str(song).strip().strip("《》"))
        elif blogger:
            rows = self.search_records(app_token, table_id, "博主名",
                                       str(blogger).strip())
        else:
            return {"hint": "请提供 song 或 blogger 之一以缩小查询范围"}
        norm = self._to_write_format
        out = []
        for f in rows:
            blogger_name = norm(f.get("博主名", ""))
            if blogger and blogger_name != str(blogger).strip():
                continue
            out.append({"blogger": blogger_name, "song": norm(f.get("歌曲名", "")),
                        "account_type": norm(f.get("账号类型", "")),
                        "douyin_id": norm(f.get("抖音号", "")),
                        "quote": norm(f.get("报价", "")),
                        "publish_date": norm(f.get("发布日期", "")),
                        "review": norm(f.get("审核状态", "")),
                        "doujia": norm(f.get("抖加金额", "")),
                        "dianfu": norm(f.get("垫付金额", "")),
                        "pay_status": norm(f.get("是否打款", "")),
                        "order": norm(f.get("是否接单", ""))})
        return {"rows": out}

    def fetch_mgmt_rows(self, song: str = "", date: str = "") -> dict:
        """查《综合管理表》日报行（Agent 工具用），支持按歌曲/日期过滤。

        totals 与 answer_text 均由代码精确计算/渲染，LLM 只允许转述，
        从结构上杜绝模型编造数字与姓名。
        """
        app_token = self.ensure_bitable()
        table_id = self.ensure_table(app_token, MGMT_TABLE, MGMT_FIELDS)
        if song:
            rows = self.search_records(app_token, table_id, "歌曲名",
                                       str(song).strip().strip("《》"))
        else:
            rows = self.search_all_records(app_token, table_id)
        norm = self._to_write_format
        reports = [{k: norm(v) for k, v in r.items() if k != "报告键"} for r in rows]

        def _num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return 0.0

        def _day(x):
            try:
                from datetime import datetime
                return datetime.fromtimestamp(int(x) / 1000).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                return str(x or "")

        if date:
            reports = [r for r in reports if _day(r.get("统计日期")) == str(date).strip()]

        totals = {
            "doujia": round(sum(_num(r.get("抖加总花费")) for r in reports), 2),
            "dianfu": round(sum(_num(r.get("垫付总花费")) for r in reports), 2),
            "total": round(sum(_num(r.get("推广总花费")) for r in reports), 2),
        }
        if not reports:
            return {"reports": [], "totals": totals,
                    "answer_text": "查询结果：没有找到相关歌曲的推广日报记录。"}
        scope = (f"歌曲《{song}》" if song else "全部歌曲") + (f" {date}" if date else "")
        lines = [f"{_day(r.get('统计日期'))}：抖加 ¥{_num(r.get('抖加总花费')):g}"
                 f"（{r.get('抖加笔数', 0)} 笔），垫付 ¥{_num(r.get('垫付总花费')):g}"
                 f"（{r.get('垫付笔数', 0)} 笔），{r.get('打款情况', '')}；"
                 f"接单：{r.get('达人接单情况', '')}"
                 for r in reports]
        answer_text = (
            f"{scope}共 {len(reports)} 条日报，抖加合计 ¥{totals['doujia']:g}，"
            f"垫付合计 ¥{totals['dianfu']:g}，总花费 ¥{totals['total']:g}。明细：\n"
            + "\n".join("- " + ln for ln in lines))
        return {"reports": reports, "totals": totals, "answer_text": answer_text}

    # ================================================== 消息
    def send_card(self, title: str, markdown: str, template: str = "blue",
                  to_chat_id: str = None) -> Optional[str]:
        """发飞书消息卡片，返回 message_id（真实模式，可用于 update_card 原地更新）。"""
        dest = to_chat_id or config.FEISHU_CHAT_ID
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": markdown}, {"tag": "hr"}],
        }
        if self.mock:
            store = _mock_load()
            store["messages"].append({"to": dest or config.FEISHU_WEBHOOK_URL or "(未配置群)",
                                      "title": title, "card": card, "sent_at": time.time()})
            _mock_save(store)
            print(f"\n{'=' * 62}\n[模拟飞书消息卡片]  -> {dest or config.FEISHU_WEBHOOK_URL or '(未配置群)'}"
                  f"\n【{title}】\n{markdown}\n{'=' * 62}")
            return None
        if dest:
            data = self._api("POST", "/open-apis/im/v1/messages",
                             {"receive_id": dest, "msg_type": "interactive",
                              "content": json.dumps(card, ensure_ascii=False)},
                             params={"receive_id_type": "chat_id"})
            return data.get("message_id")
        if config.FEISHU_WEBHOOK_URL:
            self._send_webhook(card)
            return None
        print(f"[跳过发送] 未配置 FEISHU_CHAT_ID / FEISHU_WEBHOOK_URL：【{title}】")
        return None

    def update_card(self, message_id: str, title: str, markdown: str,
                    template: str = "blue") -> bool:
        """原地更新已发送的卡片内容（回执卡“处理中”→ 最终结果）。"""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": markdown}, {"tag": "hr"}],
        }
        if self.mock:
            print(f"[模拟更新卡片 {message_id}] 【{title}】\n{markdown}")
            return True
        try:
            self._api("PATCH", f"/open-apis/im/v1/messages/{message_id}",
                      {"content": json.dumps(card, ensure_ascii=False)})
            return True
        except FeishuError as e:
            print(f"[警告] 卡片更新失败（不影响结果）：{e}")
            return False

    def _send_webhook(self, card: dict):
        url = self._validate_webhook_url(config.FEISHU_WEBHOOK_URL)
        r = self._http.post(url, json={"msg_type": "interactive", "card": card},
                            timeout=15, allow_redirects=False)
        data = r.json()
        # 自定义机器人返回旧式 StatusCode=0 或新式 code=0
        if data.get("StatusCode", data.get("code", -1)) != 0:
            raise FeishuError(f"Webhook 发送失败：{data}")

    @staticmethod
    def _validate_webhook_url(url: str) -> str:
        """防 SSRF：协议/域名/路径白名单 + 解析结果不得命中私网、环回、链路本地等保留地址。"""
        import ipaddress
        import socket
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme != "https" or p.hostname != "open.feishu.cn" \
                or not p.path.startswith("/open-apis/bot/v2/hook/"):
            raise FeishuError(
                f"Webhook 地址不合法（仅支持 https://open.feishu.cn/open-apis/bot/v2/hook/*）：{url}")
        try:
            infos = socket.getaddrinfo(p.hostname, 443, proto=socket.IPPROTO_TCP)
        except OSError as e:
            raise FeishuError(f"Webhook 域名解析失败：{e}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                raise FeishuError(f"Webhook 域名解析到私网/保留地址，已拦截：{ip}")
        return url

    def list_chats(self) -> List[Dict[str, Any]]:
        """列出机器人所在的群（setup/check 时用于拿 chat_id）。"""
        if self.mock:
            return [{"chat_id": "(mock) oc_demo_group", "name": "推广群（模拟）"}]
        return self._api("GET", "/open-apis/im/v1/chats",
                         params={"page_size": 50}).get("items", [])
