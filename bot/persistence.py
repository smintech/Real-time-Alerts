# bot/persistence.py - COMPLETE AND STANDALONE

import os
import json
import logging
import asyncio
from urllib.parse import quote_plus
from datetime import datetime, timezone, timedelta
import hashlib
import redis  # pip install redis
from psycopg2 import pool
import psycopg2.extras

from Utils.config import DB_URL
from Utils.utils import normalize_product_key  # Only needed for save_snapshot index update

logger = logging.getLogger(__name__)

# -----------------------
# Redis setup
# -----------------------
REDIS_URL = os.getenv("REDIS_URL")
_redis = None
REDIS_TTL_SECONDS = 24 * 3600
CHANNEL_REDIS_PREFIX = "channel:snap:"

def get_redis():
    global _redis
    if _redis is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL environment variable not set")
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis

# -----------------------
# Postgres pool
# -----------------------
_pg_pool = None
def get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        if not DB_URL:
            raise RuntimeError("DB_URL not set for Postgres")
        _pg_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DB_URL)
    return _pg_pool

# -----------------------
# Key helpers
# -----------------------
def _channel_key(ref: str) -> str:
    return CHANNEL_REDIS_PREFIX + quote_plus(ref)

def _url_key(url: str) -> str:
    return "snapshot:url:" + quote_plus(url)

def _product_site_key(product_key: str, site: str) -> str:
    return f"product:{product_key}:site:{site}"

def _product_sites_set_key(product_key: str) -> str:
    return f"product:{product_key}:sites"

# -----------------------
# DB table setup (product snapshots)
# -----------------------
def _db_ensure_table():
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

def _db_get_snapshot(url: str):
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

def _db_upsert_channel_snapshot(ref: str, snapshot: dict, expires_hours: int):
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        last_posted_at = snapshot.get("last_posted_at")
        last_posted_price = snapshot.get("last_posted_price")
        content_hash = snapshot.get("content_hash")  # ← NEW

        cur.execute("""
            INSERT INTO channel_snapshots
              (ref, site, title, url, current_price, raw, last_seen, expires_at, last_posted_at, last_posted_price, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ref) DO UPDATE SET
              site = EXCLUDED.site,
              title = EXCLUDED.title,
              url = EXCLUDED.url,
              current_price = EXCLUDED.current_price,
              raw = EXCLUDED.raw,
              last_seen = EXCLUDED.last_seen,
              expires_at = EXCLUDED.expires_at,
              last_posted_at = EXCLUDED.last_posted_at,
              last_posted_price = EXCLUDED.last_posted_price,
              content_hash = EXCLUDED.content_hash
        """, (
            ref,
            snapshot.get("site"),
            snapshot.get("title"),
            snapshot.get("url"),
            snapshot.get("current_price"),
            json.dumps(snapshot.get("raw") or {}),
            datetime.now(timezone.utc),
            expires_at,
            last_posted_at,
            last_posted_price,
            content_hash  # ← NEW parameter
        ))
        conn.commit()
        cur.close()
    finally:
        if conn:
            pool_conn.putconn(conn)


