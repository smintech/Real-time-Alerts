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
    # Apple iPhone 16 Pro Max
    "https://www.jumia.com.ng/iphone-16-pro-max-256gb-black-titanium-apple-12345678.html",
    "https://www.konga.com/product/apple-iphone-16-pro-max-256gb-black-titanium-5930123",
    
    # Samsung Galaxy S25 Ultra
    "https://www.jumia.com.ng/samsung-galaxy-s25-ultra-5g-512gb-titanium-gray-87654321.html",
    "https://www.konga.com/product/samsung-galaxy-s25-ultra-12gb-ram-512gb-rom-6012345",

    # === GADGETS ===
    # PlayStation 5 (Slim)
    "https://www.jumia.com.ng/sony-playstation-5-console-slim-edition-white-44332211.html",
    "https://www.konga.com/product/sony-playstation-5-ps5-slim-console-5829104",
    
    # Apple Watch Series 10
    "https://www.jumia.com.ng/apple-watch-series-10-gps-46mm-jet-black-99887766.html",
    "https://www.konga.com/product/apple-watch-series-10-gps-46-mm-jet-black-5712390",

    # === LAPTOPS ===
    # HP Pavilion 15 (Core i5, 16GB RAM)
    "https://www.jumia.com.ng/hp-pavilion-15-laptop-intel-core-i5-16gb-ram-512gb-ssd-55667788.html",
    "https://www.konga.com/product/hp-pavilion-15-intel-core-i5-16gb-ram-512gb-ssd-5421098",

    # MacBook Air M3 (13-inch)
    "https://www.jumia.com.ng/apple-macbook-air-13.6-m3-chip-8gb-256gb-ssd-midnight-11223344.html",
    "https://www.konga.com/product/apple-macbook-air-m3-chip-13-inch-8gb-ram-256gb-ssd-5309812",

    # === ACCESSORIES ===
    # Oraimo FreePods 4
    "https://www.jumia.com.ng/oraimo-freepods-4-anc-true-wireless-earbuds-black-22334455.html",
    "https://www.konga.com/product/oraimo-freepods-4-active-noise-cancelling-earbuds-5210987",

    # Anker 737 Power Bank (PowerCore 24K)
    "https://www.jumia.com.ng/anker-737-power-bank-powercore-24k-140w-output-33445566.html",
    "https://www.konga.com/product/anker-737-power-bank-gen-2-140w-output-5109876",
    
    # === CRYPTO (Special Handling) ===
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