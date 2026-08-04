"""
数据库操作模块 - PostgreSQL 连接和 CRUD 操作
"""
import os
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool

logger = logging.getLogger(__name__)

# 连接池（模块级单例）
_connection_pool: Optional[pool.ThreadedConnectionPool] = None


def init_db_pool():
    """初始化数据库连接池，从环境变量读取配置"""
    global _connection_pool

    dsn = (
        f"host={os.environ['DB_HOST']} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ['DB_NAME']} "
        f"user={os.environ['DB_USER']} "
        f"password={os.environ['DB_PASSWORD']}"
    )

    _connection_pool = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=dsn,
    )
    logger.info("数据库连接池初始化成功")

    # 验证连接
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    logger.info("数据库连接验证通过")

    # 执行增量迁移
    run_migrations()


def close_db_pool():
    """关闭数据库连接池"""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("数据库连接池已关闭")


@contextmanager
def get_conn():
    """获取数据库连接上下文管理器"""
    if _connection_pool is None:
        raise RuntimeError("数据库连接池未初始化，请先调用 init_db_pool()")
    conn = _connection_pool.getconn()
    try:
        yield conn
    finally:
        _connection_pool.putconn(conn)


# ============================================================
# 数据库迁移
# ============================================================

def run_migrations():
    """执行增量数据库迁移 (幂等，已有列/表则跳过)"""
    # ---- 旧字段迁移 ----
    migrations = [
        ("market_cap", "NUMERIC(24, 6)"),
        ("market_cap_prev", "NUMERIC(24, 6)"),
        ("holders_count", "INT"),
        ("price_usd", "NUMERIC(24, 12)"),
        ("price_usd_prev", "NUMERIC(24, 12)"),
        ("token_age", "VARCHAR(32)"),
        ("smart_wallets", "INT"),
        ("avg_buy_amount", "NUMERIC(24, 6)"),
        ("multiplier", "VARCHAR(16)"),
        ("channel_id", "INT"),
        ("group_name", "VARCHAR(128)"),
    ]
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # ---- 列迁移 ----
                for col_name, col_type in migrations:
                    cur.execute("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='debot_signal' AND column_name=%s
                    """, (col_name,))
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE debot_signal ADD COLUMN {col_name} {col_type}")
                        conn.commit()
                        logger.info(f"数据库迁移: 添加列 debot_signal.{col_name}")

                # ---- 新表创建（IF NOT EXISTS 幂等） ----
                _create_tables_if_not_exist(cur)
                conn.commit()
    except Exception as e:
        logger.warning(f"数据库迁移异常 (可能表尚未创建): {e}")


def _create_tables_if_not_exist(cur):
    """创建所有新表（用 CREATE TABLE IF NOT EXISTS，幂等）"""

    # 代币详情
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debot_token_detail (
            contract_address  VARCHAR(128) PRIMARY KEY,
            token_symbol      VARCHAR(128),
            token_name        VARCHAR(128),
            token_logo        VARCHAR(512),
            creator_address   VARCHAR(64),
            total_supply      NUMERIC(36),
            launchpad         VARCHAR(32),
            creation_time     TIMESTAMPTZ,
            is_mint_abandoned BOOLEAN,
            is_block_address  BOOLEAN,
            debot_trust       BOOLEAN,
            twitter           VARCHAR(512),
            website           VARCHAR(512),
            tags              VARCHAR(256),
            update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # 行情快照
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debot_token_metric (
            id                BIGSERIAL PRIMARY KEY,
            contract_address  VARCHAR(128) NOT NULL,
            snapshot_time     TIMESTAMPTZ NOT NULL,
            price             NUMERIC(24, 12),
            market_cap        NUMERIC(24, 6),
            fdv               NUMERIC(24, 6),
            liquidity         NUMERIC(24, 6),
            holder_count      INT,
            top10_position    NUMERIC(6, 4),
            volume_5m         NUMERIC(24, 6),
            volume_1h         NUMERIC(24, 6),
            volume_24h        NUMERIC(24, 6),
            percent_5m        NUMERIC(12, 8),
            percent_1h        NUMERIC(12, 8),
            percent_24h       NUMERIC(12, 8),
            pair_address      VARCHAR(64),
            dex_name          VARCHAR(32)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_metric_contract ON debot_token_metric(contract_address)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_metric_snapshot ON debot_token_metric(snapshot_time DESC)")

    # 信号累计统计
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debot_signal_agg (
            contract_address  VARCHAR(128) PRIMARY KEY,
            signal_count      INT,
            first_signal_time TIMESTAMPTZ,
            first_price       NUMERIC(24, 12),
            max_price         NUMERIC(24, 12),
            max_price_gain    NUMERIC(12, 6),
            update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # 聪明钱包交易明细
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debot_wallet_trade (
            id                BIGSERIAL PRIMARY KEY,
            signal_id         BIGINT REFERENCES debot_signal(id) ON DELETE CASCADE,
            contract_address  VARCHAR(128) NOT NULL,
            wallet_alias      VARCHAR(64),
            wallet_address    VARCHAR(64),
            amount            NUMERIC(36),
            price             NUMERIC(24, 12),
            volume_usd        NUMERIC(24, 6),
            trade_time        TIMESTAMPTZ
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_signal ON debot_wallet_trade(signal_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_contract ON debot_wallet_trade(contract_address)")


# ============================================================
# 信号数据操作
# ============================================================

def insert_signal(conn, signal_data: dict) -> Optional[int]:
    """
    插入一条信号主记录，重复则跳过。
    返回插入的 id，重复或失败返回 None。
    """
    sql = """
        INSERT INTO debot_signal
            (signal_time, contract_address, token_symbol,
             pool_value, holder_rate, signal_content, source_url,
             market_cap, market_cap_prev, holders_count,
             price_usd, price_usd_prev, token_age,
             smart_wallets, avg_buy_amount, multiplier,
             channel_id, group_name)
        VALUES (%(signal_time)s, %(contract_address)s, %(token_symbol)s,
                %(pool_value)s, %(holder_rate)s, %(signal_content)s, %(source_url)s,
                %(market_cap)s, %(market_cap_prev)s, %(holders_count)s,
                %(price_usd)s, %(price_usd_prev)s, %(token_age)s,
                %(smart_wallets)s, %(avg_buy_amount)s, %(multiplier)s,
                %(channel_id)s, %(group_name)s)
        ON CONFLICT (contract_address, signal_time) DO NOTHING
        RETURNING id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, signal_data)
            result = cur.fetchone()
            conn.commit()
            if result:
                return result[0]
            return None
    except Exception as e:
        conn.rollback()
        logger.error(f"插入信号失败: {e}, 数据: {signal_data.get('contract_address')}")
        return None


