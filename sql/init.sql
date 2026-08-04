-- ============================================
-- Debot AI Quant MVP 数据库初始化脚本
-- ============================================

-- 创建数据库（如果不存在，需要 superuser 权限手动执行）
-- CREATE DATABASE debot_quant;

-- ============================================
-- 表1: debot_signal 核心信号表
-- ============================================
CREATE TABLE IF NOT EXISTS debot_signal (
    id              BIGSERIAL PRIMARY KEY,
    signal_time     TIMESTAMPTZ NOT NULL,
    contract_address VARCHAR(128) NOT NULL,
    token_symbol    VARCHAR(128),
    pool_value      NUMERIC(24, 6),
    holder_rate     NUMERIC(6, 4),
    signal_content  TEXT,
    source_url      TEXT,
    -- 回测用字段 (来自 Debot 详情卡片)
    market_cap      NUMERIC(24, 6),
    market_cap_prev NUMERIC(24, 6),
    holders_count   INT,
    price_usd       NUMERIC(24, 12),
    price_usd_prev  NUMERIC(24, 12),
    token_age       VARCHAR(32),
    smart_wallets   INT,
    avg_buy_amount  NUMERIC(24, 6),
    multiplier      VARCHAR(16),
    create_time     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_processed    BOOLEAN NOT NULL DEFAULT FALSE,

    -- 联合唯一约束：同一合约+同一信号时间视为重复
    CONSTRAINT uq_signal UNIQUE (contract_address, signal_time)
);

-- 已有数据库的增量迁移：添加回测字段列 (IF NOT EXISTS 模式)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='market_cap') THEN
        ALTER TABLE debot_signal ADD COLUMN market_cap NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='market_cap_prev') THEN
        ALTER TABLE debot_signal ADD COLUMN market_cap_prev NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='holders_count') THEN
        ALTER TABLE debot_signal ADD COLUMN holders_count INT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='price_usd') THEN
        ALTER TABLE debot_signal ADD COLUMN price_usd NUMERIC(24, 12);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='price_usd_prev') THEN
        ALTER TABLE debot_signal ADD COLUMN price_usd_prev NUMERIC(24, 12);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='token_age') THEN
        ALTER TABLE debot_signal ADD COLUMN token_age VARCHAR(32);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='smart_wallets') THEN
        ALTER TABLE debot_signal ADD COLUMN smart_wallets INT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='avg_buy_amount') THEN
        ALTER TABLE debot_signal ADD COLUMN avg_buy_amount NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='multiplier') THEN
        ALTER TABLE debot_signal ADD COLUMN multiplier VARCHAR(16);
    END IF;
-- 已有数据库的增量迁移：第二波新增字段 (来自 API meta 全量数据)
DO $$
BEGIN
    -- 代币基础信息
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='token_name') THEN
        ALTER TABLE debot_signal ADD COLUMN token_name VARCHAR(128);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='token_logo') THEN
        ALTER TABLE debot_signal ADD COLUMN token_logo VARCHAR(512);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='creator_address') THEN
        ALTER TABLE debot_signal ADD COLUMN creator_address VARCHAR(64);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='total_supply') THEN
        ALTER TABLE debot_signal ADD COLUMN total_supply NUMERIC(36);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='launchpad') THEN
        ALTER TABLE debot_signal ADD COLUMN launchpad VARCHAR(32);
    END IF;
    -- 行情补充
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='fdv') THEN
        ALTER TABLE debot_signal ADD COLUMN fdv NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='volume_5m') THEN
        ALTER TABLE debot_signal ADD COLUMN volume_5m NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='volume_1h') THEN
        ALTER TABLE debot_signal ADD COLUMN volume_1h NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='volume_24h') THEN
        ALTER TABLE debot_signal ADD COLUMN volume_24h NUMERIC(24, 6);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='percent_5m') THEN
        ALTER TABLE debot_signal ADD COLUMN percent_5m NUMERIC(12, 8);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='percent_1h') THEN
        ALTER TABLE debot_signal ADD COLUMN percent_1h NUMERIC(12, 8);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='percent_24h') THEN
        ALTER TABLE debot_signal ADD COLUMN percent_24h NUMERIC(12, 8);
    END IF;
    -- DEX 信息
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='pair_address') THEN
        ALTER TABLE debot_signal ADD COLUMN pair_address VARCHAR(64);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='dex_name') THEN
        ALTER TABLE debot_signal ADD COLUMN dex_name VARCHAR(32);
    END IF;
    -- 信号统计
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='signal_count') THEN
        ALTER TABLE debot_signal ADD COLUMN signal_count INT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='first_price') THEN
        ALTER TABLE debot_signal ADD COLUMN first_price NUMERIC(24, 12);
    END IF;
    -- 安全检测
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='is_mint_abandoned') THEN
        ALTER TABLE debot_signal ADD COLUMN is_mint_abandoned BOOLEAN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='is_block_address') THEN
        ALTER TABLE debot_signal ADD COLUMN is_block_address BOOLEAN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='debot_trust') THEN
        ALTER TABLE debot_signal ADD COLUMN debot_trust BOOLEAN;
    END IF;
    -- 社交
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='twitter') THEN
        ALTER TABLE debot_signal ADD COLUMN twitter VARCHAR(512);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='website') THEN
        ALTER TABLE debot_signal ADD COLUMN website VARCHAR(512);
    END IF;
    -- 标签（逗号分隔）
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='tags') THEN
        ALTER TABLE debot_signal ADD COLUMN tags VARCHAR(256);
    END IF;
    -- 钱包明细（JSONB，便于查询）
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='debot_signal' AND column_name='wallet_stats') THEN
        ALTER TABLE debot_signal ADD COLUMN wallet_stats JSONB;
    END IF;
