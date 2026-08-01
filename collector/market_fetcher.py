"""
行情数据拉取服务 - DexScreener API
- 批量拉取代币行情快照（价格、成交量、流动性、涨跌幅）
- 增量补全，不重复覆盖已有数据
- 由 n8n 定时触发执行
"""
import os
import time
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from db import (
    init_db_pool, close_db_pool, get_conn,
    get_contracts_without_snapshot, insert_market_snapshot,
    get_market_snapshot_count,
)

logger = logging.getLogger("market_fetcher")

# DexScreener 免费 API 限制: 60 次/分钟
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/"
BATCH_SIZE = 30  # API 单次最多 30 个地址
REQUEST_INTERVAL = 1.5  # 请求间隔（秒），保证不超限


def fetch_token_batch(addresses: list) -> dict:
    """
    批量拉取代币行情数据。
    DexScreener tokens 接口，最多 30 个地址。
    返回 {"pairs": [...]}
    """
    addrs_str = ",".join(addresses)
    url = f"{DEXSCREENER_API}{addrs_str}"

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DebotQuant/1.0)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            logger.debug(f"API 返回: {len(data.get('pairs', []))} 个交易对")
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = 10 * attempt
                logger.warning(f"API 限流 (429)，等待 {wait_time}s 后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"API HTTP 错误: {e.code} - {url}")
                return {}
        except Exception as e:
            logger.warning(f"API 请求失败 (第 {attempt} 次): {e}")
            time.sleep(2 ** attempt)

    logger.error(f"API 请求最终失败: {url}")
    return {}


def parse_pair_data(pair: dict) -> dict:
    """将 DexScreener pair 数据转换为数据库快照格式"""
    base = pair.get("baseToken", {})

    snapshot = {
        "contract_address": base.get("address", ""),
        "symbol": base.get("symbol", "")[:64],
        "name": base.get("name", "")[:128],
        "price_usd": _safe_float(pair.get("priceUsd")),
        "volume_h24_usd": _safe_float(pair.get("volume", {}).get("h24")),
        "liquidity_usd": _safe_float(pair.get("liquidity", {}).get("usd")),
        "fdv_usd": _safe_float(pair.get("fdv")),
        "price_change_m5": _safe_float(pair.get("priceChange", {}).get("m5")),
        "price_change_h1": _safe_float(pair.get("priceChange", {}).get("h1")),
        "price_change_h6": _safe_float(pair.get("priceChange", {}).get("h6")),
        "price_change_h24": _safe_float(pair.get("priceChange", {}).get("h24")),
        "txns_h24_buys": pair.get("txns", {}).get("h24", {}).get("buys"),
        "txns_h24_sells": pair.get("txns", {}).get("h24", {}).get("sells"),
        "pair_created_at": _parse_pair_time(pair.get("pairCreatedAt")),
    }

    if not snapshot["contract_address"]:
        # 尝试从 quoteToken 获取
        quote = pair.get("quoteToken", {})
        snapshot["contract_address"] = quote.get("address", "")

    return snapshot


def _safe_float(value) -> float | None:
    """安全转换浮点数"""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_pair_time(ts) -> str | None:
    """解析交易对创建时间戳（毫秒）"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def run_fetch():
    """
    主执行函数：拉取所有待补全代币的行情数据。
    由 n8n 或 cron 触发调用。
    """
    logger.info("=" * 50)
    logger.info("行情数据拉取任务启动")
    logger.info("=" * 50)

    # 1. 初始化数据库
    try:
        init_db_pool()
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return {"success": False, "error": str(e), "fetched": 0, "stored": 0}

    # 2. 获取待补全的合约地址列表
    with get_conn() as conn:
        addresses = get_contracts_without_snapshot(conn, since_hours=0.25)
        total_before = get_market_snapshot_count(conn)

    if not addresses:
        logger.info("所有代币行情已是最新，无需拉取")
        close_db_pool()
        return {"success": True, "fetched": 0, "stored": 0, "total": total_before}

    logger.info(f"待拉取行情: {len(addresses)} 个代币 (已有 {total_before} 条快照)")

    # 3. 分批请求 API
    total_fetched = 0
    total_stored = 0
    batches = [addresses[i:i + BATCH_SIZE] for i in range(0, len(addresses), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        logger.info(f"批次 {batch_idx + 1}/{len(batches)}: 拉取 {len(batch)} 个代币...")

        data = fetch_token_batch(batch)
        pairs = data.get("pairs", [])

        if not pairs:
            logger.warning(f"批次 {batch_idx + 1} 无返回数据")
            continue

        total_fetched += len(pairs)

        # 4. 按合约去重：同一地址只保留流动性最高的交易对
        best_per_contract = {}
        for pair in pairs:
            snapshot = parse_pair_data(pair)
            addr = snapshot.get("contract_address", "")
            if not addr:
                continue
            liq = snapshot.get("liquidity_usd") or 0
            if addr not in best_per_contract or liq > (best_per_contract[addr].get("liquidity_usd") or 0):
                best_per_contract[addr] = snapshot

        # 5. 写入数据库
        with get_conn() as conn:
            for snapshot in best_per_contract.values():
                result_id = insert_market_snapshot(conn, snapshot)
                if result_id:
                    total_stored += 1

        logger.info(f"批次 {batch_idx + 1}: 返回 {len(pairs)} 对, 去重 {len(best_per_contract)} 代币, 入库 {total_stored}")

        # 请求间隔（保证不触发限流）
        if batch_idx < len(batches) - 1:
            time.sleep(REQUEST_INTERVAL)

    # 5. 汇总
    with get_conn() as conn:
        total_after = get_market_snapshot_count(conn)

    logger.info(f"行情拉取完成: 拉取 {total_fetched} 条, 入库 {total_stored} 条, 累计 {total_after} 条快照")

    close_db_pool()
    return {
        "success": True,
        "fetched": total_fetched,
        "stored": total_stored,
        "total_before": total_before,
        "total_after": total_after,
    }


if __name__ == "__main__":
    # 配置日志
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    result = run_fetch()
    print(json.dumps(result, ensure_ascii=False, indent=2))