def upsert_token_detail(conn, detail: dict) -> None:
    """
    插入或更新代币详情（token基础信息 + 安全 + 社交）。
    主键: contract_address
    """
    sql = """
        INSERT INTO debot_token_detail
            (contract_address, token_symbol, token_name, token_logo,
             creator_address, total_supply, launchpad, creation_time,
             is_mint_abandoned, is_block_address, debot_trust,
             twitter, website, tags, update_time)
        VALUES (%(contract_address)s, %(token_symbol)s, %(token_name)s, %(token_logo)s,
                %(creator_address)s, %(total_supply)s, %(launchpad)s, %(creation_time)s,
                %(is_mint_abandoned)s, %(is_block_address)s, %(debot_trust)s,
                %(twitter)s, %(website)s, %(tags)s, NOW())
        ON CONFLICT (contract_address) DO UPDATE SET
            token_symbol = EXCLUDED.token_symbol,
            token_name = EXCLUDED.token_name,
            token_logo = EXCLUDED.token_logo,
            total_supply = EXCLUDED.total_supply,
            is_mint_abandoned = EXCLUDED.is_mint_abandoned,
            is_block_address = EXCLUDED.is_block_address,
            debot_trust = EXCLUDED.debot_trust,
            twitter = EXCLUDED.twitter,
            website = EXCLUDED.website,
            tags = EXCLUDED.tags,
            update_time = NOW()
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, detail)
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"upsert 代币详情失败: {e}, addr={detail.get('contract_address')}")


def insert_token_metric(conn, metric: dict) -> Optional[int]:
    """
    插入一条代币行情快照。
    直接 INSERT（因为是时间序列，每次都是新记录）。
    """
    sql = """
        INSERT INTO debot_token_metric
            (contract_address, snapshot_time, price, market_cap, fdv,
             liquidity, holder_count, top10_position,
             volume_5m, volume_1h, volume_24h,
             percent_5m, percent_1h, percent_24h,
             pair_address, dex_name)
        VALUES (%(contract_address)s, %(snapshot_time)s, %(price)s, %(market_cap)s, %(fdv)s,
                %(liquidity)s, %(holder_count)s, %(top10_position)s,
                %(volume_5m)s, %(volume_1h)s, %(volume_24h)s,
                %(percent_5m)s, %(percent_1h)s, %(percent_24h)s,
                %(pair_address)s, %(dex_name)s)
        RETURNING id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, metric)
            result = cur.fetchone()
            conn.commit()
            return result[0] if result else None
    except Exception as e:
        conn.rollback()
        logger.warning(f"插入行情快照失败: {e}, addr={metric.get('contract_address')}")
        return None


