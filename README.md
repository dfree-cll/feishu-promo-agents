# Feishu Promo Agents — 飞书推广费用双 Agent 系统

基于 LLM Agent（function-calling 工具循环）的飞书群协作自动化系统。
两个各司其职的 Agent 把「群里发申请 → 人工记账 → 人工汇总」变成全自动：

- **Sub-Agent「推广助手」**：理解群里任意口语化的费用申请（抖加/垫付），写入多维表格台账，
  并以结构化事件上报上级；同时负责接单状态、打款状态的状态回写；
- **Master-Agent「综合助手」**：消费结构化事件，按 歌曲 × 日期 聚合生成推广日报写入《综合管理表》，
  并向管理者推送日报卡片；支持自然语言问账（"这首歌花了多少钱？接单打款怎么样？"）。

每天 23:00 自动完成当日终版汇总。提供零配置的离线模拟模式，克隆即可体验全流程。

## 特性

- 🤖 **真 Agent 架构**：LLM function-calling 循环驱动，控制流由模型决定，
  而非写死的脚本流程；工具参数一律经白名单校验后才落库
- 👥 **双 Agent 协作**：Sub 写明细、Master 聚合汇报，职责与数据权限分离；
  汇总类问题由 Sub 自动转交 Master（Agent 间协作）
- 💬 **群消息实时触发**：飞书官方 SDK WebSocket 长连接订阅消息事件，
  无需公网服务器；支持 @提及清洗、事件去重、多消息并发处理
- 🚦 **混合路由**：标准格式申请走规则快速路径（0 次 LLM，2-3 秒），
  口语化消息走 Agent 路径（6-9 秒）；未配置 LLM Key 自动降级为纯规则模式
- 🔁 **三层幂等**（应对飞书"至少一次"事件投递）：表格唯一键 upsert →
  事件内容哈希去重 → 消息 event_id 去重
- 📊 **数据质量护栏**：汇总口径自动核对告警；问数结果由代码精确计算并渲染，
  模型只负责转述，结构性杜绝编造数字与姓名
- ⏰ **日终汇总**：每晚 23:00 对当日全部申请做全量重算并推送终版日报
- 🔌 **模型可插拔**：支持 GLM / DeepSeek / Kimi 等任意 OpenAI 兼容服务，改一行配置切换
- 🧪 **离线模拟模式**：不接真实飞书也能完整演示读写与消息流
- 📚 **知识库**：`data/knowledge/*.md` 维护业务术语与流程，提示词注入摘要 + `search_knowledge` 工具检索，热更新
- 🧠 **记忆系统**：每个群的会话记忆（支持"再上一笔"类指代）+ 申请操作记忆（"刚才那笔"召回），持久化到本地
- 🔭 **可观测**：结构化运行日志（JSONL）、指标快照（消息/LLM/工具/错误率）、`trace` 命令回放单次处理时间线

## 架构

```
【飞书群】任意口语化消息（申请 / 状态反馈 / 提问）
        ▼
┌─ listener：长连接监听 ─────────────────────────────────┐
│ 事件去重 → @提及清洗 → 混合路由 → 线程池并发处理       │
└──────────────────────┬─────────────────────────────────┘
                       ▼
┌─ Sub-Agent「推广助手」＝ LLM + 工具循环 ────────────────┐
│  submit_application（写《推广账号表》+ 结构化上报）     │
│  update_order_status / mark_paid（状态回写）            │
│  lookup_promo_table（查重/明细）                        │
│  ask_master（Agent 间协作：汇总类问题转交上级）         │
└──────────────────────┬─────────────────────────────────┘
                       ▼ 事件总线（SummaryEvent JSON，内容哈希幂等）
┌─ Master-Agent「综合助手」＝ LLM + 工具循环 ─────────────┐
│  summarize_applications（按天全量重算 →《综合管理表》） │
│  query_daily_reports / lookup_promo_table（查真实数据） │
│  向管理者推送日报卡片（抖加/垫付分列、接单、打款）      │
└──────────────────────┬─────────────────────────────────┘
                       ▼
        飞书多维表格《推广账号表》《综合管理表》+ 群消息卡片
```

**Agent 间通信协议（SummaryEvent）**核心字段：
`event_id / event_type(doujia_summary|dianfu_summary) / summary_date / payer /
expected_pay_date / records[{song, blogger, amount, tx_count}] / warnings`
事件 ID 由内容 SHA-256 确定性生成——同一份申请重复提交不会产生新事件。

## 环境要求

- Python 3.10+
- 飞书账号（个人版即可），用于创建自建应用与测试群
- （可选）一个 OpenAI 兼容的 LLM API Key：GLM / DeepSeek / Kimi / OpenAI 均可。
  不配置时系统自动降级为规则解析模式，仍可完整运行

