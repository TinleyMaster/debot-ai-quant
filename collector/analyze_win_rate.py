"""
AI 信号胜率统计分析脚本

基于 debot_signal_agg.max_price_gain 计算：
- 方案 A：不同阈值下的胜率（max_price_gain 超过阈值即为胜）
- 方案 C：收益分档分布（让用户了解信号整体的收益结构）

用法：
    cd collector && python analyze_win_rate.py

依赖环境变量：DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
（与 db.py 一致，从 .env 读取）
"""
import json
import os
import sys
from collections import Counter

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras

# ============================================================
# 数据库连接
# ============================================================

def get_conn():
    dsn = (
        f"host={os.environ['DB_HOST']} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ['DB_NAME']} "
        f"user={os.environ['DB_USER']} "
        f"password={os.environ['DB_PASSWORD']}"
    )
    return psycopg2.connect(dsn)


# ============================================================
# 查询
# ============================================================

def fetch_signal_data(conn):
    """从 debot_signal + debot_signal_agg 查询每条信号的 max_price_gain 等关键字段。
    max_price_gain 优先从 debot_signal_agg 表取，没有则从 signal_content JSON 中提取。"""
    sql = """
    SELECT
        s.id,
        s.signal_time,
        s.contract_address,
        s.token_symbol,
        s.price_usd,
        s.market_cap,
        s.smart_wallets,
        s.multiplier,
        s.signal_content,
        a.max_price_gain,
        a.first_price,
        a.max_price,
        a.signal_count
    FROM debot_signal s
    LEFT JOIN debot_signal_agg a ON s.contract_address = a.contract_address
    ORDER BY s.signal_time DESC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    # 后处理：从 signal_content JSON 和 multiplier 列中提取 max_price_gain 作为 fallback
    for r in rows:
        if r['max_price_gain'] is not None:
            continue

        # Fallback 1: signal_content JSON
        sc = r.get('signal_content')
        if sc:
            try:
                content = json.loads(sc)
                mg = content.get('max_price_gain')
                if mg is not None:
                    r['max_price_gain'] = float(mg)
                fp = content.get('first_price')
                if fp is not None and r['first_price'] is None:
                    r['first_price'] = float(fp)
                sc_count = content.get('signal_count')
                if sc_count is not None and r['signal_count'] is None:
                    r['signal_count'] = sc_count
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        if r['max_price_gain'] is not None:
            continue

        # Fallback 2: multiplier 列（格式如 "8.6x", "0.5x"）
        mul = r.get('multiplier') or ''
        if isinstance(mul, str) and mul.endswith('x'):
            try:
                r['max_price_gain'] = float(mul[:-1])
            except ValueError:
                pass

    return rows


def fetch_metric_snapshots(conn, contract_addresses):
    """获取这些代币在 debot_token_metric 中的最新快照（供方案 B 预留）"""
    if not contract_addresses:
        return {}
    sql = """
    SELECT DISTINCT ON (contract_address)
        contract_address,
        snapshot_time,
        price,
        percent_5m,
        percent_1h,
        percent_24h
    FROM debot_token_metric
    WHERE contract_address = ANY(%s)
    ORDER BY contract_address, snapshot_time DESC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (list(contract_addresses),))
        return {r['contract_address']: r for r in cur.fetchall()}


# ============================================================
# 统计分析
# ============================================================

