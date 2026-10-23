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

# ---------- LLM（Agent 的大脑；OpenAI 兼容接口均可）----------
# 例：GLM   https://open.bigmodel.cn/api/paas/v4   模型 glm-4-flash
# 例：DeepSeek https://api.deepseek.com           模型 deepseek-chat
# 例：Kimi  https://api.moonshot.cn/v1            模型 moonshot-v1-8k
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

for d in (DATA_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)
