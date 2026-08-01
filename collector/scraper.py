"""
Playwright Debot 信号采集器
- 无头模式运行，支持 Cookie 免登录
- 轮询抓取 AI 信号列表，增量入库
- 失败重试 + 指数退避
"""
import os
import json
import time
import random
import logging
from datetime import datetime, timezone
from typing import Optional, List

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

# 时区
UTC = timezone.utc


class DebotScraper:
    """Debot 信号采集器"""

    def __init__(self, config: dict):
        self.base_url = os.environ.get("DEBOT_BASE_URL", "https://debot.ai")
        self.login_url = os.environ.get("DEBOT_LOGIN_URL", f"{self.base_url}/login")
        self.signal_url = os.environ.get("DEBOT_SIGNAL_URL", f"{self.base_url}/signals")
        self.cookie_file = os.environ.get("COOKIE_FILE", "/app/data/cookies.json")
        self.timeout = int(os.environ.get("PAGE_TIMEOUT_SECONDS", "30")) * 1000
        self.max_retries = int(os.environ.get("MAX_RETRIES", "3"))

        # 从配置文件加载选择器
        self.selectors = config.get("selectors", {})
        self.risk_selectors = config.get("risk_selectors", {})
        self.scrape_rules = config.get("scrape_rules", {})

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._block_reason = ""  # 页面阻断原因

    # ================================================================
    # 浏览器生命周期
    # ================================================================

    def start(self):
        """启动浏览器并恢复会话"""
        logger.info("启动 Playwright 浏览器...")
        self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # 创建浏览器上下文（模拟真实浏览器）
        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        # 尝试加载 Cookie
        self._load_cookies()

        self._page = self._context.new_page()
        logger.info("浏览器启动完成")

    def stop(self):
        """关闭浏览器"""
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        logger.info("浏览器已关闭")

    def _load_cookies(self):
        """从文件加载 Cookie"""
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, "r") as f:
                    cookies = json.load(f)
                self._context.add_cookies(cookies)
                logger.info(f"已加载 Cookie 文件，共 {len(cookies)} 条")
            except Exception as e:
                logger.error(f"Cookie 文件加载失败: {e}")
        else:
            logger.warning(f"Cookie 文件不存在: {self.cookie_file}，将使用未登录状态")

    def _save_cookies(self):
        """保存当前 Cookie 到文件"""
        try:
            cookies = self._context.cookies()
            os.makedirs(os.path.dirname(self.cookie_file), exist_ok=True)
            with open(self.cookie_file, "w") as f:
                json.dump(cookies, f)
            logger.info(f"Cookie 已保存，共 {len(cookies)} 条")
        except Exception as e:
            logger.error(f"保存 Cookie 失败: {e}")

    # ================================================================
    # 页面操作
    # ================================================================

    def _navigate_with_retry(self, url: str) -> bool:
        """带重试机制的页面导航"""
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"导航到: {url} (第 {attempt} 次)")
                response = self._page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")

                if response and response.status >= 400:
                    logger.warning(f"页面返回 HTTP {response.status}")
                    if attempt < self.max_retries:
                        time.sleep(2 ** attempt)
                        continue

                # 等待 React 渲染完成（SPA 页面需要额外等待异步数据加载）
                self._wait_for_spa_render()

                # 随机等待，模拟人类操作
                time.sleep(random.uniform(1, 3))
                return True

            except Exception as e:
                logger.warning(f"页面导航失败 (第 {attempt} 次): {e}")
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt + random.uniform(0, 2)
                    logger.info(f"等待 {wait_time:.1f}s 后重试...")
                    time.sleep(wait_time)

        logger.error(f"页面导航最终失败: {url}")
        return False

    def _wait_for_spa_render(self):
        """等待 SPA 页面异步数据加载完成"""
        # 等待 token 链接出现（信号列表核心元素），最多等 20 秒
        try:
            self._page.wait_for_selector("a[href*='/token/solana/']", timeout=20000)
            logger.debug("SPA 数据已渲染: 检测到 token 链接")
            # 再等 2 秒确保完整渲染
            time.sleep(2)
            return
        except Exception:
            logger.debug("token 链接未在 20s 内出现")

        # 备选：等待任何表格或卡片容器
        try:
            self._page.wait_for_selector("[class*='MuiTableRow'], [class*='card'], [class*='signal'], [class*='table']", timeout=5000)
            time.sleep(1)
        except Exception:
            pass

    def _safe_extract_text(self, selector: str, default: str = "") -> str:
        """安全提取元素文本，元素不存在返回默认值"""
        try:
            # 尝试多个候选选择器
            for sel in selector.split(","):
                sel = sel.strip()
                element = self._page.query_selector(sel)
                if element:
                    return element.inner_text().strip()
            return default
        except Exception:
            return default

    def _safe_extract_href(self, selector: str, default: str = "") -> str:
        """安全提取元素 href 属性"""
        try:
            for sel in selector.split(","):
                sel = sel.strip()
                element = self._page.query_selector(sel)
                if element:
                    href = element.get_attribute("href") or ""
                    if href:
                        return href
            return default
        except Exception:
            return default

    # ================================================================
    # 信号采集
    # ================================================================

    def check_page_blockers(self) -> dict:
        """
        检测页面是否被 Cloudflare 挑战拦截或需要登录。
        返回 {"blocked": bool, "reason": str}
        """
        result = {"blocked": False, "reason": ""}
        try:
            title = self._page.title().lower()
            body_text = self._page.inner_text("body")[:2000].lower()

            # 1. Cloudflare 挑战检测
            cf_keywords = [
                "just a moment", "verifying you are human",
                "验证你是人类", "正在检查您的浏览器",
                "checking your browser", "ddos protection",
                "cf-challenge", "cf_challenge",
                "please wait while we verify",
                "enable javascript", "请启用javascript",
            ]
            for kw in cf_keywords:
                if kw in title or kw in body_text:
                    result["blocked"] = True
                    result["reason"] = "cloudflare_challenge"
                    logger.warning(f"页面被 Cloudflare 挑战拦截: 检测到关键词 '{kw}'")
                    return result

            # 2. 登录态检测
            current_url = self._page.url.lower()
            login_indicators = [
                "/login" in current_url and "/signals" not in current_url,
                "sign in" in title,
                "请登录" in title or "请登录" in body_text,
                "connect wallet" in title and self._page.query_selector("[class*='wallet'], [class*='connect']") is not None,
            ]
            if any(login_indicators):
                result["blocked"] = True
                result["reason"] = "login_required"
                logger.warning("页面需要登录: Cookie 可能已过期")

            # 3. HTTP 状态码检测
            try:
                status = self._page.evaluate("() => window.performance?.getEntriesByType?.('navigation')?.[0]?.responseStatus")
                if status and status in (401, 403):
                    result["blocked"] = True
                    result["reason"] = f"http_{status}"
                    logger.warning(f"页面返回 HTTP {status}")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"页面阻断检测异常: {e}")

        return result

    def scrape_signals(self) -> List[dict]:
        """
        抓取当前页面的 AI 信号列表。
        返回信号数据字典列表。
        如果被 Cloudflare 拦截或需要登录，返回空列表并设置 self._block_reason。
        """
        self._block_reason = ""

        # 导航到信号页面
        if not self._navigate_with_retry(self.signal_url):
            logger.error("无法访问信号页面")
            return []

        # 页面阻断检测（Cloudflare 挑战 / 登录过期）
        blocker = self.check_page_blockers()
        if blocker["blocked"]:
            self._block_reason = blocker["reason"]
            logger.warning(f"本轮采集被阻断: {blocker['reason']}")
            return []

        # 等待信号列表加载
        try:
            primary_selector = self.selectors.get("signal_list", ".signal-card").split(",")[0].strip()
            self._page.wait_for_selector(primary_selector, timeout=self.timeout)
        except Exception:
            logger.warning("信号列表元素未出现，尝试解析当前页面内容")
            try:
                title = self._page.title()
                logger.info(f"当前页面标题: {title}")
            except Exception:
                pass

        # 滚动加载更多（如有懒加载）
        self._scroll_to_load()

        # 解析信号卡片
        signals = self._parse_signal_list()
        logger.info(f"本轮抓取到 {len(signals)} 条信号")

        # 调试：未匹配到信号时保存页面 HTML 片段供分析
        if not signals:
            self._dump_debug_html()

        return signals

    def _dump_debug_html(self):
        """保存页面 HTML 到文件，用于调试选择器"""
        try:
            html = self._page.content()
            # 只保存前 50000 字符，避免文件过大
            debug_path = "/app/data/page_debug.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html[:50000])
            logger.info(f"调试 HTML 已保存到: {debug_path}")
        except Exception as e:
            logger.warning(f"保存调试 HTML 失败: {e}")

    def _scroll_to_load(self):
        """滚动页面触发懒加载"""
        for _ in range(3):
            try:
                self._page.evaluate("window.scrollBy(0, 800)")
                time.sleep(0.5)
            except Exception:
                break

    def _parse_signal_list(self) -> List[dict]:
        """解析页面信号列表"""
        signals = []
        max_signals = self.scrape_rules.get("max_signals_per_run", 50)

        # 查找信号卡片容器
        container_selector = self.selectors.get("signal_list", ".signal-card")
        try:
            cards = self._page.query_selector_all(container_selector)
            if not cards:
                # 尝试备选选择器
                for alt_sel in container_selector.split(",")[1:]:
                    cards = self._page.query_selector_all(alt_sel.strip())
                    if cards:
                        logger.info(f"使用备选选择器匹配到 {len(cards)} 个卡片: {alt_sel}")
                        break
        except Exception as e:
            logger.error(f"查找信号卡片失败: {e}")
            return []

        logger.info(f"页面匹配到 {len(cards)} 个信号卡片元素")

        for i, card in enumerate(cards):
            if len(signals) >= max_signals:
                break

            try:
                signal = self._parse_single_card(card)
                if signal and signal.get("contract_address"):
                    signals.append(signal)
                else:
                    logger.debug(f"卡片 #{i} 解析结果无效，跳过")
            except Exception as e:
                logger.warning(f"解析卡片 #{i} 失败: {e}")
                continue

        return signals

    def _parse_single_card(self, card) -> Optional[dict]:
        """解析单个信号卡片，提取所有字段"""
        signal = {}

        # 判断 card 类型：如果是 <a> 链接，从 href 提取合约地址
        tag_name = card.evaluate("el => el.tagName.toLowerCase()")
        is_link = (tag_name == "a")

        if is_link:
            # card 本身就是 token 链接
            href = card.get_attribute("href") or ""
            contract = self._extract_contract_from_url(href)
            signal["contract_address"] = contract
            # 只取第一行作为代币符号（避免多行内容混入）
            raw_text = card.inner_text().strip()
            signal["token_symbol"] = raw_text.split("\n")[0].strip()[:128]
            signal["source_url"] = href if href.startswith("http") else f"{self.base_url}{href}"

            # 尝试从父级/兄弟元素提取其他数据
            parent = card.evaluate_handle("el => el.closest('[class*=\"card\"], [class*=\"row\"], [class*=\"item\"], tr, [class*=\"signal\"]')")
            container = parent.as_element() if parent else card
        else:
            container = card
            # 合约地址
            contract = self._safe_extract_text_in_element(card, self.selectors.get("contract_address", ""))
            signal["contract_address"] = self._clean_contract_address(contract)
            # 代币符号
            signal["token_symbol"] = self._safe_extract_text_in_element(card, self.selectors.get("token_symbol", ""))
            # 详情链接
            signal["source_url"] = self._safe_extract_href_in_element(card, self.selectors.get("source_url", ""))

        # 从容器中提取通用字段
        # 信号时间
        time_text = self._safe_extract_text_in_element(container, self.selectors.get("signal_time", ""))
        signal["signal_time"] = self._parse_time(time_text)

        # 流动池
        pool_text = self._safe_extract_text_in_element(container, self.selectors.get("pool_value", ""))
        signal["pool_value"] = self._parse_number(pool_text)

        # 大户持仓占比
        holder_text = self._safe_extract_text_in_element(container, self.selectors.get("holder_rate", ""))
        signal["holder_rate"] = self._parse_percentage(holder_text)

        # 信号文案
        signal["signal_content"] = self._safe_extract_text_in_element(container, self.selectors.get("signal_content", ""))

        return signal

    def _extract_contract_from_url(self, url: str) -> str:
        """从 Debot URL 中提取合约地址，去除前缀 ID
        如 /token/solana/314495_Hd6pqdE... -> Hd6pqdE...
        """
        import re
        match = re.search(r'/token/\w+/(?:[\d]+_)?([^/?]+)', url)
        if match:
            addr = match.group(1)
            # 去除可能的数字ID前缀（格式: 123456_actualAddress）
            if '_' in addr:
                parts = addr.split('_')
                # 最后一段是真实合约地址
                addr = parts[-1] if len(parts[-1]) > 20 else addr
            return addr
        return ""

    def _safe_extract_text_in_element(self, element, selector: str, default: str = "") -> str:
        """在指定元素内安全提取文本"""
        if not selector:
            return default
        try:
            for sel in selector.split(","):
                sel = sel.strip()
                child = element.query_selector(sel)
                if child:
                    return child.inner_text().strip()
            return default
        except Exception:
            return default

    def _safe_extract_href_in_element(self, element, selector: str, default: str = "") -> str:
        """在指定元素内安全提取 href"""
        if not selector:
            return default
        try:
            for sel in selector.split(","):
                sel = sel.strip()
                child = element.query_selector(sel)
                if child:
                    return child.get_attribute("href") or ""
            return default
        except Exception:
            return default

    # ================================================================
    # 代币风控信息采集
    # ================================================================

    def scrape_token_info(self, contract_address: str) -> Optional[dict]:
        """
        抓取单个代币的链上风险信息。
        需要先导航到代币详情页。
        """
        token_url = f"{self.base_url}/token/{contract_address}"
        if not self._navigate_with_retry(token_url):
            return None

        info = {"contract_address": contract_address}

        info["pool_lock_status"] = self._safe_extract_text(
            self.risk_selectors.get("pool_lock_status", "")
        )
        info["buy_tax"] = self._parse_percentage(
            self._safe_extract_text(self.risk_selectors.get("buy_tax", ""))
        )
        info["sell_tax"] = self._parse_percentage(
            self._safe_extract_text(self.risk_selectors.get("sell_tax", ""))
        )
        info["contract_audit"] = self._safe_extract_text(
            self.risk_selectors.get("contract_audit", "")
        )
        info["risk_flags"] = self._safe_extract_text(
            self.risk_selectors.get("risk_flags", "")
        )
        info["risk_level"] = self._assess_risk_level(info)

        return info

    def _assess_risk_level(self, info: dict) -> str:
        """根据风控数据综合判定风险等级"""
        risk_score = 0

        # 池子未锁 → 高风险
        if info.get("pool_lock_status") and "未锁" in str(info["pool_lock_status"]):
            risk_score += 3
        elif info.get("pool_lock_status") and "锁定" in str(info["pool_lock_status"]):
            risk_score -= 1

        # 交易税率
        buy_tax = info.get("buy_tax") or 0
        sell_tax = info.get("sell_tax") or 0
        max_tax = max(float(buy_tax), float(sell_tax))
        if max_tax > 0.05:
            risk_score += 3
        elif max_tax > 0.02:
            risk_score += 1

        # 合约未审计
        if info.get("contract_audit") and "未" in str(info["contract_audit"]):
            risk_score += 2

        # 有黑名单风险标记
        flags = str(info.get("risk_flags", "")).lower()
        if any(kw in flags for kw in ["黑名单", "blacklist", "mint", "freeze"]):
            risk_score += 3

        if risk_score >= 5:
            return "高"
        elif risk_score >= 2:
            return "中"
        return "低"

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _parse_time(text: str) -> Optional[str]:
        """解析时间文本为 ISO 8601 格式（UTC）"""
        if not text:
            return datetime.now(UTC).isoformat()
        try:
            # 常见时间格式处理，根据 Deboto 实际页面调整
            from dateutil import parser as dateparser
            dt = dateparser.parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat()
        except Exception:
            return datetime.now(UTC).isoformat()

    @staticmethod
    def _clean_contract_address(text: str) -> str:
        """清理合约地址格式"""
        if not text:
            return ""
        text = text.strip()
        # 移除常见前缀/后缀噪声
        text = text.replace("地址:", "").replace("CA:", "").replace("Contract:", "").strip()
        # 截取有效长度（Solana 地址 44 字符，EVM 地址 42 字符）
        # 如果文本中包含多个 token，取第一个看起来像地址的
        import re
        match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', text)
        return match.group(0) if match else text

    @staticmethod
    def _parse_number(text: str) -> Optional[float]:
        """解析金额文本为数字"""
        if not text:
            return None
        text = text.replace(",", "").replace("$", "").replace("¥", "").strip()
        # 处理 K/M/B 后缀
        multipliers = {"K": 1e3, "M": 1e6, "B": 1e9}
        for suffix, mult in multipliers.items():
            if text.upper().endswith(suffix):
                try:
                    return float(text[:-1]) * mult
                except ValueError:
                    return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _parse_percentage(text: str) -> Optional[float]:
        """解析百分比文本"""
        if not text:
            return None
        text = text.replace("%", "").strip()
        try:
            val = float(text)
            # 如果值 > 1，假设直接是百分比数值（如 5 表示 5%）
            return val / 100 if val > 1 else val
        except ValueError:
            return None
