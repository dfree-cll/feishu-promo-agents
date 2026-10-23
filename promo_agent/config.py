# -*- coding: utf-8 -*-
"""全局配置：从 .env 读取，提供路径常量。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # 允许未安装 dotenv 时直接用环境变量
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent   # 仓库根目录（包的上一级）
DATA_DIR = PROJECT_ROOT / "data"
STATE_DIR = PROJECT_ROOT / os.getenv("STATE_DIR", "state")

# ---------- 飞书凭证 ----------
FEISHU_MOCK = os.getenv("FEISHU_MOCK", "1") == "1"   # 默认离线模拟模式，配好凭证后改为 0
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE_URL = os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn")

# 为空时 setup 命令会自动创建多维表格并回写到这里
FEISHU_BITABLE_APP_TOKEN = os.getenv("FEISHU_BITABLE_APP_TOKEN", "")
# 机器人所在群 chat_id（消息卡片发到这里）；为空则尝试 Webhook 自定义机器人
FEISHU_CHAT_ID = os.getenv("FEISHU_CHAT_ID", "")
# 自定义机器人 Webhook（群设置->群机器人->自定义机器人）；配置后无需任何 IM 权限
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")

SUB_AGENT_NAME = "推广助手（Sub-Agent）"
MASTER_AGENT_NAME = "综合助手（Master-Agent）"

# ---------- 并发配置 ----------
LISTEN_WORKERS = int(os.getenv("LISTEN_WORKERS", "8"))        # 群消息并发工作线程数
MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "8"))  # LLM 调用并发上限（信号量）

# ---------- 审批配置 ----------
# 人工审核流开关：1 = 申请入表后进入「待审核」，需管理者回复同意/驳回；0 = 申请直接生效（已批准）
REVIEW_ENABLED = os.getenv("REVIEW_ENABLED", "0") == "1"
# 有权执行审核/回滚的发送者 open_id（逗号分隔）；留空 = 不校验（演示环境）
APPROVER_OPEN_IDS = [x.strip() for x in os.getenv("APPROVER_OPEN_IDS", "").split(",") if x.strip()]

# ---------- 业务日切账 ----------
# 每天该时刻日终截账：之后的申请计入下一业务日（默认 23 点）
BUSINESS_DAY_CUTOFF_HOUR = int(os.getenv("BUSINESS_DAY_CUTOFF_HOUR", "23"))

# ---------- LLM（Agent 的大脑；OpenAI 兼容接口均可）----------
# 例：GLM   https://open.bigmodel.cn/api/paas/v4   模型 glm-4-flash
# 例：DeepSeek https://api.deepseek.com           模型 deepseek-chat
# 例：Kimi  https://api.moonshot.cn/v1            模型 moonshot-v1-8k
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

for d in (DATA_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)
