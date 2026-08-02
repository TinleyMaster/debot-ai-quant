"""
Debot 信号采集服务 - 主入口
- 定时轮询 Debot AI 信号页面
- 增量入库 PostgreSQL
- 日志 + 健康监控
"""
import os
import json
import time
import random
import signal
import sys
import logging
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

from db import (
    init_db_pool, close_db_pool, get_conn,
    insert_signal, insert_token_info, insert_run_log, insert_alert,
    get_latest_signal_time, get_unprocessed_count, get_latest_unresolved_alert,
    get_latest_signals, get_token_kline, get_all_tracked_tokens,
)
from scraper import DebotScraper
from market_fetcher import run_fetch as run_market_fetch
from backtest_engine import run_backtest, run_custom_backtest, get_latest_report
from strategy_sync import run_sync as run_strategy_sync

# ============================================================
# 日志配置
# ============================================================

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/app/data/collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# 优雅退出标志
_shutdown_flag = False
# 最新阻断原因（供健康接口读取）
_last_block_reason = ""
_last_block_time = ""
# 采集器实例引用（供调试接口使用）
_scraper = None
# 连续空跑轮次计数（用于触发浏览器重启）
_consecutive_empty_rounds = 0
_last_signal_time = ""  # 最近一次成功入库的信号时间


def handle_shutdown(signum, frame):
    global _shutdown_flag
    logger.info(f"收到信号 {signum}，准备优雅退出...")
    _shutdown_flag = True


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


# ============================================================
# 配置加载
# ============================================================

