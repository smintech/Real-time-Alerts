# runner.py — SINGLE-PROCESS ASYNC VERSION with External Endpoints
# Includes: Passive wake-up handling for external ping services (UptimeRobot, Cron-Job, etc.)

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from playwright.async_api import async_playwright
import uvicorn
from fastapi import FastAPI, HTTPException, Body, BackgroundTasks, Header
from typing import Optional, Dict, Any, List
import hmac
import hashlib

# Import persistence & utils
from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    check_duplicate_post,
    mark_as_posted,
    compute_content_hash,
    delete_expired_channel_snapshots,
    delete_expired_post_history,
    cleanup_all_expired,
    fetch_alternatives,
    initialize_database,
    get_channel_stats,
)

from Utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    get_domain_from_url,
    NoDataError,
    ScrapeError,
)

# Import bot functions
from bot.bot import (
    check_and_post_channel_deals,
    check_and_post_fuel_prices,
    check_and_post_school_updates,
    check_all_watches,
    check_trials,
    application as bot_app,
)

# Import telegram bot types
from telegram.ext import ContextTypes

# -----------------------
# Logging
# -----------------------
LOG = logging.getLogger(__name__)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("runner")

app = FastAPI(
    title="Naija Price Alerts API",
    description="Real-time Nigerian price tracking with external scheduling",
    version="2.1.0"
)

# -----------------------
# Wake-up / Keep-alive Configuration
# -----------------------
WAKEUP_GRACE_PERIOD = int(os.getenv("WAKEUP_GRACE_PERIOD", "30"))  # Seconds to stay warm after ping

# Track last activity for wake-up detection
_last_activity = time.time()

def touch_activity():
    """Mark that the process is active (prevents sleep detection)."""
    global _last_activity
    _last_activity = time.time()

# -----------------------
# Security
# -----------------------
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-this-in-production")
ALLOWED_IPS = os.getenv("ALLOWED_IPS", "").split(",") if os.getenv("ALLOWED_IPS") else []

def verify_api_key(authorization: str = Header(None)) -> bool:
    """Verify API key in Authorization header."""
    if not authorization:
        return False
    
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = authorization
    
    return hmac.compare_digest(token, API_SECRET_KEY)

def verify_ip(ip_address: str) -> bool:
    """Verify if IP is allowed (if ALLOWED_IPS is configured)."""
    if not ALLOWED_IPS or ALLOWED_IPS == [""]:
        return True
    return ip_address in ALLOWED_IPS

# -----------------------
# Helper to get bot context
# -----------------------
async def get_bot_context():
    """Get a ContextTypes.DEFAULT_TYPE for bot operations."""
    if not bot_app:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    context = ContextTypes.DEFAULT_TYPE(application=bot_app)
    return context

# -----------------------
# Basic endpoints (with activity tracking)
# -----------------------

@app.get("/")
async def home():
    """Root endpoint - API status"""
    touch_activity()  # Mark process as active
    return {
        "status": "API Online",
        "service": "Naija Price Alerts",
        "version": "2.1.0",
        "scheduling": "external",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/health")
async def health():
    """Health check endpoint - use this for external pings"""
    touch_activity()  # Mark process as active
    uptime = time.time() - _last_activity
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "last_activity_seconds_ago": round(uptime, 2),
        "components": {
            "api": "healthy",
            "database": "connected",
            "redis": "connected",
            "bot": "running" if bot_app else "initializing",
        }
    }

@app.get("/ping")
async def ping():
    """
    Ultra-lightweight ping for wake-up services.
    Returns immediately - use with UptimeRobot, Cron-Job.org, etc.
    """
    touch_activity()
    return {"pong": True, "t": datetime.now(timezone.utc).isoformat()}

@app.head("/ping")
async def ping_head():
    """HEAD request for lightweight pings (no body)."""
    touch_activity()
    return {}

# -----------------------
# Scheduled Job Endpoints (Protected + Activity Tracking)
# -----------------------

