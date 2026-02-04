import pytz
import os
import re
# Nigeria timezone (West Africa Time - no DST)
TIMEZONE = pytz.timezone('Africa/Lagos')  # Standard and preferred name

# Deal scoring thresholds (% price drop)
HIGH_DEAL_THRESHOLD = 15   # >15% drop → "high" deal
MEDIUM_DEAL_THRESHOLD = 5  # 5-15% → "medium"
LOW_DEAL_THRESHOLD = 1     # 1-5% → "low"

# Freemium limits
MAX_WATCHES_FREE = 3
MAX_WATCHES_PAID = 20      # Future paid tier

# Scraping & alert settings#120
CHECK_INTERVAL_SECONDS = 3600    # 1 hour between checks (global scheduler)
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

CHANNEL_MONITORED_URLS = {
    # Phones
    "iphone-15-pro-max-256": [
        "https://www.jumia.com.ng/apple-iphone-15-pro-max-6.7-256gb-nano-sim-esim-5g-natural-381382767.html",
        "https://www.konga.com/product/apple-iphone-15-pro-max-6-7-256gb-rom-8gb-ram-1-sim-esim-5g-4441mah-blue-6612850"
    ],
    "iphone-15-pro-128": [
        "https://www.jumia.com.ng/apple-iphone-15-pro-6.1-128gb-rom-8gb-ram-nano-sim-white-389800758.html",
        "https://www.konga.com/product/apple-iphone-15-pro-6-1-128gb-rom-8gb-ram-nano-sim-5g-6826852"
    ],

    # Gaming
    "ps5-slim-1tb": [
        "https://www.jumia.com.ng/sony-playstation-5-slim-ps5-slim-console-1tb-410637026.html",
        "https://www.konga.com/product/sony-playstation-5-slim-ps5-slim-console-1tb-6834464"
    ],

    # Laptops
    "macbook-air-m3-256": [
        "https://www.jumia.com.ng/apple-macbook-air-13-m3-chip-8gb-256gb-space-gray-389242856.html",
        "https://www.konga.com/product/apple-macbook-air-m3-chip-with-8-core-cpu-and-8-core-gpu-256gb-ssd-silver-13-inch-6656405"
    ],

    # Earbuds
    "freepods-4": [
        "https://www.jumia.com.ng/oraimo-freepods-4-anc-wireless-stereo-earbuds-413570378.html",
        "https://www.konga.com/product/oraimo-freepods-4-anc-true-wireless-stereo-earbuds-6624829"
    ],

    # Crypto
    "crypto-eth": [
        "SYMBOL:USDTUSD"
    ],

    # New helpful / real product pages
}

MIN_DROP_PERCENT_FOR_CHANNEL = 3.0   # % price drop
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

DEFAULT_SCHOOL_SOURCES = {
    # --- Higher Education Regulators ---
    "NUC (Universities)": [
        "https://www.nuc.edu.ng/"
    ],
    # --- Reliable Education News Aggregators (Best for "Daily" updates) ---
    "MySchool.ng": [
        "https://myschool.ng/news"
    ],
    "Punch Education": [
        "https://punchng.com/topics/education/"
    ],

    #"Lagos State Education": [
        #"https://lasubeb.lg.gov.ng/"
    #],  # Replaced OEQA with LASUBEB (more active)
}

# bot/settings.py - Additions for School News
import re

# ENHANCED: Broader keyword detection (handles plurals/ed forms)
# ============================================================================
# IMPROVED SCHOOL NEWS KEYWORDS AND DATE PATTERNS
# ============================================================================

