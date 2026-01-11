import os
import time
import logging
import multiprocessing
import threading
import uvicorn
import signal
import sys
import traceback
import json
import hashlib
from urllib.parse import quote_plus, urlparse
from datetime import datetime, timezone
import redis        # pip install redis
import asyncio
from fastapi import FastAPI, HTTPException, Body
from bot.bot import run_bot  # blocking polling function
from utils.utils import scrape_product, compute_changes, calculate_deal_score, normalize_product_key, NoDataError, ApifyError

app = FastAPI()

# -----------------------
# Basic endpoints
# -----------------------
@app.get("/")
def home():
    return {"status": "API Online"}

@app.get("/health")
def health():
    """
    Simple health endpoint:
      - returns OK if process running,
      - returns details otherwise
    """
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

# ---------- Redis client (sync) ----------
REDIS_URL = os.getenv("REDIS_URL")
_redis = None

def get_redis():
    global _redis
    if _redis is None:
        # Use redis-py sync client. We will call it inside run_in_executor for async safety.
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis

def _url_key(url: str) -> str:
    # key for storing last snapshot by URL
    return "snapshot:url:" + quote_plus(url)

def _product_site_key(product_key: str, site: str) -> str:
    return f"product:{product_key}:site:{site}"

def _product_sites_set_key(product_key: str) -> str:
    return f"product:{product_key}:sites"


# Persistence API helpers (non-blocking in FastAPI)

async def load_last_snapshot(url: str) -> Optional[Dict]:
    """Return last snapshot stored for url or None."""
    loop = asyncio.get_event_loop()
    key = _url_key(url)
    def _load():
        r = get_redis()
        v = r.get(key)
        return json.loads(v) if v else None
    return await loop.run_in_executor(None, _load)

async def save_snapshot(url: str, product: Dict) -> None:
    """
    Save snapshot under URL and also under product_key/site mapping.
    product expected to have keys: 'url', 'site', 'current_price', 'title', 'raw', etc.
    """
    loop = asyncio.get_event_loop()

    # ensure product_key exists
    try:
        prod_key = normalize_product_key(product)
    except Exception:
        prod_key = None

    def _save():
        r = get_redis()
        now_iso = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "url": product.get("url"),
            "site": product.get("site"),
            "title": product.get("title"),
            "current_price": product.get("current_price"),
            "previous_price": product.get("previous_price"),
            "stock_status": product.get("stock_status"),
            "raw": product.get("raw"),
            "timestamp": now_iso,
        }
        # Save main URL snapshot
        r.set(_url_key(product.get("url")), json.dumps(snapshot))

        # Save per-product/per-site entry so alternatives can be fetched
        if prod_key:
            r.set(_product_site_key(prod_key, snapshot["site"]), json.dumps(snapshot))
            r.sadd(_product_sites_set_key(prod_key), snapshot["site"])

    await loop.run_in_executor(None, _save)


# Alternatives helper (returns list of {site, price})

async def fetch_alternatives(product_key: str, exclude_site: str = None) -> list:
    """
    Return other site snapshots for same product_key (quick, from Redis).
    """
    loop = asyncio.get_event_loop()

    def _fetch():
        r = get_redis()
        set_key = _product_sites_set_key(product_key)
        sites = r.smembers(set_key) or []
        results = []
        for s in sites:
            if s == exclude_site:
                continue
            v = r.get(_product_site_key(product_key, s))
            if not v:
                continue
            try:
                j = json.loads(v)
                results.append({"site": s, "price": j.get("current_price"), "url": j.get("url")})
            except Exception:
                continue
        return results

    return await loop.run_in_executor(None, _fetch)


# The /v1/track endpoint

