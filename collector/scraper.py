"""
Playwright Debot 信号采集器
- 无头模式运行，支持 Cookie 免登录
- 轮询抓取 AI 信号列表，增量入库
- 失败重试 + 指数退避
"""
import os
import re
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
        self.signal_url = os.environ.get("DEBOT_SIGNAL_URL", f"{self.base_url}/?chain=solana")
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
                # ---- Docker 容器必需 ----
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-zygote",

                # ---- 进程 / 线程封顶 ----
                "--renderer-process-limit=1",
                "--num-raster-threads=1",

                # ---- 低端设备模式（激活 Chrome 内存节省策略） ----
                "--enable-low-end-device-mode",

                # ---- CPU 相关 ----
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=TranslateUI,AudioServiceOutOfProcess,IsolateOrigins,site-per-process",

                # ---- 禁用非必要后台功能 ----
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-hang-monitor",
                "--disable-prompt-on-repost",
                "--disable-domain-reliability",

                # ---- 静音 ----
                "--mute-audio",
                "--no-first-run",

                # ---- 内存封顶 ----
                "--js-flags=--max-old-space-size=128",
                "--memory-pressure-off",
            ],
        )

        # 创建浏览器上下文（模拟真实浏览器，小视口节省资源）
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
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

    def _ensure_page(self):
        """确保页面存在，不存在则创建"""
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            logger.debug("创建新页面")

    def close_page(self):
        """关闭当前页面释放 CPU（避免 SPA WebSocket 持续渲染）"""
        if self._page and not self._page.is_closed():
            self._page.close()
            self._page = None
            logger.debug("页面已关闭释放资源")

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

        # 确保页面存在（上一轮已关闭释放资源）
        self._ensure_page()

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
                # 过滤噪声卡片：innerText < 20 字符的跳过（logo 链接等）
                raw = card.inner_text().strip()
                if len(raw) < 20:
                    logger.debug(f"卡片 #{i} innerText 过短 ({len(raw)}字符)，跳过")
                    continue

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
        """解析单个信号卡片
        
        Debot 页面有两类卡片，均使用 bella8 class:
        1) 5分钟活跃度榜单: token_name, +price%, MC, $value, TXs, rank
        2) 详细信号列表: 含 Top10%, 时间, 代币名, 合约, AI报告, 倍数, 
           聪明钱包, MC, 持有人, 价格, 流动池, ATH
        通过 innerText 中是否有 'AI报告' 来区分。
        """
        signal = {}
        tag_name = card.evaluate("el => el.tagName.toLowerCase()")
        is_link = (tag_name == "a")

        if not is_link:
            # 非链接类型卡片（备用方案）
            contract = self._safe_extract_text_in_element(card, self.selectors.get("contract_address", ""))
            signal["contract_address"] = self._clean_contract_address(contract)
            signal["token_symbol"] = self._safe_extract_text_in_element(card, self.selectors.get("token_symbol", ""))
            signal["source_url"] = self._safe_extract_href_in_element(card, self.selectors.get("source_url", ""))
            raw_text = card.inner_text().strip()
            time_text = self._safe_extract_text_in_element(card, self.selectors.get("signal_time", ""))
            signal["signal_time"] = self._parse_time(time_text)
            pool_text = self._safe_extract_text_in_element(card, self.selectors.get("pool_value", ""))
            signal["pool_value"] = self._parse_number(pool_text) or self._extract_pool_value_from_text(raw_text)
            holder_text = self._safe_extract_text_in_element(card, self.selectors.get("holder_rate", ""))
            signal["holder_rate"] = self._parse_percentage(holder_text) or self._extract_holder_rate_from_text(raw_text)
            signal["signal_content"] = self._safe_extract_text_in_element(card, self.selectors.get("signal_content", ""))
            if not signal["signal_content"]:
                signal["signal_content"] = self._extract_signal_content_from_text(raw_text, signal.get("token_symbol", ""))
            return signal

        # --- bella8 <a> 链接卡片 ---
        href = card.get_attribute("href") or ""
        contract = self._extract_contract_from_url(href)
        signal["contract_address"] = contract
        signal["source_url"] = href if href.startswith("http") else f"{self.base_url}{href}"

        raw_text = card.inner_text().strip()
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

        # 检测卡片类型（页面实际显示"报告"，非"AI报告"）
        is_detail = "报告" in raw_text

        # 初始化字段
        signal["token_symbol"] = ""
        signal["pool_value"] = None
        signal["holder_rate"] = None
        signal["signal_content"] = ""
        signal["signal_time"] = None

        if is_detail:
            self._parse_detail_card(lines, signal)
        else:
            self._parse_activity_card(lines, signal)

        # 兜底: 如果解析器没设 signal_time，使用当前时间
        if signal.get("signal_time") is None:
            signal["signal_time"] = datetime.now(UTC).isoformat()

        # 如果 token_symbol 还没值，取 lines[0]
        if not signal["token_symbol"] and lines:
            signal["token_symbol"] = lines[0][:128]

        return signal

    def _parse_activity_card(self, lines: list, signal: dict):
        """解析 5分钟活跃度榜单卡片

        格式: TOKEN_NAME, +X.X%, $X.XM/K (市值无标签), NNN, /, NNN, #N
        或:   TOKEN_NAME, +X.X%, MC, $X.XM/K, TXs, NNN, /, NNN, #N
        """
        signal["token_symbol"] = lines[0][:128] if lines else ""

        i = 1
        while i < len(lines):
            line = lines[i]
            upper = line.upper().strip()

            # 市值标签 + 下一行是值
            if upper in ("MC", "MARKET CAP", "MKT CAP", "LIQ", "LIQUIDITY") and i + 1 < len(lines):
                signal["pool_value"] = self._parse_number(lines[i + 1])
                i += 2
                continue

            # 价格变化百分比
            if (line.startswith("+") or line.startswith("-")) and "%" in line:
                signal["signal_content"] = f"24h: {line}"
                # 下一行如果是 $ 开头且没有 MC 标签，就是市值
                if i + 1 < len(lines) and lines[i + 1].startswith("$"):
                    if signal["pool_value"] is None:
                        signal["pool_value"] = self._parse_number(lines[i + 1])
                i += 1
                continue

            # 直接的 $ 值（市值无标签情况，兜底）
            if line.startswith("$") and signal["pool_value"] is None:
                signal["pool_value"] = self._parse_number(line)
                i += 1
                continue

            # 排名
            if line.startswith("#") and line[1:].isdigit():
                signal["signal_content"] = (signal["signal_content"] + f" 排名: {line}").strip()
                i += 1
                continue

            # TXs 标签
            if upper in ("TXS", "TX", "TRANSACTIONS") and i + 1 < len(lines):
                buy = lines[i + 1]
                sell = lines[i + 3] if i + 3 < len(lines) and lines[i + 2] == "/" else lines[i + 2] if i + 2 < len(lines) else "?"
                signal["signal_content"] = f"TXs: {buy}/{sell}"
                i += 4
                continue

            i += 1

        # 兜底: 如果在 lines 后面还有 $ 值没被 MC 标签覆盖，作为 pool_value
        if signal["pool_value"] is None:
            for line in lines:
                if line.startswith("$"):
                    signal["pool_value"] = self._parse_number(line)
                    break

    def _parse_detail_card(self, lines: list, signal: dict):
        """解析详细信号卡片
        
        格式: buy_buttons, ATH, $value, rank, 弃权, 黑名单, Top10, X%,
              HH:MM:SS, TOKEN_NAME, CHINESE_NAME, AGE, CONTRACT_SHORT,
              AI报告, 倍数, smart_wallets, 同时买入, 平均买入金额, $value,
              市值, $current, $prev, 持有人, N, N, 价格, $curr, $prev,
              流动池, $curr, $prev
        """
        n = len(lines)

        for i, line in enumerate(lines):
            ls = line.strip()

            # Top10 holder %
            if ls == "Top10" and i + 1 < n:
                signal["holder_rate"] = self._parse_percentage(lines[i + 1])

            # 信号时间 (HH:MM:SS)
            if re.match(r'^\d{1,2}:\d{2}:\d{2}$', ls):
                signal["signal_time"] = self._parse_time(ls)

            # 代币名: 下一个非数字行，在 Top10 和时间之后
            if (signal["token_symbol"] == "" and ls
                    and ls not in ("ATH", "Top10", "AI报告", "报告", "同时买入", "平均买入金额",
                                   "市值", "持有人", "价格", "流动池", "弃权", "黑名单")
                    and not re.match(r'^[\d.,]+$', ls)
                    and not re.match(r'^\d+[dhms]$', ls)  # age like "31d"
                    and not ls.startswith("$")
                    and not ls.startswith("<")
                    and not ls.endswith("x")
                    and not ls.endswith("%")
                    and not "pump" in ls.lower()
                    and len(ls) > 1):
                signal["token_symbol"] = ls[:128]

            # pool_value: 市值行后的第一个 $ 值
            if ls == "市值" and i + 1 < n:
                signal["pool_value"] = self._parse_number(lines[i + 1])

            # 构建 signal_content (汇总关键信息)
            if re.match(r'^<?\d+\.?\d*x$', ls) or (ls.startswith("<") and ls.endswith("x")):
                # 倍数如 4x, 12x, <1x
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += f"倍数: {ls}"
            elif "聪明钱包" in ls or "聪明钱" in ls:
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += ls
            elif ls == "平均买入金额" and i + 1 < n:
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += f"均买: {lines[i+1]}"

            # holder_rate 兜底: 百分比行在 Top10 之后
            if ls == "Top10" and i + 1 < n and signal["holder_rate"] is None:
                signal["holder_rate"] = self._parse_percentage(lines[i + 1])

    # ================================================================
    # 文本正则兜底提取 (选择器失败时的后备方案)
    # ================================================================

    @staticmethod
    def _extract_pool_value_from_text(text: str) -> Optional[float]:
        """从文本中提取流动池价值，如 '$24.5K'、'$1.2M'、'$500'"""
        # 匹配 $数字+单位 模式: $24.5K, $1.2M, $500, $45,123
        patterns = [
            (r'\$(\d+[\d,]*\.?\d*)\s*([Kk])', 1e3),     # $24.5K
            (r'\$(\d+[\d,]*\.?\d*)\s*([Mm])', 1e6),     # $1.2M
            (r'\$(\d+[\d,]*\.?\d*)\s*([Bb])', 1e9),     # $1B
            (r'\$(\d+[\d,]*\.?\d*)', 1),                  # $500
            (r'(\d+[\d,]*\.?\d*)\s*([Kk])\s*(?:pool|liquidity|liq)', 1e3),  # 24K pool
            (r'(\d+[\d,]*\.?\d*)\s*([Mm])\s*(?:pool|liquidity|liq)', 1e6),  # 2M pool
        ]
        for pattern, multiplier in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    num = float(match.group(1).replace(",", ""))
                    return num * multiplier
                except (ValueError, IndexError):
                    continue
        return None

    @staticmethod
    def _extract_holder_rate_from_text(text: str) -> Optional[float]:
        """从文本中提取持有人比例，如 '22.5%'、'Top10 45%'"""
        # 匹配百分比模式
        patterns = [
            r'(?:top\s*10|holder|holders?|持有)\s*[:：]?\s*(\d+\.?\d*)\s*%',  # "Top10: 22.5%"
            r'(\d+\.?\d*)\s*%\s*(?:holder|holders?|top\s*10|持有)',  # "22.5% holders"
            r'(\d+\.?\d*)\s*%',  # 任意百分比
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    val = float(match.group(1))
                    return val / 100 if val > 1 else val
                except ValueError:
                    continue
        return None

    @staticmethod
    def _extract_signal_content_from_text(text: str, token_symbol: str = "") -> str:
        """从文本中提取 AI 信号文案（如倍数、评分等）"""
        # 优先匹配已知信号格式
        patterns = [
            r'(x\d+\.?\d*\s*(?:multiplier|signal|倍|信号))',  # x2.5 multiplier
            r'(\d+\.?\d*x\s*(?:multiplier|signal)?)',          # 2.5x signal
            r'(AI\s*(?:score|评分|signal)[:：]?\s*\d+\.?\d*)', # AI score: 8.5
            r'(score\s*[:：]?\s*\d+\.?\d*)',                   # score: 8.5
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        # 兜底：取代币名之后的文本行（通常是描述文案），但排除明显的数据行
        lines = text.split("\n")
        symbol_idx = -1
        for i, line in enumerate(lines):
            if token_symbol and token_symbol.lower() in line.lower():
                symbol_idx = i
                break
        # 取代币名后 1-2 行作为信号内容
        content_lines = []
        for i in range(symbol_idx + 1, min(len(lines), symbol_idx + 3)):
            line = lines[i].strip()
            # 排除明显的数字/百分比/金额行
            if line and not re.match(r'^[\$\d,.%xXkKmMbB\s]+$', line):
                content_lines.append(line)
        return " ".join(content_lines[:2]) if content_lines else ""

    def _extract_contract_from_url(self, url: str) -> str:
        """从 Debot URL 中提取合约地址，去除前缀 ID
        如 /token/solana/314495_Hd6pqdE... -> Hd6pqdE...
        """
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