## 快速开始（离线模拟模式）

```bash
git clone https://github.com/<you>/feishu-promo-agents.git
cd feishu-promo-agents
pip install -r requirements.txt

copy .env.example .env        # Windows；macOS/Linux 用 cp

python -m main check          # 自检
python -m main demo           # 一键演示：样例申请 -> 入表 -> 上报 -> 日报
python -m main seed-roster    # （可选）预置达人名单
```

`demo` 会用内置样例数据完整走一遍：解析入表 → 结构化上报 → 聚合写《综合管理表》→
输出日报。表格与消息同时存档在 `state/` 目录，重复运行不会产生重复数据。

### 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q     # 纯逻辑回归用例：零网络、零飞书凭证
```


## 接入真实飞书环境

### 1. 创建自建应用

在[飞书开发者后台](https://open.feishu.cn/app)创建**企业自建应用**，然后：

1. 「凭证与基础信息」页复制 `App ID / App Secret`；
2. 「添加应用能力」中添加 **机器人**；
3. 「权限管理」开通以下权限：
   - `bitable:app`（多维表格读写）
   - `im:message` 与 `im:message:send_as_bot`（发送消息卡片）
   - `im:chat:readonly`（获取机器人所在群列表）
4. 「应用发布 → 版本管理与发布」发布新版本，并将**可用范围**设为包含你自己；
5. 把机器人加入测试群（群设置 → 群机器人 → 添加机器人 → 搜索应用名）。

### 2. 配置 .env

```ini
FEISHU_MOCK=0
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
# 以下两项留空，setup 会自动生成
FEISHU_BITABLE_APP_TOKEN=
FEISHU_CHAT_ID=
```

### 3. 初始化

```bash
python -m main setup       # 自动创建多维表格+两张表（自动共享给群主），并列出机器人所在群
```

把机器人所在群的 `chat_id` 填入 `.env` 的 `FEISHU_CHAT_ID`，然后：

```bash
python -m main check       # 全部 ✅ 即就绪
python -m main entrance    # 向群里发表格入口卡片（可置顶）
```

### 4. 开启群消息实时监听（可选）

开发者后台 →「事件与回调」→ 订阅方式选「**使用长连接接收事件**」→
添加事件 **接收消息 `im.message.receive_v1`** → 发布新版本。之后：

```bash
python -m main listen      # 常驻进程：@机器人 消息实时触发 + 每天 23:00 日终汇总
```

> 提示：`scripts/启动监听.bat` 可双击启动（带日志窗口）；
> 把 `scripts/listen_hidden.bat` 的快捷方式放入 Windows「启动」文件夹即可登录后静默自启。

### 5. 配置 LLM（启用 Agent 路径）

在 `.env` 填入任一 OpenAI 兼容服务：

| 服务 | LLM_BASE_URL | 模型示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash` / `deepseek-v4-pro` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

## 命令一览

| 命令 | 说明 |
|---|---|
| `python -m main check` | 连通性自检（凭证/表格/群） |
| `python -m main setup` | 真实模式一键建表 + 共享 + 列群 |
| `python -m main demo` | 规则路径一键演示（样例数据） |
| `python -m main agent-demo` | Agent 路径演示（口语化样例，需 LLM） |
| `python -m main listen` | 群消息监听常驻（含 23:00 日终定时） |
| `python -m main schedule` | 仅 23:00 日终汇总常驻 |
| `python -m main summary` | 手动执行一次当日日终总结 |
| `python -m main ingest --file <file> \| --text "<文本>"` | 摄入申请总结文本 |
| `python -m main entrance` | 重发"表格入口"卡片 |
| `python -m main seed-roster` | 预置达人名单（幂等） |
| `python -m main rebuild` | 从事件总线全量重算日报 |
| `python -m main reset` | 清空表格记录与事件状态 |
| `python -m main metrics` | 查看可观测指标（消息/LLM/工具/错误率） |
| `python -m main trace [run_id]` | 回放一次 Agent 处理的完整时间线（`last`=最近一次） |
| `python -m main kb "抖加和垫付的区别"` | 检索知识库（调试） |

## Agent 工具一览

| 工具 | 归属 | 说明 |
|---|---|---|
| `submit_application` | Sub | 提交一笔费用申请（抖加/垫付），写台账 + 结构化上报一步完成 |
| `lookup_promo_table` | Sub/Master | 查询台账明细（报价/抖音号/接单/打款），可查重 |
| `update_order_status` | Sub | 回写达人接单状态（已接单/待确认/已拒绝） |
| `mark_paid` | Sub | 回写打款状态与实际打款日期 |
| `notify_master`（内部） | Sub | 结构化事件上报（已合并进 submit_application） |
| `ask_master` | Sub | 汇总类问题转交 Master-Agent（Agent 间协作） |
| `summarize_applications` | Master | 按天全量重算并写《综合管理表》 |
| `query_daily_reports` | Master | 查询日报（支持按歌曲/日期过滤） |