@app.post("/v1/track")
async def track_product(payload: dict = Body(...)):
    url = payload.get("url") or payload.get("product_url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    # 1) Scrape product (run blocking scrape in executor)
    loop = asyncio.get_event_loop()
    try:
        product = await loop.run_in_executor(None, scrape_product, url)
    except NoDataError as e:
        # return a well-formed response but indicate no data
        raise HTTPException(status_code=404, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Scrape failed for %s", url)
        raise HTTPException(status_code=500, detail=f"scrape error: {e}")

    # 2) load previous snapshot
    old = await load_last_snapshot(url)

    # adapt old->shape used by compute_changes (it expects dict with 'current_price' and 'stock_status')
    old_minimal = None
    if old:
        old_minimal = {"current_price": old.get("current_price"), "stock_status": old.get("stock_status")}

    # 3) compute changes
    try:
        changes = compute_changes(old_minimal, product)
    except Exception:
        # be defensive
        changes = {"changed": True, "what_changed": [], "price_diff_percent": 0.0}

    # 4) deal scoring
    price_diff = changes.get("price_diff_percent", 0.0)
    try:
        deal_score = calculate_deal_score(price_diff)
    except Exception:
        deal_score = "none"

    severity = deal_score if deal_score in ("high", "medium") else "low"

    # 5) persist snapshot (async)
    try:
        await save_snapshot(url, product)
    except Exception:
        logger.exception("Failed saving snapshot for %s", url)

    # 6) gather alternatives using product_key
    try:
        product_key = normalize_product_key(product)
        alternatives = await fetch_alternatives(product_key, exclude_site=product.get("site"))
    except Exception:
        alternatives = []

    # 7) build canonical event JSON (your contract)
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
            "Buy now ,strong price drop" if deal_score == "high"
            else "Monitor price" if deal_score == "medium"
            else "No immediate action"
        ),
        "alternatives": alternatives,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    return response

# -----------------------
# Logging configuration
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("app")

# -----------------------
# Bot process manager
# -----------------------
class BotManager:
    def __init__(self, target_callable, max_restarts=5, restart_window_seconds=300):
        """
        Manages a blocking bot function running inside a child process.

        - target_callable: the function to run in child process (run_bot)
        - max_restarts: max restarts allowed within restart_window_seconds
        - restart_window_seconds: sliding window to limit restart storms
        """
        self.target = target_callable
        self.proc = None
        self._lock = threading.Lock()
        self.should_stop = threading.Event()

        # restart policy
        self.max_restarts = max_restarts
        self.restart_window = restart_window_seconds
        self.restart_timestamps = []  # times of recent restarts

    def start(self):
        """Start the bot process once (non-daemon so we can control it)."""
        with self._lock:
            if self.proc and self.proc.is_alive():
                logger.info("Bot process already running (pid=%s)", getattr(self.proc, "pid", None))
                return

            logger.info("Starting bot process...")
            # Use a concrete target wrapper to avoid child import surprises
            self.proc = multiprocessing.Process(target=self._run_target_wrapper)
            self.proc.start()
            logger.info("Started bot process (pid=%s)", self.proc.pid)

    def _run_target_wrapper(self):
        """
        Wrapper executed in subprocess to run user's blocking bot function.
        Any exception is caught and printed — the parent will see process exit code.
        """
        try:
            # Optionally set process title or extra logging here
            self.target()
        except Exception:
            # print stack trace inside child (helpful for debugging when logging is aggregated)
            traceback.print_exc()
            # Ensure child exits with non-zero code
            os._exit(1)
        # clean exit
        os._exit(0)

    def stop(self, timeout=5):
        """Request bot process stop."""
        with self._lock:
            if not self.proc:
                return
            if not self.proc.is_alive():
                logger.info("Bot process not alive; nothing to stop.")
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
        """
        Monitor thread. Restarts bot when it dies, according to restart policy.
        This runs in a separate daemon thread in the parent process.
        """
        logger.info("Bot monitor thread started")
        while not self.should_stop.is_set():
            with self._lock:
                proc = self.proc

            if proc is None:
                # not started yet — start it
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to start bot process")
                    time.sleep(poll_interval)
                    continue
                time.sleep(poll_interval)
                continue

            if not proc.is_alive():
                exitcode = proc.exitcode
                logger.error("Bot process exited (pid=%s, code=%s)", getattr(proc, "pid", None), exitcode)

                # Enforce restart policy: do not restart more than max_restarts within restart_window
                now = time.time()
                # remove old timestamps out of the restart window
                self.restart_timestamps = [t for t in self.restart_timestamps if now - t <= self.restart_window]

                if len(self.restart_timestamps) >= self.max_restarts:
                    logger.critical(
                        "Bot has restarted %s times within %s seconds — halting further restarts to avoid crash loop.",
                        len(self.restart_timestamps), self.restart_window
                    )
                    # mark should_stop so monitor exits and leaves uvicorn running (or caller can stop)
                    self.should_stop.set()
                    break

                # record restart and restart with exponential backoff
                self.restart_timestamps.append(now)
                backoff = min(2 ** len(self.restart_timestamps), 30)
                logger.info("Restarting bot process in %s seconds (attempt #%s)...", backoff, len(self.restart_timestamps))
                time.sleep(backoff)

                # start new process
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to restart bot process")
            else:
                # healthy; sleep a bit
                time.sleep(poll_interval)

        logger.info("Bot monitor thread exiting")

    def request_stop(self):
        """Signal the monitor to stop and terminate the child process."""
        logger.info("BotManager.request_stop called")
        self.should_stop.set()
        try:
            self.stop()
        except Exception:
            logger.exception("Error while stopping bot on request")

# -----------------------
# Global manager instance (keeps API surface small)
# -----------------------
bot_manager = BotManager(target_callable=run_bot, max_restarts=6, restart_window_seconds=300)

# -----------------------
# Signal handling for graceful shutdown
# -----------------------
def _on_terminate(signum, frame):
    logger.info("Received termination signal (%s). Shutting down...", signum)
    bot_manager.request_stop()
    # give some time for uvicorn to shutdown via normal lifecycle (if running)
    # then exit
    # Note: We don't call sys.exit here because signal handler executes in main thread's context.
    # Let the main thread proceed with shutdown.
signal.signal(signal.SIGINT, _on_terminate)
signal.signal(signal.SIGTERM, _on_terminate)

# -----------------------
# Startup / Shutdown events for FastAPI
# -----------------------
@app.on_event("startup")
def on_startup():
    # Start the bot + monitor thread
    try:
        logger.info("Application startup: starting bot process & monitor")
        bot_manager.start()
        monitor_thread = threading.Thread(target=bot_manager.monitor_and_restart, name="BotMonitor", daemon=True)
        monitor_thread.start()
        # attach monitor thread to manager for possible inspection
        bot_manager._monitor_thread = monitor_thread
    except Exception:
        logger.exception("Error during startup sequence")

@app.on_event("shutdown")
def _on_shutdown():
    LOG.info("App shutdown: stopping bot manager")
    try:
        bot_manager.request_stop()
        # give small grace
    except Exception:
        LOG.exception("Error during shutdown sequence")

# -----------------------
# Main runner (keeps your original pattern but safer)
# -----------------------
if __name__ == "__main__":
    # Optionally configure WORKER_PORT via env (useful on hosting)
    port = int(os.getenv("PORT", "8000"))

    # Brief startup log
    logger.info("Starting FastAPI + bot runner (uvicorn will block). PID=%s", os.getpid())

    # Run Uvicorn programmatically. If Uvicorn crashes, we make sure to stop bot.
    try:
        # Use uvicorn.run directly (blocks current thread). FastAPI lifecycle events will run (startup then shutdown).
        uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
    except Exception as e:
        logger.exception("Uvicorn runtime crashed: %s", e)
    finally:
        # Ensure bot process is stopped before exiting the program
        try:
            logger.info("Main process exiting; stopping bot manager")
            bot_manager.request_stop()
            # small delay to allow cleanup
            time.sleep(0.5)
        except Exception:
            logger.exception("Error while shutting down bot manager")
        logger.info("Exiting main process")