# persistence.py - FIXED POSTGRES SSL CONNECTION FOR NEON + IMPROVED CLEANUP

import os
import json
import logging
import asyncio
import psycopg2
import psycopg2.pool
import psycopg2.extras
from urllib.parse import quote_plus
from datetime import datetime, timezone, timedelta
import hashlib
import redis  # pip install redis
from typing import Optional, Dict, List, Any
from decimal import Decimal  # For handling Postgres NUMERIC

from Utils.config import DB_URL
from Utils.utils import normalize_product_key

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

REDIS_URL = os.getenv("REDIS_URL")
REDIS_TTL_SECONDS = 48 * 3600  # 48 hours for fast dedup cache
POSTGRES_RETENTION_DAYS = int(os.getenv("POSTGRES_RETENTION_DAYS", "30"))  # 30 days default

# Neon/PostgreSQL connection settings
POSTGRES_CONNECTION_PARAMS = {
    "keepalives": 1,  # Enable TCP keepalives
    "keepalives_idle": 30,  # Start after 30 seconds idle
    "keepalives_interval": 10,  # Send every 10 seconds
    "keepalives_count": 5,  # Max 5 keepalive packets
    "connect_timeout": 10,  # Connection timeout
    "sslmode": "require",  # Neon requires SSL
}

# Redis key prefixes
CHANNEL_SNAP_PREFIX = "channel:snap:"          # Full snapshot
CHANNEL_DEDUP_PREFIX = "channel:dedup:"        # Content hash for dedup
CHANNEL_RECENT_PREFIX = "channel:recent:"      # Recent post timestamps (sorted set)
PRODUCT_SNAP_PREFIX = "snapshot:url:"          # Product snapshots
PRODUCT_SITE_PREFIX = "product:"               # Product alternatives index

# ═══════════════════════════════════════════════════════════════════════════
# DECIMAL/FLOAT CONVERTER
# ═══════════════════════════════════════════════════════════════════════════

def _convert_decimals(obj):
    """Recursively convert Decimal to float in dict/list."""
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(i) for i in obj]
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj

# ═══════════════════════════════════════════════════════════════════════════
# REDIS CONNECTION
# ═══════════════════════════════════════════════════════════════════════════

_redis = None

def get_redis():
    """Get or create Redis connection."""
    global _redis
    if _redis is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL environment variable not set")
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis

# ═══════════════════════════════════════════════════════════════════════════
# POSTGRES CONNECTION POOL (FIXED FOR NEON SSL)
# ═══════════════════════════════════════════════════════════════════════════

_pg_pool = None

def get_pg_pool():
    """Get or create Postgres connection pool with Neon SSL resilience."""
    global _pg_pool
    if _pg_pool is None:
        if not DB_URL:
            raise RuntimeError("DB_URL not set for Postgres")
        
        # Parse DB_URL and add connection parameters
        import urllib.parse
        parsed = urllib.parse.urlparse(DB_URL)
        query_params = urllib.parse.parse_qs(parsed.query)
        
        # Add Neon-specific connection parameters
        for key, value in POSTGRES_CONNECTION_PARAMS.items():
            if key not in query_params:
                query_params[key] = [str(value)]
        
        # Reconstruct URL with parameters
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        enhanced_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
        
        # Create connection pool with resilience
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=enhanced_url,
            **POSTGRES_CONNECTION_PARAMS
        )
        logger.info("PostgreSQL connection pool created with Neon SSL settings")
    return _pg_pool

def get_pg_connection():
    """Get connection from pool with automatic retry on SSL failure."""
    pool_obj = get_pg_pool()
    
    for attempt in range(3):
        try:
            conn = pool_obj.getconn()
            # Test connection
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.commit()
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"PostgreSQL connection attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(1)
            else:
                raise
        except Exception as e:
            logger.error(f"Unexpected PostgreSQL error: {e}")
            raise

def return_pg_connection(conn):
    """Return connection to pool with proper cleanup."""
    if conn:
        try:
            # Rollback any pending transaction
            conn.rollback()
            pool_obj = get_pg_pool()
            pool_obj.putconn(conn)
        except Exception as e:
            logger.warning(f"Error returning connection to pool: {e}")
            try:
                conn.close()
            except:
                pass