END $$;

-- 索引
CREATE INDEX IF NOT EXISTS idx_signal_time ON debot_signal(signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_signal_contract ON debot_signal(contract_address);
CREATE INDEX IF NOT EXISTS idx_signal_processed ON debot_signal(is_processed) WHERE is_processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_signal_create_time ON debot_signal(create_time DESC);


-- ============================================
-- 表1.1: debot_token_detail 代币详情表（来自 Debot API meta）
-- ============================================
CREATE TABLE IF NOT EXISTS debot_token_detail (
    contract_address  VARCHAR(128) PRIMARY KEY,
    token_symbol      VARCHAR(128),
    token_name        VARCHAR(128),
    token_logo        VARCHAR(512),
    creator_address   VARCHAR(64),
    total_supply      NUMERIC(36),
    launchpad         VARCHAR(32),
    creation_time     TIMESTAMPTZ,
    -- 安全信息
    is_mint_abandoned BOOLEAN,
    is_block_address  BOOLEAN,
    debot_trust       BOOLEAN,
    -- 社交
    twitter           VARCHAR(512),
    website           VARCHAR(512),
    tags              VARCHAR(256),
    update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_detail_symbol ON debot_token_detail(token_symbol);
CREATE INDEX IF NOT EXISTS idx_detail_launchpad ON debot_token_detail(launchpad);


-- ============================================
-- 表1.2: debot_token_metric 代币行情快照（每次采集时写入）
-- ============================================
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
);

CREATE INDEX IF NOT EXISTS idx_metric_contract ON debot_token_metric(contract_address);
CREATE INDEX IF NOT EXISTS idx_metric_snapshot ON debot_token_metric(snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_metric_dex ON debot_token_metric(dex_name);


-- ============================================
-- 表1.3: debot_signal_agg 代币信号累计统计
-- ============================================
CREATE TABLE IF NOT EXISTS debot_signal_agg (
    contract_address  VARCHAR(128) PRIMARY KEY,
    signal_count      INT,
    first_signal_time TIMESTAMPTZ,
    first_price       NUMERIC(24, 12),
    max_price         NUMERIC(24, 12),
    max_price_gain    NUMERIC(12, 6),
    update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================
-- 表1.4: debot_wallet_trade 聪明钱包交易明细
-- ============================================
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
);

CREATE INDEX IF NOT EXISTS idx_wallet_signal ON debot_wallet_trade(signal_id);
CREATE INDEX IF NOT EXISTS idx_wallet_contract ON debot_wallet_trade(contract_address);
CREATE INDEX IF NOT EXISTS idx_wallet_alias ON debot_wallet_trade(wallet_alias);


-- ============================================
-- 表2: token_base_info 代币风控信息表
-- ============================================
CREATE TABLE IF NOT EXISTS token_base_info (
    id                BIGSERIAL PRIMARY KEY,
    contract_address  VARCHAR(128) NOT NULL UNIQUE,
    pool_lock_status  VARCHAR(32),
    buy_tax           NUMERIC(6, 4),
    sell_tax          NUMERIC(6, 4),
    contract_audit    VARCHAR(64),
    risk_flags        TEXT,
    risk_level        VARCHAR(16) CHECK (risk_level IN ('低', '中', '高')),
    update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_contract ON token_base_info(contract_address);
CREATE INDEX IF NOT EXISTS idx_token_risk ON token_base_info(risk_level);


-- ============================================
-- 表3: token_kline_data 行情K线数据表（预留，MVP暂不写入）
-- ============================================
CREATE TABLE IF NOT EXISTS token_kline_data (
    id                BIGSERIAL PRIMARY KEY,
    contract_address  VARCHAR(128) NOT NULL,
    time_frame        VARCHAR(8) NOT NULL CHECK (time_frame IN ('5min', '15min', '30min', '1h', '2h', '4h')),
    timestamp         TIMESTAMPTZ NOT NULL,
    open              NUMERIC(24, 12),
    high              NUMERIC(24, 12),
    low               NUMERIC(24, 12),
    price             NUMERIC(24, 12),
    volume            NUMERIC(24, 6),
    signal_ref_id     BIGINT REFERENCES debot_signal(id) ON DELETE SET NULL,
    create_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_kline UNIQUE (contract_address, time_frame, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_kline_contract_time ON token_kline_data(contract_address, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_kline_timeframe ON token_kline_data(contract_address, time_frame, timestamp DESC);


-- ============================================
-- 表3.5: token_market_snapshot 行情快照表（DexScreener 数据）
-- ============================================
CREATE TABLE IF NOT EXISTS token_market_snapshot (
    id                BIGSERIAL PRIMARY KEY,
    contract_address  VARCHAR(128) NOT NULL,
    symbol            VARCHAR(64),
    name              VARCHAR(128),
    price_usd         NUMERIC(24, 12),
    volume_h24_usd    NUMERIC(24, 6),
    liquidity_usd     NUMERIC(24, 6),
    fdv_usd           NUMERIC(24, 6),
    price_change_m5   NUMERIC(8, 4),
    price_change_h1   NUMERIC(8, 4),
    price_change_h6   NUMERIC(8, 4),
    price_change_h24  NUMERIC(8, 4),
    txns_h24_buys     INT,
    txns_h24_sells    INT,
    pair_created_at   TIMESTAMPTZ,
    snapshot_time     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_market_snapshot UNIQUE (contract_address, snapshot_time)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_contract ON token_market_snapshot(contract_address, snapshot_time DESC);


-- ============================================
-- 表4: best_strategy_config 最优策略配置表（预留，MVP暂不写入）
-- ============================================
CREATE TABLE IF NOT EXISTS best_strategy_config (
    id                BIGSERIAL PRIMARY KEY,
    strategy_params   JSONB NOT NULL,
    backtest_profit   NUMERIC(16, 6),
    max_drawdown      NUMERIC(8, 4),
    win_rate          NUMERIC(6, 4),
    backtest_date_range VARCHAR(64),
    update_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_enable         BOOLEAN NOT NULL DEFAULT TRUE
);


-- ============================================
-- 表5: collector_run_log 采集运行日志表（用于健康监控）
-- ============================================
CREATE TABLE IF NOT EXISTS collector_run_log (
    id              BIGSERIAL PRIMARY KEY,
    run_time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signals_scraped INT NOT NULL DEFAULT 0,
    signals_new     INT NOT NULL DEFAULT 0,
    errors_count    INT NOT NULL DEFAULT 0,
    error_detail    TEXT,
    alert_type      VARCHAR(32),
    duration_ms     INT
);

CREATE INDEX IF NOT EXISTS idx_runlog_time ON collector_run_log(run_time DESC);


-- ============================================
-- 表6: collector_alerts 采集异常告警记录表
-- ============================================
CREATE TABLE IF NOT EXISTS collector_alerts (
    id              BIGSERIAL PRIMARY KEY,
    alert_time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_type      VARCHAR(32) NOT NULL,
    alert_message   TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_time   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alerts_time ON collector_alerts(alert_time DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON collector_alerts(alert_type, resolved);
