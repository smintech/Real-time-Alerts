from apify_client import ApifyClient
from config import APIFY_TOKEN
import os
from typing import Dict, Optional

# New: Import from settings
from bot.settings import (
    SUPPORTED_SITES,
    HIGH_DEAL_THRESHOLD,
    MEDIUM_DEAL_THRESHOLD,
    LOW_DEAL_THRESHOLD,
    MIN_CHANGE_TO_ALERT,
)

# Initialize client once (fallback to env if config missing)
client = ApifyClient(APIFY_TOKEN or os.getenv("APIFY_TOKEN"))


def scrape_product(url: str) -> Dict:
    """
    Scrape a single product URL using the best available Apify actor for Jumia.
    Supports direct product URLs and returns normalized data.
    """
    url_lower = url.lower()

    # Validate supported site (extensible for Konga later)
    if not any(site in url_lower for site in SUPPORTED_SITES):
        raise ValueError(f"Unsupported site in MVP. Supported: {SUPPORTED_SITES}. Got: {url}")

    if "jumia.com.ng" not in url_lower:
        raise NotImplementedError("Only Jumia.ng fully implemented in MVP")

    actor_id = "buseta/jumia-advanced-scraper"
    run_input = {
        "scrape_type": "product",
        "product_urls": [url],
        "get_reviews": False,
         # Keep fast & cheap for price monitoring
        "image_resolution": "low",
    }

    # Run synchronously
    run = client.actor(actor_id).call(run_input=run_input)

    dataset_items = client.dataset(run["defaultDatasetId"]).list_items()
    items = dataset_items.get("items", [])

    if not items:
        raise ValueError(f"No data extracted for {url}")

    raw = items[0]  # Single product expected

    price_info = raw.get("price", {})
    current_price = price_info.get("price_ngn")
    previous_price = price_info.get("old_price_ngn")

    # Infer stock: most Jumia pages hide price if OOS
    stock_status = "available" if current_price is not None else "out_of_stock"

    return {
        "title": raw.get("name"),
        "current_price": current_price,  # Numeric NGN
        "previous_price": previous_price,
        "discount_percent": price_info.get("discount"),
        "stock_status": stock_status,
        "url": url,
    }


def compute_changes(old_data: Optional[Dict], new_data: Dict) -> Dict:
    """
    Compute differences between old and new snapshots.
    Includes price drop percentage (positive = drop).
    """
    if old_data is None:
        return {
            "changed": True,
            "what_changed": ["new_product"],
            "price_diff_percent": 0.0,
        }

    changed = False
    what_changed = []
    price_diff_percent = 0.0

    old_price = old_data.get("current_price")
    new_price = new_data.get("current_price")

    if old_price != new_price:
        changed = True
        what_changed.append("price")
        if old_price and old_price > 0:
            price_diff_percent = round(((old_price - new_price) / old_price) * 100, 2)

    if old_data.get("stock_status") != new_data.get("stock_status"):
        changed = True
        what_changed.append("stock")

    return {
        "changed": changed,
        "what_changed": what_changed,
        "price_diff_percent": price_diff_percent,  # Positive = price dropped
        "significant_change": abs(price_diff_percent) >= MIN_CHANGE_TO_ALERT or "stock" in what_changed,
    }


def calculate_deal_score(price_diff_percent: float, historical_avg: Optional[float] = None) -> str:
    """
    Deal scoring using thresholds from settings.py.
    Later: incorporate historical_avg and competitor data.
    """
    drop = max(price_diff_percent, 0)  # Only drops count as deals

    if drop > HIGH_DEAL_THRESHOLD:
        return "high"
    elif drop > MEDIUM_DEAL_THRESHOLD:
        return "medium"
    elif drop > LOW_DEAL_THRESHOLD:
        return "low"
    return "none"


# Stubs for Phase 2
def scrape_fuel_prices(state: Optional[str] = None) -> Dict:
    raise NotImplementedError("Fuel scraping coming in Month 2")


def scrape_electricity_tariffs() -> Dict:
    raise NotImplementedError("Tariff scraping coming in Month 2")