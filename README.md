# Debot AI Quant

基于 Debot.ai 信号的 Solana Memecoin 量化回测系统。自动采集 AI 交易信号，拉取 DexScreener 行情数据，仅用真实时间序列运行参数网格回测，生成对齐 Debot 跟单页面的策略配置报告。

## 架构

```
[n8n 定时调度] ──────────┐
                         │ (双重保险)
                         ▼
┌──────────────────────────────────────────────┐
│            collector 服务 (Playwright)         │
│                                              │
│  scraper.py ────────► Debot.ai (信号抓取)      │
│  market_fetcher.py ─► DexScreener API (行情)   │
│  backtest_engine.py ► 参数网格回测             │
│  strategy_sync.py ──► 策略报告生成             │
│  main.py ───────────► HTTP API (:8080)        │
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
│  index.html  │  参数调整 / 回测 / 策略排名
└──────────────┘
```

## 项目结构

```
debot-ai-quant/
├── docker-compose.yml          # Docker 编排（PostgreSQL + collector）
├── .env.example                # 环境变量模板
├── collector/                  # 核心服务
│   ├── main.py                 # 主入口：HTTP API + 信号采集 + 后台行情拉取
│   ├── scraper.py              # Playwright 信号采集器
│   ├── config.json             # 页面选择器配置
│   ├── market_fetcher.py       # DexScreener 行情拉取
│   ├── db.py                   # PostgreSQL CRUD（连接池）
│   ├── backtest_engine.py      # 回测引擎（参数网格搜索）
│   ├── strategy_sync.py        # 策略报告生成器
│   ├── index.html              # Web 回测操作台
│   ├── Dockerfile              # 容器构建文件
│   └── requirements.txt        # Python 依赖
├── n8n/                        # n8n 工作流定义
│   ├── market_fetch_workflow.json    # 定时行情拉取（每 15 分钟）
│   ├── backtest_workflow.json        # 每日回测 + 策略报告
│   ├── health_check_workflow.json    # 健康监控
│   └── strategy_sync_workflow.json   # 策略同步
└── sql/
    └── init.sql                # 数据库 DDL
```

## 核心工作流

### 1. 信号采集

`scraper.py` 通过 Playwright 无头浏览器定时访问 Debot.ai，解析信号列表页面：

- 加载已缓存的登录 Cookie 恢复会话
- 等待 SPA (React) 异步渲染完成
- 提取每笔信号的合约地址、代币符号、信号时间、流动池、大户占比、信号文案
- 去重写入 `debot_signal` 表（联合唯一约束：`contract_address + signal_time`）
- 检测 Cloudflare 人机验证、Cookie 过期等阻断场景并告警

采集间隔为 **随机 60~180 秒**，模拟真人刷新节奏，规避 Cloudflare 时序指纹识别。

### 2. 行情数据补全

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

即使 n8n 宕机，后台线程照样跑；即使两者同时失败一轮，下一轮数据库查询自动补上缺口。

### 3. 参数网格回测

`backtest_engine.py` 模拟 Debot 跟单实盘交易逻辑：

**参数网格**（与 Debot 跟单页面字段 1:1 对齐）：

| 参数 | 网格值 |
|------|--------|
| 止盈阈值 | 20% / 50% / 100% |
| 止损阈值 | 3% / 5% / 10% |
| 最大持仓 | 0.5h / 1h / 4h / 24h |
| 单币买入次数 | 1 / 3 |
| 最低流动性 | $0 / $10,000 |
| 最低持有人比例 | 0% / 5% |
| 代币最大创建时间 | 不限 / 24h |
| 信号确认数 | 1 / 2 个 |

共 **1152 组** 参数组合。

**交易成本模型**（模拟真实 Solana 链上成本）：

| 成本项 | 数值 |
|--------|------|
| 单笔买入额 | 0.1 SOL（≈$15） |
| 买入滑点 | 20%（土狗币流动性冲击） |
| 卖出滑点 | 20% |
| DEX 手续费 | 0.25%（买卖各一次） |
| 优先费 | 0.001 SOL |
| 基础 Gas | 0.000005 SOL |

单笔交易固定成本 ≈ (0.001 + 0.000005) × 2 × $150 ≈ $0.30

**回测逻辑**：

1. 加载信号和**仅真实**行情快照（不做任何合成外推）
2. 入场延迟：信号发出后至少 60 秒的第一笔快照作为买入价（模拟看信号→决策→链上执行的延迟）
3. 遍历每笔信号，应用过滤条件（流动性、代币年龄、持有人比例、信号确认）
4. 遍历后续快照判断止盈/止损/超时退出
5. 计算扣除全部摩擦成本后的净盈亏
6. 汇总每组的胜率、盈亏比、最大回撤等统计指标

### 4. 策略报告生成

`strategy_sync.py` 从回测结果中筛选最优策略，生成对齐 Debot 跟单页面字段的中文配置报告，供人工照填配置。

### 5. Web 操作台

`index.html` 提供交互式回测界面（`http://localhost:8080`）：

- 左侧：8 个参数滑块 + 4 组快速预设（保守/均衡/激进/超短线）
- 右侧：统计卡片 + 策略排名表 + 交易明细表
- 三种回测模式：当前参数回测 / 全部迭代回测 / 加载活跃策略报告
- 数据质量不足时显示警告横幅

## HTTP API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（未处理信号数、阻断状态、最新告警） |
| GET | `/fetch-market` | 触发行情数据拉取（3 次重试） |
| GET | `/run-backtest` | 触发全部参数网格回测 |
| POST | `/backtest-custom` | 自定义参数单次回测（JSON body） |
| GET | `/sync-strategy` | 触发策略同步报告 |
| GET | `/backtest-report` | 获取最新回测报告（含交易明细） |
| GET | `/` `/index.html` | Web 回测操作台 |

