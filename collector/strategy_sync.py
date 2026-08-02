"""
策略报告生成器
- 从 best_strategy_config 读取当前最优策略
- 生成对齐 Debot 跟单页面的参数报告
- 回测完成后手动填入 Debot: https://docs.debot.ai/basic-features/xin-hao-gen-dan
"""
import json
import logging

from db import get_conn, get_active_strategy
from backtest_engine import BUY_AMOUNT_SOL, BUY_AMOUNT_USD

logger = logging.getLogger("strategy_report")

# Debot 跟单页面字段 → 回测参数映射
# 用于生成"照填"报告
FIELD_LABELS = {
    "max_buys_per_token": "单币最大买入次数",
    "take_profit": "止盈阈值(涨幅)",
    "stop_loss": "止损阈值(跌幅)",
    "max_hold_hours": "最长持仓时间",
    "min_liquidity_usd": "最低流动性(USD)",
    "min_holder_rate": "最低持有人比例",
    "max_token_age_hours": "代币最大创建时间(h, 0=不限)",
    "signal_confirm_count": "信号确认数(N分钟内)",
    "signal_confirm_minutes": "信号确认窗口(分钟)",
}


def format_report(active: dict) -> dict:
    """将数据库策略记录转为 Debot 跟单报告"""
    if not active:
        return {}

    params = active.get("strategy_params", {})
    if isinstance(params, str):
        params = json.loads(params)

    debot_config = {}
    # 每次买入数量是固定值，不参与回测搜索
    debot_config["每次买入数量"] = f"{BUY_AMOUNT_SOL} SOL (≈${BUY_AMOUNT_USD:.2f})"
    for key, label in FIELD_LABELS.items():
        val = params.get(key, "")
        if key in ("take_profit", "stop_loss", "min_holder_rate"):
            val = f"{val * 100:.0f}%" if isinstance(val, (int, float)) else val
        elif key == "min_liquidity_usd":
            val = f"${val:,.0f}" if isinstance(val, (int, float)) and val > 0 else "不限"
        elif key == "max_hold_hours":
            val = f"{val}h" if val else val
        elif key == "max_token_age_hours":
            val = f"{val}h" if val else "不限"
        elif key == "signal_confirm_count":
            val = f"{val} 个信号" if val else val
        elif key == "signal_confirm_minutes":
            val = f"{val} 分钟内" if val else val
        elif key == "max_buys_per_token":
            val = f"{val} 次" if val else val
        debot_config[label] = str(val)

    return {
        "debot_config": debot_config,
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
        logger.info("Debot 跟单配置 (照填)")
        logger.info("=" * 40)
        for key, val in report["debot_config"].items():
            logger.info(f"  {key}: {val}")
        logger.info("---")
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
