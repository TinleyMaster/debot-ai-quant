"""
scraper.py 工具方法单元测试
只测试纯静态方法，不需要启动浏览器
"""
import re
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import DebotScraper


# ============================================================
# 测试：_parse_number (数字解析，含下标数字)
# ============================================================

class TestParseNumber:
    def test_plain_numbers(self):
        """普通数字"""
        assert DebotScraper._parse_number("123") == 123.0
        assert DebotScraper._parse_number("123.45") == 123.45
        assert DebotScraper._parse_number("0.000123") == 0.000123

    def test_with_currency(self):
        """带货币符号"""
        assert DebotScraper._parse_number("$123.45") == 123.45
        assert DebotScraper._parse_number("¥999") == 999.0
        assert DebotScraper._parse_number("  $1.23  ") == 1.23

    def test_with_commas(self):
        """带千分位逗号"""
        assert DebotScraper._parse_number("1,234.56") == 1234.56
        assert DebotScraper._parse_number("$45,123") == 45123.0

    def test_km_suffix(self):
        """K/M/B 后缀"""
        assert DebotScraper._parse_number("24.5K") == 24500.0
        assert DebotScraper._parse_number("1.2M") == 1200000.0
        assert DebotScraper._parse_number("$3.5B") == 3500000000.0
        assert DebotScraper._parse_number("100k") == 100000.0  # 小写

    def test_subscript_single(self):
        """下标数字（单个数）"""
        # $0.0₄7582 → 0.00007582
        result = DebotScraper._parse_number("0.0₄7582")
        assert result == pytest.approx(0.00007582)

    def test_subscript_with_dollar(self):
        """下标数字带 $"""
        result = DebotScraper._parse_number("$0.0₅1234")
        assert result == pytest.approx(0.000001234)

    def test_subscript_zero(self):
        """下标为 0 → 小数点后 0 个零再跟数字，即 0.0₀1 = 0.1"""
        result = DebotScraper._parse_number("0.0₀1")
        assert result == pytest.approx(0.1)

    def test_subscript_nine(self):
        """下标为 9"""
        result = DebotScraper._parse_number("0.0₉1")
        assert result == pytest.approx(1e-10)

    def test_empty_and_none(self):
        """空值"""
        assert DebotScraper._parse_number("") is None
        assert DebotScraper._parse_number(None) is None

    def test_invalid(self):
        """无效文本"""
        assert DebotScraper._parse_number("abc") is None
        assert DebotScraper._parse_number("N/A") is None


# ============================================================
# 测试：_parse_percentage (百分比解析)
# ============================================================

class TestParsePercentage:
    def test_with_percent(self):
        """带 % 号"""
        assert DebotScraper._parse_percentage("25%") == pytest.approx(0.25)
        assert DebotScraper._parse_percentage("5.5%") == pytest.approx(0.055)
        assert DebotScraper._parse_percentage("100%") == pytest.approx(1.0)

    def test_without_percent(self):
        """不带 % 号（>1 认为是百分比数值）"""
        assert DebotScraper._parse_percentage("30") == pytest.approx(0.30)

    def test_decimal_fraction(self):
        """<1 的小数，认为已是比例"""
        assert DebotScraper._parse_percentage("0.25") == pytest.approx(0.25)

    def test_negative(self):
        """负百分比"""
        assert DebotScraper._parse_percentage("-5%") == pytest.approx(-0.05)

    def test_empty(self):
        assert DebotScraper._parse_percentage("") is None
        assert DebotScraper._parse_percentage(None) is None

    def test_invalid(self):
        assert DebotScraper._parse_percentage("abc") is None


# ============================================================
# 测试：_extract_pool_value_from_text (流动池提取)
# ============================================================

class TestExtractPoolValue:
    def test_dollar_k(self):
        assert DebotScraper._extract_pool_value_from_text("$24.5K liquidity") == 24500.0
        assert DebotScraper._extract_pool_value_from_text("Liq: $100K") == 100000.0

    def test_dollar_m(self):
        assert DebotScraper._extract_pool_value_from_text("$1.2M pool") == 1200000.0

    def test_dollar_plain(self):
        """纯数字美元（第一个匹配的 $ 数字）"""
        assert DebotScraper._extract_pool_value_from_text("Market Cap: $50000") == 50000.0
        assert DebotScraper._extract_pool_value_from_text("$100 liq pool") == 100.0

    def test_k_liq_keyword(self):
        assert DebotScraper._extract_pool_value_from_text("24K liquidity") == 24000.0
        assert DebotScraper._extract_pool_value_from_text("5M pool") == 5000000.0

    def test_no_match(self):
        assert DebotScraper._extract_pool_value_from_text("no number here") is None
        assert DebotScraper._extract_pool_value_from_text("") is None


# ============================================================
# 测试：_extract_holder_rate_from_text (Top10 持仓比例)
# ============================================================

class TestExtractHolderRate:
    def test_top10_prefix(self):
        assert DebotScraper._extract_holder_rate_from_text("Top10 27.7%") == pytest.approx(0.277)
        assert DebotScraper._extract_holder_rate_from_text("top 10: 45%") == pytest.approx(0.45)

    def test_holder_prefix(self):
        assert DebotScraper._extract_holder_rate_from_text("holder 22.5%") == pytest.approx(0.225)
        assert DebotScraper._extract_holder_rate_from_text("holders: 30%") == pytest.approx(0.30)

    def test_chinese_prefix(self):
        assert DebotScraper._extract_holder_rate_from_text("持有 50%") == pytest.approx(0.50)

    def test_any_percentage(self):
        assert DebotScraper._extract_holder_rate_from_text("some text 15% more text") == pytest.approx(0.15)

    def test_no_percent(self):
        assert DebotScraper._extract_holder_rate_from_text("no percent here") is None