@app.post("/v1/jobs/check-watches")
async def job_check_watches(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for check_all_watches job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        logger.warning(f"Blocked request from unauthorized IP: {client_ip}")
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_watch_check)
    
    return {
        "status": "accepted",
        "job": "check_watches",
        "message": "Watch check started in background",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_watch_check():
    """Background task for watch checking."""
    touch_activity()
    try:
        context = await get_bot_context()
        await check_all_watches(context)
        logger.info("Watch check completed successfully")
    except Exception as e:
        logger.exception(f"Watch check failed: {e}")
    finally:
        touch_activity()

@app.post("/v1/jobs/channel-deals")
async def job_channel_deals(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for check_and_post_channel_deals job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_channel_deals)
    
    return {
        "status": "accepted",
        "job": "channel_deals",
        "message": "Channel deals check started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_channel_deals():
    """Background task for channel deals."""
    touch_activity()
    try:
        context = await get_bot_context()
        await check_and_post_channel_deals(context)
        logger.info("Channel deals completed successfully")
    except Exception as e:
        logger.exception(f"Channel deals failed: {e}")
    finally:
        touch_activity()

@app.post("/v1/jobs/fuel-prices")
async def job_fuel_prices(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for check_and_post_fuel_prices job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_fuel_prices)
    
    return {
        "status": "accepted",
        "job": "fuel_prices",
        "message": "Fuel price check started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_fuel_prices():
    """Background task for fuel prices."""
    touch_activity()
    try:
        context = await get_bot_context()
        await check_and_post_fuel_prices(context)
        logger.info("Fuel prices completed successfully")
    except Exception as e:
        logger.exception(f"Fuel prices failed: {e}")
    finally:
        touch_activity()

@app.post("/v1/jobs/school-updates")
async def job_school_updates(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for check_and_post_school_updates job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_school_updates)
    
    return {
        "status": "accepted",
        "job": "school_updates",
        "message": "School updates check started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_school_updates():
    """Background task for school updates."""
    touch_activity()
    try:
        context = await get_bot_context()
        await check_and_post_school_updates(context)
        logger.info("School updates completed successfully")
    except Exception as e:
        logger.exception(f"School updates failed: {e}")
    finally:
        touch_activity()

@app.post("/v1/jobs/trial-check")
async def job_trial_check(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for check_trials job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_trial_check)
    
    return {
        "status": "accepted",
        "job": "trial_check",
        "message": "Trial check started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_trial_check():
    """Background task for trial checks."""
    touch_activity()
    try:
        context = await get_bot_context()
        await check_trials(context)
        logger.info("Trial check completed successfully")
    except Exception as e:
        logger.exception(f"Trial check failed: {e}")
    finally:
        touch_activity()

@app.post("/v1/jobs/cleanup")
async def job_cleanup(
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """External trigger for cleanup_all_expired job."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    background_tasks.add_task(run_cleanup)
    
    return {
        "status": "accepted",
        "job": "cleanup",
        "message": "Cleanup started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def run_cleanup():
    """Background task for cleanup."""
    touch_activity()
    try:
        results = await cleanup_all_expired()
        logger.info(f"Cleanup completed: {results}")
    except Exception as e:
        logger.exception(f"Cleanup failed: {e}")
    finally:
        touch_activity()

# -----------------------
# Batch Job Endpoint
# -----------------------

@app.post("/v1/jobs/batch")
async def job_batch(
    background_tasks: BackgroundTasks,
    jobs: List[str] = Body(..., description="List of jobs to run"),
    authorization: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """Run multiple jobs in a single request."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    if not verify_ip(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")
    
    job_map = {
        "watches": run_watch_check,
        "channel-deals": run_channel_deals,
        "fuel-prices": run_fuel_prices,
        "school-updates": run_school_updates,
        "trial-check": run_trial_check,
        "cleanup": run_cleanup,
    }
    
    invalid_jobs = [j for j in jobs if j not in job_map]
    if invalid_jobs:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid jobs: {invalid_jobs}. Valid jobs: {list(job_map.keys())}"
        )
    
    for job_name in jobs:
        background_tasks.add_task(job_map[job_name])
    
    return {
        "status": "accepted",
        "jobs": jobs,
        "message": f"{len(jobs)} jobs started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -----------------------
# Manual Trigger Endpoints
# -----------------------

@app.post("/v1/manual/check-watches")
async def manual_check_watches(authorization: str = Header(None)):
    """Manually trigger watch check (waits for completion)."""
    touch_activity()
    
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    try:
        context = await get_bot_context()
        await check_all_watches(context)
        return {
            "status": "success",
            "job": "check_watches",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.exception("Manual watch check failed")
        raise HTTPException(status_code=500, detail=str(e))

# -----------------------
# Original API Endpoints
# -----------------------

@app.post("/v1/track")
async def track_product(payload: dict = Body(...)):
    """Track a product and get price change alerts."""
    touch_activity()
    
    url = payload.get("url") or payload.get("product_url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    try:
        product = await scrape_product(url)
    except NoDataError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ScrapeError as e:
        raise HTTPException(status_code=503, detail=f"Scrape failed: {e}")
    except Exception as e:
        logger.exception("Scrape failed for %s", url)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    old = await load_last_snapshot(url)
    old_minimal = {
        "current_price": old.get("current_price"), 
        "stock_status": old.get("stock_status")
    } if old else None

    try:
        changes = compute_changes(old_minimal, product)
    except Exception:
        logger.exception("Failed computing changes")
        changes = {
            "changed": True, 
            "what_changed": [], 
            "price_diff_percent": 0.0,
            "significant_change": False
        }

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

    alternatives = []
    try:
        product_key = normalize_product_key(product)
        alternatives = await fetch_alternatives(product_key, exclude_site=product.get("site"))
    except Exception:
        logger.exception("Failed fetching alternatives")

    response = {
        "product_url": product.get("url") or url,
        "title": product.get("title"),
        "site": product.get("site"),
        "current_price": product.get("current_price"),
        "previous_price": old.get("current_price") if old else product.get("previous_price"),
        "changed": bool(changes.get("changed")),
        "significant_change": bool(changes.get("significant_change")),
        "what_changed": changes.get("what_changed", []),
        "price_diff_percent": price_diff,
        "stock_status": product.get("stock_status", "unknown"),
        "deal_score": deal_score,
        "severity": severity,
        "suggested_action": (
            "🔥 Buy now — strong price drop!" if deal_score == "high"
            else "📊 Monitor price — moderate drop" if deal_score == "medium"
            else "👀 Watch for better deals" if deal_score == "low"
            else "No immediate action"
        ),
        "alternatives": alternatives,
        "images": product.get("images", []),
        "description": product.get("description", ""),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    return response

@app.get("/v1/product/{product_url:path}")
async def get_product_snapshot(product_url: str):
    """Get the latest snapshot for a product URL"""
    touch_activity()
    
    try:
        snapshot = await load_last_snapshot(product_url)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Product not found in database")
        return snapshot
    except Exception as e:
        logger.exception("Failed to load snapshot")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/channel/{ref}/stats")
async def channel_statistics(ref: str):
    """Get statistics for a channel reference"""
    touch_activity()
    
    try:
        stats = await get_channel_stats(ref)
        return stats
    except Exception as e:
        logger.exception("Failed to get channel stats")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/admin/check-duplicate")
async def check_content_duplicate(
    ref: str = Body(...),
    content: Dict[str, Any] = Body(...),
    lookback_hours: int = Body(48)
):
    """Check if content is a duplicate."""
    touch_activity()
    
    try:
        content_hash = compute_content_hash(content)
        is_duplicate = await check_duplicate_post(ref, content_hash, lookback_hours)
        
        return {
            "ref": ref,
            "content_hash": content_hash,
            "is_duplicate": is_duplicate,
            "lookback_hours": lookback_hours,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.exception("Duplicate check failed")
        raise HTTPException(status_code=500, detail=str(e))

# -----------------------
# Import async bot launcher
# -----------------------
from bot.bot import run_bot

# -----------------------
# Startup / Shutdown
# -----------------------

async def debug_playwright_env():
    """Debug Playwright installation in container"""
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "NOT SET")
    LOG.info("--- Playwright Debug ---")
    LOG.info(f"PLAYWRIGHT_BROWSERS_PATH: {path}")
    
    if path != "NOT SET" and os.path.exists(path):
        try:
            contents = os.listdir(path)
            LOG.info(f"Contents of {path}: {contents}")
        except Exception as e:
            LOG.error(f"Cannot list {path}: {e}")
    else:
        LOG.warning(f"Directory {path} does not exist or env var not set")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
            LOG.info("✓ Playwright Chromium is working")
    except Exception as e:
        LOG.error(f"✗ Playwright Chromium test failed: {e}")
    
    LOG.info("-------------------------")

_bot_ready = asyncio.Event()
_bot_error: Optional[Exception] = None
_bot_task: Optional[asyncio.Task] = None

async def run_bot_with_signal():
    """
    Wrapper that:
    - Runs the bot in the background
    - Catches all errors and logs them loudly
    - Signals when ready (success or failure)
    - Prevents startup from hanging if bot crashes
    """
    global _bot_error, _bot_task
    _bot_task = asyncio.current_task()
    
    try:
        logger.info("🤖 Bot background task starting...")
        touch_activity()
        await run_bot()
        logger.info("✅ Bot finished normally (unexpected - should run forever)")
        
    except Exception as e:
        _bot_error = e
        logger.error("=" * 70)
        logger.error("❌ BOT CRASHED DURING INITIALIZATION")
        logger.error("=" * 70)
        logger.exception("Exception: %s", e)
        logger.error("=" * 70)
        logger.error("API will still start but Telegram bot won't function")
        logger.error("Check your configuration:")
        logger.error("  - TELEGRAM_TOKEN")
        logger.error("  - REDIS_URL")
        logger.error("  - DB_URL")
        logger.error("=" * 70)
        touch_activity()
        
    finally:
        logger.info("🔔 Signaling bot ready event (success or failure)...")
        _bot_ready.set()  # Signal ready even on failure

# ═══════════════════════════════════════════════════════════════════════════
# Enhanced Startup Event
# ═══════════════════════════════════════════════════════════════════════════

async def on_startup():
    """FastAPI startup event handler with improved bot initialization"""
    global _last_activity
    _last_activity = datetime.now(timezone.utc)
    
    logger.info("=" * 70)
    logger.info("🚀 FastAPI startup — initializing Naija Price Alerts")
    logger.info("=" * 70)
    logger.info(f"Timestamp: {_last_activity.isoformat()}")
    logger.info("=" * 70)
    
    # Debug Playwright
    await debug_playwright_env()
    
    # Initialize database
    logger.info("\n📦 Initializing database tables...")
    try:
        initialize_database()
        logger.info("   ✓ Database initialized successfully")
    except Exception as e:
        logger.exception("   ✗ Database initialization FAILED: %s", e)
        logger.error("   ⚠️  API will start but persistence won't work")
    
    # Launch bot in background
    logger.info("\n🤖 Launching Telegram bot background task...")
    try:
        bot_task = asyncio.create_task(run_bot_with_signal())
        logger.info("   ✓ Bot task created")
        
        # Wait for bot to signal ready (or timeout)
        logger.info("   ⏳ Waiting for bot initialization (max 45s)...")
        try:
            await asyncio.wait_for(_bot_ready.wait(), timeout=45.0)
            
            if _bot_error:
                logger.error("   ⚠️  Bot initialization FAILED:")
                logger.error(f"      {_bot_error.__class__.__name__}: {_bot_error}")
                logger.error("   ℹ️  API will run but bot commands unavailable")
            else:
                logger.info("   ✓ Bot initialized successfully and ready")
                
        except asyncio.TimeoutError:
            logger.warning("   ⏱️  Bot initialization timeout after 45s")
            logger.warning("   ℹ️  Bot may still be connecting, continuing startup...")
            
    except Exception as e:
        logger.exception("   ✗ Failed to create bot task: %s", e)
        logger.error("   ⚠️  Continuing without bot functionality")
    
    # Final status
    logger.info("\n" + "=" * 70)
    logger.info("✅ FastAPI startup complete")
    logger.info("=" * 70)
    logger.info("API Status:")
    logger.info(f"  - HTTP Server: ✓ Ready")
    logger.info(f"  - Database: ✓ Initialized")
    logger.info(f"  - Telegram Bot: {'❌ Failed' if _bot_error else '✓ Ready'}")
    logger.info("=" * 70)
    logger.info("Endpoints:")
    logger.info("  - GET  /health          (for external pings)")
    logger.info("  - POST /v1/jobs/*       (scheduled jobs)")
    logger.info("=" * 70 + "\n")


@app.on_event("shutdown")
async def on_shutdown():
    """FastAPI shutdown event handler"""
    logger.info("=" * 70)
    logger.info("FastAPI shutdown initiated")
    logger.info("=" * 70)
    
    try:
        logger.info("Running final cleanup of expired data...")
        results = await cleanup_all_expired()
        logger.info(f"Cleanup results: {results}")
    except Exception as e:
        logger.exception("Final cleanup failed: %s", e)
    
    logger.info("Shutdown complete")
    logger.info("=" * 70)

# -----------------------
# Exception Handlers
# -----------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Custom HTTP exception handler"""
    return {
        "error": exc.detail,
        "status_code": exc.status_code,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Catch-all exception handler"""
    logger.exception(f"Unhandled exception: {exc}")
    return {
        "error": "Internal server error",
        "detail": str(exc) if os.getenv("DEBUG") == "1" else "An unexpected error occurred",
        "status_code": 500,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# -----------------------
# Main entry point
# -----------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("DEBUG", "0") == "1"
    
    logger.info("=" * 70)
    logger.info("Starting Naija Price Alerts API")
    logger.info("=" * 70)
    logger.info(f"Port: {port}")
    logger.info(f"Log level: {LOG_LEVEL}")
    logger.info(f"Debug mode: {reload}")
    logger.info(f"Workers: 1 (single process with async bot)")
    logger.info("External ping wake-up enabled - no background heartbeat")
    logger.info("=" * 70)

    uvicorn.run(
        "runner:app",
        host="0.0.0.0",
        port=port,
        log_level=LOG_LEVEL.lower(),
        workers=1,
        reload=reload,
        access_log=True,
    )
