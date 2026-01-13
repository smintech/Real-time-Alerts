import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
APIFY_TOKEN = os.getenv('APIFY_TOKEN')
DB_URL = os.getenv('DB_URL')
REDIS_URL = os.getenv('REDIS_URL')
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SITE_ACTOR_MAP = {
    'jumia.com.ng': 'fatihtahta/jumia-scraper'
}