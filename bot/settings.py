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
CHECK_INTERVAL_SECONDS = 120 #3600    # 1 hour between checks (global scheduler)
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
    # Flagship Phones
    "https://www.jumia.com.ng/hp-elitebook-840-g6-intel-core-i5-16gb-ram512gb-ssdbacklit-keyboardfp-reader-wins-11-pro-laptopbag-393059791.html",
    "https://www.jumia.com.ng/hp-elitebook-840-g6-intel-core-i5-8gb-ram512gb-ssdbacklit-keyboardfp-reader-wins-11-laptop-bag-332628501.html",
    "https://www.jumia.com.ng/hp-elitebook-840-g6-touchscreen-intel-core-i5-16gb-ram512gb-ssd-backlit-keyboardwindows11-probag-202178413.html",
    # Add 10–20 more specific product URLs (not catalogs)
    # iPhone examples:
    "https://www.jumia.com.ng/apple-iphone-15-pro-max-256gb-apple-mpg.html",
]

MIN_DROP_PERCENT_FOR_CHANNEL = 5.0   # % price drop
MIN_SAVINGS_FOR_CHANNEL = 15000      # ₦15,000 minimum savings
MIN_DEAL_SCORE_FOR_CHANNEL = "medium"  # only medium/high
MAX_CHANNEL_POSTS_PER_RUN = 10       # safety cap per hour

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