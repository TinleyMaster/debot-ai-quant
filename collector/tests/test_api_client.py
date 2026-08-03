"""
api_client.py 单元测试
使用 mock 替换 urllib.request.urlopen，不依赖真实网络
"""
import json
import time
import pytest
from unittest.mock import patch, MagicMock
from io import BytesIO

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_client import DebotAPIClient, create_client_from_env, DEFAULT_UA


# ============================================================
# 辅助函数 & 测试数据
# ============================================================

def _make_mock_response(data: dict, status: int = 200):
    """构造一个 mock HTTP 响应"""
    body = json.dumps(data).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def _make_api_response(results, tokens, total=None, next_page=None, code=0):
    """构造标准 API 返回结构"""
    return {
        "code": code,
        "description": "success",
        "data": {
            "results": results,
            "meta": {"tokens": tokens},
            "total": total if total is not None else len(results),
            "next": next_page,
        }
    }


def _make_result(idx: int = 1, token_addr: str = None):
    """构造一条 API 结果项"""
    addr = token_addr or f"TOKEN{idx:03d}pump"
    return {
        "id": f"sig-{idx:05d}",
        "channel_id": 1,
        "create_time": 1785700000 + idx * 60,
        "chain": "solana",
        "token": addr,
        "group_name": "SmartMoney#all",
        "avg_wallet_volume": str(100 + idx),
        "token_trading_stat": {
            "fdv": 100000 + idx * 1000,
            "holders": 100 + idx,
            "lastUpdateTime": 1785700000,
            "liquidity": 50000 + idx * 500,
            "mkt_cap": 60000 + idx * 600,
            "price": 0.0001 + idx * 0.00001,
            "percent1h": 5.5 + idx * 0.1,
            "percent5m": 1.2 + idx * 0.05,
            "percent24h": 20.0 + idx * 0.5,
            "volume_1h": 10000 + idx * 100,
            "volume_24h": 100000 + idx * 1000,
        },
        "wallet_stats": [
            {"alias": f"SW-{i}", "amount": "1000000", "amount_origin": 0,
             "last_trade_time": 1785700000, "price": str(0.0001 + i * 0.00001),
             "token": addr, "token_symbol": f"TOK{idx}",
             "volume": str(100 + i * 10)}
            for i in range(3)
        ]
    }


def _make_token(idx: int = 1):
    """构造一个代币信息"""
    addr = f"TOKEN{idx:03d}pump"
    return {
        "chain": "solana",
        "address": addr,
        "creator_address": f"CREATOR{idx:03d}",
        "symbol": f"TOK{idx}",
        "name": f"Token {idx}",
        "decimals": 6,
        "logo": f"https://example.com/logo{idx}.png",
        "total_supply": 1000000000000,
        "launchpad": "pump",
        "creation_timestamp": int(time.time()) - 3600 * idx,  # idx 小时前创建
    }


# ============================================================
# 测试：初始化
# ============================================================

class TestInit:
    def test_default_init(self):
        """默认初始化参数"""
        client = DebotAPIClient(base_url="http://test.local")
        assert client.base_url == "http://test.local"
        assert client.chain == "solana"
        assert client.page_size == 50
        assert client.max_pages == 20
        assert client.request_interval == 0.3
        assert client.user_agent == DEFAULT_UA
        assert client.total_api_calls == 0
        assert client.total_signals_fetched == 0

    def test_custom_init(self):
        """自定义参数"""
        client = DebotAPIClient(
            base_url="http://test.local",
            chain="eth",
            page_size=10,
            max_pages=5,
            request_interval=0.1,
        )
        assert client.chain == "eth"
        assert client.page_size == 10
        assert client.max_pages == 5
        assert client.request_interval == 0.1

    @patch.dict(os.environ, {
        "DEBOT_BASE_URL": "http://env.local",
        "DEBOT_CHAIN": "bsc",
        "DEBOT_API_PAGE_SIZE": "25",
        "DEBOT_API_MAX_PAGES": "10",
        "DEBOT_API_INTERVAL": "0.5",
    }, clear=True)
    def test_create_client_from_env(self):
        """从环境变量创建客户端"""
        client = create_client_from_env()
        assert client.base_url == "http://env.local"
        assert client.chain == "bsc"
        assert client.page_size == 25
        assert client.max_pages == 10
        assert client.request_interval == 0.5


# ============================================================
# 测试：_format_duration
# ============================================================