_SCHOOL_KEYWORDS_RE = re.compile(
    r"""
    # Core academic terms
    \b(?:academic\s+(?:calendar|session|year|break|calendar|activities|program|schedule))\b|
    \b(?:semester|session|term)\b|
    
    # Exams and assessments
    \b(?:exam(?:ination)?s?|test(?:s)?|assessment(?:s)?|quiz(?:zes)?)\b|
    \b(?:JAMB|UTME|POST-UTME|DE|Direct\s+Entry)\b|
    \b(?:WAEC|NECO|GCE|BECE|NABTEB|SSCE|SSC|WASSCE)\b|
    \b(?:result(?:s)?|score(?:s)?|mark(?:s)?|grade(?:s)?)\b|
    \b(?:mock|preliminary|practice|trial)\b|
    \b(?:rescheduled?|postponed?|cancell?ed?|deferred?|moved)\b|
    
    # Admissions and registration
    \b(?:admission(?:s)?|matriculation|enrollment|entrance)\b|
    \b(?:registration|enrolment|application(?:s)?|applying)\b|
    \b(?:cut-?off|cutoff|aggregate|score|points)\b|
    \b(?:form(?:s)?|portal|website|platform|interface)\b|
    
    # Institutions and education bodies
    \b(?:university|college|polytechnic|institute|academy|school)\b|
    \b(?:varsity|uni|poly|tech|col|inst)\b|
    \b(?:NUC|JAMB|NECO|WAEC|TETFUND|NBTI|NBTE|NCCE)\b|
    \b(?:education|educational|learning|teaching|tuition)\b|
    
    # Academic activities
    \b(?:resumption|resumed?|resum(?:ing|ption)?)\b|
    \b(?:holiday|break|vacation|recess|closure|closed?)\b|
    \b(?:strike|industrial\s+action|protest|union)\b|
    \b(?:suspend(?:ed|sion)?|suspension|halted?)\b|
    
    # Academic materials and resources
    \b(?:timetable|schedule|calendar|plan|agenda)\b|
    \b(?:syllabus|curriculum|course\s+outline|scheme)\b|
    \b(?:textbook(?:s)?|material(?:s)?|resource(?:s)?|handout(?:s)?)\b|
    
    # Fees and financials
    \b(?:fee(?:s)?|tuition|charges|payment(?:s)?)\b|
    \b(?:scholarship(?:s)?|bursary|grant(?:s)?|award(?:s)?)\b|
    \b(?:funding|finance|financial|monetary)\b|
    
    # Deadlines and announcements
    \b(?:deadline|due\s+date|closing\s+date|expir(?:y|ation))\b|
    \b(?:announcement|notice|circular|memo|bulletin|update)\b|
    \b(?:important|urgent|critical|vital|crucial)\b|
    \b(?:release(?:d)?|published?|issued?|shared?)\b|
    
    # Student activities
    \b(?:student(?:s)?|undergraduate(?:s)?|postgraduate(?:s)?)\b|
    \b(?:fresh(?:er|man)|fresher(?:s)?|new\s+student(?:s)?)\b|
    \b(?:orientation|induction|matriculation|convocation)\b|
    
    # Teaching staff
    \b(?:lecturer(?:s)?|professor(?:s)?|teacher(?:s)?|instructor(?:s)?)\b|
    \b(?:staff|faculty|academic\s+staff|non-?academic)\b|
    
    # Online platforms
    \b(?:portal|website|platform|online|digital|e-?\s*learning)\b|
    \b(?:upload|download|submit|register|apply|login)\b|
    
    # Date-related terms
    \b(?:begin(?:s|ning)?|start(?:s)?|commence(?:s|ment)?)\b|
    \b(?:end(?:s)?|conclude(?:s)?|finish(?:es)?|complete(?:s)?)\b|
    \b(?:extend(?:ed|sion)?|prolonged?|additional|extra)\b|
    
    # COVID/emergency terms (still relevant)
    \b(?:COVID|coronavirus|pandemic|lockdown|remote)\b|
    \b(?:online|virtual|digital|e-?\s*class(?:es)?)\b
    """,
    flags=re.I | re.X
)

_DATE_RE = re.compile(
    r"""
    # Full date formats (most specific first)
    (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*
    (?:\b\d{1,2}\s+
        (?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
         Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
        (?:\s+\d{4})?\b
    )|
    
    # ISO format: 2024-02-04
    \b\d{4}-\d{2}-\d{2}\b|
    
    # Month Day, Year: February 4, 2024
    \b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
       Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
    \s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|
    
    # Day Month Year: 4th February 2024
    \b\d{1,2}(?:st|nd|rd|th)?\s+
    (?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
     Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
    \s+\d{4}\b|
    
    # Numeric formats: 04/02/2024, 04-02-2024, 04.02.2024
    \b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b|
    
    # With time: February 4, 2024 8:38 pm
    \b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
       Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
    \s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)\b|
    
    # Abbreviated: Feb 4, '24 or 4 Feb '24
    \b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)
    \s+\d{1,2},?\s+(?:'?\d{2,4})\b|
    
    # Relative dates: today, yesterday, tomorrow, next week, last month
    \b(?:today|yesterday|tomorrow|now|current(?:ly)?)\b|
    \b(?:last|next|previous|upcoming|coming|forthcoming)\s+
    (?:week|month|year|semester|session|term)\b|
    
    # Year-only patterns for academic years: 2024/2025, 2024-2025
    \b\d{4}[/\-]\d{4}\b|
    
    # Quarter references: Q1 2024, 1st Quarter 2024
    \b(?:Q[1-4]|Quarter\s+[1-4])\s+\d{4}\b
    """,
    flags=re.I | re.X
)