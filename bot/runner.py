# runner.py — FINAL SINGLE-PROCESS ASYNC VERSION (2026 style – no multiprocessing)

import os
import time
import logging
import asyncio
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Body

# Import persistence & utils
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
)

# -----------------------
# Logging
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("runner")

app = FastAPI(title="Naija Price Alerts API")

# -----------------------
# Basic endpoints
# -----------------------
@app.get("/")
async def home():
    return {"status": "API Online"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
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
# Import async bot launcher (must be after app definition for nest_asyncio)
# -----------------------
from bot.bot import run_bot # Async version with nest_asyncio

# -----------------------
# Startup / Shutdown
# -----------------------
@app.on_event("startup")
async def on_startup():
    logger.info("FastAPI startup — preparing DB")
    try:
        _db_ensure_table()
        _db_create_channel_table()
    except Exception as e:
        logger.exception("DB setup failed: %s", e)

    # Launch bot in background (same process, async)
    asyncio.create_task(run_bot())
    logger.info("Bot launched as background async task (no multiprocessing)")

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI shutdown — no bot cleanup needed (handled in bot.py)")

# -----------------------
# Main entry point
# -----------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting uvicorn server on 0.0.0.0:%d (single process with async bot)", port)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=LOG_LEVEL.lower(),
        workers=1  # Single worker — perfect for async bot in same process
    )