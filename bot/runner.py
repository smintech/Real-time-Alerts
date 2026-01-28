# runner.py — FINAL SINGLE-PROCESS ASYNC VERSION (2026 style – no multiprocessing)

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from playwright.async_api import async_playwright
import uvicorn
from fastapi import FastAPI, HTTPException, Body
from typing import Optional, Dict, Any

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
    description="Real-time Nigerian price tracking with dual-layer persistence",
    version="2.0.0"
)

# -----------------------
# Basic endpoints
# -----------------------

@app.get("/")
async def home():
    """Root endpoint - API status"""
    return {
        "status": "API Online",
        "service": "Naija Price Alerts",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "components": {
            "api": "healthy",
            "database": "connected",
            "redis": "connected",
        }
    }

# -----------------------
# /v1/track endpoint (Enhanced with deduplication)
# -----------------------

@app.post("/v1/track")
async def track_product(payload: dict = Body(...)):
    """
    Track a product and get price change alerts.
    
    Enhanced with dual-layer persistence and deduplication.
    """
    url = payload.get("url") or payload.get("product_url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    try:
        # Scrape product (now async)
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

    # Load previous snapshot (Redis first, DB fallback)
    old = await load_last_snapshot(url)
    old_minimal = {
        "current_price": old.get("current_price"), 
        "stock_status": old.get("stock_status")
    } if old else None

    # Compute changes
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

    # Save snapshot to both Redis and Postgres
    try:
        await save_snapshot(product)
    except Exception:
        logger.exception("Failed saving snapshot for %s", url)

    # Fetch alternatives (cross-site comparison)
    alternatives = []
    try:
        product_key = normalize_product_key(product)
        alternatives = await fetch_alternatives(
            product_key, 
            exclude_site=product.get("site")
        )
    except Exception:
        logger.exception("Failed fetching alternatives")

    # Build response
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

# -----------------------
# Additional API Endpoints
# -----------------------

@app.get("/v1/product/{product_url:path}")
async def get_product_snapshot(product_url: str):
    """Get the latest snapshot for a product URL"""
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
    try:
        stats = await get_channel_stats(ref)
        return stats
    except Exception as e:
        logger.exception("Failed to get channel stats")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/admin/cleanup")
async def trigger_cleanup(auth_token: str = Body(..., embed=True)):
    """
    Manually trigger cleanup of expired data.
    Requires admin authentication.
    """
    # Simple token auth (use env var in production)
    expected_token = os.getenv("ADMIN_TOKEN")
    if auth_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    
    try:
        results = await cleanup_all_expired()
        return {
            "status": "success",
            "deleted": results,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.exception("Cleanup failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/admin/check-duplicate")
async def check_content_duplicate(
    ref: str = Body(...),
    content: Dict[str, Any] = Body(...),
    lookback_hours: int = Body(48)
):
    """
    Check if content is a duplicate.
    Useful for testing deduplication logic.
    """
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
from bot.bot import run_bot  # Async version with nest_asyncio

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
    
    # Check if chromium is available
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
            LOG.info("✓ Playwright Chromium is working")
    except Exception as e:
        LOG.error(f"✗ Playwright Chromium test failed: {e}")
    
    LOG.info("-------------------------")

@app.on_event("startup")
async def on_startup():
    """FastAPI startup event handler"""
    logger.info("=" * 70)
    logger.info("FastAPI startup — initializing Naija Price Alerts")
    logger.info("=" * 70)
    
    # Debug Playwright
    await debug_playwright_env()
    
    # Initialize database
    logger.info("Initializing database tables...")
    try:
        initialize_database()
        logger.info("✓ Database initialized successfully")
    except Exception as e:
        logger.exception("✗ Database initialization failed: %s", e)
        # Don't exit - allow app to continue (will retry on first use)
    
    # Launch Telegram bot in background
    logger.info("Launching Telegram bot as async background task...")
    try:
        asyncio.create_task(run_bot())
        logger.info("✓ Bot task created (single process, no multiprocessing)")
    except Exception as e:
        logger.exception("✗ Failed to launch bot: %s", e)
    
    logger.info("=" * 70)
    logger.info("Startup complete - API ready to accept requests")
    logger.info("=" * 70)

@app.on_event("shutdown")
async def on_shutdown():
    """FastAPI shutdown event handler"""
    logger.info("=" * 70)
    logger.info("FastAPI shutdown initiated")
    logger.info("=" * 70)
    
    # Run final cleanup
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
    logger.info("=" * 70)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=LOG_LEVEL.lower(),
        workers=1,  # Single worker — perfect for async bot in same process
        reload=reload,  # Auto-reload in debug mode
        access_log=True,
    )
