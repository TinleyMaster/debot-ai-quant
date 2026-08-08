# Debot AI Quant

基于 Debot.ai 信号的 Solana Memecoin 量化回测系统。自动采集 AI 交易信号，拉取 DexScreener 行情数据，仅用真实时间序列运行参数网格回测，生成对齐 Debot 跟单页面的策略配置报告。

## 架构

```
[n8n 定时调度] ──────────┐
                         │ (双重保险)
                         ▼
┌──────────────────────────────────────────────┐
│              collector 服务                    │
│                                              │
│  api_client.py ──────► Debot API (信号拉取)    │
│  scraper.py ─────────► Debot.ai (Playwright 回退)│
│  market_fetcher.py ──► DexScreener API (行情)  │
│  backtest_engine.py ─► 参数网格回测             │
│  analyze_win_rate.py ► 信号胜率统计            │
│  strategy_sync.py ───► 策略报告生成             │
│  main.py ────────────► HTTP API (:8080)        │
│       ▲                  │                    │
│       │  ┌──后台行情线程──┘ (独立于 n8n)        │
│       │                  │                    │
│  ┌────┴──────┐           │                    │
│  │ PostgreSQL │◄─────────┘                    │
│  └───────────┘                               │
└──────────────────────────────────────────────┘
     │
     ▼
┌──────────────┐
│  Web 操作台   │  (localhost:8080)
│  index.html  │  参数调整 / 信号监控 / K线 / 回测
└──────────────┘
```

## 项目结构

```
debot-ai-quant/
├── docker-compose.yml          # Docker 编排（PostgreSQL + collector）
├── .env.example                # 环境变量模板
├── collector/                  # 核心服务
│   ├── main.py                 # 主入口：HTTP API + 信号采集 + 后台行情拉取
│   ├── api_client.py           # Debot API 客户端（首选采集方式）
│   ├── scraper.py              # Playwright 信号采集器（API 不可用时回退）
│   ├── config.json             # 页面选择器配置
│   ├── market_fetcher.py       # DexScreener 行情拉取
│   ├── db.py                   # PostgreSQL CRUD（连接池 + 分表操作）
│   ├── backtest_engine.py      # 回测引擎（参数网格搜索）
│   ├── strategy_sync.py        # 策略报告生成器
│   ├── analyze_win_rate.py     # 信号胜率统计分析脚本
│   ├── index.html              # Web 回测操作台
│   ├── Dockerfile              # 容器构建文件
│   └── requirements.txt        # Python 依赖
├── n8n/                        # n8n 工作流定义
│   ├── market_fetch_workflow.json    # 定时行情拉取（每 15 分钟）
│   ├── backtest_workflow.json        # 每日回测 + 策略报告
│   ├── health_check_workflow.json    # 健康监控
│   └── strategy_sync_workflow.json   # 策略同步
└── sql/
    └── init.sql                # 数据库 DDL（11 张表）
```

## 核心工作流

### 1. 信号采集（API 优先 + Playwright 回退）

采集策略为双通道：

| 通道 | 方式 | 性能 | 适用场景 |
|------|------|------|----------|
| API | `api_client.py` 直接调用 Debot REST API | ~6s / 200+ 条 | **默认首选** |
| Playwright | `scraper.py` 无头浏览器渲染 | ~30s / 12 条 | API 连续失败 3 次后自动回退 |

API 客户端通过 `/api/community/signal/channel/list` 分页拉取全量信号，逐条标准化后写入数据库。返回数据包含代币基础信息、行情指标、Top10 持仓比例、安全检测、社交信息、钱包交易明细等完整字段。

采集间隔为**随机 60~180 秒**，模拟真人刷新节奏。

### 2. 数据分表存储

信号入库时自动拆分为 5 张表，避免 JSON 膨胀：