## 数据库表

| 表名 | 用途 |
|------|------|
| `debot_signal` | 信号数据（联合唯一约束：contract_address + signal_time） |
| `token_base_info` | 代币风控信息（池锁、税率、审计、风险等级） |
| `token_market_snapshot` | 行情快照（联合唯一约束：contract_address + snapshot_time） |
| `token_kline_data` | K 线数据表（预留，暂未写入） |
| `best_strategy_config` | 最优策略配置（JSONB 存储参数 + 回测指标） |
| `collector_run_log` | 采集运行日志 |
| `collector_alerts` | 采集异常告警 |

## 部署

### 本地开发

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入数据库密码等信息

# 2. 启动服务
docker-compose up -d

# 3. 访问 Web 操作台
open http://localhost:8080
```

## 已实现功能

- [x] Debot.ai 信号自动采集（Playwright 无头浏览器 + Cookie 会话管理）
- [x] 随机采集间隔（60~180s），规避 Cloudflare 时序指纹
- [x] Cloudflare 挑战检测与 Cookie 过期告警
- [x] DexScreener 行情数据拉取（批量 + 限流 + 去重）
- [x] 三重行情数据保障（n8n + 后台线程 + 数据库自愈）
- [x] 参数网格回测引擎（1152 组组合，对齐 Debot 跟单页面）
- [x] 仅使用真实快照数据回测，无合成外推
- [x] 入场延迟 60s 模拟信号→执行的实盘延迟
- [x] 真实交易摩擦成本模型（20% 滑点、DEX 手续费、优先费、Gas）
- [x] 信号确认机制（时间窗口内多信号确认）
- [x] 代币风控信息采集（池锁、税率、审计）
- [x] Web 操作台（参数调整、回测、策略排名、交易明细）
- [x] 策略报告生成（中文，对齐 Debot 跟单配置页面）
- [x] n8n 工作流自动化（15 分钟行情拉取、每日回测、健康监控）
- [x] 数据库去重索引（按 contract_address + 维度字段联合唯一）
- [x] 健康检查接口（阻断检测、信号堆积监控）
- [x] Docker 容器化部署（PostgreSQL + Playwright 双容器编排）

## 已知问题 & 待改进

### 严重 ⚠️

1. **20% 滑点过于激进**：当前双向 20% 滑点意味着原始价格上涨约 54% 才能盈亏平衡。而参数网格中止盈阈值最高仅 100%，50% 以下的止盈设置将几乎全部亏损。建议方案：
   - 降低滑点至 10%（盈亏平衡点降至 ~25% 涨幅，与 20% 止盈档位匹配）
   - 或保留 20% 滑点但将止盈网格上调至 [0.6, 1.0, 2.0]
   - 入场 60s 延迟已部分覆盖了信号→执行的价格滑移，无需双重叠加

### 中等

2. **硬编码 SOL 价格**：`SOL_PRICE_USD = 150.0` 固定不变，实际 SOL 价格波动会直接影响成本模型中固定费用的美元价值。建议改为从 DexScreener 或 CoinGecko 实时获取。

3. **`poll_interval` 死代码**：`main.py` 中从环境变量读取并校验 `poll_interval`，但实际循环中使用的是 `random.randint(60, 180)` 硬编码范围。建议让随机范围基于 `poll_interval` 浮动（如 `poll_interval ± 50%`），或删除无用变量。

4. **策略过滤门槛过高**：`filter_best_strategies` 要求至少 5 笔交易。数据积累初期大部分参数组合交易数不足 5 笔，导致策略排名为空。建议初期放宽至 3 笔，或在前端显示提示。

### 轻微

5. **无数据库清理策略**：`token_market_snapshot` 表随时间线性增长（每代币每天 96 条 × N 个代币）。建议增加保留策略（如只保留最近 7 天数据）或定期清理任务。

6. **无 DexScreener 速率监控**：当前无请求计数器或速率限制告警。如果追踪代币数增长到数百个，可能触发 API 限流。

7. **代币详情页未启用风控采集**：`scrape_token_info` 函数已实现但未在 `run_once` 中调用，代币风控（池锁、税率、审计）数据为空。如需使用建议以更低频率（如每 10 个新信号才抓 1 次）逐步采集。

8. **退出时后台线程可能中断中**：信号处理和 DB 连接池关闭先于 daemon 行情线程退出，极端情况下后台线程可能正在写库时被强制终止。影响极小（daemon 线程自动回收），但建议在 finally 中显式设置 `_shutdown_flag = True` 并短暂等待。

9. **回测结果无持久化**：`run_backtest()` 的结果仅在内存中，重新部署后丢失。当前依赖 `best_strategy_config` 表保存最优策略元数据，但完整回测结果和交易明细不可回溯。

10. **止盈止损为离散时间点判断**：15 分钟快照间隔意味着可能在两个快照之间就已触发止盈/止损，回测会低估止盈触发、高估止损触发。积累更密集的数据后会改善。

11. **幸存者偏差**：DexScreener API 对已退市或流动性枯竭的代币返回空数据，回测只看得到"活下来"的代币。这是 API 数据源本身的局限，无法在代码层修复。

## 技术栈

- **运行时**: Python 3.13 + Playwright 1.49（无头 Chromium）
- **数据库**: PostgreSQL 17
- **调度**: n8n 工作流引擎 + collector 内置后台线程
- **行情源**: DexScreener 免费 API
- **信号源**: Debot.ai（Playwright 采集）
- **部署**: Docker Compose（本地）