# ═══════════════════════════════════════════════════════════════════════════
# KEY HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _channel_snap_key(ref: str) -> str:
    """Redis key for channel snapshot."""
    return CHANNEL_SNAP_PREFIX + quote_plus(ref)

def _channel_dedup_key(content_hash: str) -> str:
    """Redis key for content hash dedup."""
    return CHANNEL_DEDUP_PREFIX + content_hash

def _channel_recent_key(ref: str) -> str:
    """Redis sorted set key for recent posts."""
    return CHANNEL_RECENT_PREFIX + quote_plus(ref) + ":posts"

def _url_key(url: str) -> str:
    """Redis key for product snapshot by URL."""
    return PRODUCT_SNAP_PREFIX + quote_plus(url)

def _product_site_key(product_key: str, site: str) -> str:
    """Redis key for product alternative by site."""
    return f"{PRODUCT_SITE_PREFIX}{product_key}:site:{site}"

def _product_sites_set_key(product_key: str) -> str:
    """Redis set key for all sites selling this product."""
    return f"{PRODUCT_SITE_PREFIX}{product_key}:sites"

def compute_content_hash(data: Dict[str, Any]) -> str:
    """
    Generate a stable hash for deduplication.
    Uses title, price, and key raw data fields.
    """
    content_parts = [
        str(data.get("title", "")),
        str(data.get("current_price", "")),
        str(data.get("item_count", "")),
    ]
    
    # Add raw data if available
    raw = data.get("raw", {})
    if isinstance(raw, dict):
        # For school news, include first snippet
        if "items" in raw and isinstance(raw["items"], list) and raw["items"]:
            content_parts.append(str(raw["items"][0].get("title", "")))
            content_parts.append(str(raw["items"][0].get("snippet", ""))[:100])
    
    content_str = "|".join(content_parts)
    return hashlib.sha256(content_str.encode()).hexdigest()[:16]

# ═══════════════════════════════════════════════════════════════════════════
# DATABASE SCHEMA INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════

def _db_ensure_product_table():
    """Create product_snapshots table if not exists."""
    conn = None
    try:
        conn = get_pg_connection()
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
          last_checked_at TIMESTAMPTZ DEFAULT NOW(),
          created_at TIMESTAMPTZ DEFAULT NOW()
        );
        
        CREATE INDEX IF NOT EXISTS idx_product_snapshots_site 
          ON product_snapshots(site);
        CREATE INDEX IF NOT EXISTS idx_product_snapshots_last_checked 
          ON product_snapshots(last_checked_at);
        """)
        cur.close()
        logger.info("Ensured product_snapshots table exists")
    finally:
        return_pg_connection(conn)

def _db_ensure_channel_table():
    """Create channel_snapshots table with all fields."""
    conn = None
    try:
        conn = get_pg_connection()
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
          content_hash TEXT,
          item_count INTEGER,
          created_at TIMESTAMPTZ DEFAULT NOW()
        );
        
        CREATE INDEX IF NOT EXISTS idx_channel_snapshots_expires 
          ON channel_snapshots(expires_at);
        CREATE INDEX IF NOT EXISTS idx_channel_snapshots_content_hash 
          ON channel_snapshots(content_hash);
        CREATE INDEX IF NOT EXISTS idx_channel_snapshots_last_posted 
          ON channel_snapshots(last_posted_at);
        CREATE INDEX IF NOT EXISTS idx_channel_snapshots_site 
          ON channel_snapshots(site);
        """)
        cur.close()
        logger.info("Ensured channel_snapshots table exists")
    finally:
        return_pg_connection(conn)

