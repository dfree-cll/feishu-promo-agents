# -*- coding: utf-8 -*-
"""把仓库根目录加入 sys.path，保证 `pytest` 与 `python -m pytest` 都能直接导入 promo_agent。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
