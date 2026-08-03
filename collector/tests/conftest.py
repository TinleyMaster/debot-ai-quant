"""
pytest 配置 - mock 掉 playwright 和数据库，使测试不依赖外部环境
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock

# 在导入 scraper 之前 mock playwright 模块
mock_playwright = MagicMock()
mock_sync_api = MagicMock()
mock_sync_api.sync_playwright = MagicMock()
mock_playwright.sync_api = mock_sync_api

sys.modules.setdefault('playwright', mock_playwright)
sys.modules.setdefault('playwright.sync_api', mock_sync_api)

# 也 mock psycopg2 避免集成测试时依赖
mock_psycopg2 = MagicMock()
sys.modules.setdefault('psycopg2', mock_psycopg2)
sys.modules.setdefault('psycopg2.extras', MagicMock())
sys.modules.setdefault('psycopg2.pool', MagicMock())

# 创建临时日志目录，避免 main.py 导入时 FileNotFoundError
tmp_log_dir = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp_log_dir, "data"), exist_ok=True)
# 把 LOG_DIR 设为临时目录（main.py 里如果用的是硬编码路径，需要另外处理）
# 这里我们在导入 main 之前先确保 /app/data 存在
import builtins
original_open = builtins.open
def _patched_open(file, *args, **kwargs):
    if isinstance(file, str) and file == "/app/data/collector.log":
        # 重定向到临时文件
        return original_open(os.path.join(tmp_log_dir, "data", "collector.log"), *args, **kwargs)
    return original_open(file, *args, **kwargs)
builtins.open = _patched_open