class TestFormatDuration:
    def test_seconds(self):
        assert DebotAPIClient._format_duration(0) == "0s"
        assert DebotAPIClient._format_duration(30) == "30s"
        assert DebotAPIClient._format_duration(59) == "59s"

    def test_minutes(self):
        assert DebotAPIClient._format_duration(60) == "1m"
        assert DebotAPIClient._format_duration(300) == "5m"
        assert DebotAPIClient._format_duration(3599) == "59m"

    def test_hours(self):
        assert DebotAPIClient._format_duration(3600) == "1h"
        assert DebotAPIClient._format_duration(10800) == "3h"
        assert DebotAPIClient._format_duration(86399) == "23h"

    def test_days(self):
        assert DebotAPIClient._format_duration(86400) == "1d"
        assert DebotAPIClient._format_duration(86400 * 2 + 3600 * 5) == "2d5h"
        assert DebotAPIClient._format_duration(86400 * 7) == "7d"

    def test_negative(self):
        """负数按 0 处理"""
        assert DebotAPIClient._format_duration(-10) == "0s"


# ============================================================
# 测试：_get (HTTP 请求)
# ============================================================

class TestGet:
    def test_get_success(self):
        """正常 GET 请求返回"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        response_data = {"code": 0, "data": {"key": "value"}}
        mock_resp = _make_mock_response(response_data)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._get("/api/test")
            assert result == {"key": "value"}
            assert client.total_api_calls == 1

    def test_get_with_params(self):
        """GET 请求带参数"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        response_data = {"code": 0, "data": {}}
        mock_resp = _make_mock_response(response_data)

        captured_url = []
        def mock_urlopen(req, **kwargs):
            captured_url.append(req.full_url)
            return mock_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client._get("/api/test", {"page_size": 10, "chain": "solana"})
            assert "page_size=10" in captured_url[0]
            assert "chain=solana" in captured_url[0]

    def test_get_headers(self):
        """验证请求头包含 User-Agent 和 Referer"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        response_data = {"code": 0, "data": {}}
        mock_resp = _make_mock_response(response_data)

        captured_headers = {}
        def mock_urlopen(req, **kwargs):
            captured_headers.update(dict(req.headers))
            return mock_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client._get("/api/test")
            assert "User-agent" in captured_headers  # urllib 会转成首字母大写
            assert "Referer" in captured_headers
            assert "Chrome" in captured_headers.get("User-agent", "")

    def test_get_non_zero_code(self):
        """API 返回非 0 code"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        response_data = {"code": 500, "description": "server error"}
        mock_resp = _make_mock_response(response_data)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client._get("/api/test")
            assert result is None

    def test_get_http_error(self):
        """HTTP 403 错误"""
        import urllib.error
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(
                       "http://test.local", 403, "Forbidden", {}, None)):
            result = client._get("/api/test")
            assert result is None

    def test_get_network_error(self):
        """网络异常"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = client._get("/api/test")
            assert result is None

    def test_rate_limiting(self):
        """请求间隔限流"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0.1)

        response_data = {"code": 0, "data": {}}
        mock_resp = _make_mock_response(response_data)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            start = time.time()
            client._get("/api/test1")
            client._get("/api/test2")
            elapsed = time.time() - start
            # 第二次请求应该被限流至少 0.1 秒
            assert elapsed >= 0.08  # 留点容差


# ============================================================
# 测试：fetch_signal_page
# ============================================================

class TestFetchSignalPage:
    def test_fetch_first_page(self):
        """获取第一页"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        results = [_make_result(i) for i in range(3)]
        tokens = {r["token"]: _make_token(i) for i, r in enumerate(results)}
        api_resp = _make_api_response(results, tokens, total=100, next_page=2)

        mock_resp = _make_mock_response(api_resp)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            data = client.fetch_signal_page(1)
            assert data is not None
            assert len(data["results"]) == 3
            assert data["total"] == 100
            assert data["next"] == 2
            assert len(data["meta"]["tokens"]) == 3

    def test_fetch_page_params(self):
        """验证第 N 页请求参数"""
        client = DebotAPIClient(base_url="http://test.local", page_size=20, chain="solana", request_interval=0)

        api_resp = _make_api_response([], {}, total=0, next_page=None)
        mock_resp = _make_mock_response(api_resp)

        captured_url = []
        def mock_urlopen(req, **kwargs):
            captured_url.append(req.full_url)
            return mock_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # 第一页不带 next
            client.fetch_signal_page(1)
            assert "next=" not in captured_url[0]
            assert "page_size=20" in captured_url[0]
            assert "chain=solana" in captured_url[0]

            # 第二页带 next=2
            client.fetch_signal_page(2)
            assert "next=2" in captured_url[1]

    def test_fetch_page_failure(self):
        """获取失败返回 None"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        with patch("urllib.request.urlopen", side_effect=Exception("error")):
            result = client.fetch_signal_page(1)
            assert result is None