# ============================================================
# 测试：_extract_contract_from_url (合约地址提取)
# ============================================================

class TestExtractContractFromUrl:
    def test_simple(self):
        s = DebotScraper.__new__(DebotScraper)
        assert s._extract_contract_from_url("/token/solana/Abc123def") == "Abc123def"

    def test_with_prefix_id(self):
        s = DebotScraper.__new__(DebotScraper)
        assert s._extract_contract_from_url("/token/solana/314495_Abc123defpump") == "Abc123defpump"

    def test_full_url(self):
        s = DebotScraper.__new__(DebotScraper)
        assert s._extract_contract_from_url("https://debot.ai/token/solana/TOKEN001pump") == "TOKEN001pump"

    def test_with_query(self):
        s = DebotScraper.__new__(DebotScraper)
        assert s._extract_contract_from_url("/token/solana/Abc123?tab=trades") == "Abc123"

    def test_no_match(self):
        s = DebotScraper.__new__(DebotScraper)
        assert s._extract_contract_from_url("https://example.com/something") == ""


# ============================================================
# 测试：_clean_contract_address (地址清理)
# ============================================================

class TestCleanContractAddress:
    def test_clean(self):
        assert DebotScraper._clean_contract_address("  Abc123def  ") == "Abc123def"

    def test_with_prefix(self):
        assert DebotScraper._clean_contract_address("地址: Abc123def") == "Abc123def"
        assert DebotScraper._clean_contract_address("CA: XYZ789") == "XYZ789"
        assert DebotScraper._clean_contract_address("Contract: TOKEN001pump") == "TOKEN001pump"

    def test_extract_from_text(self):
        """从包含噪声的文本中提取 base58 地址"""
        # base58 不含 0/O/l/I，测试地址用合法字符（>=32字符才匹配）
        addr = "TKENxyzabcdefghjkmnpABCDEFGHJKpump"  # 36字符，合法 base58
        assert DebotScraper._clean_contract_address(f"合约: {addr} (点击复制)") == addr

    def test_empty(self):
        assert DebotScraper._clean_contract_address("") == ""
        assert DebotScraper._clean_contract_address(None) == ""


# ============================================================
# 测试：_parse_time (时间解析)
# ============================================================

class TestParseTime:
    def test_hhmmss(self):
        """HH:MM:SS 格式"""
        result = DebotScraper._parse_time("10:27:37")
        assert result is not None
        assert "T10:27:37" in result or "T10:27:37" in result

    def test_empty(self):
        """空值返回当前时间"""
        result = DebotScraper._parse_time("")
        assert result is not None
        # 应该是最近的时间
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(result)
        now = datetime.now(timezone.utc)
        assert abs((now - parsed).total_seconds()) < 5


# ============================================================
# 测试：_extract_signal_content_from_text (信号内容提取)
# ============================================================

class TestExtractSignalContent:
    def test_multiplier_x_prefix(self):
        text = "TOKEN\nx2.5 multiplier\nsome other text"
        result = DebotScraper._extract_signal_content_from_text(text, "TOKEN")
        assert "x2.5" in result.lower()

    def test_multiplier_suffix(self):
        text = "TOKEN\n2.5x signal\nmore"
        result = DebotScraper._extract_signal_content_from_text(text, "TOKEN")
        assert "2.5x" in result.lower()

    def test_ai_score(self):
        text = "TOKEN\nAI score: 8.5\nsomething"
        result = DebotScraper._extract_signal_content_from_text(text, "TOKEN")
        assert "AI" in result
        assert "8.5" in result

    def test_fallback_after_symbol(self):
        text = "TOKEN\n这是信号描述\n第二行描述"
        result = DebotScraper._extract_signal_content_from_text(text, "TOKEN")
        assert "信号描述" in result


# ============================================================
# 测试：DebotScraper 初始化
# ============================================================

class TestScraperInit:
    def test_init_with_config(self):
        """初始化时配置正确加载"""
        config = {
            "selectors": {"signal_list": ".custom-card"},
            "risk_selectors": {"risk": ".risk-class"},
            "scrape_rules": {"max_signals_per_run": 100},
        }
        scraper = DebotScraper(config)
        assert scraper.selectors["signal_list"] == ".custom-card"
        assert scraper.scrape_rules["max_signals_per_run"] == 100
        assert scraper.base_url.startswith("http")

    def test_init_default_config(self):
        """空配置时 selectors 和 scrape_rules 为空 dict（非 None）"""
        scraper = DebotScraper({})
        assert isinstance(scraper.selectors, dict)
        assert isinstance(scraper.scrape_rules, dict)
        assert scraper.base_url == "https://debot.ai"

    def test_base_url_from_env(self, monkeypatch):
        """从环境变量读取 base_url"""
        monkeypatch.setenv("DEBOT_BASE_URL", "https://test.debot.ai")
        # 强制重新读取 env (避免 dotenv 或缓存)
        import os
        assert os.environ["DEBOT_BASE_URL"] == "https://test.debot.ai"
        scraper = DebotScraper({})
        assert scraper.base_url == "https://test.debot.ai"