def upsert_signal_agg(conn, agg: dict) -> None:
    """
    插入或更新代币信号累计统计。
    主键: contract_address
    """
    sql = """
        INSERT INTO debot_signal_agg
            (contract_address, signal_count, first_signal_time, first_price,
             max_price, max_price_gain, update_time)
        VALUES (%(contract_address)s, %(signal_count)s, %(first_signal_time)s, %(first_price)s,
                %(max_price)s, %(max_price_gain)s, NOW())
        ON CONFLICT (contract_address) DO UPDATE SET
            signal_count = EXCLUDED.signal_count,
            first_signal_time = EXCLUDED.first_signal_time,
            first_price = EXCLUDED.first_price,
            max_price = EXCLUDED.max_price,
            max_price_gain = EXCLUDED.max_price_gain,
            update_time = NOW()
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, agg)
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"upsert 信号累计失败: {e}, addr={agg.get('contract_address')}")


def insert_wallet_trades(conn, signal_id: int, contract_address: str, wallets: list) -> None:
    """
    批量插入聪明钱包交易明细。
    """
    if not wallets:
        return
    sql = """
        INSERT INTO debot_wallet_trade
            (signal_id, contract_address, wallet_alias, wallet_address,
             amount, price, volume_usd, trade_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        with conn.cursor() as cur:
            for w in wallets:
                cur.execute(sql, (
                    signal_id,
                    contract_address,
                    w.get("alias"),
                    w.get("wallet") or None,
                    float(w.get("amount", 0)) if w.get("amount") else None,
                    float(w.get("price", 0)) if w.get("price") else None,
                    float(w.get("volume", 0)) if w.get("volume") else None,
                    _ts_to_dt(w.get("last_trade_time")),
                ))
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"插入钱包交易明细失败: {e}, signal_id={signal_id}")


def _ts_to_dt(ts):
    """时间戳转 datetime"""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except:
        return None


def insert_token_info(conn, token_data: dict) -> Optional[int]:
    """
    插入或更新代币风控信息。
    返回 id。
    """
    sql = """
        INSERT INTO token_base_info
            (contract_address, pool_lock_status, buy_tax, sell_tax,
             contract_audit, risk_flags, risk_level)
        VALUES (%(contract_address)s, %(pool_lock_status)s, %(buy_tax)s, %(sell_tax)s,
                %(contract_audit)s, %(risk_flags)s, %(risk_level)s)
        ON CONFLICT (contract_address)
        DO UPDATE SET
            pool_lock_status = EXCLUDED.pool_lock_status,
            buy_tax = EXCLUDED.buy_tax,
            sell_tax = EXCLUDED.sell_tax,
            contract_audit = EXCLUDED.contract_audit,
            risk_flags = EXCLUDED.risk_flags,
            risk_level = EXCLUDED.risk_level,
            update_time = NOW()
        RETURNING id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, token_data)
            result = cur.fetchone()
            conn.commit()
            return result[0] if result else None
    except Exception as e:
        conn.rollback()
        logger.error(f"插入代币信息失败: {e}, 合约: {token_data.get('contract_address')}")
        return None


def get_latest_signal_time(conn) -> Optional[str]:
    """查询数据库中最新一条信号的 signal_time，用于增量采集"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(signal_time) FROM debot_signal")
            result = cur.fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"查询最新信号时间失败: {e}")
        return None


