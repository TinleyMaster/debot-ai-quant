"""
Debot API 客户端 - 直接通过 HTTP API 采集 AI 信号。
比 Playwright 更快、数据更准，作为首选方案；API 不可用时回退到 Playwright。
"""
import os
import time
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class DebotAPIClient:
    """
    Debot API 客户端。
    核心接口: /api/community/signal/channel/list
    仅需 User-Agent 即可访问，无需登录。
    """

    def __init__(self, base_url: str = None, chain: str = "solana",
                 page_size: int = 50, max_pages: int = 20,
                 request_interval: float = 0.3, cookie_file: str = None):
        self.base_url = base_url or os.environ.get("DEBOT_BASE_URL", "https://debot.ai")
        self.chain = chain
        self.page_size = page_size
        self.max_pages = max_pages
        self.request_interval = request_interval
        self.user_agent = DEFAULT_UA
        self._last_request_time = 0
        # 运行统计
        self.total_api_calls = 0
        self.total_signals_fetched = 0

        # 加载 cookie（支持从文件或环境变量读取）
        self.cookie = None
        cf = cookie_file or os.environ.get("DEBOT_COOKIE_FILE")
        if cf and os.path.exists(cf):
            try:
                self.cookie = self._load_cookie_from_file(cf)
                logger.info(f"已加载 cookie 文件: {cf}")
            except Exception as e:
                logger.warning(f"加载 cookie 文件失败: {e}")
        if not self.cookie:
            env_cookie = os.environ.get("DEBOT_COOKIE")
            if env_cookie:
                self.cookie = env_cookie

    @staticmethod
    def _load_cookie_from_file(path: str) -> str:
        """从 JSON 文件加载 cookie，返回 Cookie header 字符串"""
        with open(path) as f:
            cookies = json.load(f)
        parts = [f"{c['name']}={c['value']}" for c in cookies]
        return "; ".join(parts)

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """
        发送 GET 请求，返回解析后的 JSON。
        失败返回 None。
        """
        # 简单限流
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        headers = {
            "User-Agent": self.user_agent,
            "Referer": f"{self.base_url}/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._last_request_time = time.time()
                self.total_api_calls += 1
                data = json.loads(resp.read())
                if data.get("code") == 0:
                    return data.get("data")
                else:
                    logger.warning(f"API 返回错误: code={data.get('code')}, "
                                   f"msg={data.get('description', data.get('msg', ''))}")
                    return None
        except urllib.error.HTTPError as e:
            logger.error(f"API HTTP 错误: {e.code} {e.reason}, url={path}")
            return None
        except Exception as e:
            logger.error(f"API 请求失败: {e}, url={path}")
            return None

    def fetch_signal_page(self, page: int = 1) -> Optional[dict]:
        """
        获取一页信号列表。
        返回包含 results, meta.tokens, next, total 的 dict。
        """
        params = {
            "page_size": self.page_size,
            "chain": self.chain,
        }
        if page and page > 1:
            params["next"] = page

        data = self._get("/api/community/signal/channel/list", params)
        if data:
            results = data.get("results", [])
            tokens = data.get("meta", {}).get("tokens", {})
            logger.debug(f"API 第 {page} 页: {len(results)} 条信号, {len(tokens)} 个代币, "
                         f"total={data.get('total')}, next={data.get('next')}")
        return data

    def fetch_all_signals(self, max_signals: int = None) -> List[Dict]:
        """
        拉取全部信号（分页遍历），返回标准化后的信号列表。
        每条信号格式与 scraper.py 解析出的格式一致，可直接入库。

        Args:
            max_signals: 最大信号数上限，None 表示不限制（受 max_pages 限制）
        """
        all_signals = []
        all_tokens = {}      # 合并多页的代币基础信息
        all_metrics = {}     # 合并多页的行情指标（含 top10_position）
        all_signal_stats = {}  # 合并多页的信号统计（含 max_price_gain）
        all_safe_info = {}   # 安全信息
        all_social_info = {} # 社交信息
        all_token_tags = {}  # 代币标签
        seen_ids = set()
        page = 1
        total = None

        while page <= self.max_pages:
            data = self.fetch_signal_page(page)
            if not data:
                logger.warning(f"第 {page} 页获取失败，停止翻页")
                break

            results = data.get("results", [])
            meta = data.get("meta", {})
            tokens = meta.get("tokens", {})
            metrics = meta.get("metrics", {})
            signal_stats = meta.get("signals", {})
            safe_info = meta.get("safe_info", {})
            social_info = meta.get("social_info", {})
            token_tags = meta.get("token_tags", {})
            total = data.get("total")

            if not results:
                logger.info(f"第 {page} 页无数据，已到末尾")
                break

            # 合并各维度信息
            all_tokens.update(tokens)
            all_metrics.update(metrics)
            all_signal_stats.update(signal_stats)
            all_safe_info.update(safe_info)
            all_social_info.update(social_info)
            all_token_tags.update(token_tags)

            # 标准化每条信号
            new_this_page = 0
            for result in results:
                sig_id = result.get("id")
                if sig_id and sig_id in seen_ids:
                    continue
                if sig_id:
                    seen_ids.add(sig_id)

                signal = self._normalize_signal(
                    result,
                    token_info_map=all_tokens,
                    metrics_map=all_metrics,
                    signal_stats_map=all_signal_stats,
                    safe_info_map=all_safe_info,
                    social_info_map=all_social_info,
                    token_tags_map=all_token_tags,
                )
                if signal:
                    all_signals.append(signal)
                    new_this_page += 1

                if max_signals and len(all_signals) >= max_signals:
                    logger.info(f"已达到最大信号数 {max_signals}，停止翻页")
                    return all_signals

            logger.info(f"第 {page} 页: {new_this_page} 条新信号, 累计 {len(all_signals)} 条")

            # 判断是否还有下一页
            next_page = data.get("next")
            if next_page is None or next_page == "":
                break
            # next 可能是字符串页码
            try:
                next_page_int = int(next_page)
                if next_page_int <= page:
                    break
                page = next_page_int
            except (ValueError, TypeError):
                break

            # 如果已经达到 total，也停止
            if total and len(all_signals) >= total:
                break

        if total:
            logger.info(f"API 采集完成: 共 {len(all_signals)} 条信号 (总数 {total})")
        else:
            logger.info(f"API 采集完成: 共 {len(all_signals)} 条信号")

        self.total_signals_fetched = len(all_signals)
        return all_signals

    def _normalize_signal(self, result: dict,
                          token_info_map: dict = None,
                          metrics_map: dict = None,
                          signal_stats_map: dict = None,
                          safe_info_map: dict = None,
                          social_info_map: dict = None,
                          token_tags_map: dict = None) -> Optional[dict]:
        """
        将 API 返回的单条信号，标准化为 debot_signal 表的字段格式。
        与 scraper.py 的输出格式保持一致，便于复用数据库写入逻辑。

        Args:
            result: API results 数组中的单条信号
            token_info_map: meta.tokens 中的代币基础信息 {addr: info}
            metrics_map: meta.metrics 中的行情指标 {addr: metrics}（含 top10_position）
            signal_stats_map: meta.signals 中的信号统计 {addr: stats}（含 max_price_gain）
            safe_info_map: meta.safe_info 安全信息 {addr: info}
            social_info_map: meta.social_info 社交信息 {addr: info}
            token_tags_map: meta.token_tags 标签 {addr: [tags]}
        """
        try:
            token_info_map = token_info_map or {}
            metrics_map = metrics_map or {}
            signal_stats_map = signal_stats_map or {}
            safe_info_map = safe_info_map or {}
            social_info_map = social_info_map or {}
            token_tags_map = token_tags_map or {}

            contract_address = result.get("token")
            if not contract_address:
                return None

            # 代币基础信息
            token_data = token_info_map.get(contract_address, {})
            symbol = token_data.get("symbol", "")
            token_name = token_data.get("name", "")

            # 行情数据（优先用 token_trading_stat，metrics 里的作为补充）
            ts = result.get("token_trading_stat", {})
            price_usd = ts.get("price")
            market_cap = ts.get("mkt_cap")
            holders = ts.get("holders")
            liquidity = ts.get("liquidity")
            fdv = ts.get("fdv")

            # 从 metrics 补充 Top10 持仓比例等字段
            metrics = metrics_map.get(contract_address, {})
            top10_position = metrics.get("top10_position")

            # 从 signal_stats 取最大涨幅等统计
            sig_stat = signal_stats_map.get(contract_address, {})
            max_price_gain = sig_stat.get("max_price_gain")
            first_price = sig_stat.get("first_price")
            max_price = sig_stat.get("max_price")
            signal_count = sig_stat.get("signal_count")
            first_time = sig_stat.get("first_time")
            token_level = sig_stat.get("token_level", "")  # gold/silver/bronze

            # 安全信息 / 社交信息 / 标签
            # safe_info 的键可能是地址，也可能嵌套 chain，做兼容
            safe_info = safe_info_map.get(contract_address, {})
            if not safe_info:
                for v in safe_info_map.values():
                    if isinstance(v, dict) and contract_address in v:
                        safe_info = v[contract_address]
                        break
            social_info = social_info_map.get(contract_address, {})
            token_tags = token_tags_map.get(contract_address, [])

            # 钱包统计
            wallet_stats = result.get("wallet_stats", [])
            smart_wallets = len(wallet_stats) if wallet_stats else 0

            # 平均买入金额 (用 wallet_stats 里的 volume 算)
            avg_buy = None
            if wallet_stats:
                volumes = []
                for w in wallet_stats:
                    try:
                        v = float(w.get("volume", 0))
                        if v > 0:
                            volumes.append(v)
                    except (ValueError, TypeError):
                        pass
                if volumes:
                    avg_buy = sum(volumes) / len(volumes)

            # 信号时间
            create_time = result.get("create_time")
            if create_time:
                signal_time = datetime.fromtimestamp(create_time, tz=timezone.utc)
            else:
                signal_time = datetime.now(timezone.utc)

            # 倍数：用 max_price_gain（最大涨幅倍数），比如 8.6 表示 8.6 倍
            multiplier = None
            if max_price_gain is not None and max_price_gain > 0:
                multiplier = f"{max_price_gain:.1f}x"

            percent_5m = ts.get("percent5m")
            percent_1h = ts.get("percent1h")

            # 信号内容 — 把所有维度的信息都存进去，供前端详情弹窗展示
            signal_content = json.dumps({
                # 信号基本信息
                "signal_id": result.get("id"),
                "channel_id": result.get("channel_id"),
                "group_name": result.get("group_name"),
                "create_time": create_time,
                "avg_wallet_volume": result.get("avg_wallet_volume"),
                # 价格与涨跌幅
                "price": price_usd,
                "market_cap": market_cap,
                "fdv": fdv,
                "liquidity": liquidity,
                "holders": holders,
                "percent_5m": percent_5m,
                "percent_1h": percent_1h,
                "percent_12h": ts.get("percent12h"),
                "percent_24h": ts.get("percent24h"),
                "volume_5m": ts.get("volume_5minutes"),
                "volume_1h": ts.get("volume_1h"),
                "volume_6h": ts.get("volume_6h"),
                "volume_12h": ts.get("volume_12h"),
                "volume_24h": ts.get("volume_24h"),
                "last_update_time": ts.get("lastUpdateTime"),
                # 聪明钱
                "wallet_count": smart_wallets,
                "wallet_stats": wallet_stats,
                # 代币基础信息
                "token_name": token_name,
                "token_symbol": symbol,
                "token_address": contract_address,
                "creator_address": token_data.get("creator_address"),
                "total_supply": token_data.get("total_supply"),
                "decimals": token_data.get("decimals"),
                "logo": token_data.get("logo"),
                "launchpad": token_data.get("launchpad"),
                "launchpad_extra": token_data.get("launchpad_extra"),
                "creation_timestamp": token_data.get("creation_timestamp"),
                # Metrics（含 top10）
                "top10_position": top10_position,
                "pair": metrics.get("pair"),
                "dex_name": metrics.get("dex_name"),
                "token_reserve": metrics.get("token_reserve"),
                # 信号统计（最大涨幅）
                "max_price_gain": max_price_gain,
                "max_price": max_price,
                "first_price": first_price,
                "first_time": first_time,
                "signal_count": signal_count,
                # 安全信息
                "safe_info": safe_info,
                # 社交信息
                "social_info": social_info,
                # 标签
                "tags": token_tags,
            }, ensure_ascii=False)

            # 代币年龄（从创建时间算）
            token_age = None
            creation_ts = token_data.get("creation_timestamp")
            if creation_ts:
                age_seconds = int(time.time()) - creation_ts
                token_age = self._format_duration(age_seconds)

            # 安全信息解析（结构: {"solana": {is_mint_abandoned, is_block_address}, "debot_trust": true}）
            sol_safe = safe_info.get("solana", {}) if isinstance(safe_info, dict) else {}
            def _to_bool_flag(val):
                if val is None:
                    return None
                return bool(val)
            is_mint_abandoned = _to_bool_flag(sol_safe.get("is_mint_abandoned"))
            is_block_address = _to_bool_flag(sol_safe.get("is_block_address"))
            debot_trust = safe_info.get("debot_trust") if isinstance(safe_info, dict) else None

            return {
                "signal_time": signal_time.isoformat(),
                "contract_address": contract_address,
                "token_symbol": symbol,
                "token_name": token_name,
                "token_logo": token_data.get("logo"),
                "creator_address": token_data.get("creator_address"),
                "total_supply": token_data.get("total_supply"),
                "launchpad": token_data.get("launchpad"),
                "channel_id": result.get("channel_id"),
                "group_name": result.get("group_name"),
                "pool_value": liquidity,
                "holder_rate": top10_position,
                "signal_content": signal_content,
                "source_url": f"{self.base_url}/token/solana/{contract_address}",
                "market_cap": market_cap,
                "market_cap_prev": None,
                "holders_count": holders,
                "price_usd": price_usd,
                "price_usd_prev": None,
                "token_age": token_age,
                "creation_timestamp": creation_ts,  # unix 时间戳，秒
                "smart_wallets": smart_wallets,
                "avg_buy_amount": avg_buy,
                "multiplier": multiplier,
                "fdv": fdv,
                "volume_5m": ts.get("volume_5minutes"),
                "volume_1h": ts.get("volume_1h"),
                "volume_24h": ts.get("volume_24h"),
                "percent_5m": percent_5m,
                "percent_1h": percent_1h,
                "percent_24h": ts.get("percent24h"),
                "pair_address": metrics.get("pair"),
                "dex_name": metrics.get("dex_name"),
                "signal_count": signal_count,
                "token_level": token_level,        # gold / silver / bronze
                "first_price": first_price,
                "first_time": first_time,  # unix 时间戳，秒
                "is_mint_abandoned": is_mint_abandoned,
                "is_block_address": is_block_address,
                "debot_trust": debot_trust,
                "twitter": social_info.get("twitter"),
                "website": social_info.get("website"),
                "tags": ",".join(token_tags) if token_tags else None,
                # wallet_stats_list: 原始 list（给写入明细表用）
                "wallet_stats_list": wallet_stats if wallet_stats else [],
            }
        except Exception as e:
            token_addr = result.get('token') if result else 'unknown'
            logger.error(f"标准化信号失败: {e}, token={token_addr}")
            return None

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """将秒数格式化为人类可读的时长，如 '3h', '2d', '30m'"""
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m"
        elif seconds < 86400:
            return f"{seconds // 3600}h"
        else:
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            if hours == 0:
                return f"{days}d"
            return f"{days}d{hours}h"

    def test_connection(self) -> bool:
        """测试 API 是否可用"""
        data = self.fetch_signal_page(1)
        return data is not None and len(data.get("results", [])) > 0


# 便捷函数
def create_client_from_env() -> DebotAPIClient:
    """从环境变量创建客户端"""
    return DebotAPIClient(
        base_url=os.environ.get("DEBOT_BASE_URL", "https://debot.ai"),
        chain=os.environ.get("DEBOT_CHAIN", "solana"),
        page_size=int(os.environ.get("DEBOT_API_PAGE_SIZE", "50")),
        max_pages=int(os.environ.get("DEBOT_API_MAX_PAGES", "20")),
        request_interval=float(os.environ.get("DEBOT_API_INTERVAL", "0.3")),
        cookie_file=os.environ.get("DEBOT_COOKIE_FILE"),
    )