## 数据与表格设计

系统自动创建一个多维表格，含两张数据表：

**《推广账号表》**（Sub-Agent 维护，博主粒度）

- 主档列（投放前预置，Agent 不覆盖）：账号类型、抖音号、报价、发布日期、是否接单
- 执行列（申请/反馈驱动）：抖加金额/笔数/支付人、垫付金额、是否打款、
  打款人、预计打款日期、实际打款日期
- 唯一键 `行键 = 歌曲|博主`：同一博主的抖加与垫付合并在同一行

**《综合管理表》**（Master-Agent 维护，歌曲 × 日期粒度）

- 每日一行：抖加/垫付/总花费（分列）、笔数、参与达人数、达人接单情况、
  打款情况、支付人/打款人、核对备注（口径差异自动告警）
- 唯一键 `报告键 = 日期|歌曲`

## 设计要点

- **三层幂等**：表格行键 upsert → 事件内容哈希（SHA-256）→ 消息 event_id 去重缓存。
  飞书事件为"至少一次"投递，重复消息在每一层都会被拦截
- **混合路由与优雅降级**：标准格式走规则快速路径，口语化走 LLM；
  LLM 不可用时自动回落规则模式，系统永远可用
- **防幻觉**：问数结果由代码精确计算并渲染为 `answer_text`，
  通过工具循环的 `answer_key` 直通返回，模型只负责转述
- **参数安全**：LLM 生成的工具参数一律过 `validators` 白名单
  （类型转换、长度、金额范围、日期格式、角色区分）后才落库
- **稳定性**：飞书 API 调用带超时/限流重试退避；Master 按天全量重算 +
  失败按日期隔离（事件保留自动重试）；共享状态文件加锁；单条消息异常不影响整体服务

## 目录结构

```
feishu-promo-agents/
├── main.py                  # CLI 入口
├── promo_agent/             # 核心代码包
│   ├── config.py            #   .env 配置加载
│   ├── models.py            #   数据模型（申请/事件/日报）
│   ├── llm_client.py        #   LLM 客户端 + 通用 tool-calling 循环
│   ├── validators.py        #   工具参数校验白名单
│   ├── feishu_client.py     #   飞书开放平台客户端（Bitable + IM/Webhook）
│   ├── table_schema.py      #   表字段定义与行映射
│   ├── extractor.py         #   规则解析兜底路径 + 口径核对
│   ├── agent_tools.py       #   工具清单（JSON Schema）
│   ├── sub_agent.py         #   Sub-Agent「推广助手」
│   ├── master_agent.py      #   Master-Agent「综合助手」
│   ├── listener.py          #   长连接监听（路由/去重/并发/定时）
│   ├── knowledge.py         #   知识库检索（热更新）
│   ├── memory.py            #   会话记忆 + 操作记忆
│   └── observability.py     #   结构化日志/指标/追踪
├── data/
│   ├── roster.json          #   达人名单（seed-roster 预置用）
│   ├── knowledge/           #   知识库内容（Markdown，可直接编辑扩充）
│   └── samples/             #   申请总结样例
├── scripts/                 # 启动脚本（前台/静默）
├── state/                   # 运行时产物（不入库）
├── requirements.txt
├── .env.example
└── CHANGELOG.md             # 变更日志（本地维护）
```

## 常见问题

**Q：调用报 `99991672` 权限错误？**
应用缺少对应 scope。按报错信息里的官方链接开通权限，并**发布新版本**后重试。

**Q：报 `232034` 应用不可用？**
应用的「可用范围」为空或不含操作者，且/或最新版本未生效。设置可用范围后重新发布版本。

**Q：群里添加机器人时搜不到应用？**
确认已添加「机器人」应用能力、版本已发布生效、可用范围包含自己，再用应用名搜索。

**Q：应用创建的多维表格我在云文档里看不到？**
应用自建的文档归属应用本身。系统在创建后会自动把群主加为协作者并发送通知；
也可直接点消息卡片里的表格链接。

**Q：同一笔申请发多次会重复记账吗？**
不会。表格行按唯一键 upsert，事件按内容哈希去重，消息按 event_id 去重，三层防线。

**Q：回复大概多久？**
标准格式申请约 2-3 秒（规则快速路径）；口语化消息约 6-9 秒（LLM 工具循环）；
多条消息并发处理、互不排队。

## 变更历史

见 [CHANGELOG.md](CHANGELOG.md)（本地维护，不入库）。