def get_unprocessed_count(conn) -> int:
    """统计未处理的信号数量"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM debot_signal WHERE is_processed = FALSE")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"统计未处理信号失败: {e}")
        return 0


def insert_run_log(conn, signals_scraped: int, signals_new: int,
                   errors_count: int, error_detail: str = None,
                   alert_type: str = None, duration_ms: int = None):
    """记录本轮采集运行日志"""
    sql = """
        INSERT INTO collector_run_log
            (signals_scraped, signals_new, errors_count, error_detail, alert_type, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (signals_scraped, signals_new, errors_count,
                            error_detail, alert_type, duration_ms))
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"记录运行日志失败: {e}")


def insert_alert(conn, alert_type: str, alert_message: str = None):
    """记录一条告警"""
    sql = """
        INSERT INTO collector_alerts (alert_type, alert_message)
        VALUES (%s, %s)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (alert_type, alert_message))
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"记录告警失败: {e}")


def get_latest_unresolved_alert(conn) -> Optional[dict]:
    """获取最新一条未解决的告警"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT alert_type, alert_message, alert_time FROM collector_alerts "
                "WHERE resolved = FALSE ORDER BY alert_time DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                return {"alert_type": row[0], "alert_message": row[1], "alert_time": str(row[2])}
    except Exception:
        pass
    return None


# ============================================================
# 行情数据操作
# ============================================================

def get_distinct_contracts(conn) -> list:
    """获取所有去重的合约地址列表"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT contract_address FROM debot_signal WHERE contract_address != ''")
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"获取合约地址列表失败: {e}")
        return []


def get_contracts_without_snapshot(conn, since_hours: float = 0.25) -> list:
    """
    获取需要补全行情快照的合约地址。
    since_hours: 多少小时内有快照的视为已补全，跳过。
    设为 0.25 小时（15分钟），对齐土狗币行情节奏，确保每次拉取都能写入新快照。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ds.contract_address
                FROM debot_signal ds
                WHERE ds.contract_address != ''
                AND NOT EXISTS (
                    SELECT 1 FROM token_market_snapshot tms
                    WHERE tms.contract_address = ds.contract_address
                    AND tms.snapshot_time > NOW() - INTERVAL '%s hours'
                )
            """, (since_hours,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"获取待补全合约列表失败: {e}")
        return []


def insert_market_snapshot(conn, snapshot: dict) -> Optional[int]:
    """插入一条行情快照记录，同一合约+时间戳重复则更新"""
    sql = """
        INSERT INTO token_market_snapshot
            (contract_address, symbol, name, price_usd, volume_h24_usd,
             liquidity_usd, fdv_usd, price_change_m5, price_change_h1,
             price_change_h6, price_change_h24, txns_h24_buys, txns_h24_sells,
             pair_created_at)
        VALUES (%(contract_address)s, %(symbol)s, %(name)s, %(price_usd)s,
                %(volume_h24_usd)s, %(liquidity_usd)s, %(fdv_usd)s,
                %(price_change_m5)s, %(price_change_h1)s, %(price_change_h6)s,
                %(price_change_h24)s, %(txns_h24_buys)s, %(txns_h24_sells)s,
                %(pair_created_at)s)
        ON CONFLICT (contract_address, snapshot_time)
        DO UPDATE SET
            price_usd = EXCLUDED.price_usd,
            volume_h24_usd = EXCLUDED.volume_h24_usd,
            liquidity_usd = EXCLUDED.liquidity_usd,
            price_change_m5 = EXCLUDED.price_change_m5,
            price_change_h1 = EXCLUDED.price_change_h1,
            price_change_h6 = EXCLUDED.price_change_h6,
            price_change_h24 = EXCLUDED.price_change_h24
        RETURNING id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, snapshot)
            result = cur.fetchone()
            conn.commit()
            return result[0] if result else None
    except Exception as e:
        conn.rollback()
        logger.error(f"插入行情快照失败: {e}")
        return None


