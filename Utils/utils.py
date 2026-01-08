from apify_client import ApifyClient
from config import APIFY_TOKEN
import os
from typing import Dict, Optional

# Initialize client once (fallback to env if config missing)
client = ApifyClient(APIFY_TOKEN)


def scrape_product(url: str) -> Dict:
    """
    Scrape a single product URL using the best available Apify actor for Jumia.
    Supports direct product URLs and returns normalized data.
    """
    url_lower = url.lower()

    if "jumia.com.ng" not in url_lower:
        raise ValueError(f"Only Jumia.ng supported in MVP: {url}")

    actor_id = "buseta/jumia-advanced-scraper"
    run_input = {
        "scrape_type": "product",
        "product_urls": [url],
        "get_reviews": False,  # Keep fast & cheap for price monitoring
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
    discount = price_info.get("discount")  # e.g., "20%"

    # Infer stock: most Jumia pages hide price if OOS
    stock_status = "available" if current_price is not None else "out_of_stock"

    return {
        "title": raw.get("name"),
        "current_price": current_price,  # Numeric NGN
        "previous_price": previous_price,
        "discount_percent": discount,
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
    }


def calculate_deal_score(price_diff_percent: float, historical_avg: Optional[float] = None) -> str:
    """
    Simple deal scoring for MVP.
    Later: incorporate historical_avg and competitor data.
    """
    drop = max(price_diff_percent, 0)  # Only drops count as deals

    if drop > 15:
        return "high"
    elif drop > 5:
        return "medium"
    elif drop > 0:
        return "low"
    return "none"


# Stubs for Phase 2
def scrape_fuel_prices(state: Optional[str] = None) -> Dict:
    raise NotImplementedError("Fuel scraping coming in Month 2")


def scrape_electricity_tariffs() -> Dict:
    raise NotImplementedError("Tariff scraping coming in Month 2")