def _db_ensure_post_history_table():
    """Create channel_post_history table for tracking all posts."""
    conn = None
    try:
        conn = get_pg_connection()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_post_history (
          id SERIAL PRIMARY KEY,
          ref TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          posted_at TIMESTAMPTZ DEFAULT NOW(),
          price NUMERIC,
          item_count INTEGER,
          title TEXT,
          expires_at TIMESTAMPTZ
        );
        
        CREATE INDEX IF NOT EXISTS idx_post_history_ref 
          ON channel_post_history(ref);
        CREATE INDEX IF NOT EXISTS idx_post_history_content_hash 
          ON channel_post_history(content_hash);
        CREATE INDEX IF NOT EXISTS idx_post_history_posted_at 
          ON channel_post_history(posted_at);
        CREATE INDEX IF NOT EXISTS idx_post_history_expires 
          ON channel_post_history(expires_at);
        """)
        cur.close()
        logger.info("Ensured channel_post_history table exists")
    finally:
        return_pg_connection(conn)

def initialize_database():
    """Initialize all database tables and indexes."""
    _db_ensure_product_table()
    _db_ensure_channel_table()
    _db_ensure_post_history_table()
    logger.info("Database initialization complete")

# ═══════════════════════════════════════════════════════════════════════════
# PRODUCT SNAPSHOTS (DUAL-LAYER)
# ═══════════════════════════════════════════════════════════════════════════

def _db_get_product_snapshot(url: str) -> Optional[Dict]:
    """Get product snapshot from Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT url, site, title, current_price, previous_price, 
                   stock_status, raw, last_checked_at
            FROM product_snapshots
            WHERE url = %s
            LIMIT 1
        """, (url,))
        row = cur.fetchone()
        cur.close()
        conn.commit()
        return _convert_decimals(dict(row)) if row else None
    finally:
        return_pg_connection(conn)

def _db_upsert_product_snapshot(snapshot: dict):
    """Insert or update product snapshot in Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
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
            snapshot.get("last_checked_at", datetime.now(timezone.utc))
        ))
        conn.commit()
        cur.close()
    finally:
        return_pg_connection(conn)

async def load_last_snapshot(url: str) -> Optional[Dict]:
    """
    Load product snapshot (Redis first, Postgres fallback).
    """
    loop = asyncio.get_event_loop()
    key = _url_key(url)

    # Try Redis first (fast cache)
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

    # Fallback to Postgres
    try:
        return await loop.run_in_executor(None, _db_get_product_snapshot, url)
    except Exception:
        logger.exception("DB read failed for %s", url)
        return None

async def save_snapshot(snapshot: dict, redis_ttl: int = REDIS_TTL_SECONDS):
    """
    Save product snapshot to both Redis and Postgres.
    Also updates alternatives index.
    """
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

    # Write to Redis (async)
    def _write_redis():
        r = get_redis()
        r.set(key, json.dumps(payload), ex=redis_ttl)

    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        logger.exception("Redis write failed for %s", snapshot.get("url"))

    # Write to Postgres (async)
    try:
        await loop.run_in_executor(None, _db_upsert_product_snapshot, payload)
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
                r.expire(_product_sites_set_key(product_key), redis_ttl)
            await loop.run_in_executor(None, _update_index)
    except Exception:
        logger.exception("Failed updating alternatives index for %s", snapshot.get("url"))

# ═══════════════════════════════════════════════════════════════════════════
# CHANNEL SNAPSHOTS (DUAL-LAYER WITH DEDUPLICATION)
# ═══════════════════════════════════════════════════════════════════════════

def _db_get_channel_snapshot(ref: str) -> Optional[Dict]:
    """Get channel snapshot from Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ref, site, title, url, current_price, raw, last_seen, 
                   expires_at, last_posted_at, last_posted_price, 
                   content_hash, item_count
            FROM channel_snapshots
            WHERE ref = %s
            LIMIT 1
        """, (ref,))
        row = cur.fetchone()
        cur.close()
        conn.commit()
        
        if not row:
            return None
        
        # Check expiration
        if row.get("expires_at") and row["expires_at"] <= datetime.now(timezone.utc):
            return None
        
        return _convert_decimals(dict(row))
    finally:
        return_pg_connection(conn)