def compute_win_rate(rows: list) -> dict:
    """
    方案 A：不同阈值下的胜率。

    阈值定义（基于 max_price_gain）：
    - > 0%（即 max_price_gain > 1.0，至少不亏）
    - >= 10%（1.1x）
    - >= 50%（1.5x）
    - >= 100%（2.0x，翻倍）
    - >= 200%（3.0x）
    - >= 500%（6.0x）
    - >= 1000%（11.0x）

    注：max_price_gain 为 None 表示该代币信号未被 API 返回该字段，标记为"未知"。
    """
    total = len(rows)
    known = [r for r in rows if r['max_price_gain'] is not None]
    unknown = total - len(known)

    # max_price_gain = (max_price / first_price) - 1，即涨幅比例
    # 例: 0.5 = 50%涨幅(1.5x), 1.0 = 翻倍(2x), 8.6 = 860%涨幅(9.6x)
    thresholds = [
        ("任意盈利 (> 0%)", lambda g: g > 0.0),
        ("涨幅 >= 10%", lambda g: g >= 0.10),
        ("涨幅 >= 50%", lambda g: g >= 0.50),
        ("翻倍 >= 100%", lambda g: g >= 1.00),
        ("涨幅 >= 200%", lambda g: g >= 2.00),
        ("五倍 >= 500%", lambda g: g >= 5.00),
        ("十倍 >= 1000%", lambda g: g >= 10.00),
    ]

    win_rates = []
    for label, pred in thresholds:
        win = sum(1 for r in known if pred(r['max_price_gain']))
        rate = win / len(known) * 100 if known else 0
        win_rates.append({
            "label": label,
            "win_count": win,
            "known_count": len(known),
            "rate": rate,
        })

    return {
        "total": total,
        "known": len(known),
        "unknown": unknown,
        "win_rates": win_rates,
    }


def compute_distribution(rows: list) -> dict:
    """
    方案 C：收益分档分布。

    档位（基于涨幅比例，max_price_gain = (max/first) - 1）：
    - 亏损 (< 0%)
    - 持平 (0% ~ 10%)
    - 小涨 (10% ~ 50%)
    - 中涨 (50% ~ 100%)
    - 翻倍 (100% ~ 200%)
    - 大涨 (200% ~ 500%)
    - 暴涨 (500% ~ 1000%)
    - 超级暴涨 (>= 1000%)
    - 未知
    """
    buckets = [
        ("亏损 (< 0%)",              lambda g: g < 0.0),
        ("持平 (0% ~ 10%)",          lambda g: 0.0 <= g < 0.10),
        ("小涨 (10% ~ 50%)",         lambda g: 0.10 <= g < 0.50),
        ("中涨 (50% ~ 100%)",        lambda g: 0.50 <= g < 1.00),
        ("翻倍 (100% ~ 200%)",       lambda g: 1.00 <= g < 2.00),
        ("大涨 (200% ~ 500%)",       lambda g: 2.00 <= g < 5.00),
        ("暴涨 (500% ~ 1000%)",      lambda g: 5.00 <= g < 10.00),
        ("超级暴涨 (>= 1000%)",      lambda g: g >= 10.00),
    ]

    total = len(rows)
    known = [r for r in rows if r['max_price_gain'] is not None]
    unknown = total - len(known)

    distribution = []
    for label, pred in buckets:
        count = sum(1 for r in known if pred(r['max_price_gain']))
        pct = count / total * 100 if total else 0
        distribution.append({"label": label, "count": count, "pct": pct})

    if unknown > 0:
        distribution.append({"label": "未知 (无 max_price_gain)", "count": unknown, "pct": unknown / total * 100})

    return {
        "total": total,
        "distribution": distribution,
    }


