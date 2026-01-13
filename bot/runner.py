# runner.py — FINAL WORKING VERSION (2026 style)

import os
import time
import logging
import multiprocessing
import threading
import uvicorn
import signal
import sys
import traceback
import asyncio
from fastapi import FastAPI, HTTPException, Body

# ──────────────────────────────────────────────────────────────
# CRITICAL: Import run_bot FIRST — multiprocessing child needs it early
from bot.bot import run_bot

# Import persistence & utils (adjust paths if needed)
from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    delete_expired_channel_snapshots,
    fetch_alternatives,
    _db_ensure_table,
    _db_create_channel_table,
)
from Utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    NoDataError,
    ApifyError,
)

# -----------------------
# Logging (early!)
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("runner")

app = FastAPI(title="Naija Price Alerts API")

# -----------------------
# Basic endpoints
# -----------------------
@app.get("/")
async def home():
    return {"status": "API Online", "bot_running": bot_manager.proc is not None and bot_manager.proc.is_alive()}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "bot_alive": bot_manager.proc.is_alive() if bot_manager.proc else False
    }

# -----------------------
# /v1/track endpoint
# -----------------------
@app.post("/v1/track")
async def track_product(payload: dict = Body(...)):
    url = payload.get("url") or payload.get("product_url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    loop = asyncio.get_running_loop()
    try:
        product = await loop.run_in_executor(None, scrape_product, url)
    except NoDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Scrape failed for %s", url)
        raise HTTPException(status_code=500, detail=f"scrape error: {e}")

    old = await load_last_snapshot(url)
    old_minimal = {"current_price": old.get("current_price"), "stock_status": old.get("stock_status")} if old else None

    try:
        changes = compute_changes(old_minimal, product)
    except Exception:
        changes = {"changed": True, "what_changed": [], "price_diff_percent": 0.0}

    price_diff = changes.get("price_diff_percent", 0.0)
    try:
        deal_score = calculate_deal_score(price_diff)
    except Exception:
        deal_score = "none"

    severity = deal_score if deal_score in ("high", "medium") else "low"

    try:
        await save_snapshot(product)
    except Exception:
        logger.exception("Failed saving snapshot for %s", url)

    try:
        product_key = normalize_product_key(product)
        alternatives = await fetch_alternatives(product_key, exclude_site=product.get("site"))
    except Exception:
        alternatives = []

    response = {
        "product_url": product.get("url") or url,
        "title": product.get("title"),
        "current_price": product.get("current_price"),
        "previous_price": old.get("current_price") if old else product.get("previous_price"),
        "changed": bool(changes.get("changed")),
        "what_changed": changes.get("what_changed", []),
        "stock_status": product.get("stock_status", "unknown"),
        "deal_score": deal_score,
        "severity": severity,
        "suggested_action": (
            "Buy now — strong price drop" if deal_score == "high"
            else "Monitor price" if deal_score == "medium"
            else "No immediate action"
        ),
        "alternatives": alternatives,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    return response

# -----------------------
# Bot Manager (unchanged — but now we see logs!)
# -----------------------
class BotManager:
    def __init__(self, target_callable, max_restarts=6, restart_window_seconds=300):
        self.target = target_callable
        self.proc = None
        self._lock = threading.Lock()
        self.should_stop = threading.Event()
        self.max_restarts = max_restarts
        self.restart_window = restart_window_seconds
        self.restart_timestamps = []

    def start(self):
        with self._lock:
            if self.proc and self.proc.is_alive():
                logger.info("Bot already running (pid=%s)", self.proc.pid)
                return

            logger.info("Launching bot in child process...")
            self.proc = multiprocessing.Process(target=self._run_target_wrapper, name="TelegramBotProcess")
            self.proc.start()
            logger.info("Bot process started — PID: %s", self.proc.pid)

    def _run_target_wrapper(self):
        logger.info("Child process started — running bot polling...")
        try:
            self.target()
        except Exception as e:
            logger.exception("Bot crashed in child process!")
            traceback.print_exc()
            os._exit(1)
        logger.info("Bot polling exited cleanly")
        os._exit(0)

    def stop(self, timeout=8):
        with self._lock:
            if not self.proc or not self.proc.is_alive():
                return
            logger.info("Stopping bot process (pid=%s)...", self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.join(timeout)
                if self.proc.is_alive():
                    logger.warning("Bot didn't stop gracefully — killing")
                    self.proc.kill()
                    self.proc.join(2)
            except Exception as e:
                logger.exception("Error stopping bot: %s", e)
            finally:
                self.proc = None
                logger.info("Bot process stopped")

    def monitor_and_restart(self, poll_interval=3):
        logger.info("Bot monitor thread started")
        while not self.should_stop.is_set():
            with self._lock:
                proc = self.proc

            if proc is None:
                logger.info("No bot process — starting one")
                self.start()
                time.sleep(poll_interval)
                continue

            if not proc.is_alive():
                exitcode = proc.exitcode
                logger.error("Bot process died (exit code %s)", exitcode)

                now = time.time()
                self.restart_timestamps = [t for t in self.restart_timestamps if now - t <= self.restart_window]

                if len(self.restart_timestamps) >= self.max_restarts:
                    logger.critical("Too many restarts (%d) — giving up to prevent crash loop", len(self.restart_timestamps))
                    self.should_stop.set()
                    break

                self.restart_timestamps.append(now)
                backoff = min(2 ** len(self.restart_timestamps), 60)
                logger.info("Restarting bot in %d seconds (attempt %d)...", backoff, len(self.restart_timestamps))
                time.sleep(backoff)
                self.start()

            time.sleep(poll_interval)

        logger.info("Monitor thread exiting")

    def request_stop(self):
        logger.info("Requesting bot shutdown")
        self.should_stop.set()
        self.stop()

# Global bot manager
bot_manager = BotManager(target_callable=run_bot, max_restarts=6, restart_window_seconds=300)

# Signal handlers
def _on_terminate(signum, frame):
    logger.info("Received signal %s — initiating shutdown", signum)
    bot_manager.request_stop()

signal.signal(signal.SIGINT, _on_terminate)
signal.signal(signal.SIGTERM, _on_terminate)

# -----------------------
# Startup / Shutdown
# -----------------------
@app.on_event("startup")
def on_startup():
    logger.info("FastAPI startup — preparing bot & DB")
    try:
        _db_ensure_table()
        _db_create_channel_table()
    except Exception as e:
        logger.exception("DB setup failed: %s", e)

    logger.info("Launching bot monitor thread")
    monitor_thread = threading.Thread(
        target=bot_manager.monitor_and_restart,
        name="BotMonitor",
        daemon=True
    )
    monitor_thread.start()
    bot_manager._monitor_thread = monitor_thread

@app.on_event("shutdown")
def on_shutdown():
    logger.info("FastAPI shutdown — stopping bot")
    bot_manager.request_stop()

# -----------------------
# Main entry point
# -----------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting uvicorn server on 0.0.0.0:%d (bot runs in child process)", port)

    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level=LOG_LEVEL.lower(),
            workers=1,  # single worker — enough for MVP + bot child
        )
    except Exception as e:
        logger.exception("Uvicorn crashed: %s", e)
    finally:
        logger.info("Main process exiting — stopping bot manager")
        bot_manager.request_stop()
        time.sleep(1)  # give child time to terminate
        logger.info("Shutdown complete")