# ============================================================
# 测试：_normalize_signal (信号标准化)
# ============================================================

class TestNormalizeSignal:
    def test_normalize_full(self):
        """完整字段标准化"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1, "ADDR001pump")
        token_map = {"ADDR001pump": _make_token(1)}

        signal = client._normalize_signal(result, token_map)

        assert signal is not None
        assert signal["contract_address"] == "ADDR001pump"
        assert signal["token_symbol"] == "TOK1"
        assert signal["market_cap"] == pytest.approx(60600)
        assert signal["price_usd"] == pytest.approx(0.00011)
        assert signal["holders_count"] == 101
        assert signal["pool_value"] == pytest.approx(50500)
        assert signal["smart_wallets"] == 3
        assert signal["source_url"] == "http://test.local/token/solana/ADDR001pump"
        assert signal["signal_time"]  # 有值
        assert signal["token_age"]  # 有值
        assert signal["avg_buy_amount"] > 0
        assert signal["multiplier"] is None  # API 里没有倍数
        assert signal["holder_rate"] is None  # API 里没有 top10

    def test_normalize_signal_content(self):
        """signal_content 是合法 JSON"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1)
        token_map = {result["token"]: _make_token(1)}

        signal = client._normalize_signal(result, token_map)
        content = json.loads(signal["signal_content"])
        assert "percent_5m" in content
        assert "percent_24h" in content
        assert "volume_24h" in content
        assert "wallet_count" in content
        assert "launchpad" in content

    def test_normalize_missing_token(self):
        """代币信息不在 token_map 里"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1, "UNKNOWNpump")

        signal = client._normalize_signal(result, {})
        assert signal is not None
        assert signal["token_symbol"] == ""  # 取不到 symbol 就为空
        assert signal["token_age"] is None  # 没创建时间就没有 age

    def test_normalize_no_contract(self):
        """没有合约地址返回 None"""
        client = DebotAPIClient(base_url="http://test.local")
        result = {"token": ""}
        signal = client._normalize_signal(result, {})
        assert signal is None

    def test_normalize_no_create_time(self):
        """没有 create_time 时用当前时间"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1)
        result["create_time"] = None
        token_map = {result["token"]: _make_token(1)}

        signal = client._normalize_signal(result, token_map)
        assert signal is not None
        assert signal["signal_time"]  # 应该有当前时间

    def test_normalize_wallet_stats_empty(self):
        """wallet_stats 为空时 smart_wallets = 0, avg_buy = None"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1)
        result["wallet_stats"] = []
        token_map = {result["token"]: _make_token(1)}

        signal = client._normalize_signal(result, token_map)
        assert signal["smart_wallets"] == 0
        assert signal["avg_buy_amount"] is None

    def test_normalize_avg_buy_calculation(self):
        """平均买入金额计算正确"""
        client = DebotAPIClient(base_url="http://test.local")
        result = _make_result(1)
        # 3 个钱包，volume 分别是 100, 110, 120 → 平均 110
        token_map = {result["token"]: _make_token(1)}

        signal = client._normalize_signal(result, token_map)
        assert signal["avg_buy_amount"] == pytest.approx(110.0)

    def test_normalize_exception_returns_none(self):
        """异常时返回 None 不崩溃"""
        client = DebotAPIClient(base_url="http://test.local")
        signal = client._normalize_signal(None, {})
        assert signal is None


# ============================================================
# 测试：fetch_all_signals (分页拉取)
# ============================================================

class TestFetchAllSignals:
    def test_fetch_single_page(self):
        """只有一页数据"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        results = [_make_result(i) for i in range(5)]
        tokens = {r["token"]: _make_token(i) for i, r in enumerate(results)}

        api_resp = _make_api_response(results, tokens, total=5, next_page=None)
        mock_resp = _make_mock_response(api_resp)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            signals = client.fetch_all_signals()
            assert len(signals) == 5
            assert client.total_api_calls == 1

    def test_fetch_multi_page(self):
        """多页翻页"""
        client = DebotAPIClient(base_url="http://test.local", page_size=3, request_interval=0)

        # 模拟 2 页数据
        page1_results = [_make_result(i) for i in range(3)]
        page1_tokens = {r["token"]: _make_token(i) for i, r in enumerate(page1_results)}
        page2_results = [_make_result(i + 3) for i in range(2)]
        page2_tokens = {r["token"]: _make_token(i + 3) for i, r in enumerate(page2_results)}

        responses = [
            _make_api_response(page1_results, page1_tokens, total=5, next_page=2),
            _make_api_response(page2_results, page2_tokens, total=5, next_page=None),
        ]

        mock_resps = [_make_mock_response(r) for r in responses]
        with patch("urllib.request.urlopen", side_effect=mock_resps):
            signals = client.fetch_all_signals()
            assert len(signals) == 5
            assert client.total_api_calls == 2

    def test_fetch_max_signals_limit(self):
        """max_signals 限制"""
        client = DebotAPIClient(base_url="http://test.local", page_size=10, request_interval=0)

        results = [_make_result(i) for i in range(10)]
        tokens = {r["token"]: _make_token(i) for i, r in enumerate(results)}
        api_resp = _make_api_response(results, tokens, total=100, next_page=2)
        mock_resp = _make_mock_response(api_resp)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            signals = client.fetch_all_signals(max_signals=5)
            assert len(signals) == 5  # 第一页取前 5 个就停
            assert client.total_api_calls == 1

    def test_fetch_page_failure_stops(self):
        """某页失败后停止翻页"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        results = [_make_result(i) for i in range(3)]
        tokens = {r["token"]: _make_token(i) for i, r in enumerate(results)}

        # 第一页正常，第二页失败
        ok_resp = _make_mock_response(
            _make_api_response(results, tokens, total=100, next_page=2))

        with patch("urllib.request.urlopen", side_effect=[ok_resp, Exception("fail")]):
            signals = client.fetch_all_signals()
            assert len(signals) == 3
            # 第一页成功 +1，第二页异常不加，所以总共 1 次成功调用
            assert client.total_api_calls == 1

    def test_fetch_empty_results(self):
        """第一页就空"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        api_resp = _make_api_response([], {}, total=0, next_page=None)
        mock_resp = _make_mock_response(api_resp)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            signals = client.fetch_all_signals()
            assert len(signals) == 0

    def test_fetch_deduplication(self):
        """信号 ID 去重"""
        client = DebotAPIClient(base_url="http://test.local", page_size=3, request_interval=0)

        # 两页有重复的信号 (id 相同)
        results = [_make_result(i) for i in range(3)]
        tokens = {r["token"]: _make_token(i) for i, r in enumerate(results)}

        # 第二页和第一页一样（模拟重复）
        responses = [
            _make_api_response(results, tokens, total=3, next_page=2),
            _make_api_response(results, tokens, total=3, next_page=None),
        ]
        mock_resps = [_make_mock_response(r) for r in responses]

        with patch("urllib.request.urlopen", side_effect=mock_resps):
            signals = client.fetch_all_signals()
            assert len(signals) == 3  # 去重后应该是 3 条，不是 6 条

    def test_fetch_max_pages_limit(self):
        """max_pages 限制翻页次数"""
        client = DebotAPIClient(
            base_url="http://test.local",
            page_size=10,
            max_pages=3,
            request_interval=0,
        )

        call_count = 0
        def make_resp(req, **kwargs):
            nonlocal call_count
            call_count += 1
            page_idx = call_count - 1  # 0-based
            # 每页生成 10 个不同 token (用 page_idx 偏移避免重复)
            start_idx = page_idx * 10
            results = [_make_result(start_idx + i) for i in range(10)]
            tokens = {r["token"]: _make_token(start_idx + i) for i, r in enumerate(results)}
            data = _make_api_response(results, tokens, total=1000, next_page=call_count + 1)
            mock = _make_mock_response(data)
            mock.__enter__.return_value = mock
            return mock

        with patch("urllib.request.urlopen", side_effect=make_resp):
            signals = client.fetch_all_signals()
            assert call_count == 3  # 最多 3 页
            assert len(signals) == 30  # 3 页 × 10 条


# ============================================================
# 测试：test_connection
# ============================================================

class TestConnection:
    def test_connection_ok(self):
        """连接正常"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        results = [_make_result(1)]
        tokens = {r["token"]: _make_token(1) for r in results}
        api_resp = _make_api_response(results, tokens, total=1, next_page=None)
        mock_resp = _make_mock_response(api_resp)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.test_connection() is True

    def test_connection_fail(self):
        """连接失败"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        with patch("urllib.request.urlopen", side_effect=Exception("error")):
            assert client.test_connection() is False

    def test_connection_empty(self):
        """返回空数据"""
        client = DebotAPIClient(base_url="http://test.local", request_interval=0)

        api_resp = _make_api_response([], {}, total=0, next_page=None)
        mock_resp = _make_mock_response(api_resp)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.test_connection() is False