def get_market_snapshot_count(conn) -> int:
    """统计行情快照数量"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM token_market_snapshot")
            return cur.fetchone()[0]
    except Exception:
        return 0


# ============================================================
# 回测数据操作
# ============================================================

def get_backtest_data(conn) -> dict:
    """
    加载回测所需的全量数据：信号列表 + 行情快照。
    返回 {"signals": [...], "snapshots": {addr: [...]}}
    """
    result = {"signals": [], "snapshots": {}}

    # 加载信号（限制 1000 条）
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, signal_time, contract_address, token_symbol,
                       pool_value, holder_rate, signal_content,
                       market_cap, holders_count, price_usd,
                       token_age, smart_wallets, avg_buy_amount, multiplier
                FROM debot_signal
                WHERE contract_address != ''
                ORDER BY signal_time DESC
                LIMIT 1000
            """)
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                signal = dict(zip(columns, row))
                # Decimal -> float 转换
                for key in ("pool_value", "holder_rate", "market_cap",
                           "price_usd", "avg_buy_amount"):
                    if signal.get(key) is not None:
                        signal[key] = float(signal[key])
                result["signals"].append(signal)
    except Exception as e:
        logger.error(f"加载信号数据失败: {e}")

    # 加载行情快照
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contract_address, price_usd, volume_h24_usd,
                       liquidity_usd, fdv_usd,
                       price_change_m5, price_change_h1, price_change_h6, price_change_h24,
                       pair_created_at, snapshot_time
                FROM token_market_snapshot
                WHERE price_usd IS NOT NULL AND price_usd > 0
                ORDER BY contract_address, snapshot_time
            """)
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                snap = dict(zip(columns, row))
                # Decimal -> float 转换
                for key in ("price_usd", "volume_h24_usd", "liquidity_usd",
                           "price_change_m5", "price_change_h1",
                           "price_change_h6", "price_change_h24"):
                    if snap.get(key) is not None:
                        snap[key] = float(snap[key])
                addr = snap["contract_address"]
                if addr not in result["snapshots"]:
                    result["snapshots"][addr] = []
                result["snapshots"][addr].append(snap)
    except Exception as e:
        logger.error(f"加载行情快照失败: {e}")

    return result


def save_best_strategy(conn, strategy_data: dict) -> int | None:
    """保存最优策略到 best_strategy_config 表"""
    sql = """
        INSERT INTO best_strategy_config
            (strategy_params, backtest_profit, max_drawdown, win_rate,
             backtest_date_range, is_enable)
        VALUES (%(strategy_params)s, %(backtest_profit)s, %(max_drawdown)s,
                %(win_rate)s, %(backtest_date_range)s, %(is_enable)s)
        RETURNING id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, strategy_data)
            result = cur.fetchone()
            conn.commit()
            return result[0] if result else None
    except Exception as e:
        conn.rollback()
        logger.error(f"保存最优策略失败: {e}")
        return None