def compute_summary_stats(rows: list) -> dict:
    """基本统计：均值、中位数、最值"""
    known = [r['max_price_gain'] for r in rows if r['max_price_gain'] is not None]
    if not known:
        return {"mean": None, "median": None, "min": None, "max": None, "count": 0}

    known_sorted = sorted(known)
    n = len(known_sorted)
    median = known_sorted[n // 2] if n % 2 else (known_sorted[n // 2 - 1] + known_sorted[n // 2]) / 2

    return {
        "count": n,
        "mean": sum(known) / n,
        "median": median,
        "min": known_sorted[0],
        "max": known_sorted[-1],
    }


def compute_token_level(rows: list) -> dict:
    """
    按代币聚合去重统计（同一合约多次信号，只算一次）
    每个代币取 max_price_gain 的最大值。
    """
    by_addr = {}
    for r in rows:
        addr = r['contract_address']
        if addr not in by_addr:
            by_addr[addr] = r
        else:
            # 取 max_price_gain 大的那条
            cur = by_addr[addr]['max_price_gain'] or 0
            new = r['max_price_gain'] or 0
            if new > cur:
                by_addr[addr] = r

    return compute_win_rate(list(by_addr.values())), compute_distribution(list(by_addr.values()))


def compute_wallet_correlation(rows: list) -> dict:
    """
    按聪明钱包数量分组，看钱包数跟胜率的关系。
    - 0-5 个钱包：低关注
    - 6-15 个钱包：中关注
    - 16-30 个钱包：高关注
    - > 30 个钱包：极高关注
    """
    groups = {
        "0-5 个钱包": [],
        "6-15 个钱包": [],
        "16-30 个钱包": [],
        "> 30 个钱包": [],
    }

    for r in rows:
        w = r['smart_wallets'] or 0
        if w <= 5:
            groups["0-5 个钱包"].append(r)
        elif w <= 15:
            groups["6-15 个钱包"].append(r)
        elif w <= 30:
            groups["16-30 个钱包"].append(r)
        else:
            groups["> 30 个钱包"].append(r)

    results = {}
    for label, group in groups.items():
        if not group:
            results[label] = {"count": 0, "known": 0, "win_rate_any": None, "win_rate_double": None}
            continue
        known = [r for r in group if r['max_price_gain'] is not None]
        win_1x = sum(1 for r in known if r['max_price_gain'] > 0.0)
        win_2x = sum(1 for r in known if r['max_price_gain'] >= 1.0)
        results[label] = {
            "count": len(group),
            "known": len(known),
            "win_rate_any": win_1x / len(known) * 100 if known else None,
            "win_rate_double": win_2x / len(known) * 100 if known else None,
        }

    return results


# ============================================================
# 输出
# ============================================================

def print_report(rows: list, win_rate: dict, dist: dict, stats: dict,
                 token_win: dict, token_dist: dict, wallet: dict):
    """打印完整的胜率分析报告"""
    total = len(rows)
    unique_tokens = len(set(r['contract_address'] for r in rows))

    print("=" * 70)
    print("         Debot AI 信号胜率分析报告")
    print("=" * 70)
    print(f"  总信号数: {total}")
    print(f"  独立代币数: {unique_tokens}")
    print(f"  有 max_price_gain 数据: {win_rate['known']}")
    print(f"  缺失 max_price_gain: {win_rate['unknown']}")
    print()

    # --- 基本统计 ---
    print("-" * 70)
    print("【基本统计】max_price_gain 分布")
    print("-" * 70)
    print(f"  均值:   {stats['mean']*100:.1f}%" if stats['mean'] is not None else "  均值:   N/A")
    print(f"  中位数: {stats['median']*100:.1f}%" if stats['median'] is not None else "  中位数: N/A")
    print(f"  最小值: {stats['min']*100:.1f}%" if stats['min'] is not None else "  最小值: N/A")
    print(f"  最大值: {stats['max']*100:.1f}%" if stats['max'] is not None else "  最大值: N/A")
    print()

    # --- 方案 A：胜率 ---
    print("-" * 70)
    print("【方案 A】不同阈值下的胜率（基于 max_price_gain）")
    print("-" * 70)
    print(f"  {'阈值':<32s} {'胜出数':>6s}  {'胜率':>8s}")
    print(f"  {'─' * 32} {'─' * 6} {'─' * 8}")
    for wr in win_rate['win_rates']:
        print(f"  {wr['label']:<32s} {wr['win_count']:>5d}  {wr['rate']:>6.1f}%")
    print()
    print("  ⚠ max_price_gain 是历史最高涨幅，非固定时间后涨幅。")
    print("    实际交易无法精准卖在最高点，以上胜率偏乐观。")
    print()

    # --- 方案 C：分档 ---
    print("-" * 70)
    print("【方案 C】收益分档分布")
    print("-" * 70)
    print(f"  {'档位':<30s} {'信号数':>6s}  {'占比':>8s}  {'柱状图'}")
    print(f"  {'─' * 30} {'─' * 6} {'─' * 8} {'─' * 20}")
    max_count = max(d['count'] for d in dist['distribution']) if dist['distribution'] else 1
    for d in dist['distribution']:
        bar_len = int(d['count'] / max_count * 20) if max_count else 0
        bar = "█" * bar_len
        print(f"  {d['label']:<30s} {d['count']:>6d}  {d['pct']:>6.1f}%  {bar}")
    print()

    # --- 按代币去重 ---
    print("-" * 70)
    print("【代币级胜率】同一代币多次信号只算一次（取最佳 max_price_gain）")
    print("-" * 70)
    if token_win['known']:
        print(f"  独立代币数: {len(token_dist['distribution'])} (已知 {token_win['known']})")
        print()
        for wr in token_win['win_rates']:
            print(f"  {wr['label']:<32s} {wr['win_count']:>5d}  {wr['rate']:>6.1f}%")
    print()

    # --- 聪明钱包数 vs 胜率 ---
    print("-" * 70)
    print("【聪明钱包数 vs 胜率】")
    print("-" * 70)
    print(f"  {'分组':<18s} {'信号数':>6s}  {'任意盈利':>10s}  {'翻倍率':>10s}")
    print(f"  {'─' * 18} {'─' * 6} {'─' * 10} {'─' * 10}")
    for label, w in wallet.items():
        wr1 = f"{w['win_rate_any']:.1f}%" if w['win_rate_any'] is not None else "N/A"
        wr2 = f"{w['win_rate_double']:.1f}%" if w['win_rate_double'] is not None else "N/A"
        print(f"  {label:<18s} {w['count']:>6d}  {wr1:>10s}  {wr2:>10s}")
    print()

    # --- 解读 ---
    print("=" * 70)
    print("【解读建议】")
    print("=" * 70)
    if stats['mean'] is not None and stats['mean'] > 1.0:
        print(f"  - 信号平均涨幅 {stats['mean']*100:.0f}%，整体收益很可观")
    elif stats['mean'] is not None and stats['mean'] > 0:
        print(f"  - 信号平均涨幅 {stats['mean']*100:.0f}%，勉强盈利")
    elif stats['mean'] is not None:
        print(f"  - 信号平均涨幅为负，需要筛选信号源")
    else:
        print(f"  - 信号平均涨幅偏低或为负，需要筛选信号源")

    wr_1x = win_rate['win_rates'][0]['rate'] if win_rate['win_rates'] else 0
    print(f"  - 有 {wr_1x:.1f}% 的信号至少有过浮盈机会")

    # 钱包相关性判断
    high_wallet = wallet.get("> 30 个钱包", {})
    low_wallet = wallet.get("0-5 个钱包", {})
    if high_wallet.get('win_rate_double') and low_wallet.get('win_rate_double'):
        if high_wallet['win_rate_double'] > low_wallet['win_rate_double'] * 1.5:
            print(f"  - 聪明钱包数 > 30 的信号翻倍率明显更高 ({high_wallet['win_rate_double']:.1f}% vs {low_wallet['win_rate_double']:.1f}%)，建议优先跟单高关注度信号")
        else:
            print(f"  - 聪明钱包数与翻倍率相关性不明显，钱包数可能不是有效筛选指标")

    print()
    print("  📌 下一步：等行情采集积累足够历史快照后，用固定时间窗口重新计算")
    print("     （信号发出价 vs N 分钟后价格），得出更贴近实际交易的胜率。")
    print("=" * 70)


# ============================================================
# 主入口
# ============================================================

def main():
    print("正在连接数据库...")
    try:
        conn = get_conn()
        print("数据库连接成功")
    except Exception as e:
        print(f"数据库连接失败: {e}")
        sys.exit(1)

    try:
        print("正在查询信号数据...")
        rows = fetch_signal_data(conn)
        print(f"查询到 {len(rows)} 条信号")

        if not rows:
            print("没有数据，请先运行采集器收集数据。")
            return

        # 方案 A
        win_rate = compute_win_rate(rows)

        # 方案 C
        dist = compute_distribution(rows)

        # 基本统计
        stats = compute_summary_stats(rows)

        # 代币级去重
        token_win, token_dist = compute_token_level(rows)

        # 钱包相关性
        wallet = compute_wallet_correlation(rows)

        # 打印报告
        print_report(rows, win_rate, dist, stats, token_win, token_dist, wallet)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
