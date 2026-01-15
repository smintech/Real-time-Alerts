import pytz
import os
# Nigeria timezone (West Africa Time - no DST)
TIMEZONE = pytz.timezone('Africa/Lagos')  # Standard and preferred name

# Deal scoring thresholds (% price drop)
HIGH_DEAL_THRESHOLD = 15   # >15% drop → "high" deal
MEDIUM_DEAL_THRESHOLD = 5  # 5-15% → "medium"
LOW_DEAL_THRESHOLD = 1     # 1-5% → "low"

# Freemium limits
MAX_WATCHES_FREE = 3
MAX_WATCHES_PAID = 20      # Future paid tier

# Scraping & alert settings
CHECK_INTERVAL_SECONDS = 1020 #3600    # 1 hour between checks (global scheduler)
MIN_CHANGE_TO_ALERT = 0.5       # % price change minimum to trigger alert (avoid noise)

# Supported categories for MVP (expand later)
CATEGORIES = ['phones', 'gadgets', 'laptops', 'accessories']

# Supported sites (for validation & future expansion)
SUPPORTED_SITES = ['jumia.com.ng', 'konga.com','binance.com']
#spam protection
ALLOWED_DIRECTIONS = ["low", "high", "both"]
ADD_COOLDOWN_SECONDS = 10
SCRAPE_TIMEOUT = 15
# Scheduler failure handling
MAX_WATCH_FAILURES = 3           # consecutive failures → pause watch
FAILURE_BACKOFF_BASE = 60        # seconds base for exponential backoff
NOTIFY_ON_DELISTED = True      # send user notice on delist/OOS/pause
# Channel auto-posting (optional feature)
AUTO_POST_TO_CHANNEL = True  # Set to False to disable entirely
CHANNEL_DEAL_CHAT_ID = [int(x.strip()) for x in os.getenv("CHANNEL", "").split(",") if x.strip()] # Your channel ID (make bot admin with post rights)

# Channel monitoring settings
CHANNEL_MONITORED_URLS = [
    # === PHONES ===
    # Apple iPhone 15 Pro Max (256GB) — Jumia + Konga
    "https://www.jumia.com.ng/apple-iphone-15-pro-max-6.7-256gb-nano-sim-esim-5g-natural-381382767.html",
    "https://www.konga.com/product/apple-iphone-15-pro-max-6-7-256gb-rom-8gb-ram-1-sim-esim-5g-4441mah-blue-6612850",

    # Apple iPhone 15 Pro (128GB) — Jumia + Konga
    "https://www.jumia.com.ng/apple-iphone-15-pro-6.1-128gb-rom-8gb-ram-nano-sim-white-389800758.html",
    "https://www.konga.com/product/apple-iphone-15-pro-6-1-128gb-rom-8gb-ram-nano-sim-5g-6826852",

    # === GADGETS ===
    # PlayStation 5 Slim — Jumia + Konga
    "https://www.jumia.com.ng/sony-playstation-5-slim-ps5-slim-console-1tb-410637026.html",
    "https://www.konga.com/product/sony-playstation-5-slim-ps5-slim-console-1tb-6834464",

    # Apple Watch (Series 10 / Series 9 pages exist) — Jumia + Konga
    "https://www.jumia.com.ng/slp/apple-watch-series-10-cost-sale",   # Jumia listing / product collection (Series 10)
    "https://www.konga.com/product/apple-watch-series-10-with-sport-band-gps-42mm-black-6598000",

    # === LAPTOPS ===
    # HP Pavilion 15 (i5, 16GB, 512GB) — Jumia + Konga
    "https://www.jumia.com.ng/pavilion-15-11th-gen-intel-core-i5-512gb-ssd-16gb-ram-touchscreenbacklit-keyboard-hp-mpg2159579.html",
    "https://www.konga.com/product/hp-pavilion-15-12th-gen-intel-core-i5-16gb-ram-512gb-ssd-backlit-keyboard-15-6-touch-wins11-6069271",

    # MacBook Air (M3) — Jumia + Konga
    "https://www.jumia.com.ng/apple-macbook-air-13-m3-chip-8gb-256gb-space-gray-389242856.html",
    "https://www.konga.com/product/apple-macbook-air-m3-chip-with-8-core-cpu-and-8-core-gpu-256gb-ssd-silver-13-inch-6656405",

    # === ACCESSORIES ===
    # Oraimo FreePods 4 — Jumia + Konga
    "https://www.jumia.com.ng/oraimo-freepods-4-anc-wireless-stereo-earbuds-413570378.html",
    "https://www.konga.com/product/oraimo-freepods-4-active-noise-cancellation-wireless-earbuds-6295340",

    # Anker 737 / PowerCore (140W) — Jumia (search listing) + Konga product
    "https://www.jumia.com.ng/slp/anker-portable-charger-for-travel",  # Anker product listing/search page
    "https://www.konga.com/product/anker-737-ganprime-24000-power-bank-pd-140w-black-6861844",

    # === CRYPTO (special handling) ===
    "SYMBOL:ETHUSDT",
]

MIN_DROP_PERCENT_FOR_CHANNEL = 5.0   # % price drop
MIN_SAVINGS_FOR_CHANNEL = 10000      # ₦15,000 minimum savings
MIN_DEAL_SCORE_FOR_CHANNEL = "medium"  # only medium/high
MAX_CHANNEL_POSTS_PER_RUN = 5       # safety cap per hour

PAID_TIERS = {
    "basic": {
        "name": "Basic Pro",
        "max_watches": 20,
        "trial_days": 3,          
        "price_monthly_ngn": 5000,
        "features": ["More watches", "Priority alerts", "Category filters"]
    },
    "merchant": {
        "name": "Merchant Plan",
        "max_watches": 75,
        "trial_days": 7,
        "price_monthly_ngn": 15000,
        "features": ["Bulk add", "Price history export", "Competitor tracking"]
    },
    "business": {
        "name": "Business Plan",
        "max_watches": 1000,      
        "trial_days": 14,
        "price_monthly_ngn": 30000,
        "features": ["Team accounts(shared dashboard for multiple users)", "Priority alert notifications (faster scheduler checks)", "Custom reports(export price history CSV/PDF)"]
    }
}
DEFAULT_FREE_LIMIT = MAX_WATCHES_FREE