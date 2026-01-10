import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
APIFY_TOKEN = os.getenv('APIFY_TOKEN')
DB_URL = os.getenv('DB_URL', 'postgresql://user:pass@localhost/naija_db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
ADMIN_IDS = os.getenv(ADMIN_IDS)
SUPPORTED_SITES = ['Jumia.ng', 'konga.com','binance.com']
SITE_ACTOR_MAP = {
    'jumia.ng': 'buseta/jumia-advanced-scraper'
}