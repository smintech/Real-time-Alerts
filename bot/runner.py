# runner.py (replace your current app runner with this)
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
from datetime import datetime, timezone, timedelta
import redis        # pip install redis
import asyncio
from fastapi import FastAPI, HTTPException, Body
from bot.bot import run_bot  # blocking polling function
from utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    NoDataError,
    ApifyError,
)
from psycopg2 import pool, sql
import psycopg2.extras
from config import DB_URL

from typing import Optional, Dict, Any

# -----------------------
# Logging configuration (early so handlers can use it)
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
    """
    Simple health endpoint:
      - returns OK if process running,
      - returns details otherwise
    """
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

# ---------- Redis client (sync) ----------
REDIS_URL = os.getenv("REDIS_URL")
_redis = None
REDIS_TTL_SECONDS = 24 * 3600
CHANNEL_REDIS_PREFIX = "channel:snap:"

def get_redis():
    global _redis
    if _redis is None:
        # Use redis-py sync client. We will call it inside run_in_executor for async safety.
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis

_pg_pool = None
def get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        if not DB_URL:
            raise RuntimeError("DB_URL not set for Postgres fallback")
        # ThreadedConnectionPool from psycopg2
        _pg_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DB_URL)
    return _pg_pool

def _db_ensure_table():
    """Ensure product_snapshots table exists (safe to call on startup)."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_snapshots (
          url TEXT PRIMARY KEY,
          site TEXT,
          title TEXT,
          current_price NUMERIC,
          previous_price NUMERIC,
          stock_status TEXT,
          raw JSONB,
          last_checked_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.close()
        logger.info("Ensured product_snapshots table exists")
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_get_snapshot(url: str) -> Optional[Dict[str, Any]]:
    """Return latest snapshot row for url or None. Blocking."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT url, site, title, current_price, previous_price, stock_status, raw,
                   last_checked_at
            FROM product_snapshots
            WHERE url = %s
            LIMIT 1
        """, (url,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_upsert_snapshot(snapshot: Dict[str, Any]) -> None:
    """Insert or update snapshot for url. Blocking."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_snapshots
              (url, site, title, current_price, previous_price, stock_status, raw, last_checked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET
              site = EXCLUDED.site,
              title = EXCLUDED.title,
              current_price = EXCLUDED.current_price,
              previous_price = EXCLUDED.previous_price,
              stock_status = EXCLUDED.stock_status,
              raw = EXCLUDED.raw,
              last_checked_at = EXCLUDED.last_checked_at
        """, (
            snapshot.get("url"),
            snapshot.get("site"),
            snapshot.get("title"),
            snapshot.get("current_price"),
            snapshot.get("previous_price"),
            snapshot.get("stock_status"),
            json.dumps(snapshot.get("raw") or {}),
            datetime.now(timezone.utc)
        ))
        conn.commit()
        cur.close()
    finally:
        if conn:
            pool_conn.putconn(conn)

def _channel_key(ref: str) -> str:
    return CHANNEL_REDIS_PREFIX + quote_plus(ref)

def _url_key(url: str) -> str:
    # key for storing last snapshot by URL
    return "snapshot:url:" + quote_plus(url)

def _product_site_key(product_key: str, site: str) -> str:
    return f"product:{product_key}:site:{site}"

def _product_sites_set_key(product_key: str) -> str:
    return f"product:{product_key}:sites"

# -------------------------
# Channel table helpers (blocking)
# -------------------------
def _db_create_channel_table():
    """Create channel_snapshots table if missing."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_snapshots (
          ref TEXT PRIMARY KEY,
          site TEXT,
          title TEXT,
          url TEXT,
          current_price NUMERIC,
          raw JSONB,
          last_seen TIMESTAMPTZ DEFAULT NOW(),
          expires_at TIMESTAMPTZ
        );
        """)
        cur.close()
        logger.info("Ensured channel_snapshots table exists")
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_upsert_channel_snapshot(ref: str, snapshot: dict, expires_hours: int = 48):
    """Blocking DB upsert for a single channel snapshot."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        cur.execute("""
            INSERT INTO channel_snapshots (ref, site, title, url, current_price, raw, last_seen, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ref) DO UPDATE SET
              site = EXCLUDED.site,
              title = EXCLUDED.title,
              url = EXCLUDED.url,
              current_price = EXCLUDED.current_price,
              raw = EXCLUDED.raw,
              last_seen = EXCLUDED.last_seen,
              expires_at = EXCLUDED.expires_at
        """, (
            ref,
            snapshot.get("site"),
            snapshot.get("title"),
            snapshot.get("url"),
            snapshot.get("current_price"),
            json.dumps(snapshot.get("raw") or {}),
            datetime.now(timezone.utc),
            expires_at
        ))
        conn.commit()
        cur.close()
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_get_channel_snapshot(ref: str) -> Optional[Dict[str, Any]]:
    """Blocking DB read for channel snapshot. Returns None if expired/missing."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ref, site, title, url, current_price, raw, last_seen, expires_at
            FROM channel_snapshots
            WHERE ref = %s
            LIMIT 1
        """, (ref,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        # expire check
        exp = row.get("expires_at")
        if exp and exp <= datetime.now(timezone.utc):
            return None
        return dict(row)
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_delete_expired_channel_snapshots():
    """Blocking cleanup: remove DB rows where expires_at <= now()."""
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor()
        cur.execute("DELETE FROM channel_snapshots WHERE expires_at <= NOW()")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return deleted
    finally:
        if conn:
            pool_conn.putconn(conn)

# ---------- Async-friendly persistence API helpers (non-blocking) ----------
async def load_last_snapshot(url: str) -> Optional[Dict[str, Any]]:
    """
    Try Redis first (fast). If missing, fallback to Postgres and return that.
    Returns snapshot dict or None.
    """
    # try redis
    loop = asyncio.get_event_loop()
    key = _url_key(url)

    def _read_redis():
        r = get_redis()
        v = r.get(key)
        return v

    try:
        val = await loop.run_in_executor(None, _read_redis)
        if val:
            try:
                data = json.loads(val)
                # ensure last_checked_at is parsed to ISO string if present
                return data
            except Exception:
                # fallback to DB if Redis data corrupted
                logger.exception("Corrupt redis snapshot for %s", url)
                pass
    except Exception:
        # Redis might be down — fallback to DB
        logger.exception("Redis read failed for %s", url)

    # fallback to DB (blocking call in executor)
    try:
        db_row = await loop.run_in_executor(None, _db_get_snapshot, url)
        return db_row
    except Exception:
        logger.exception("DB read failed for %s", url)
        return None

async def save_snapshot(snapshot: Dict[str, Any], redis_ttl: int = REDIS_TTL_SECONDS) -> None:
    """
    Save snapshot to Redis (with TTL) and persist to Postgres (background).
    Snapshot should include: url, site, title, current_price, previous_price, stock_status, raw.
    """
    loop = asyncio.get_event_loop()
    key = _url_key(snapshot.get("url"))

    # prepare redis payload
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "url": snapshot.get("url"),
        "site": snapshot.get("site"),
        "title": snapshot.get("title"),
        "current_price": snapshot.get("current_price"),
        "previous_price": snapshot.get("previous_price"),
        "stock_status": snapshot.get("stock_status"),
        "raw": snapshot.get("raw") or {},
        "last_checked_at": now_iso
    }

    def _write_redis():
        r = get_redis()
        r.set(key, json.dumps(payload), ex=redis_ttl)

    # schedule redis write (fast)
    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        # Redis might be down — continue, DB is fallback
        logger.exception("Failed writing snapshot to Redis for %s", snapshot.get("url"))

    # always persist to Postgres in background (we don't block scheduler)
    try:
        await loop.run_in_executor(None, _db_upsert_snapshot, payload)
    except Exception:
        # DB failure — log at caller; not raising here for resilience
        logger.exception("Failed to persist snapshot to Postgres for %s", snapshot.get("url"))

# ---------- Channel snapshot async wrappers ----------
async def save_channel_snapshot(ref: str, snapshot: dict, ttl_seconds: int = 48 * 3600):
    """
    Save channel snapshot to Redis (ttl) and persist to Postgres as fallback.
    `ref` is a unique identifier you use for channel entries (e.g. the URL).
    """
    loop = asyncio.get_event_loop()
    key = _channel_key(ref)

    # Redis write
    def _write_redis():
        try:
            r = get_redis()
            r.set(key, json.dumps({
                "ref": ref,
                "site": snapshot.get("site"),
                "title": snapshot.get("title"),
                "url": snapshot.get("url"),
                "current_price": snapshot.get("current_price"),
                "raw": snapshot.get("raw") or {},
                "last_seen": datetime.now(timezone.utc).isoformat()
            }), ex=ttl_seconds)
        except Exception:
            logger.exception("Redis write failed for channel snapshot %s", ref)

    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        logger.exception("Redis executor error while saving channel snapshot %s", ref)

    # DB upsert (background)
    try:
        await loop.run_in_executor(None, _db_upsert_channel_snapshot, ref, snapshot)
    except Exception:
        logger.exception("DB upsert failed for channel snapshot %s", ref)

async def load_channel_snapshot(ref: str) -> Optional[Dict[str, Any]]:
    """Load channel snapshot from Redis first, fallback to DB. Returns None if expired/missing."""
    loop = asyncio.get_event_loop()
    key = _channel_key(ref)

    def _read_redis():
        try:
            r = get_redis()
            v = r.get(key)
            return v
        except Exception:
            return None

    try:
        val = await loop.run_in_executor(None, _read_redis)
        if val:
            try:
                return json.loads(val)
            except Exception:
                logger.exception("Corrupt Redis channel snapshot %s", ref)
                # fall back to DB below
    except Exception:
        logger.exception("Redis read executor error for channel snapshot %s", ref)

    # fallback to DB
    try:
        row = await loop.run_in_executor(None, _db_get_channel_snapshot, ref)
        return row
    except Exception:
        logger.exception("DB fallback read error for channel snapshot %s", ref)
        return None

async def delete_expired_channel_snapshots() -> int:
    """Run DB cleanup to delete expired channel snapshots. Returns number deleted."""
    loop = asyncio.get_event_loop()
    try:
        deleted = await loop.run_in_executor(None, _db_delete_expired_channel_snapshots)
        return deleted or 0
    except Exception:
        logger.exception("Failed deleting expired channel snapshots")
        return 0

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

# -----------------------
# The /v1/track endpoint
# -----------------------
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

    # 5) persist snapshot (async) -- CORRECTED: pass product dict (not url+product)
    try:
        await save_snapshot(product)
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
# Bot process manager (unchanged)
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
                now_ts = time.time()
                # remove old timestamps out of the restart window
                self.restart_timestamps = [t for t in self.restart_timestamps if now_ts - t <= self.restart_window]

                if len(self.restart_timestamps) >= self.max_restarts:
                    logger.critical(
                        "Bot has restarted %s times within %s seconds — halting further restarts to avoid crash loop.",
                        len(self.restart_timestamps), self.restart_window
                    )
                    # mark should_stop so monitor exits and leaves uvicorn running (or caller can stop)
                    self.should_stop.set()
                    break

                # record restart and restart with exponential backoff
                self.restart_timestamps.append(now_ts)
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
# Background cleanup task for channel snapshots
# -----------------------
_cleanup_task = None
async def _channel_cleanup_loop():
    """Periodically delete expired channel snapshots (once every 24 hours)."""
    while True:
        try:
            deleted = await delete_expired_channel_snapshots()
            if deleted:
                logger.info("Deleted %d expired channel snapshots", deleted)
        except Exception:
            logger.exception("Channel cleanup loop error")
        # sleep 24 hours
        await asyncio.sleep(24 * 3600)

# -----------------------
# Startup / Shutdown events for FastAPI
# -----------------------
@app.on_event("startup")
def on_startup():
    # Ensure DB tables exist
    try:
        _db_ensure_table()
    except Exception:
        logger.exception("Failed ensuring product_snapshots table")

    try:
        _db_create_channel_table()
    except Exception:
        logger.exception("Failed ensuring channel_snapshots table")

    # Start channel cleanup loop (background)
    global _cleanup_task
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # schedule background task
            _cleanup_task = asyncio.ensure_future(_channel_cleanup_loop())
        else:
            # if not running yet, create a thread that will start cleanup after loop runs
            pass
    except Exception:
        logger.exception("Failed to start channel cleanup task")

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
    logger.info("App shutdown: stopping bot manager and cleanup task")
    try:
        bot_manager.request_stop()
    except Exception:
        logger.exception("Error while stopping bot manager")

    # cancel cleanup task if running
    try:
        if _cleanup_task and not _cleanup_task.done():
            _cleanup_task.cancel()
    except Exception:
        logger.exception("Error cancelling cleanup task")

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