| 表名 | 用途 | 写入方式 |
|------|------|----------|
| `debot_signal` | 核心信号记录（信号时间、合约、价格、市值等） | INSERT（唯一约束去重） |
| `debot_token_detail` | 代币基础信息（名称、Logo、创建者、发行量、安全检测、社交链接） | UPSERT |
| `debot_token_metric` | 行情快照（价格、成交量、涨跌幅、持有人数、Top10 持仓） | INSERT（时序追加） |
| `debot_signal_agg` | 信号累计统计（首次信号时间/价格、最高涨幅倍数） | UPSERT |
| `debot_wallet_trade` | 聪明钱包交易明细（地址、成交额、时间） | INSERT |

### 3. 行情数据补全

`market_fetcher.py` 通过 DexScreener 免费 API 补全代币行情快照：

- 查询最近 15 分钟内无快照记录的合约地址
- 批量调用 `https://api.dexscreener.com/latest/dex/tokens/`（每次最多 30 个地址）
- 同一合约多交易对去重，只保留流动性最高的
- 入库字段：价格、24h 成交量、流动性、FDV、各时段涨跌幅（5m/1h/6h/24h）、买卖笔数、创建时间
- 限流处理：请求间隔 1.5s，429 自动指数退避重试

**双重保险机制**：

| 层级 | 触发方 | 间隔 | 失败处理 |
|------|--------|------|----------|
| n8n 定时器 | n8n workflow | 每 15 分钟 | HTTP 端点内 3 次重试 |
| 后台线程 | collector 内部 | 每 10~20 分钟随机 | 异常捕获后下轮自动重试 |
| 数据库自愈 | SQL 查询 | 按需 | `since_hours=0.25` 自动补漏 |

### 4. 参数网格回测

`backtest_engine.py` 模拟 Debot 跟单实盘交易逻辑：

**参数网格**（与 Debot 跟单页面字段 1:1 对齐）：

| 参数 | 网格值 |
|------|--------|
| 止盈阈值 | 60% / 100% / 200% |
| 止损阈值 | 3% / 5% / 10% |
| 最大持仓 | 1h / 4h / 24h |
| 单币买入次数 | 1 / 3 |
| 信号确认数 | 1 / 3 个 |
| 信号确认窗口 | 1 / 5 分钟 |
| 最低市值 | $0 / $10,000 |
| 最高市值 | $0 / $500,000 |
| 最低持有人 | 0 / 100 |
| 最高持有人 | 0 / 5000 |
| Top10 持仓上限 | 不限 / 10% |
| 代币最小年龄 | 0 / 10 分钟 |
| 代币最大年龄 | 不限 / 24h |
| 交易时段 | 全天 / 08:00-18:00 |

共 **576 组**参数组合。

**交易成本模型**（模拟真实 Solana 链上成本）：

| 成本项 | 数值（默认） |
|--------|------|
| 单笔买入额 | 1.0 SOL（≈$150） |
| 买入滑点 | 30% |
| 卖出滑点 | 30% |
| DEX 手续费 | 0.25%（买卖各一次） |
| 优先费 | 0.001 SOL |
| 贿赂费 | 0.003 SOL（可选） |

**回测逻辑**：

1. 加载信号和**仅真实**行情快照（不做任何合成外推）
2. 入场延迟：信号发出后至少 60 秒的第一笔快照作为买入价
3. 逐笔信号应用过滤条件（市值、持有人数、Top10 持仓、时段、信号确认）
4. 遍历后续快照判断止盈/止损/超时退出
5. 计算扣除全部摩擦成本后的净盈亏
6. 收益率 = 累积盈亏 / 总投入资金；回撤 = 峰值—谷底金额 / 总投入资金

### 5. Web 操作台

`index.html` 提供交互式界面（`http://localhost:8080`），4 个 Tab：

| Tab | 功能 |
|-----|------|
| **策略排名** | 统计卡片 + 网格排名表（19 列），支持当前参数回测 / 全部迭代回测 |
| **交易明细** | 回测产生的逐笔交易记录 |
| **最新信号** | 50 条实时信号（30 秒自动刷新），点击行弹出完整详情弹窗（8 个分类 section），点击代币符号跳转 K 线 |
| **K线行情** | 代币价格走势图，标注信号时间告警线（橙色竖虚线）和回测买卖点 |

