# runner.py - FINAL COMPLETE VERSION (works with bot/persistence.py)

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

# Critical: Import run_bot at the very top for multiprocessing child process safety
from bot.bot import run_bot

# Import all needed persistence functions (standalone module - no cycles)
from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    delete_expired_channel_snapshots,
    fetch_alternatives,
    _db_ensure_table,
    _db_create_channel_table,
    _migrate_channel_table,
)

from Utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    NoDataError,
    ApifyError,
)
from typing import Optional

# -----------------------
# Logging
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("app")

app = FastAPI()

# -----------------------
# Basic endpoints
# -----------------------
@app.get("/")
def home():
    return {"status": "API Online"}

@app.get("/health")
def health():
    return {"status": "ok", "time": asyncio.get_event_loop().time()}

# -----------------------
# /v1/track endpoint (uses persistence)
# -----------------------
@app.post("/v1/track")
async def track_product(payload: dict = Body(...)):
    url = payload.get("url") or payload.get("product_url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    loop = asyncio.get_event_loop()
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
            "Buy now! Strong price drop" if deal_score == "high"
            else "Worth monitoring" if deal_score == "medium"
            else "No action needed"
        ),
        "alternatives": alternatives,
        "timestamp": asyncio.get_event_loop().time()
    }

    return response

# -----------------------
# BotManager class
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
                logger.info("Bot process already running (pid=%s)", getattr(self.proc, "pid", None))
                return

            logger.info("Starting bot process...")
            self.proc = multiprocessing.Process(target=self._run_target_wrapper)
            self.proc.start()
            logger.info("Started bot process (pid=%s)", self.proc.pid)

    def _run_target_wrapper(self):
        try:
            self.target()
        except Exception:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)

    def stop(self, timeout=5):
        with self._lock:
            if not self.proc or not self.proc.is_alive():
                return
            logger.info("Terminating bot process (pid=%s)...", self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.join(timeout)
                if self.proc.is_alive():
                    logger.warning("Bot did not exit gracefully; killing...")
                    self.proc.kill()
                    self.proc.join(1)
            except Exception as e:
                logger.exception("Error terminating bot process: %s", e)
            finally:
                logger.info("Bot process stopped.")
                self.proc = None

    def monitor_and_restart(self, poll_interval=2):
        logger.info("Bot monitor thread started")
        while not self.should_stop.is_set():
            with self._lock:
                proc = self.proc
            if proc is None:
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to start bot process")
                time.sleep(poll_interval)
                continue
            if not proc.is_alive():
                exitcode = proc.exitcode
                logger.error("Bot process exited (pid=%s, code=%s)", getattr(proc, "pid", None), exitcode)
                now_ts = time.time()
                self.restart_timestamps = [t for t in self.restart_timestamps if now_ts - t <= self.restart_window]
                if len(self.restart_timestamps) >= self.max_restarts:
                    logger.critical("Too many restarts - stopping attempts")
                    self.should_stop.set()
                    break
                self.restart_timestamps.append(now_ts)
                backoff = min(2 ** len(self.restart_timestamps), 30)
                logger.info("Restarting in %s seconds...", backoff)
                time.sleep(backoff)
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to restart bot process")
            else:
                time.sleep(poll_interval)
        logger.info("Bot monitor thread exiting")

    def request_stop(self):
        logger.info("BotManager.request_stop called")
        self.should_stop.set()
        try:
            self.stop()
        except Exception:
            logger.exception("Error while stopping bot")

# Global manager
bot_manager = BotManager(target_callable=run_bot)

# Signal handling
def _on_terminate(signum, frame):
    logger.info("Received signal %s - shutting down", signum)
    bot_manager.request_stop()

signal.signal(signal.SIGINT, _on_terminate)
signal.signal(signal.SIGTERM, _on_terminate)

# Cleanup task
_cleanup_task = None
async def _channel_cleanup_loop():
    while True:
        try:
            deleted = await delete_expired_channel_snapshots()
            if deleted:
                logger.info("Cleaned %d expired channel snapshots", deleted)
        except Exception:
            logger.exception("Cleanup error")
        await asyncio.sleep(24 * 3600)

# Startup / Shutdown
@app.on_event("startup")
async def on_startup():
    try:
        _db_ensure_table()
        _db_create_channel_table()
        _migrate_channel_table()
    except Exception as e:
        logger.exception("DB setup failed: %s", e)

    global _cleanup_task
    _cleanup_task = asyncio.create_task(_channel_cleanup_loop())

    logger.info("Starting bot process & monitor")
    bot_manager.start()
    monitor_thread = threading.Thread(target=bot_manager.monitor_and_restart, daemon=True)
    monitor_thread.start()
    bot_manager._monitor_thread = monitor_thread

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutdown: stopping bot and cleanup")
    bot_manager.request_stop()
    if _cleanup_task:
        _cleanup_task.cancel()

# Main
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting uvicorn on port %s", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())