def get_active_strategy(conn) -> dict | None:
    """
    获取当前启用的最优策略。
    返回策略记录 dict 或 None。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_params, backtest_profit, max_drawdown,
                       win_rate, backtest_date_range, update_time, is_enable
                FROM best_strategy_config
                WHERE is_enable = TRUE
                ORDER BY update_time DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cur.description]
            strategy = dict(zip(columns, row))
            # JSONB 字段转 dict
            if isinstance(strategy.get("strategy_params"), str):
                strategy["strategy_params"] = __import__("json").loads(strategy["strategy_params"])
            # Decimal -> float
            for key in ("backtest_profit", "max_drawdown", "win_rate"):
                if strategy.get(key) is not None:
                    strategy[key] = float(strategy[key])
            # datetime -> str
            if strategy.get("update_time"):
                strategy["update_time"] = str(strategy["update_time"])
            return strategy
    except Exception as e:
        logger.error(f"获取活跃策略失败: {e}")
        return None


# ============================================================
# Web 操作台查询
# ============================================================

def get_latest_signals(conn, limit: int = 50) -> list:
    """获取最新 N 条信号，关联代币详情和行情，供 Web 前端实时展示"""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.signal_time, s.contract_address, s.token_symbol,
                       s.pool_value, s.holder_rate, s.signal_content,
                       s.market_cap, s.market_cap_prev, s.holders_count,
                       s.price_usd, s.price_usd_prev, s.token_age,
                       s.smart_wallets, s.avg_buy_amount, s.multiplier,
                       s.channel_id, s.group_name,
                       -- token_detail 扩展字段
                       d.token_name, d.token_logo, d.creator_address,
                       d.total_supply, d.launchpad, d.creation_time,
                       d.is_mint_abandoned, d.is_block_address, d.debot_trust,
                       d.twitter, d.website, d.tags,
                       -- 最新行情快照
                       m.top10_position, m.volume_5m, m.volume_1h, m.volume_24h,
                       m.percent_5m, m.percent_1h, m.percent_24h,
                       m.pair_address, m.dex_name, m.fdv,
                       -- 信号累计
                       a.signal_count, a.first_signal_time,
                       a.first_price, a.max_price, a.max_price_gain
                FROM debot_signal s
                LEFT JOIN debot_token_detail d ON d.contract_address = s.contract_address
                LEFT JOIN LATERAL (
                    SELECT top10_position, volume_5m, volume_1h, volume_24h,
                           percent_5m, percent_1h, percent_24h,
                           pair_address, dex_name, fdv
                    FROM debot_token_metric
                    WHERE contract_address = s.contract_address
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                ) m ON TRUE
                LEFT JOIN debot_signal_agg a ON a.contract_address = s.contract_address
                WHERE s.contract_address != ''
                ORDER BY s.signal_time DESC
                LIMIT %s
            """, (limit,))
            columns = [desc[0] for desc in cur.description]
            results = []
            for row in cur.fetchall():
                item = dict(zip(columns, row))
                # 数值类型转换
                for key in ("pool_value", "holder_rate", "market_cap", "market_cap_prev",
                           "price_usd", "price_usd_prev", "avg_buy_amount",
                           "total_supply", "fdv",
                           "volume_5m", "volume_1h", "volume_24h",
                           "percent_5m", "percent_1h", "percent_24h",
                           "first_price", "max_price", "max_price_gain",
                           "top10_position"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                # 时间转字符串
                for key in ("signal_time", "creation_time", "first_signal_time"):
                    if item.get(key):
                        item[key] = str(item[key])
                results.append(item)
            return results
    except Exception as e:
        logger.error(f"获取最新信号失败: {e}")
        return []


def get_token_kline(conn, contract_address: str, limit: int = 100) -> list:
    """获取指定代币的价格历史 (K线数据)"""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT price_usd, volume_h24_usd, liquidity_usd,
                       price_change_m5, price_change_h1,
                       txns_h24_buys, txns_h24_sells,
                       snapshot_time
                FROM token_market_snapshot
                WHERE contract_address = %s AND price_usd IS NOT NULL AND price_usd > 0
                ORDER BY snapshot_time ASC
                LIMIT %s
            """, (contract_address, limit))
            columns = [desc[0] for desc in cur.description]
            results = []
            for row in cur.fetchall():
                item = dict(zip(columns, row))
                for key in ("price_usd", "volume_h24_usd", "liquidity_usd",
                           "price_change_m5", "price_change_h1"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                if item.get("snapshot_time"):
                    item["snapshot_time"] = str(item["snapshot_time"])
                results.append(item)
            return results
    except Exception as e:
        logger.error(f"获取K线数据失败 ({contract_address[:12]}...): {e}")
        return []


def get_all_tracked_tokens(conn) -> list:
    """获取所有有行情快照的代币列表 (symbol + address)，供前端下拉选择"""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (tms.contract_address)
                    tms.contract_address, tms.symbol, tms.name,
                    tms.price_usd, tms.snapshot_time
                FROM token_market_snapshot tms
                WHERE tms.price_usd IS NOT NULL AND tms.price_usd > 0
                ORDER BY tms.contract_address, tms.snapshot_time DESC
                LIMIT 200
            """)
            columns = [desc[0] for desc in cur.description]
            results = []
            for row in cur.fetchall():
                item = dict(zip(columns, row))
                if item.get("price_usd") is not None:
                    item["price_usd"] = float(item["price_usd"])
                if item.get("snapshot_time"):
                    item["snapshot_time"] = str(item["snapshot_time"])
                results.append(item)
            return results
    except Exception as e:
        logger.error(f"获取代币列表失败: {e}")
        return []