- 左侧参数面板可折叠，支持 4 组快速预设（保守/均衡/激进/短打）
- 顶部实时参数栏动态反映当前设置值

### 6. 胜率分析

`analyze_win_rate.py` 基于 `max_price_gain` 字段计算信号胜率：

- **方案 A**：不同阈值下胜率统计（任意盈利 / 10% / 50% / 翻倍 / 5 倍 / 10 倍）
- **方案 C**：收益分档分布（亏损 / 持平 / 小涨 / 中涨 / 翻倍 / 大涨 / 暴涨）
- 按代币去重、聪明钱包数量分组交叉分析

## HTTP API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（未处理信号数、阻断状态、采集方式） |
| GET | `/version` | 当前部署版本（Git commit hash） |
| GET | `/fetch-market` | 触发行情数据拉取（3 次重试） |
| GET | `/run-backtest` | 触发全部参数网格回测 |
| POST | `/backtest-custom` | 自定义参数单次回测（JSON body） |
| GET | `/backtest-report` | 获取最新回测报告（含交易明细） |
| GET | `/latest-signals` | 获取最新 50 条信号（含分表 JOIN 数据） |
| GET | `/token-kline` | 获取指定代币的 K 线快照（用于 K 线图表） |
| GET | `/latest-trades` | 获取聪明钱包最近的交易记录 |
| GET | `/sync-strategy` | 触发策略同步报告 |
| GET | `/` `/index.html` | Web 回测操作台 |

服务端使用 `ThreadingHTTPServer`（多线程），回测运行期间不影响其他请求。

## 数据库表

| 表名 | 用途 |
|------|------|
| `debot_signal` | 信号数据（联合唯一约束：contract_address + signal_time） |
| `debot_token_detail` | 代币详情（名称、Logo、创建者、安全检测、社交链接） |
| `debot_token_metric` | 行情快照（价格、成交量、涨跌幅、持有人、Top10 持仓） |
| `debot_signal_agg` | 信号累计统计（首次时间/价格、最高涨幅） |
| `debot_wallet_trade` | 聪明钱包交易明细 |
| `token_market_snapshot` | DexScreener 行情快照（联合唯一约束：contract_address + snapshot_time） |
| `best_strategy_config` | 最优策略配置（JSONB 存储参数 + 回测指标） |
| `collector_run_log` | 采集运行日志 |
| `collector_alerts` | 采集异常告警 |

## 部署

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入数据库密码等信息

# 2. 启动服务
docker-compose up -d

# 3. 访问 Web 操作台
open http://localhost:8080
```

## 技术栈

- **运行时**: Python 3.13 + Playwright 1.49（无头 Chromium）
- **数据库**: PostgreSQL 17
- **调度**: n8n 工作流引擎 + collector 内置后台线程
- **行情源**: DexScreener 免费 API
- **信号源**: Debot.ai（API 优先 + Playwright 回退）
- **前端**: 原生 HTML/JS + Chart.js 4.4（K 线图表）
- **部署**: Docker Compose（本地 + Zeabur）

## 已知问题 & 待改进

### 严重

1. **止损滑价**：15 分钟快照间隔下，两次快照之间价格可能已断崖下跌，止损实际滑价远超设定值。需要更高的快照采集频率来改善。

2. **幸存者偏差**：DexScreener API 对已退市或流动性枯竭的代币返回空数据，回测只看得到"活下来"的代币。这是 API 数据源本身的局限。

### 中等

3. **SOL 价格硬编码**：`sol_price_usd = 150.0` 固定不变，实际 SOL 价格波动影响成本模型的美元价值。建议改为实时获取。

4. **无数据库清理策略**：`token_market_snapshot` 和 `debot_token_metric` 表随时间线性增长。建议增加定期清理任务。

5. **回测结果无持久化**：完整回测结果和交易明细仅保存在内存中，重新部署后丢失。

### 轻微

6. **无 DexScreener 速率监控**：如果追踪代币数增长到数百个，可能触发 API 限流。

7. **grid 耗时文案硬编码**：前端"576 组参数"为手动维护，未从后端动态获取。
