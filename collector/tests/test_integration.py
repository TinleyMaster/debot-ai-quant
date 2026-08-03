"""
主流程集成测试 - 测试 API 优先 + Playwright fallback 逻辑
使用 mock 方式，不依赖真实数据库和浏览器
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from io import BytesIO

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# mock 数据库
# ============================================================

@pytest.fixture
def mock_db():
    """mock 掉所有数据库操作"""
    with patch("main.init_db_pool"), \
         patch("main.close_db_pool"), \
         patch("main.get_conn"), \
         patch("main.insert_signal", return_value=None), \
         patch("main.insert_run_log"), \
         patch("main.insert_alert"), \
         patch("main.get_unprocessed_count", return_value=0), \
         patch("main.get_latest_unresolved_alert", return_value=None):
        yield


# ============================================================
# 测试：API 模式正常工作
# ============================================================

def _make_mock_response(data):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


class TestAPIMode:
    def test_api_mode_fetch_success(self, mock_db):
        """API 模式下成功拉取信号"""
        from api_client import DebotAPIClient

        # 构造 API 响应
        results = [
            {
                "id": "sig-001",
                "create_time": 1785700000,
                "token": "TOKEN001pump",
                "token_trading_stat": {"price": 0.001, "mkt_cap": 100000, "holders": 100},
                "wallet_stats": [{"volume": "100"}, {"volume": "200"}],
            }
        ]
        tokens = {
            "TOKEN001pump": {"symbol": "TOK1", "name": "Token 1", "creation_timestamp": 1785600000}
        }
        api_data = {
            "code": 0,
            "data": {
                "results": results,
                "meta": {"tokens": tokens},
                "total": 1,
                "next": None,
            }
        }
        mock_resp = _make_mock_response(api_data)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            client = DebotAPIClient(base_url="http://test.local", request_interval=0)
            signals = client.fetch_all_signals()

            assert len(signals) == 1
            assert signals[0]["contract_address"] == "TOKEN001pump"
            assert signals[0]["token_symbol"] == "TOK1"
            assert signals[0]["smart_wallets"] == 2
            assert signals[0]["avg_buy_amount"] == 150.0  # (100+200)/2

    def test_run_once_api_mode(self, mock_db):
        """run_once 在 API 模式下正常运行"""
        import main

        # 重置全局状态
        main._collect_method = "api"
        main._api_fallback_count = 0
        main._consecutive_empty_rounds = 0

        # mock API 客户端
        mock_api = MagicMock()
        mock_api.total_api_calls = 2
        mock_api.fetch_all_signals.return_value = [
            {"contract_address": "TEST1", "token_symbol": "T1",
             "signal_time": "2026-01-01T00:00:00+00:00",
             "market_cap": 1000, "price_usd": 0.01,
             "holders_count": 10, "smart_wallets": 3,
             "signal_content": "{}", "pool_value": 500,
             "source_url": "http://test/1", "holder_rate": None,
             "market_cap_prev": None, "price_usd_prev": None,
             "token_age": "1h", "avg_buy_amount": 100,
             "multiplier": None}
        ]

        # mock 数据库 insert_signal 返回 1（表示新数据）
        with patch("main.insert_signal", return_value=1):
            stats = main.run_once(scraper=None, api_client=mock_api)

            assert stats["method"] == "api"
            assert stats["scraped"] == 1
            assert stats["new"] == 1
            assert stats["errors"] == 0
            assert main._api_fallback_count == 0  # 成功时计数清零


# ============================================================
# 测试：API 失败 → 回退到 Playwright
# ============================================================

class TestFallback:
    def test_api_failure_increments_count(self, mock_db):
        """API 失败时 fallback 计数增加"""
        import main

        main._collect_method = "api"
        main._api_fallback_count = 0

        mock_api = MagicMock()
        mock_api.fetch_all_signals.side_effect = Exception("API down")

        stats = main.run_once(scraper=None, api_client=mock_api)

        assert stats["method"] == "api"
        assert stats["errors"] >= 1
        assert main._api_fallback_count == 1

    def test_api_empty_increments_count(self, mock_db):
        """API 返回空数据时 fallback 计数增加"""
        import main

        main._collect_method = "api"
        main._api_fallback_count = 0

        mock_api = MagicMock()
        mock_api.fetch_all_signals.return_value = []

        stats = main.run_once(scraper=None, api_client=mock_api)

        assert stats["scraped"] == 0
        assert main._api_fallback_count == 1

    def test_api_fallback_after_3_failures(self, mock_db):
        """连续 3 次失败后切换到 playwright 模式"""
        import main

        main._collect_method = "api"
        main._api_fallback_count = 0

        # mock playwright scraper 也返回空
        mock_scraper = MagicMock()
        mock_scraper.scrape_signals.return_value = []
        mock_scraper._block_reason = ""

        mock_api = MagicMock()
        mock_api.fetch_all_signals.side_effect = Exception("fail")

        # 失败 3 次
        for i in range(3):
            main.run_once(scraper=mock_scraper, api_client=mock_api)

        # 第 3 次失败后应该切换模式
        assert main._collect_method == "playwright"

    def test_playwright_mode_uses_scraper(self, mock_db):
        """playwright 模式下调用 scraper"""
        import main

        main._collect_method = "playwright"

        mock_scraper = MagicMock()
        mock_scraper.scrape_signals.return_value = [
            {"contract_address": "TEST2", "token_symbol": "T2",
             "signal_time": "2026-01-01T00:00:00+00:00",
             "market_cap": 2000, "price_usd": 0.02,
             "holders_count": 20, "smart_wallets": 5,
             "signal_content": "{}", "pool_value": 1000,
             "source_url": "http://test/2", "holder_rate": None,
             "market_cap_prev": None, "price_usd_prev": None,
             "token_age": "2h", "avg_buy_amount": 200,
             "multiplier": None}
        ]
        mock_scraper._block_reason = ""

        with patch("main.insert_signal", return_value=1):
            stats = main.run_once(scraper=mock_scraper, api_client=None)

            assert stats["method"] == "playwright"
            mock_scraper.scrape_signals.assert_called_once()
            assert stats["scraped"] == 1
            assert stats["new"] == 1


# ============================================================
# 测试：数据库写入
# ============================================================

class TestDatabaseIntegration:
    def test_insert_signal_dedupe(self, mock_db):
        """重复信号不重复插入"""
        import main

        main._collect_method = "api"

        mock_api = MagicMock()
        mock_api.fetch_all_signals.return_value = [
            {"contract_address": "DUP1", "token_symbol": "D",
             "signal_time": "2026-01-01T00:00:00+00:00",
             "market_cap": 100, "price_usd": 0.01,
             "holders_count": 1, "smart_wallets": 1,
             "signal_content": "{}", "pool_value": 50,
             "source_url": "http://test/1", "holder_rate": None,
             "market_cap_prev": None, "price_usd_prev": None,
             "token_age": "1h", "avg_buy_amount": 10,
             "multiplier": None}
        ]

        # insert_signal 返回 None 表示重复
        with patch("main.insert_signal", return_value=None):
            stats = main.run_once(scraper=None, api_client=mock_api)
            assert stats["scraped"] == 1
            assert stats["new"] == 0  # 没有新增

    def test_run_log_written(self, mock_db):
        """每轮都会写入运行日志"""
        import main
        from main import _write_run_log

        mock_insert = MagicMock()
        with patch("main.insert_run_log", mock_insert):
            _write_run_log({"scraped": 10, "new": 5, "errors": 0,
                           "error_detail": None, "alert_type": None,
                           "duration_ms": 1000})
            mock_insert.assert_called_once()


# ============================================================
# 测试：去重逻辑
# ============================================================

class TestDeduplication:
    def test_api_dedup_by_id(self):
        """API 拉取按信号 ID 去重"""
        from api_client import DebotAPIClient

        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        # 两条相同 ID 的信号
        result1 = {
            "id": "same-id",
            "token": "TOKEN1pump",
            "create_time": 1785700000,
            "token_trading_stat": {"price": 0.01, "mkt_cap": 1000, "holders": 10},
            "wallet_stats": [],
        }
        token_map = {"TOKEN1pump": {"symbol": "T1", "creation_timestamp": 1785600000}}

        # 模拟两页都包含同一个信号
        api_data = {
            "code": 0,
            "data": {
                "results": [result1, result1],  # 同页重复
                "meta": {"tokens": token_map},
                "total": 1,
                "next": None,
            }
        }
        mock_resp = _make_mock_response(api_data)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            signals = client.fetch_all_signals()
            assert len(signals) == 1  # 去重后只有 1 条


# ============================================================
# 测试：配置环境变量
# ============================================================

class TestEnvConfig:
    def test_api_client_env_config(self, monkeypatch):
        """API 客户端从环境变量读取配置"""
        monkeypatch.setenv("DEBOT_BASE_URL", "https://custom.api")
        monkeypatch.setenv("DEBOT_CHAIN", "bsc")
        monkeypatch.setenv("DEBOT_API_PAGE_SIZE", "30")
        monkeypatch.setenv("DEBOT_API_MAX_PAGES", "5")
        monkeypatch.setenv("DEBOT_API_INTERVAL", "0.5")

        from api_client import create_client_from_env
        client = create_client_from_env()

        assert client.base_url == "https://custom.api"
        assert client.chain == "bsc"
        assert client.page_size == 30
        assert client.max_pages == 5
        assert client.request_interval == 0.5

    def test_api_client_env_defaults(self, monkeypatch):
        """未设置环境变量时使用默认值"""
        # 确保环境变量为空
        for key in ["DEBOT_BASE_URL", "DEBOT_CHAIN", "DEBOT_API_PAGE_SIZE",
                     "DEBOT_API_MAX_PAGES", "DEBOT_API_INTERVAL"]:
            monkeypatch.delenv(key, raising=False)

        from api_client import create_client_from_env
        client = create_client_from_env()

        assert client.base_url == "https://debot.ai"
        assert client.chain == "solana"
        assert client.page_size == 50
        assert client.max_pages == 20
        assert client.request_interval == 0.3