# -----------------------
# Channel snapshots table
# -----------------------
def _db_create_channel_table():
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
          last_posted_price NUMERIC,
          raw JSONB,
          last_seen TIMESTAMPTZ DEFAULT NOW(),
          expires_at TIMESTAMPTZ,
          last_posted_at TIMESTAMPTZ,
          content_hash TEXT           -- ← NEW: for change detection
        );
        """)
        cur.close()
        logger.info("Ensured channel_snapshots table exists")
    finally:
        if conn:
            pool_conn.putconn(conn)

def _migrate_channel_table():
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
        ALTER TABLE channel_snapshots
        ADD COLUMN IF NOT EXISTS last_posted_at TIMESTAMPTZ;
        """)
        logger.info("Migrated channel_snapshots: added last_posted_at")
    except Exception as exc:
        logger.exception("Channel table migration failed: %s", exc)
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_get_channel_snapshot(ref: str):
    conn = None
    try:
        pool_conn = get_pg_pool()
        conn = pool_conn.getconn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ref, site, title, url, current_price, raw, last_seen, expires_at, 
                   last_posted_at, last_posted_price, content_hash
            FROM channel_snapshots
            WHERE ref = %s
            LIMIT 1
        """, (ref,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        if row.get("expires_at") and row["expires_at"] <= datetime.now(timezone.utc):
            return None
        return dict(row)
    finally:
        if conn:
            pool_conn.putconn(conn)

def _db_delete_expired_channel_snapshots():
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

# -----------------------
# Async wrappers (non-blocking)
# -----------------------
async def load_last_snapshot(url: str):
    loop = asyncio.get_event_loop()
    key = _url_key(url)

    def _read_redis():
        r = get_redis()
        return r.get(key)

    try:
        val = await loop.run_in_executor(None, _read_redis)
        if val:
            try:
                return json.loads(val)
            except Exception:
                logger.exception("Corrupt Redis snapshot for %s", url)
    except Exception:
        logger.exception("Redis read failed for %s", url)

    try:
        return await loop.run_in_executor(None, _db_get_snapshot, url)
    except Exception:
        logger.exception("DB read failed for %s", url)
        return None

async def save_snapshot(snapshot: dict, redis_ttl: int = REDIS_TTL_SECONDS):
    loop = asyncio.get_event_loop()
    key = _url_key(snapshot.get("url"))

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

    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        logger.exception("Redis write failed for %s", snapshot.get("url"))

    try:
        await loop.run_in_executor(None, _db_upsert_snapshot, payload)
    except Exception:
        logger.exception("DB upsert failed for %s", snapshot.get("url"))

    # Update alternatives index
    try:
        product_key = normalize_product_key(snapshot)
        site = snapshot.get("site")
        if product_key and site:
            def _update_index():
                r = get_redis()
                site_key = _product_site_key(product_key, site)
                site_data = {
                    "current_price": snapshot.get("current_price"),
                    "url": snapshot.get("url")
                }
                r.set(site_key, json.dumps(site_data), ex=redis_ttl)
                r.sadd(_product_sites_set_key(product_key), site)
            await loop.run_in_executor(None, _update_index)
    except Exception:
        logger.exception("Failed updating alternatives index for %s", snapshot.get("url"))

async def save_channel_snapshot(ref: str, snapshot: dict, ttl_seconds: int = 48 * 3600, expires_hours: int = 48):
    loop = asyncio.get_event_loop()
    key = _channel_key(ref)

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
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "last_posted_at": snapshot.get("last_posted_at"),
                "last_posted_price": snapshot.get("last_posted_price")
            }), ex=ttl_seconds)
        except Exception:
            logger.exception("Redis channel write failed %s", ref)

    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        pass

    try:
        await loop.run_in_executor(None, _db_upsert_channel_snapshot, ref, snapshot, expires_hours)
    except Exception:
        logger.exception("DB channel upsert failed %s", ref)

async def load_channel_snapshot(ref: str):
    loop = asyncio.get_event_loop()
    key = _channel_key(ref)

    def _read_redis():
        try:
            r = get_redis()
            return r.get(key)
        except Exception:
            return None

    try:
        val = await loop.run_in_executor(None, _read_redis)
        if val:
            try:
                return json.loads(val)
            except Exception:
                logger.exception("Corrupt Redis channel %s", ref)
    except Exception:
        pass

    try:
        return await loop.run_in_executor(None, _db_get_channel_snapshot, ref)
    except Exception:
        logger.exception("DB channel read failed %s", ref)
        return None

async def delete_expired_channel_snapshots() -> int:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _db_delete_expired_channel_snapshots) or 0
    except Exception:
        logger.exception("Cleanup failed")
        return 0

async def fetch_alternatives(product_key: str, exclude_site: str = None) -> list:
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

def wipe_channel_snapshots_redis(dry_run: bool = False) -> int:
    """
    Wipes Redis channel snapshots only (channel:snap:*).

    dry_run=True → just prints keys, does NOT delete
    Returns number of keys matched.
    """
    r = get_redis()
    pattern = f"{CHANNEL_REDIS_PREFIX}*"
    keys = list(r.scan_iter(pattern))

    logger.warning("Channel snapshot Redis wipe requested | keys=%d | dry_run=%s", len(keys), dry_run)

    for key in keys:
        try:
            ttl = r.ttl(key)
            val = r.get(key)
            logger.warning("KEY=%s TTL=%s VAL=%s", key, ttl, val[:200] if val else None)
        except Exception:
            logger.warning("KEY=%s (unable to read value)", key)

    if keys and not dry_run:
        r.delete(*keys)
        logger.warning("Channel snapshot Redis wipe completed")

    return len(keys)

