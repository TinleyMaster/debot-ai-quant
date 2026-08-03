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
                 request_interval: float = 0.3):
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
        all_tokens = {}  # 合并多页的代币信息
        seen_ids = set()
        page = 1
        total = None

        while page <= self.max_pages:
            data = self.fetch_signal_page(page)
            if not data:
                logger.warning(f"第 {page} 页获取失败，停止翻页")
                break

            results = data.get("results", [])
            tokens = data.get("meta", {}).get("tokens", {})
            total = data.get("total")

            if not results:
                logger.info(f"第 {page} 页无数据，已到末尾")
                break

            # 合并代币信息
            all_tokens.update(tokens)

            # 标准化每条信号
            new_this_page = 0
            for result in results:
                sig_id = result.get("id")
                if sig_id and sig_id in seen_ids:
                    continue
                if sig_id:
                    seen_ids.add(sig_id)

                signal = self._normalize_signal(result, all_tokens)
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

    def _normalize_signal(self, result: dict, token_info_map: dict) -> Optional[dict]:
        """
        将 API 返回的单条信号，标准化为 debot_signal 表的字段格式。
        与 scraper.py 的输出格式保持一致，便于复用数据库写入逻辑。
        """
        try:
            contract_address = result.get("token")
            if not contract_address:
                return None

            # 代币基础信息
            token_data = token_info_map.get(contract_address, {})
            symbol = token_data.get("symbol", "")
            token_name = token_data.get("name", "")

            # 行情数据
            ts = result.get("token_trading_stat", {})
            price_usd = ts.get("price")
            market_cap = ts.get("mkt_cap")
            holders = ts.get("holders")
            liquidity = ts.get("liquidity")
            fdv = ts.get("fdv")

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

            # 倍数（从价格变化估算，没有直接字段，用 wallet_stats 的价格差估算）
            # 注意：API 里没有直接的 "multiplier"（详情卡上的倍数），
            # 这里用涨跌幅替代或留空
            multiplier = None
            percent_5m = ts.get("percent5m")
            percent_1h = ts.get("percent1h")

            # 信号内容（把交易统计存进去，供前端展示）
            signal_content = json.dumps({
                "channel_id": result.get("channel_id"),
                "group_name": result.get("group_name"),
                "avg_wallet_volume": result.get("avg_wallet_volume"),
                "percent_5m": percent_5m,
                "percent_1h": percent_1h,
                "percent_12h": ts.get("percent12h"),
                "percent_24h": ts.get("percent24h"),
                "volume_5m": ts.get("volume_5minutes"),
                "volume_1h": ts.get("volume_1h"),
                "volume_24h": ts.get("volume_24h"),
                "liquidity": liquidity,
                "fdv": fdv,
                "wallet_count": smart_wallets,
                "token_name": token_name,
                "launchpad": token_data.get("launchpad"),
                "creation_timestamp": token_data.get("creation_timestamp"),
                "creator_address": token_data.get("creator_address"),
            }, ensure_ascii=False)

            # 代币年龄（从创建时间算）
            token_age = None
            creation_ts = token_data.get("creation_timestamp")
            if creation_ts:
                age_seconds = int(time.time()) - creation_ts
                token_age = self._format_duration(age_seconds)

            return {
                "signal_time": signal_time.isoformat(),
                "contract_address": contract_address,
                "token_symbol": symbol,
                "pool_value": liquidity,  # 流动池
                "holder_rate": None,  # API 里没有 top10 持仓比例
                "signal_content": signal_content,
                "source_url": f"{self.base_url}/token/solana/{contract_address}",
                "market_cap": market_cap,
                "market_cap_prev": None,
                "holders_count": holders,
                "price_usd": price_usd,
                "price_usd_prev": None,
                "token_age": token_age,
                "smart_wallets": smart_wallets,
                "avg_buy_amount": avg_buy,
                "multiplier": multiplier,
            }
        except Exception as e:
            logger.error(f"标准化信号失败: {e}, token={result.get('token')}")
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
    )
