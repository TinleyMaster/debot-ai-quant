"""
回测运算引擎
- 读取历史信号 + 行情快照，模拟 Debot 跟单实盘交易
- 参数网格搜索最优跟单配置
- 参数完全对齐 Debot「创建跟单」页面字段:
  基础设置 / 买入策略 / 卖出策略 / 高级过滤 / 风险控制
"""
import os
import json
import time
import logging
from itertools import product
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict

from dotenv import load_dotenv

load_dotenv()

from db import get_conn, get_backtest_data, save_best_strategy, get_active_strategy

logger = logging.getLogger("backtest")


# ============================================================
# 回测参数定义 — 完全对齐 Debot「创建跟单」页面
# ============================================================

@dataclass
class BacktestParams:
    """单组回测参数，字段名一一对应 Debot 跟单配置"""

    # -- 基础设置 --
    buy_amount_sol: float = 1.0         # 买入金额(SOL) — Debot: 买入金额

    # -- 买入策略 --
    max_buys_per_token: int = 1         # 单币最大买入次数(0=不限) — Debot: 买入次数

    # -- 卖出策略 --
    take_profit: float = 1.0            # 止盈阈值(价格涨幅倍数) — Debot: 止盈止损(翻倍出本)
    stop_loss: float = 0.10             # 止损阈值(价格跌幅) — Debot: 止盈止损
    max_hold_hours: int = 8             # 最长持仓小时(超时自动卖出)

    # -- 高级过滤: 信号确认 --
    signal_confirm_minutes: int = 1     # 时间窗口(分钟) — Debot: 几分钟几个信号
    signal_confirm_count: int = 3       # 窗口内信号数 — Debot: 几分钟几个信号
    only_first_signal: bool = False     # 只跟首次信号 — Debot: 只跟首次信号

    # -- 高级过滤: 代币筛选 --
    min_token_age_minutes: int = 0      # 代币最小创建时间(0=不限) — Debot: 代币创建时间 Min
    max_token_age_hours: int = 0        # 代币最大创建时间(0=不限) — Debot: 代币创建时间 Max
    min_market_cap_usd: float = 0       # 最低市值 — Debot: 币种市值 Min
    max_market_cap_usd: float = 0       # 最高市值(0=不限) — Debot: 币种市值 Max
    min_holders: int = 0                # 最低持有人数 — Debot: 持有人 Min
    max_holders: int = 0                # 最高持有人数(0=不限) — Debot: 持有人 Max
    max_holder_rate: float = 0          # 最高TOP持仓比例(0=不限) — Debot: TOP持仓小于

    # -- 高级过滤: 时间 --
    runtime_start_hour: int = 0         # 运行开始(小时) — Debot: 运行时间段 开始
    runtime_end_hour: int = 0           # 运行结束(小时,0=24点) — Debot: 运行时间段 结束

    # -- 风险控制 --
    slippage_pct: float = 0.30          # 滑点 — Debot: 滑点
    priority_fee_sol: float = 0.001     # 优先费(SOL) — Debot: 优先费
    bribe_fee_sol: float = 0.003        # 贿赂费(SOL) — Debot: 贿赂费
    price_deviation_pct: float = 0.0    # 价格偏差 — Debot: 价格偏差

    # -- 固定参考价 (用于展示，非网格搜索维度) --
    sol_price_usd: float = 150.0        # SOL 参考价

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        return (f"tp{self.take_profit}_sl{self.stop_loss}"
                f"_h{self.max_hold_hours}_b{self.max_buys_per_token}"
                f"_sc{self.signal_confirm_count}")


# 参数网格定义
import random

PARAM_GRID = {
    # -- 卖出策略 --
    "take_profit": [0.6, 1.0, 2.0],
    "stop_loss": [0.03, 0.05, 0.10],
    "max_hold_hours": [1, 4, 24],

    # -- 买入策略 --
    "max_buys_per_token": [1, 3],

    # -- 信号确认 --
    "signal_confirm_count": [1, 3],
    "signal_confirm_minutes": [1, 5],

    # -- 代币筛选 --
    "min_token_age_minutes": [0, 10],
    "max_token_age_hours": [0, 24],
    "min_market_cap_usd": [0, 10000],
    "max_market_cap_usd": [0, 500000],
    "min_holders": [0, 100],
    "max_holders": [0, 5000],
    "max_holder_rate": [0, 0.10],

    # -- 时间 --
    "runtime_start_hour": [0],
    "runtime_end_hour": [0, 6],

    # -- 风险控制 (大多数策略共享默认值，不在网格中展开) --
    "slippage_pct": [0.20, 0.25, 0.30],
    "priority_fee_sol": [0.001],
    "bribe_fee_sol": [0.003],
    "price_deviation_pct": [0.0],
}