def load_config() -> dict:
    """加载采集器配置文件"""
    config_path = os.environ.get("CONFIG_FILE", "/app/config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
            logger.info(f"配置文件已加载: {config_path}")
            return config
    else:
        logger.warning(f"配置文件不存在: {config_path}，使用默认配置")
        return {"selectors": {}, "risk_selectors": {}, "scrape_rules": {}}


# ============================================================
# 主循环
# ============================================================

def run_once(scraper: DebotScraper) -> dict:
    """
    执行一轮采集任务。
    返回统计信息。
    """
    global _last_block_reason, _last_block_time, _consecutive_empty_rounds, _last_signal_time
    start_time = time.time()
    stats = {"scraped": 0, "new": 0, "errors": 0, "error_detail": None, "alert_type": None}

    try:
        # 1. 抓取信号列表
        signals = scraper.scrape_signals()
        stats["scraped"] = len(signals)

        # 检测页面阻断
        if scraper._block_reason:
            stats["alert_type"] = scraper._block_reason
            _last_block_reason = scraper._block_reason
            _last_block_time = datetime.now(timezone.utc).isoformat()
            logger.warning(f"页面被阻断: {scraper._block_reason}")

        if not signals:
            _consecutive_empty_rounds += 1
            logger.info(f"本轮未抓取到信号 (连续 {_consecutive_empty_rounds} 轮空跑)")
            # 连续 5 轮空跑 + 非阻断状态 → 强制重启浏览器
            if _consecutive_empty_rounds >= 5 and not scraper._block_reason:
                logger.warning(f"连续 {_consecutive_empty_rounds} 轮空跑，强制重启浏览器")
                scraper.restart_browser()
                _consecutive_empty_rounds = 0
            return stats

        # 重置空跑计数
        _consecutive_empty_rounds = 0

        # 2. 写入数据库（去重）
        with get_conn() as conn:
            for sig in signals:
                try:
                    new_id = insert_signal(conn, sig)
                    if new_id:
                        stats["new"] += 1
                        _last_signal_time = sig.get("signal_time", "")
                        logger.info(f"新信号入库: {sig.get('token_symbol', '?')} {sig['contract_address']}")
                except Exception as e:
                    stats["errors"] += 1
                    logger.error(f"信号入库失败: {e}")

        logger.info(f"本轮完成: 抓取 {stats['scraped']} 条, 新增 {stats['new']} 条, 错误 {stats['errors']} 次")

    except Exception as e:
        stats["errors"] += 1
        stats["error_detail"] = str(e)[:500]
        logger.error(f"采集轮次异常: {e}", exc_info=True)

    finally:
        stats["duration_ms"] = int((time.time() - start_time) * 1000)

        # 写入运行日志
        try:
            with get_conn() as conn:
                insert_run_log(
                    conn,
                    signals_scraped=stats["scraped"],
                    signals_new=stats["new"],
                    errors_count=stats["errors"],
                    error_detail=stats.get("error_detail"),
                    alert_type=stats.get("alert_type"),
                    duration_ms=stats.get("duration_ms"),
                )
                # 页面阻断时写入告警表
                if stats.get("alert_type"):
                    alert_msg = f"页面阻断类型: {stats['alert_type']}"
                    if stats["alert_type"] == "login_required":
                        alert_msg = "Cookie 已过期，需要重新导入登录凭证"
                    elif stats["alert_type"] == "cloudflare_challenge":
                        alert_msg = "被 Cloudflare 人机验证拦截，可能需要手动验证"
                    insert_alert(conn, stats["alert_type"], alert_msg)
        except Exception as e:
            logger.error(f"写入运行日志失败: {e}")

    return stats


# ============================================================
# 健康检测 HTTP 服务
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    """简易健康检测接口，供 n8n 轮询"""

    def do_GET(self):
        if self.path == "/health":
            try:
                with get_conn() as conn:
                    unprocessed = get_unprocessed_count(conn)
                    alert = get_latest_unresolved_alert(conn)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = json.dumps({
                    "status": "ok",
                    "unprocessed_signals": unprocessed,
                    "block_reason": _last_block_reason,
                    "last_block_time": _last_block_time,
                    "consecutive_empty_rounds": _consecutive_empty_rounds,
                    "last_signal_time": _last_signal_time,
                    "version": os.environ.get("GIT_COMMIT", "unknown"),
                })
                self.wfile.write(resp.encode())
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
                self.send_response(500)
                self.end_headers()

        elif self.path == "/version":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "version": os.environ.get("GIT_COMMIT", "unknown"),
                "commit_msg": os.environ.get("GIT_COMMIT_MSG", ""),
            }).encode())

        elif self.path == "/fetch-market":
            # n8n 触发行情数据拉取（带重试，防止偶发失败造成数据断档）
            logger.info("收到行情拉取请求")
            result = None
            for attempt in range(3):
                try:
                    result = run_market_fetch()
                    break
                except Exception as e:
                    logger.warning(f"行情拉取失败 (attempt {attempt+1}/3): {e}")
                    if attempt < 2:
                        time.sleep(5)
            if result is None:
                result = {"success": False, "error": "3次重试均失败"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

        elif self.path == "/run-backtest":
            # n8n 触发回测运算
            logger.info("收到回测请求")
            try:
                result = run_backtest()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            except Exception as e:
                logger.error(f"回测失败: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path == "/sync-strategy":
            # n8n 触发策略同步
            logger.info("收到策略同步请求")
            try:
                result = run_strategy_sync()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            except Exception as e:
                logger.error(f"策略同步失败: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path == "/backtest-report":
            # Web 前端请求最新回测报告
            logger.info("收到报告请求")
            try:
                result = get_latest_report()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            except Exception as e:
                logger.error(f"获取报告失败: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path == "/" or self.path == "/index.html":
            # Web 操作台首页
            try:
                html_path = os.path.join(os.path.dirname(__file__), "index.html")
                with open(html_path, "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()

        elif self.path.startswith("/latest-signals"):
            # 获取最新信号 (支持 ?limit=50)
            try:
                qs = self.path.split("?")[1] if "?" in self.path else ""
                params = dict(p.split("=") for p in qs.split("&") if "=" in p)
                limit = int(params.get("limit", 50))
            except Exception:
                limit = 50
            try:
                with get_conn() as conn:
                    signals = get_latest_signals(conn, limit=min(limit, 200))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "signals": signals, "count": len(signals)}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path.startswith("/token-kline"):
            # 获取代币 K 线数据 (?address=xxx&limit=100)
            try:
                qs = self.path.split("?")[1] if "?" in self.path else ""
                params = dict(p.split("=") for p in qs.split("&") if "=" in p)
                address = params.get("address", "")
                limit = int(params.get("limit", 100))
            except Exception:
                address = ""
                limit = 100
            if not address:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "缺少 address 参数"}).encode())
                return
            try:
                with get_conn() as conn:
                    kline = get_token_kline(conn, address, limit=min(limit, 500))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "kline": kline, "count": len(kline)}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path.startswith("/tracked-tokens"):
            # 获取所有代币列表（供前端下拉选择）
            try:
                with get_conn() as conn:
                    tokens = get_all_tracked_tokens(conn)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "tokens": tokens, "count": len(tokens)}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

        elif self.path == "/debug-scrape":
            # 即时抓取调试: 返回页面匹配到的原始卡片信息
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            result = {"status": "error", "message": "scraper not available"}
            if _scraper:
                try:
                    _scraper._ensure_page()
                    page = _scraper._page
                    # 导航到信号页
                    page.goto(_scraper.signal_url, timeout=30000, wait_until="domcontentloaded")
                    _scraper._wait_for_spa_render()
                    # 匹配所有 token 链接
                    cards = page.query_selector_all("a[href*='/token/solana/']")
                    card_data = []
                    for i, c in enumerate(cards):
                        try:
                            text = c.inner_text().strip()
                            href = c.get_attribute("href") or ""
                            contract = _scraper._extract_contract_from_url(href)
                            card_data.append({
                                "i": i,
                                "len": len(text),
                                "href": href,
                                "contract": contract[:12] + "..." if len(contract) > 15 else contract,
                                "text_preview": text[:200]
                            })
                        except Exception:
                            pass
                    result = {
                        "status": "ok",
                        "url": page.url,
                        "title": page.title(),
                        "total_matched": len(cards),
                        "valid_cards": sum(1 for c in card_data if c["len"] >= 20),
                        "cards": card_data
                    }
                except Exception as e:
                    result = {"status": "error", "message": str(e)}
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

        elif self.path == "/debug-card":
            # 查看卡片调试 HTML (在线分析 Debot 页面结构)
            debug_path = "/app/data/card_debug.html"
            try:
                with open(debug_path, "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<pre style='white-space:pre-wrap;word-break:break-all;font-size:12px;background:#1a1a2e;color:#e0e0e0;padding:16px'>{html}</pre>".encode())
            except FileNotFoundError:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Debug HTML not yet generated. Wait for next scrape cycle.")
            except Exception as e:
                self.send_response(500)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """重定向 HTTP 日志到应用日志"""
        logger.debug(f"HealthCheck: {args[0]}")

    def do_POST(self):
        """处理 POST 请求"""
        if self.path == "/backtest-custom":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                params = json.loads(body)
                logger.info(f"收到自定义回测请求: {params.get('take_profit', '?')}")
                result = run_custom_backtest(params)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            except Exception as e:
                logger.error(f"自定义回测失败: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        """CORS 预检"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def start_health_server():
    """在后台线程启动健康检测 HTTP 服务"""
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("健康检测服务已启动: http://0.0.0.0:8080/health")
    return server


# ============================================================
# 主入口
# ============================================================

def main():
    """主函数：持续轮询采集"""
    logger.info("=" * 60)
    logger.info("Debot 信号采集服务启动")
    logger.info(f"时区: UTC-5 (America/New_York)")
    logger.info(f"采集间隔: {os.environ.get('POLL_INTERVAL_SECONDS', '60')}s")
    logger.info("=" * 60)

    # 初始化数据库连接池
    try:
        init_db_pool()
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        logger.error("请检查数据库环境变量配置 (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)")
        sys.exit(1)

    # 启动健康检测 HTTP 服务
    health_server = start_health_server()

    # ---- 后台行情拉取线程（独立于 n8n，双重保险防止数据断档） ----
    def _market_fetch_loop():
        """后台定时拉取行情，间隔 10~20 分钟随机，与 n8n 互为冗余"""
        logger.info("后台行情拉取线程已启动")
        while not _shutdown_flag:
            try:
                interval = random.randint(600, 1200)  # 10~20 分钟
                for _ in range(interval):
                    if _shutdown_flag:
                        return
                    time.sleep(1)
                if _shutdown_flag:
                    return
                logger.info("[后台] 开始行情拉取...")
                result = run_market_fetch()
                logger.info(f"[后台] 行情拉取完成: fetched={result.get('fetched',0)}, stored={result.get('stored',0)}")
            except Exception as e:
                logger.error(f"[后台] 行情拉取异常: {e}")

    market_thread = threading.Thread(target=_market_fetch_loop, daemon=True)
    market_thread.start()

    # 加载配置
    config = load_config()

    # 启动采集器
    scraper = DebotScraper(config)
    scraper.start()

    global _scraper
    _scraper = scraper

    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "90"))
    # 安全限制：最低 60 秒（降低 Cloudflare bot score 风险）
    if poll_interval < 60:
        logger.warning(f"采集间隔 {poll_interval}s 低于安全限制，已调整为 60s")
        poll_interval = 60

    round_count = 0

    try:
        while not _shutdown_flag:
            round_count += 1
            logger.info(f"--- 第 {round_count} 轮采集开始 ---")

            run_once(scraper)

            # 关闭页面释放 CPU（避免 Debot SPA WebSocket 持续渲染）
            scraper.close_page()

            # 随机间隔 60~180s，模拟真人刷新节奏，规避 Cloudflare 时序指纹
            actual_interval = random.randint(60, 180)
            logger.info(f"等待 {actual_interval}s 后下一轮...\n")

            # 分段 sleep，支持优雅退出
            for _ in range(actual_interval):
                if _shutdown_flag:
                    break
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("手动中断")
    finally:
        logger.info("正在关闭服务...")
        health_server.shutdown()
        scraper.stop()
        close_db_pool()
        logger.info("服务已安全退出")


if __name__ == "__main__":
    main()