def _db_upsert_channel_snapshot(ref: str, snapshot: dict, expires_hours: int):
    """Insert or update channel snapshot in Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        
        cur.execute("""
            INSERT INTO channel_snapshots
              (ref, site, title, url, current_price, raw, last_seen, expires_at, 
               last_posted_at, last_posted_price, content_hash, item_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
              content_hash = EXCLUDED.content_hash,
              item_count = EXCLUDED.item_count
        """, (
            ref,
            snapshot.get("site"),
            snapshot.get("title"),
            snapshot.get("url"),
            snapshot.get("current_price"),
            json.dumps(snapshot.get("raw") or {}),
            datetime.now(timezone.utc),
            expires_at,
            snapshot.get("last_posted_at"),
            snapshot.get("last_posted_price"),
            snapshot.get("content_hash"),
            snapshot.get("item_count")
        ))
        conn.commit()
        cur.close()
    finally:
        return_pg_connection(conn)

def _db_record_post(ref: str, snapshot: dict, retention_days: int):
    """Record a post in history table."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        
        expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)
        
        cur.execute("""
            INSERT INTO channel_post_history
              (ref, content_hash, posted_at, price, item_count, title, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            ref,
            snapshot.get("content_hash"),
            datetime.now(timezone.utc),
            snapshot.get("current_price"),
            snapshot.get("item_count"),
            snapshot.get("title"),
            expires_at
        ))
        conn.commit()
        cur.close()
    finally:
        return_pg_connection(conn)

def _db_check_duplicate_content(content_hash: str, lookback_hours: int = 48) -> bool:
    """Check if content hash exists in recent history."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        
        cur.execute("""
            SELECT 1 FROM channel_post_history
            WHERE content_hash = %s AND posted_at > %s
            LIMIT 1
        """, (content_hash, cutoff))
        
        result = cur.fetchone()
        cur.close()
        conn.commit()
        return result is not None
    finally:
        return_pg_connection(conn)

async def load_channel_snapshot(ref: str) -> Optional[Dict]:
    """
    Load channel snapshot (Redis first, Postgres fallback).
    """
    loop = asyncio.get_event_loop()
    key = _channel_snap_key(ref)

    # Try Redis first
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
                logger.exception("Corrupt Redis channel snapshot %s", ref)
    except Exception:
        pass

    # Fallback to Postgres
    try:
        return await loop.run_in_executor(None, _db_get_channel_snapshot, ref)
    except Exception:
        logger.exception("DB channel read failed %s", ref)
        return None

async def save_channel_snapshot(
    ref: str, 
    snapshot: dict, 
    ttl_seconds: int = REDIS_TTL_SECONDS,
    expires_hours: int = POSTGRES_RETENTION_DAYS * 24
):
    """
    Save channel snapshot to both Redis and Postgres.
    
    Redis layers:
    1. Full snapshot (channel:snap:{ref})
    2. Content hash for dedup (channel:dedup:{hash})
    3. Recent posts sorted set (channel:recent:{ref}:posts)
    
    Postgres:
    1. channel_snapshots (current state)
    2. channel_post_history (all posts for analytics)
    """
    loop = asyncio.get_event_loop()
    
    # Compute content hash if not provided
    if "content_hash" not in snapshot:
        snapshot["content_hash"] = compute_content_hash(snapshot)
    
    content_hash = snapshot["content_hash"]
    now = datetime.now(timezone.utc)
    
    # Prepare snapshot with timestamps
    full_snapshot = {
        "ref": ref,
        "site": snapshot.get("site"),
        "title": snapshot.get("title"),
        "url": snapshot.get("url"),
        "current_price": snapshot.get("current_price"),
        "raw": snapshot.get("raw") or {},
        "last_seen": now.isoformat(),
        "last_posted_at": snapshot.get("last_posted_at"),
        "last_posted_price": snapshot.get("last_posted_price"),
        "content_hash": content_hash,
        "item_count": snapshot.get("item_count")
    }

    # Write to Redis (3 operations)
    def _write_redis():
        try:
            r = get_redis()
            
            # 1. Full snapshot
            snap_key = _channel_snap_key(ref)
            r.set(snap_key, json.dumps(full_snapshot), ex=ttl_seconds)
            
            # 2. Content hash for dedup
            dedup_key = _channel_dedup_key(content_hash)
            r.set(dedup_key, json.dumps({
                "ref": ref,
                "posted_at": now.isoformat(),
                "title": snapshot.get("title")
            }), ex=ttl_seconds)
            
            # 3. Recent posts sorted set (score = timestamp)
            recent_key = _channel_recent_key(ref)
            r.zadd(recent_key, {content_hash: now.timestamp()})
            r.expire(recent_key, ttl_seconds)
            
            # Cleanup old entries from sorted set (keep last 50)
            r.zremrangebyrank(recent_key, 0, -51)
            
        except Exception:
            logger.exception("Redis channel write failed %s", ref)

    try:
        await loop.run_in_executor(None, _write_redis)
    except Exception:
        pass

    # Write to Postgres (2 operations)
    try:
        await loop.run_in_executor(None, _db_upsert_channel_snapshot, ref, snapshot, expires_hours)
    except Exception:
        logger.exception("DB channel upsert failed %s", ref)
    
    # Record post in history if this is a new post
    if snapshot.get("last_posted_at"):
        try:
            await loop.run_in_executor(None, _db_record_post, ref, snapshot, POSTGRES_RETENTION_DAYS)
        except Exception:
            logger.exception("Failed to record post history for %s", ref)

async def check_duplicate_post(
    ref: str,
    content_hash: str,
    lookback_hours: int = 48
) -> bool:
    """
    Check if this content was recently posted.
    
    Fast path: Redis sorted set lookup
    Fallback: Postgres history table
    
    Returns True if duplicate, False if unique.
    """
    loop = asyncio.get_event_loop()
    
    # Fast path: Check Redis dedup key
    def _check_redis():
        try:
            r = get_redis()
            
            # Check dedup key
            dedup_key = _channel_dedup_key(content_hash)
            if r.exists(dedup_key):
                return True
            
            # Check recent posts sorted set
            recent_key = _channel_recent_key(ref)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
            recent_hashes = r.zrangebyscore(recent_key, cutoff, '+inf')
            
            return content_hash in recent_hashes
            
        except Exception:
            logger.exception("Redis dedup check failed")
            return False
    
    try:
        is_dup = await loop.run_in_executor(None, _check_redis)
        if is_dup:
            logger.info("Duplicate detected in Redis: %s (hash=%s)", ref, content_hash[:8])
            return True
    except Exception:
        pass
    
    # Fallback: Check Postgres
    try:
        is_dup = await loop.run_in_executor(None, _db_check_duplicate_content, content_hash, lookback_hours)
        if is_dup:
            logger.info("Duplicate detected in Postgres: %s (hash=%s)", ref, content_hash[:8])
        return is_dup
    except Exception:
        logger.exception("DB dedup check failed")
        return False

async def mark_as_posted(ref: str, snapshot: dict):
    """
    Mark snapshot as posted by updating last_posted_at and last_posted_price.
    """
    snapshot["last_posted_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["last_posted_price"] = snapshot.get("current_price")
    
    await save_channel_snapshot(ref, snapshot)

# ═══════════════════════════════════════════════════════════════════════════
# CLEANUP OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def _db_delete_expired_channel_snapshots() -> int:
    """Delete expired channel snapshots from Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM channel_snapshots WHERE expires_at <= NOW()")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return deleted
    finally:
        return_pg_connection(conn)

def _db_delete_expired_post_history() -> int:
    """Delete expired post history from Postgres."""
    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM channel_post_history WHERE expires_at <= NOW()")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return deleted
    finally:
        return_pg_connection(conn)

async def delete_expired_channel_snapshots() -> int:
    """Delete expired snapshots from Postgres."""
    loop = asyncio.get_event_loop()
    try:
        count = await loop.run_in_executor(None, _db_delete_expired_channel_snapshots)
        logger.info("Deleted %d expired channel snapshots", count)
        return count or 0
    except Exception:
        logger.exception("Cleanup failed")
        return 0

async def delete_expired_post_history() -> int:
    """Delete expired post history from Postgres."""
    loop = asyncio.get_event_loop()
    try:
        count = await loop.run_in_executor(None, _db_delete_expired_post_history)
        logger.info("Deleted %d expired post history records", count)
        return count or 0
    except Exception:
        logger.exception("Post history cleanup failed")
        return 0

async def cleanup_all_expired() -> Dict[str, int]:
    """Run all cleanup tasks."""
    results = {}
    results["channel_snapshots"] = await delete_expired_channel_snapshots()
    results["post_history"] = await delete_expired_post_history()
    return results

# ═══════════════════════════════════════════════════════════════════════════
# ALTERNATIVES / CROSS-SITE COMPARISON
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_alternatives(product_key: str, exclude_site: str = None) -> List[Dict]:
    """
    Fetch alternative listings for the same product from different sites.
    """
    loop = asyncio.get_event_loop()

    def _fetch():
        r = get_redis()
        set_key = _product_sites_set_key(product_key)
        sites = r.smembers(set_key) or []
        results = []
        
        for site in sites:
            if site == exclude_site:
                continue
            
            site_key = _product_site_key(product_key, site)
            val = r.get(site_key)
            if not val:
                continue
            
            try:
                data = json.loads(val)
                results.append({
                    "site": site,
                    "price": data.get("current_price"),
                    "url": data.get("url")
                })
            except Exception:
                continue
        
        return results

    return await loop.run_in_executor(None, _fetch)

# ═══════════════════════════════════════════════════════════════════════════
# ADMIN / MAINTENANCE OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def wipe_channel_snapshots_redis(dry_run: bool = False) -> int:
    """
    Wipe all Redis channel-related keys.
    
    Patterns wiped:
    - channel:snap:*
    - channel:dedup:*
    - channel:recent:*
    
    dry_run=True → just prints keys, does NOT delete
    Returns number of keys matched.
    """
    r = get_redis()
    patterns = [
        f"{CHANNEL_SNAP_PREFIX}*",
        f"{CHANNEL_DEDUP_PREFIX}*",
        f"{CHANNEL_RECENT_PREFIX}*"
    ]
    
    all_keys = []
    for pattern in patterns:
        keys = list(r.scan_iter(pattern))
        all_keys.extend(keys)
    
    logger.warning(
        "Channel snapshot Redis wipe requested | keys=%d | dry_run=%s", 
        len(all_keys), dry_run
    )
    
    if dry_run:
        for key in all_keys[:10]:  # Show first 10
            try:
                ttl = r.ttl(key)
                val = r.get(key)
                logger.warning("KEY=%s TTL=%s VAL=%s", key, ttl, val[:200] if val else None)
            except Exception:
                logger.warning("KEY=%s (unable to read value)", key)
    else:
        if all_keys:
            # Delete in batches of 1000
            for i in range(0, len(all_keys), 1000):
                batch = all_keys[i:i+1000]
                r.delete(*batch)
            logger.warning("Channel snapshot Redis wipe completed: %d keys deleted", len(all_keys))
    
    return len(all_keys)

async def get_channel_stats(ref: str) -> Dict[str, Any]:
    """
    Get statistics for a channel ref.
    """
    loop = asyncio.get_event_loop()
    
    def _get_stats():
        r = get_redis()
        recent_key = _channel_recent_key(ref)
        
        # Get post count in last 48 hours
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
        recent_count = r.zcount(recent_key, cutoff, '+inf')
        
        # Get total posts tracked
        total_count = r.zcard(recent_key)
        
        # Get most recent post
        latest = r.zrevrange(recent_key, 0, 0, withscores=True)
        latest_timestamp = datetime.fromtimestamp(latest[0][1], tz=timezone.utc) if latest else None
        
        return {
            "ref": ref,
            "posts_last_48h": recent_count,
            "total_posts_tracked": total_count,
            "latest_post": latest_timestamp.isoformat() if latest_timestamp else None
        }
    
    try:
        return await loop.run_in_executor(None, _get_stats)
    except Exception:
        logger.exception("Failed to get channel stats for %s", ref)
        return {"ref": ref, "error": "stats_unavailable"}

# ═══════════════════════════════════════════════════════════════════════════
# INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════

# Auto-initialize on import (can be disabled if needed)
try:
    initialize_database()
except Exception as e:
    logger.warning("Database initialization skipped (will retry on first use): %s", e)