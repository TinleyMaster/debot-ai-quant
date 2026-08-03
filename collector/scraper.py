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

    def is_browser_alive(self) -> bool:
        """检测浏览器和上下文是否存活"""
        try:
            if self._browser is None or not self._browser.is_connected():
                return False
            if self._context is None:
                return False
            # 轻量探活：尝试获取 contexts 列表
            self._browser.contexts
            return True
        except Exception:
            return False

    def restart_browser(self):
        """完全重启浏览器（用于崩溃后自愈）"""
        logger.warning("正在重启浏览器...")
        try:
            self.stop()
        except Exception:
            pass
        self.start()
        logger.info("浏览器已重启")

    def _ensure_page(self):
        """确保页面存在，浏览器崩溃时自动重启"""
        if not self.is_browser_alive():
            logger.warning("浏览器已崩溃，触发自动重启")
            self.restart_browser()

        if self._page is None or self._page.is_closed():
            try:
                self._page = self._context.new_page()
                logger.debug("创建新页面")
            except Exception as e:
                logger.error(f"创建新页面失败: {e}，尝试重启浏览器")
                self.restart_browser()
                self._page = self._context.new_page()
                logger.info("浏览器重启后创建新页面成功")

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
                # 导航前确保浏览器存活
                if not self.is_browser_alive():
                    logger.warning("导航前检测到浏览器已崩溃，重启中...")
                    self.restart_browser()
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

        # 解析信号卡片（内部处理虚拟滚动加载）
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

    def _find_detail_cards(self) -> list:
        """
        查找当前视口中所有"详情卡片"（含完整 AI 信号信息的卡片）。
        方法：用 JS 找出同时含 "Top10" + "smart wallets" + "Market Cap"
        且包含代币链接的卡片容器元素，加特殊属性标记后返回 ElementHandle 列表。
        """
        js_script = """
            () => {
                const all = document.querySelectorAll('*');
                const candidates = [];
                for (const el of all) {
                    const txt = (el.innerText || '').trim();
                    if (txt.length < 200 || txt.length > 3000) continue;
                    if (txt.indexOf('Top10') < 0) continue;
                    if (txt.indexOf('smart wallets') < 0) continue;
                    if (txt.indexOf('Market Cap') < 0) continue;
                    if (!el.querySelector('a[href*="/token/solana/"]')) continue;
                    candidates.push(el);
                }
                const cards = candidates.filter(c => {
                    return !candidates.some(other => other !== c && c.contains(other));
                });
                cards.forEach((c, i) => c.setAttribute('data-scraper-detail', i));
                return cards.length;
            }
        """
        try:
            count = self._page.evaluate(js_script)
            if count and count > 0:
                return self._page.query_selector_all("[data-scraper-detail]")
        except Exception as e:
            logger.warning(f"查找详情卡片失败: {e}")
        return []

    def _find_scroll_container(self) -> str:
        """
        查找虚拟列表的滚动容器选择器。
        详情卡片的祖先中，overflowY 为 auto 且高度远小于滚动高度的那个。
        """
        js_script = """
            () => {
                const all = document.querySelectorAll('*');
                // 先找任意一张详情卡
                let detailCard = null;
                for (const el of all) {
                    const txt = (el.innerText || '').trim();
                    if (txt.length < 200 || txt.length > 3000) continue;
                    if (txt.indexOf('Top10') < 0) continue;
                    if (txt.indexOf('smart wallets') < 0) continue;
                    if (txt.indexOf('Market Cap') < 0) continue;
                    if (!el.querySelector('a[href*="/token/solana/"]')) continue;
                    detailCard = el;
                    break;
                }
                if (!detailCard) return null;
                // 向上找有滚动条的祖先
                let ancestor = detailCard.parentElement;
                while (ancestor && ancestor !== document.body) {
                    const style = window.getComputedStyle(ancestor);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        ancestor.scrollHeight > ancestor.clientHeight + 100) {
                        // 返回一个稳定的选择器
                        if (ancestor.className && typeof ancestor.className === 'string') {
                            const cls = ancestor.className.split(/\\s+/).filter(c => c).slice(0, 3).join('.');
                            if (cls) return '.' + cls;
                        }
                    }
                    ancestor = ancestor.parentElement;
                }
                return null;
            }
        """
        try:
            sel = self._page.evaluate(js_script)
            return sel
        except Exception as e:
            logger.warning(f"查找滚动容器失败: {e}")
        return None

    def _scroll_down(self, scroll_sel: str, ratio: float = 0.7) -> bool:
        """
        向下滚动虚拟列表容器一屏的 ratio 比例。
        返回是否还有更多可滚动（未到底部）。
        """
        js_script = f"""
            () => {{
                const el = document.querySelector('{scroll_sel}');
                if (!el) return {{ hasMore: false }};
                const step = el.clientHeight * {ratio};
                el.scrollTop += step;
                const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
                return {{ hasMore: !atBottom, scrollTop: el.scrollTop, scrollHeight: el.scrollHeight }};
            }}
        """
        try:
            result = self._page.evaluate(js_script)
            return result.get('hasMore', False)
        except Exception as e:
            logger.warning(f"滚动失败: {e}")
            return False

    def _parse_signal_list(self) -> List[dict]:
        """
        解析页面信号列表 - 虚拟滚动逐屏采集。

        Debot 的 AI 信号列表是虚拟滚动列表，DOM 中只保留视口附近的卡片。
        策略：逐屏向下滚动，每屏解析当前可见的详情卡片，去重后合并。
        """
        signals = []
        seen = set()  # 去重: contract_address + signal_time
        seen_contracts = set()  # 去重: 只看合约地址（同一代币在多个视口出现）
        max_signals = self.scrape_rules.get("max_signals_per_run", 200)
        max_scrolls = self.scrape_rules.get("max_scroll_per_run", 30)

        # 找到滚动容器
        scroll_sel = self._find_scroll_container()
        if not scroll_sel:
            logger.warning("未找到滚动容器，回退到静态采集")
            detail_cards_raw = self._find_detail_cards()
            for card in detail_cards_raw:
                try:
                    signal = self._parse_single_card(card)
                    if signal and signal.get("contract_address"):
                        key = signal["contract_address"]
                        if key not in seen_contracts:
                            seen_contracts.add(key)
                            signals.append(signal)
                except Exception:
                    continue
            return signals

        logger.info(f"滚动容器: {scroll_sel}，开始逐屏采集（最多 {max_scrolls} 屏，上限 {max_signals} 条）")

        # 先滚回顶部（确保从头开始）
        try:
            self._page.evaluate(f"""() => {{
                const el = document.querySelector('{scroll_sel}');
                if (el) el.scrollTop = 0;
            }}""")
            time.sleep(0.5)
        except Exception:
            pass

        scroll_count = 0
        same_count_streak = 0  # 连续几屏没有新增代币，可能到底了
        last_count = 0

        while scroll_count < max_scrolls and len(signals) < max_signals:
            # 清除旧标记
            try:
                self._page.evaluate("""() => {
                    document.querySelectorAll('[data-scraper-detail]').forEach(el => {
                        el.removeAttribute('data-scraper-detail');
                    });
                }""")
            except Exception:
                pass

            # 采集当前视口的详情卡
            detail_cards_raw = self._find_detail_cards()

            new_this_screen = 0
            for card in detail_cards_raw:
                try:
                    signal = self._parse_single_card(card)
                    if signal and signal.get("contract_address"):
                        key = signal["contract_address"]
                        if key not in seen_contracts:
                            seen_contracts.add(key)
                            signals.append(signal)
                            new_this_screen += 1
                except Exception:
                    continue

            if new_this_screen == 0 and len(signals) == last_count:
                same_count_streak += 1
                if same_count_streak >= 3:
                    logger.info(f"连续 {same_count_streak} 屏无新增，已到底部")
                    break
            else:
                same_count_streak = 0

            last_count = len(signals)
            scroll_count += 1

            # 向下滚动
            has_more = self._scroll_down(scroll_sel, 0.7)
            time.sleep(1.0)

            if not has_more and same_count_streak >= 2:
                logger.info("已滚动到底部")
                break

        logger.info(f"本轮解析完成: 滚动 {scroll_count} 屏，共 {len(signals)} 条详情信号")
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
            # 非链接类型卡片（详情卡片等容器元素）
            raw_text = card.inner_text().strip()
            if not raw_text:
                return None

            # 从卡片内部的第一个代币链接提取合约地址和 source_url
            inner_link = card.query_selector("a[href*='/token/solana/']")
            if inner_link:
                href = inner_link.get_attribute("href") or ""
                signal["contract_address"] = self._extract_contract_from_url(href)
                signal["source_url"] = href if href.startswith("http") else f"{self.base_url}{href}"
            else:
                contract = self._safe_extract_text_in_element(
                    card, self.selectors.get("contract_address", ""))
                signal["contract_address"] = self._clean_contract_address(contract)
                signal["source_url"] = ""

            # 检测卡片类型（详情卡片含 Top10 + smart wallets + Market Cap）
            is_detail = ("Top10" in raw_text
                         and "smart wallets" in raw_text
                         and "Market Cap" in raw_text)

            # 初始化所有字段
            signal["token_symbol"] = ""
            signal["pool_value"] = None
            signal["holder_rate"] = None
            signal["signal_content"] = ""
            signal["signal_time"] = None
            signal["market_cap"] = None
            signal["market_cap_prev"] = None
            signal["holders_count"] = None
            signal["price_usd"] = None
            signal["price_usd_prev"] = None
            signal["token_age"] = None
            signal["smart_wallets"] = None
            signal["avg_buy_amount"] = None
            signal["multiplier"] = None

            if is_detail:
                lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
                self._parse_detail_card(lines, signal)
            else:
                # 活跃度卡片：使用选择器 + 正则兜底
                signal["token_symbol"] = self._safe_extract_text_in_element(
                    card, self.selectors.get("token_symbol", ""))
                time_text = self._safe_extract_text_in_element(
                    card, self.selectors.get("signal_time", ""))
                signal["signal_time"] = self._parse_time(time_text)
                pool_text = self._safe_extract_text_in_element(
                    card, self.selectors.get("pool_value", ""))
                signal["pool_value"] = self._parse_number(
                    pool_text) or self._extract_pool_value_from_text(raw_text)
                holder_text = self._safe_extract_text_in_element(
                    card, self.selectors.get("holder_rate", ""))
                signal["holder_rate"] = self._parse_percentage(
                    holder_text) or self._extract_holder_rate_from_text(raw_text)
                signal["signal_content"] = self._safe_extract_text_in_element(
                    card, self.selectors.get("signal_content", ""))
                if not signal["signal_content"]:
                    signal["signal_content"] = self._extract_signal_content_from_text(
                        raw_text, signal.get("token_symbol", ""))

            # 兜底：token_symbol 为空时用正则从文本提取
            if not signal["token_symbol"]:
                lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
                if lines:
                    signal["token_symbol"] = lines[0][:128]

            # 兜底：signal_time 为空时使用当前时间
            if signal.get("signal_time") is None:
                signal["signal_time"] = datetime.now(UTC).isoformat()

            return signal

        # --- bella8 <a> 链接卡片 ---
        href = card.get_attribute("href") or ""
        contract = self._extract_contract_from_url(href)
        signal["contract_address"] = contract
        signal["source_url"] = href if href.startswith("http") else f"{self.base_url}{href}"

        raw_text = card.inner_text().strip()
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

        # 检测卡片类型（详情卡片含 Top10 + smart wallets + Market Cap）
        is_detail = ("Top10" in raw_text
                     and "smart wallets" in raw_text
                     and "Market Cap" in raw_text)

        # 初始化字段
        signal["token_symbol"] = ""
        signal["pool_value"] = None
        signal["holder_rate"] = None
        signal["signal_content"] = ""
        signal["signal_time"] = None
        signal["market_cap"] = None
        signal["market_cap_prev"] = None
        signal["holders_count"] = None
        signal["price_usd"] = None
        signal["price_usd_prev"] = None
        signal["token_age"] = None
        signal["smart_wallets"] = None
        signal["avg_buy_amount"] = None
        signal["multiplier"] = None

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
        """解析活跃度榜单卡片

        实际结构（7-8行）:
          [0] TOKEN_NAME 或 标记字母(如 "M")
          [1] 代币名（当 [0] 是标记时） 或 +X.X%（涨跌幅）
          [+/-X.X%]    (涨跌幅)
          MC           (标签)
          $X.XK/M      (市值)
          TXs          (标签)
          NNN/NNN      (买卖笔数) 或 NNN + / + NNN(三行)
          #N           (排名)

        第0行如果是单个字母/短标记（长度<=2 且不是百分比），第1行才是代币名。
        """
        # 代币名: 找到第一个看起来像代币名的行（不是涨跌幅、不是标记、不是标签）
        token_name = ""
        for j, line in enumerate(lines):
            if not line:
                continue
            # 跳过涨跌幅行
            if (line.startswith("+") or line.startswith("-")) and "%" in line:
                continue
            # 跳过标签
            if line.upper() in ("MC", "TXS", "TX", "LIQ"):
                break
            # 跳过$开头、#开头
            if line.startswith("$") or line.startswith("#"):
                continue
            # 跳过纯数字
            if re.match(r'^[\d.,]+$', line):
                continue
            # 跳过短标记（单字母或非常短的标记词）
            # 但如果是第二个可行行且前一个是短标记，就取这个
            if len(line) <= 2 and j == 0:
                continue
            # 看起来像代币名
            if re.match(r'^[\w$:.\-+]+$', line) and len(line) >= 2:
                token_name = line[:128]
                break
        signal["token_symbol"] = token_name

        content_parts = []  # 累积信号信息而非覆盖
        i = 1
        while i < len(lines):
            line = lines[i]
            upper = line.upper().strip()

            # 市值标签 + 下一行是值
            if upper in ("MC", "MARKET CAP", "MKT CAP", "LIQ", "LIQUIDITY") and i + 1 < len(lines):
                val = self._parse_number(lines[i + 1])
                signal["pool_value"] = val
                signal["market_cap"] = val
                i += 2
                continue

            # 价格变化百分比
            if (line.startswith("+") or line.startswith("-")) and "%" in line:
                content_parts.append(f"24h: {line}")
                # 下一行如果是 $ 开头且没有 MC 标签，就是市值
                if i + 1 < len(lines) and lines[i + 1].startswith("$"):
                    if signal.get("pool_value") is None:
                        val = self._parse_number(lines[i + 1])
                        signal["pool_value"] = val
                        signal["market_cap"] = val
                i += 1
                continue

            # 直接的 $ 值（市值无标签情况，兜底）
            if line.startswith("$") and signal.get("pool_value") is None:
                val = self._parse_number(line)
                signal["pool_value"] = val
                signal["market_cap"] = val
                i += 1
                continue

            # TXs 标签（必须先于排名检测，确保 buy/sell 不被当成排名）
            if upper in ("TXS", "TX", "TRANSACTIONS") and i + 1 < len(lines):
                buy = lines[i + 1]
                sell = lines[i + 3] if i + 3 < len(lines) and lines[i + 2] == "/" else lines[i + 2] if i + 2 < len(lines) else "?"
                content_parts.append(f"成交量: {buy}/{sell}")
                i += 4
                continue

            # 排名 (独立行)
            if line.startswith("#") and line[1:].isdigit():
                content_parts.append(line)
                i += 1
                continue

            i += 1

        # 组装最终 signal_content
        signal["signal_content"] = " · ".join(content_parts) if content_parts else ""

        # 兜底: 如果在 lines 后面还有 $ 值没被 MC 标签覆盖，作为 pool_value
        if signal.get("pool_value") is None:
            for line in lines:
                if line.startswith("$"):
                    val = self._parse_number(line)
                    signal["pool_value"] = val
                    signal["market_cap"] = val
                    break

    def _parse_detail_card(self, lines: list, signal: dict):
        """解析详细信号卡片（英文标签版本，已验证的完整结构）

        完整结构 (33-34行):
          [ 0] "12" / "2" / "M" 等   (左边栏数字/标记，跳过)
          [ 1] "No Mint"             (风险标记1，可选)
          [ 2] "Blacklist"           (风险标记2，可选)
          [ 3] "Top10"               ← Top10 标签
          [ 4] "27.7%"               ← Top10 比例
          [ 5] "10:27:37"            ← 信号时间 (HH:MM:SS)
          [ 6] "MADLADS"             ← 代币名 (或第6行是"Live"等状态，第7行才是代币名)
          [ 7] "30m"                 ← 代币年龄 (1d/2h/30m/865d)
          [ 8] "4sYb...XpZ9"         (短合约地址，跳过，从 href 取)
          [ 9] "AI"                  (标签，跳过)
          [10] "1x" / "48"           (倍数 或 涨幅数字)
          [11] "%"                   (当涨幅数字单独成行时，下一行是 %)
          [12] "3 smart wallets"     (聪明钱包数)
          [13] "Buy"                 (标签，跳过)
          [14] "Avg Buy"
          [15] "$301.68"             (平均买入金额)
          [16] "Market Cap"
          [17] "$43.6K"              (当前市值)
          [18] "$76.5K"              (前市值)
          [19] "Holder"
          [20] "122"                 (当前持有人，可能带逗号: 1,781)
          [21] "213"                 (前持有人)
          [22] "Price"
          [23] "$0.0₄4356"           (当前价格，下标数字格式)
          [24] "$0.0₄7648"           (前价格)
          [25] "Liq"
          [26] "$16.2K"              (当前流动池)
          [27] "$23.5K"              (前流动池)
          [28-31] "0.01"/"0.1"/"0.5"/"1"  (进度条刻度，跳过)
          [32] "ATH"
          [33] "$41.2K"              (ATH市值，跳过)

        说明:
        - 风险标记数量不固定（0-3个不等，如 No Mint, Blacklist, Mint 等）
        - "Top10" 标签和百分比行是固定的
        - 时间行是固定格式 HH:MM:SS
        - 时间行之后的第一个看起来像代币名的行就是代币名
        - 代币名后面是年龄（d/h/m 格式）
        """
        n = len(lines)

        # === 定位关键行索引 ===
        top10_idx = None
        time_idx = None
        ai_idx = None

        for i, ls in enumerate(lines):
            if ls == "Top10" and top10_idx is None:
                top10_idx = i
            if re.match(r'^\d{1,2}:\d{2}:\d{2}$', ls) and time_idx is None:
                time_idx = i
            if ls == "AI" and ai_idx is None:
                ai_idx = i

        # --- Top10 持仓比例 ---
        if top10_idx is not None and top10_idx + 1 < n:
            signal["holder_rate"] = self._parse_percentage(lines[top10_idx + 1])

        # --- 信号时间 ---
        if time_idx is not None:
            signal["signal_time"] = self._parse_time(lines[time_idx])

        # --- 代币名: 时间行之后、年龄行之前、AI行之前 ---
        if time_idx is not None and not signal.get("token_symbol"):
            # 从时间行下一行开始，到 AI 行之前，找看起来像代币名的
            end = ai_idx if ai_idx is not None else min(time_idx + 6, n)
            for j in range(time_idx + 1, end):
                candidate = lines[j].strip()
                if not candidate:
                    continue
                # 跳过年龄格式
                if re.match(r'^\d+[dhms]$', candidate):
                    break
                # 跳过 LIVE 等状态词
                if candidate in ("LIVE", "New", "Trending", "Hot", "Pinned"):
                    continue
                # 跳过合约地址
                if "pump" in candidate.lower() and len(candidate) > 10:
                    break
                # 跳过纯数字、百分比、$开头、#开头
                if (candidate.startswith("$")
                        or candidate.startswith("#")
                        or candidate.endswith("%")
                        or re.match(r'^[\d.,]+$', candidate)):
                    continue
                # 代币名：字母数字符号组合，长度 2-32
                if re.match(r'^[\w$:.\-+]+$', candidate) and 2 <= len(candidate) <= 32:
                    signal["token_symbol"] = candidate[:128]
                    break

        # --- 代币年龄 (d/h/m 格式) ---
        # 在时间行之后、AI 行之前找
        if signal.get("token_age") is None:
            start = time_idx + 1 if time_idx is not None else 0
            end = ai_idx if ai_idx is not None else min(start + 5, n)
            for j in range(start, end):
                if re.match(r'^\d+[dhms]$', lines[j]):
                    signal["token_age"] = lines[j]
                    break

        for i, line in enumerate(lines):
            ls = line.strip()

            # --- 倍数 ---
            # 形如 2x, 12x, <1x, 0.5x
            if (re.match(r'^<?\d+\.?\d*x$', ls)
                    and signal.get("multiplier") is None
                    and ls != "x"):
                signal["multiplier"] = ls
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += f"倍数: {ls}"

            # --- 倍数/涨幅: 纯数字 + 下一行是 "%" ---
            if (re.match(r'^\d+\.?\d*$', ls)
                    and i + 1 < n
                    and lines[i + 1].strip() == "%"
                    and signal.get("multiplier") is None):
                pct = f"{ls}%"
                signal["multiplier"] = pct
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += f"涨幅: {pct}"

            # --- 聪明钱包数量: "3 smart wallets" ---
            if "smart wallets" in ls:
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += ls
                m = re.search(r'(\d+)', ls)
                if m and signal.get("smart_wallets") is None:
                    signal["smart_wallets"] = int(m.group(1))

            # --- 平均买入金额: Avg Buy + $值 ---
            if ls in ("Avg Buy", "Avg Buy Amount") and i + 1 < n:
                if signal["signal_content"]:
                    signal["signal_content"] += "; "
                signal["signal_content"] += f"均买: {lines[i+1]}"
                if signal.get("avg_buy_amount") is None:
                    signal["avg_buy_amount"] = self._parse_number(lines[i + 1])

            # --- 市值: Market Cap + 当前值 + 前值 ---
            if ls == "Market Cap" and i + 1 < n:
                signal["market_cap"] = self._parse_number(lines[i + 1])
                if i + 2 < n:
                    signal["market_cap_prev"] = self._parse_number(lines[i + 2])
                if signal.get("pool_value") is None:
                    signal["pool_value"] = signal["market_cap"]

            # --- 持有人数量: Holder + 当前 + 前 ---
            if ls == "Holder" and i + 1 < n:
                try:
                    signal["holders_count"] = int(lines[i + 1].replace(",", ""))
                except (ValueError, TypeError):
                    pass

            # --- 价格: Price + 当前 + 前 (可能含下标数字格式) ---
            if ls == "Price" and i + 1 < n:
                signal["price_usd"] = self._parse_number(lines[i + 1])
                if i + 2 < n:
                    signal["price_usd_prev"] = self._parse_number(lines[i + 2])

            # --- 流动池: Liq + 当前 + 前 ---
            if ls in ("Liq", "Liquidity") and i + 1 < n:
                pool_val = self._parse_number(lines[i + 1])
                if pool_val is not None:
                    signal["pool_value"] = pool_val

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
        """解析金额文本为数字。支持下标数字格式如 $0.0₄7582（₄表示4个0）。"""
        if not text:
            return None
        text = text.replace(",", "").replace("$", "").replace("¥", "").strip()

        # 处理下标数字格式: 如 "0.0₄7582" -> 0.00007582
        # 下标数字表示"小数点后的 0 的个数"
        # 例如 0.0₄7582 表示小数点后有 4 个 0 然后是 7582
        # Unicode 下标数字: ₀₁₂₃₄₅₆₇₈₉ (U+2080 - U+2089)
        subscript_map = {
            '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4',
            '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9',
        }
        subscript_pattern = re.compile(r'([₀₁₂₃₄₅₆₇₈₉]+)')
        m = subscript_pattern.search(text)
        if m:
            sub_digits = m.group(1)
            zero_count = int(''.join(subscript_map[c] for c in sub_digits))
            # 找到小数点位置
            dot_idx = text.find('.')
            if dot_idx >= 0:
                # 保留整数部分和小数点，然后补 zero_count 个 0，再加下标后的数字
                int_part = text[:dot_idx + 1]  # 如 "0."
                after = text[m.end():]         # 如 "7582"
                text = int_part + '0' * zero_count + after
            else:
                # 没有小数点，直接在数字间插零
                before = text[:m.start()]
                after = text[m.end():]
                text = before + '0' * zero_count + after

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
            # 如果绝对值 > 1，假设直接是百分比数值（如 5 表示 5%，-5 表示 -5%）
            return val / 100 if abs(val) > 1 else val
        except ValueError:
            return None