MAX_GRID_COMBOS = int(os.environ.get("MAX_GRID_COMBOS", "3000"))  # 网格组合上限，超限随机采样

# ---- 交易成本常量 ----

DEX_FEE = 0.0025                 # 0.25% DEX 交易手续费，买卖各收一次
BASE_GAS_SOL = 0.000005          # 基础 Gas ~0.000005 SOL

INITIAL_PORTFOLIO_USD = 10000    # 回测初始资金(用于计算回撤比例)


# ============================================================
# 回测数据结构
# ============================================================

@dataclass
class TradeResult:
    """单笔交易结果"""
    token_symbol: str
    contract_address: str
    entry_price: float
    exit_price: float
    profit_pct: float          # 价格涨跌幅
    profit_usd: float           # 实际盈亏 (USD)
    hold_hours: float           # 持仓时长
    exit_reason: str            # "take_profit" | "stop_loss" | "timeout"
    signal_time: str
    entry_time: str
    exit_time: str


@dataclass
class StrategyResult:
    """一组参数的完整回测结果"""
    params: BacktestParams
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_profit_usd: float = 0.0     # 累计盈亏 (USD)
    total_profit_pct: float = 0.0     # 组合收益率
    avg_profit_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    trades: list = field(default_factory=list)

    def to_summary_dict(self) -> dict:
        return {
            "params": self.params.to_dict(),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "total_profit_usd": round(self.total_profit_usd, 2),
            "total_profit_pct": round(self.total_profit_pct, 4),
            "avg_profit_pct": round(self.avg_profit_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "max_consecutive_losses": self.max_consecutive_losses,
        }


# ============================================================
# 回测引擎
# ============================================================

class BacktestEngine:
    """回测运算引擎 — 对齐 Debot 跟单逻辑"""

    def __init__(self):
        self.signals = []
        self.snapshots = {}  # {contract_address: [snapshot, ...]}
        self.data_quality = {}  # 数据质量诊断

    def load_data(self):
        """从数据库加载信号和行情数据（仅使用真实快照，不做任何合成外推）"""
        with get_conn() as conn:
            data = get_backtest_data(conn)
        self.signals = data.get("signals", [])
        self.snapshots = data.get("snapshots", {})

        logger.info(f"加载 {len(self.signals)} 条信号, {len(self.snapshots)} 个代币的快照")

        # 数据质量诊断（仅统计真实快照的时间点分布）
        time_points = defaultdict(set)
        for addr, snaps in self.snapshots.items():
            for s in snaps:
                t = s.get("snapshot_time")
                if t:
                    time_points[addr].add(str(t)[:16])
        tokens_with_multi = sum(1 for pts in time_points.values() if len(pts) >= 2)
        total_time_points = sum(len(pts) for pts in time_points.values())
        self.data_quality = {
            "total_tokens": len(time_points),
            "tokens_with_multiple_timepoints": tokens_with_multi,
            "total_unique_timepoints": total_time_points,
            "sufficient": tokens_with_multi >= 5,
        }
        logger.info(f"数据质量: {tokens_with_multi}/{len(time_points)} 个代币有≥2个真实时间点, "
                     f"总计 {total_time_points} 个独立时间点")
        if not self.data_quality["sufficient"]:
            logger.warning("多时间点数据不足 (需≥5个代币有≥2个快照)。"
                           "行情拉取需持续运行积累真实时间序列，当前回测结果仅供参考。")

    # ---- 信号确认：几分钟内出现 N 个信号 ----

    def _build_signal_confirm_index(self, params: BacktestParams) -> dict:
        """
        构建信号确认索引。
        对每个代币，在 signal_confirm_minutes 窗口内统计信号数。
        返回 {contract_address: {signal_time: confirm_count}}
        """
        if params.signal_confirm_count <= 1:
            return {}  # 不需要确认

        window = timedelta(minutes=params.signal_confirm_minutes)
        index = defaultdict(dict)

        # 按代币分组
        by_token = defaultdict(list)
        for s in self.signals:
            t = s["signal_time"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            by_token[s["contract_address"]].append((t, s))

        for addr, sig_list in by_token.items():
            sig_list.sort(key=lambda x: x[0])
            for i, (t, _) in enumerate(sig_list):
                # 统计 [t - window, t] 窗口内的信号数
                count = 0
                for j in range(i, -1, -1):
                    if t - sig_list[j][0] <= window:
                        count += 1
                    else:
                        break
                index[addr][t] = count

        return index

    def _is_signal_confirmed(self, signal: dict, confirm_index: dict,
                             params: BacktestParams) -> bool:
        """检查信号是否满足确认条件(时间窗口 + 数量)"""
        if params.signal_confirm_count <= 1:
            return True
        addr = signal["contract_address"]
        t = signal["signal_time"]
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        count = confirm_index.get(addr, {}).get(t, 1)
        return count >= params.signal_confirm_count

    def _is_in_runtime(self, signal_time, params: BacktestParams) -> bool:
        """检查信号时间是否在运行时间段内"""
        if params.runtime_start_hour == 0 and params.runtime_end_hour == 0:
            return True
        if isinstance(signal_time, str):
            signal_time = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
        h = signal_time.hour
        start, end = params.runtime_start_hour, params.runtime_end_hour
        if end == 0:
            end = 24
        if start <= end:
            return start <= h < end
        else:
            return h >= start or h < end

    # ---- 参数网格 ----

    def generate_param_grid(self) -> list:
        """生成参数网格，组合数超 MAX_GRID_COMBOS 时随机采样"""
        keys = list(PARAM_GRID.keys())
        values = list(PARAM_GRID.values())
        full_count = 1
        for v in values:
            full_count *= len(v)
        combinations = list(product(*values))
        total = len(combinations)
        if total > MAX_GRID_COMBOS:
            logger.warning(f"参数网格 {total} 组超过上限 {MAX_GRID_COMBOS}，随机采样")
            combinations = random.sample(combinations, MAX_GRID_COMBOS)
            total = MAX_GRID_COMBOS
        params_list = []
        for combo in combinations:
            p = BacktestParams(**dict(zip(keys, combo)))
            params_list.append(p)
        logger.info(f"参数网格: {total} 组组合 (全量 {full_count})")
        return params_list

    # ---- 主回测 ----

    def run_grid_search(self) -> list:
        """遍历参数网格，运行全部回测"""
        self.load_data()

        if not self.signals:
            logger.warning("无信号数据，回测终止")
            return []

        param_list = self.generate_param_grid()
        results = []

        for i, params in enumerate(param_list):
            logger.info(f"[{i+1}/{len(param_list)}] 测试参数: {params.label}")
            result = self.run_single(params)
            results.append(result)

        results.sort(key=lambda r: r.profit_factor, reverse=True)
        return results

    def run_single(self, params: BacktestParams) -> StrategyResult:
        """对单组参数执行完整回测"""
        result = StrategyResult(params=params)
        cumulative_profit = 0.0
        peak_cumulative = 0.0
        max_drawdown_usd = 0.0
        consecutive_losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        buy_amount_usd = params.buy_amount_sol * params.sol_price_usd

        # 构建信号确认索引
        confirm_index = self._build_signal_confirm_index(params)

        # 跟踪每个代币已买入次数
        token_buy_count = defaultdict(int)
        # 只跟首次信号：已跟过的代币
        followed_addrs = set()

        for signal in self.signals:
            addr = signal["contract_address"]

            # 只跟首次信号过滤
            if params.only_first_signal:
                if addr in followed_addrs:
                    continue
                followed_addrs.add(addr)

            # 运行时间段过滤
            if not self._is_in_runtime(signal["signal_time"], params):
                continue

            # 信号确认过滤
            if not self._is_signal_confirmed(signal, confirm_index, params):
                continue

            trade = self._simulate_trade(signal, params, token_buy_count)
            if trade is None:
                continue

            # 更新买入计数
            token_buy_count[signal["contract_address"]] += 1

            result.trades.append(trade)
            result.total_trades += 1

            # 统计盈亏
            if trade.profit_pct > 0:
                result.winning_trades += 1
                gross_profit += trade.profit_pct
                consecutive_losses = 0
            else:
                result.losing_trades += 1
                gross_loss += min(abs(trade.profit_pct), 1.0)
                consecutive_losses += 1
                result.max_consecutive_losses = max(
                    result.max_consecutive_losses, consecutive_losses
                )

            # 累计盈亏（基于累积利润曲线计算）
            result.total_profit_usd += trade.profit_usd
            cumulative_profit += trade.profit_usd
            if cumulative_profit > peak_cumulative:
                peak_cumulative = cumulative_profit
            drawdown = peak_cumulative - cumulative_profit
            max_drawdown_usd = max(max_drawdown_usd, drawdown)

        # 汇总统计
        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
            total_invested = result.total_trades * buy_amount_usd
            result.total_profit_pct = cumulative_profit / total_invested if total_invested > 0 else 0.0
            result.max_drawdown_pct = max_drawdown_usd / total_invested if total_invested > 0 else 0.0

        if result.winning_trades > 0:
            result.avg_profit_pct = gross_profit / result.winning_trades
        if result.losing_trades > 0:
            result.avg_loss_pct = gross_loss / result.losing_trades

        if gross_loss > 0:
            result.profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            result.profit_factor = 9999  # 无亏损，盈亏比视为极大（JSON-safe）

        logger.info(f"  -> 交易 {result.total_trades} 笔, 胜率 {result.win_rate:.1%}, "
                     f"盈亏 ${result.total_profit_usd:+.2f}, "
                     f"组合收益 {result.total_profit_pct:+.2%}, "
                     f"盈亏比 {result.profit_factor:.2f}")

        return result

    def _simulate_trade(self, signal: dict, params: BacktestParams,
                        token_buy_count: dict) -> TradeResult | None:
        """模拟单笔交易，应用所有 Debot 过滤条件 + 动态成本模型"""
        contract = signal["contract_address"]
        signal_time = signal["signal_time"]
        if isinstance(signal_time, str):
            signal_time = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
        token_symbol = signal.get("token_symbol", "?") or contract[:8]

        # -- 单币最大买入次数过滤 --
        if token_buy_count.get(contract, 0) >= params.max_buys_per_token:
            return None

        # -- TOP持仓过滤 (Debot: TOP持仓小于 X%) --
        holder_rate = signal.get("holder_rate")
        if holder_rate is None or holder_rate < 0:
            # 未知 / 哨兵值 → 若过滤开启则拒绝（保守策略，防镰刀币绕过）
            if params.max_holder_rate > 0:
                return None
            holder_rate = 0
        if params.max_holder_rate > 0 and holder_rate > params.max_holder_rate:
            return None

        # 获取行情快照
        snap_list = self.snapshots.get(contract, [])
        if not snap_list:
            return None
        snap_list.sort(key=lambda s: s["snapshot_time"])

        # 信号发出后至少延迟 60s 作为入场时间
        min_entry_time = signal_time + timedelta(seconds=60)
        entry_snap = None
        for snap in snap_list:
            if snap["snapshot_time"] >= min_entry_time:
                entry_snap = snap
                break
        if entry_snap is None:
            entry_snap = snap_list[-1]

        raw_entry_price = entry_snap.get("price_usd") or 0
        if raw_entry_price <= 0:
            return None

        # -- 市值范围过滤 (Debot: 币种市值 Min/Max) --
        market_cap = entry_snap.get("market_cap_usd") or entry_snap.get("fdv_usd") or 0
        if params.min_market_cap_usd > 0 and market_cap < params.min_market_cap_usd:
            return None
        if params.max_market_cap_usd > 0 and market_cap > params.max_market_cap_usd:
            return None

        # -- 持有人数过滤 (Debot: 持有人 Min/Max) --
        holders = entry_snap.get("holders") or signal.get("holders_count") or 0
        if params.min_holders > 0 and holders < params.min_holders:
            return None
        if params.max_holders > 0 and holders > params.max_holders:
            return None

        # -- 代币创建时间过滤 (Debot: 代币创建时间 Min/Max) --
        pair_created = entry_snap.get("pair_created_at")
        if pair_created:
            if isinstance(pair_created, str):
                pair_created = datetime.fromisoformat(pair_created.replace("Z", "+00:00"))
            st = signal_time.replace(tzinfo=None) if signal_time.tzinfo else signal_time
            pc = pair_created.replace(tzinfo=None) if pair_created.tzinfo else pair_created
            token_age_seconds = (st - pc).total_seconds()
            token_age_minutes = token_age_seconds / 60
            if params.min_token_age_minutes > 0 and token_age_minutes < params.min_token_age_minutes:
                return None
            if params.max_token_age_hours > 0 and token_age_seconds / 3600 > params.max_token_age_hours:
                return None

        # ---- 动态成本模型 (来自参数配置) ----
        # 买入实际成交价 = 报价 × (1 + 滑点 + 价格偏差 + DEX手续费)
        effective_slippage = params.slippage_pct + params.price_deviation_pct
        entry_price = raw_entry_price * (1 + effective_slippage + DEX_FEE)

        # 固定成本 SOL = (优先费 + 贿赂费 + 基础Gas) × 2 (买卖各一次)
        fixed_cost_sol = (params.priority_fee_sol + params.bribe_fee_sol + BASE_GAS_SOL) * 2
        fixed_cost_usd = fixed_cost_sol * params.sol_price_usd

        # 买入金额 USD
        buy_amount_usd = params.buy_amount_sol * params.sol_price_usd

        # 模拟持仓过程
        max_hold = timedelta(hours=params.max_hold_hours)
        raw_exit_price = raw_entry_price
        exit_reason = "timeout"
        exit_snap_time = entry_snap["snapshot_time"]

        for snap in snap_list:
            if snap["snapshot_time"] <= entry_snap["snapshot_time"]:
                continue

            snap_time = snap["snapshot_time"]
            hold_duration = snap_time - entry_snap["snapshot_time"]
            if hold_duration > max_hold:
                break

            snap_price = snap.get("price_usd") or 0
            if snap_price <= 0:
                continue

            raw_change = (snap_price - raw_entry_price) / raw_entry_price

            if raw_change >= params.take_profit:
                raw_exit_price = snap_price
                exit_reason = "take_profit"
                exit_snap_time = snap_time
                break

            if raw_change <= -params.stop_loss:
                raw_exit_price = snap_price
                exit_reason = "stop_loss"
                exit_snap_time = snap_time
                break

            raw_exit_price = snap_price
            exit_snap_time = snap_time

        # 卖出实际成交价 = 报价 × (1 - 滑点 - 价格偏差 - DEX手续费)
        exit_price = raw_exit_price * (1 - effective_slippage - DEX_FEE)

        # ---- 计算净盈亏 ----
        net_profit_pct = (exit_price - entry_price) / entry_price
        profit_usd = buy_amount_usd * net_profit_pct - fixed_cost_usd
        net_profit_pct = profit_usd / buy_amount_usd

        hold_hours = (exit_snap_time - entry_snap["snapshot_time"]).total_seconds() / 3600

        def _fmt(dt) -> str:
            if hasattr(dt, 'isoformat'):
                return dt.isoformat()
            return str(dt)

        return TradeResult(
            token_symbol=token_symbol,
            contract_address=contract,
            entry_price=round(entry_price, 12),
            exit_price=round(exit_price, 12),
            profit_pct=round(net_profit_pct, 6),
            profit_usd=round(profit_usd, 6),
            hold_hours=round(hold_hours, 1),
            exit_reason=exit_reason,
            signal_time=_fmt(signal_time),
            entry_time=_fmt(entry_snap["snapshot_time"]),
            exit_time=_fmt(exit_snap_time),
        )


# ============================================================
# 结果筛选 & 存储
# ============================================================

def filter_best_strategies(results: list, top_n: int = 5) -> list:
    """
    筛选最优策略组合。
    按综合评分排序：利润因子 × 胜率 / (回撤 + 0.01)
    """
    valid = []
    for r in results:
        if r.total_trades < 5:
            continue
        valid.append(r)

    valid.sort(key=lambda r: (
        max(r.profit_factor, 0.001)
        * max(r.win_rate, 0.001)
        / (r.max_drawdown_pct + 0.01)
    ), reverse=True)
    return valid[:top_n]


def save_results(results: list):
    """将最优策略写入数据库"""
    best = filter_best_strategies(results, top_n=3)
    if not best:
        logger.warning("没有符合过滤条件的策略")
        return []

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE best_strategy_config SET is_enable = FALSE")
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"禁用旧策略失败: {e}")

    saved = []
    for r in best:
        strategy_data = {
            "strategy_params": json.dumps(r.params.to_dict(), ensure_ascii=False),
            "backtest_profit": r.total_profit_pct,
            "max_drawdown": r.max_drawdown_pct,
            "win_rate": r.win_rate,
            "backtest_date_range": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            "is_enable": True,
        }
        with get_conn() as conn:
            result_id = save_best_strategy(conn, strategy_data)
            if result_id:
                saved.append(strategy_data)
                logger.info(f"最优策略已保存: id={result_id}, {r.to_summary_dict()}")

    return saved


# ============================================================
# 主入口
# ============================================================

def run_backtest() -> dict:
    """主执行函数：运行完整回测流程"""
    logger.info("=" * 50)
    logger.info("回测引擎启动")
    logger.info(f"参数网格: {len(list(product(*PARAM_GRID.values())))} 组")
    logger.info("=" * 50)

    start_time = time.time()

    try:
        engine = BacktestEngine()
        results = engine.run_grid_search()

        if not results:
            logger.warning("回测完成，但没有有效结果")
            return {"success": True, "combinations": 0, "top_strategies": [], "saved": 0, "duration_s": 0}

        # 输出 top 5
        logger.info("\n" + "=" * 60)
        logger.info("Top 5 策略:")
        for i, r in enumerate(results[:5]):
            logger.info(f"  #{i+1}: {r.to_summary_dict()}")

        saved = save_results(results)

        elapsed = round(time.time() - start_time, 1)
        logger.info(f"回测完成, 耗时 {elapsed}s")

        return {
            "success": True,
            "combinations": len(results),
            "top_strategies": [r.to_summary_dict() for r in results[:5]],
            "saved": len(saved),
            "duration_s": elapsed,
            "data_quality": engine.data_quality,
        }

    except Exception as e:
        logger.error(f"回测异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_backtest()
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ============================================================
# Web 操作台接口
# ============================================================

def run_custom_backtest(params_dict: dict) -> dict:
    """
    用指定参数运行单次回测（不存库），返回详细交易列表。
    params_dict: 可选键名对应 BacktestParams 字段，缺省使用默认值。
    """
    logger.info("自定义参数回测启动")

    start_time = time.time()

    try:
        # 构建参数对象（仅接受已知字段）
        valid_keys = set(BacktestParams.__dataclass_fields__.keys())
        filtered = {k: v for k, v in params_dict.items() if k in valid_keys}
        params = BacktestParams(**filtered)

        engine = BacktestEngine()
        engine.load_data()

        if not engine.signals:
            return {"success": True, "params": params.to_dict(), "trades": [], "summary": None}

        result = engine.run_single(params)

        # 序列化交易列表
        trades_json = []
        for t in result.trades:
            trades_json.append({
                "token_symbol": t.token_symbol,
                "contract_address": t.contract_address,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "profit_pct": t.profit_pct,
                "profit_usd": t.profit_usd,
                "hold_hours": t.hold_hours,
                "exit_reason": t.exit_reason,
                "signal_time": t.signal_time,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
            })

        elapsed = round(time.time() - start_time, 1)

        return {
            "success": True,
            "params": params.to_dict(),
            "summary": result.to_summary_dict(),
            "trades": trades_json,
            "duration_s": elapsed,
            "data_quality": engine.data_quality,
        }

    except Exception as e:
        logger.error(f"自定义回测异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def get_latest_report() -> dict:
    """
    读取最近一次网格回测的完整结果，包含交易明细。
    """
    logger.info("读取最新回测报告")

    try:
        with get_conn() as conn:
            active = get_active_strategy(conn)

        if not active:
            return {"success": True, "has_report": False, "message": "暂无回测报告"}

        # 用活跃策略参数重新跑一次获取交易明细
        params = active.get("strategy_params", {})
        if isinstance(params, str):
            params = json.loads(params)

        valid_keys = set(BacktestParams.__dataclass_fields__.keys())
        filtered = {k: v for k, v in params.items() if k in valid_keys}
        bp = BacktestParams(**filtered)

        engine = BacktestEngine()
        engine.load_data()
        result = engine.run_single(bp)

        trades_json = []
        for t in result.trades:
            trades_json.append({
                "token_symbol": t.token_symbol,
                "contract_address": t.contract_address,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "profit_pct": t.profit_pct,
                "profit_usd": t.profit_usd,
                "hold_hours": t.hold_hours,
                "exit_reason": t.exit_reason,
                "signal_time": t.signal_time,
                "entry_time": t.entry_time,
            })

        return {
            "success": True,
            "has_report": True,
            "params": bp.to_dict(),
            "summary": result.to_summary_dict(),
            "trades": trades_json,
            "strategy_id": active.get("id"),
            "updated_at": str(active.get("update_time", "")),
        }

    except Exception as e:
        logger.error(f"读取报告异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
