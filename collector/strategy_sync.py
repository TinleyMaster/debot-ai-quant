"""
策略报告生成器
- 从 best_strategy_config 读取当前最优策略
- 生成对齐 Debot「创建跟单」页面的参数报告，直接照填
"""
import json
import logging

from db import get_conn, get_active_strategy

logger = logging.getLogger("strategy_report")

# Debot「创建跟单」页面字段 → 回测参数映射 (按表单分区排列)
FIELD_LABELS = {
    # -- 基础设置 --
    "buy_amount_sol": "买入金额(SOL)",

    # -- 买入策略 --
    "max_buys_per_token": "买入次数(每币)",

    # -- 卖出策略 --
    "take_profit": "止盈(价格涨幅倍数)",
    "stop_loss": "止损(价格跌幅)",
    "max_hold_hours": "最长持仓时间",

    # -- 高级过滤 --
    "only_first_signal": "只跟首次信号",
    "signal_confirm_minutes": "信号确认(分钟)",
    "signal_confirm_count": "信号确认(个数)",
    "min_token_age_minutes": "代币创建时间 Min(分钟)",
    "max_token_age_hours": "代币创建时间 Max(小时)",
    "min_market_cap_usd": "币种市值 Min(USD)",
    "max_market_cap_usd": "币种市值 Max(USD)",
    "min_holders": "持有人 Min",
    "max_holders": "持有人 Max",
    "max_holder_rate": "TOP持仓小于",
    "runtime_start_hour": "运行开始(时)",
    "runtime_end_hour": "运行结束(时)",

    # -- 风险控制 --
    "slippage_pct": "滑点",
    "priority_fee_sol": "优先费(SOL)",
    "bribe_fee_sol": "贿赂费(SOL)",
    "price_deviation_pct": "价格偏差",
}

SECTION_ORDER = [
    ("基础设置", ["buy_amount_sol"]),
    ("买入策略", ["max_buys_per_token"]),
    ("卖出策略", ["take_profit", "stop_loss", "max_hold_hours"]),
    ("高级过滤", ["only_first_signal", "signal_confirm_minutes", "signal_confirm_count",
                   "min_token_age_minutes", "max_token_age_hours",
                   "min_market_cap_usd", "max_market_cap_usd",
                   "min_holders", "max_holders", "max_holder_rate",
                   "runtime_start_hour", "runtime_end_hour"]),
    ("风险控制", ["slippage_pct", "priority_fee_sol", "bribe_fee_sol", "price_deviation_pct"]),
]


def format_value(key: str, val) -> str:
    """格式化参数值为人读形式"""
    if val is None or val == "":
        return "不限" if "max" in key or key.endswith("_hours") else ""

    if key in ("take_profit", "stop_loss", "slippage_pct", "price_deviation_pct", "max_holder_rate"):
        if isinstance(val, bool):
            return "是" if val else "否"
        if isinstance(val, (int, float)) and val > 0:
            return f"{val * 100:.0f}%" if val < 1 else f"{val:.0f}%"
        return str(val)

    if key == "only_first_signal":
        return "是" if val else "否"

    if key == "buy_amount_sol":
        return f"{val} SOL" if isinstance(val, (int, float)) else str(val)

    if key == "max_hold_hours":
        return f"{val}h" if val else "不限"

    if key == "max_token_age_hours":
        return f"{val}h" if val else "不限"

    if key == "min_token_age_minutes":
        return f"{val}分钟" if val else "不限"

    if key in ("min_market_cap_usd", "max_market_cap_usd"):
        if isinstance(val, (int, float)) and val > 0:
            if val >= 1e6:
                return f"${val/1e6:.1f}M"
            return f"${val/1e3:.0f}K"
        return "不限"

    if key in ("runtime_start_hour", "runtime_end_hour"):
        if isinstance(val, (int, float)):
            v = int(val) if val else 0
            return f"{v}:00"
        return str(val)

    if key in ("priority_fee_sol", "bribe_fee_sol"):
        if isinstance(val, (int, float)):
            return f"{val:.3f} SOL" if val > 0 else "无"
        return str(val)

    return str(val)


def format_report(active: dict) -> dict:
    """将数据库策略记录转为 Debot 跟单报告，按分区排列"""
    if not active:
        return {}

    params = active.get("strategy_params", {})
    if isinstance(params, str):
        params = json.loads(params)

    sections = []
    for section_name, keys in SECTION_ORDER:
        items = []
        for key in keys:
            label = FIELD_LABELS.get(key, key)
            val = format_value(key, params.get(key))
            items.append((label, val))
        sections.append({"section": section_name, "items": items})

    return {
        "sections": sections,
        "params_raw": params,
        "backtest_stats": {
            "胜率": f"{float(active.get('win_rate', 0)) * 100:.1f}%",
            "累计收益": f"{float(active.get('backtest_profit', 0)) * 100:+.1f}%",
            "最大回撤": f"{float(active.get('max_drawdown', 0)) * 100:.1f}%",
            "回测区间": active.get("backtest_date_range", ""),
            "更新时间": str(active.get("update_time", "")),
        },
    }


def run_sync() -> dict:
    """主执行函数：读取最优策略并生成 Debot 配置报告"""
    logger.info("=" * 50)
    logger.info("策略报告生成")
    logger.info("=" * 50)

    try:
        with get_conn() as conn:
            active = get_active_strategy(conn)

        if not active:
            logger.warning("无启用的策略")
            return {"success": True, "has_strategy": False, "message": "当前无启用的最优策略"}

        report = format_report(active)

        logger.info("=" * 40)
        logger.info("Debot 跟单配置 (按表单分区照填)")
        for section in report["sections"]:
            logger.info(f"--- {section['section']} ---")
            for label, val in section["items"]:
                logger.info(f"  {label}: {val}")
        logger.info("=" * 40)
        for key, val in report["backtest_stats"].items():
            logger.info(f"  {key}: {val}")
        logger.info("=" * 40)

        return {
            "success": True,
            "has_strategy": True,
            "strategy_id": active.get("id"),
            "report": report,
        }

    except Exception as e:
        logger.error(f"策略报告异常: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_sync()
    print(json.dumps(result, ensure_ascii=False, indent=2))
