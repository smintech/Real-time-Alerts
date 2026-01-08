from apify_client import ApifyClient
from config import APIFY_TOKEN

def compute_diff(old_price, new_price):
    percent = ((old_price - new_price) / old_price) * 100
    return {'changed': old_price != new_price, 'percent': percent}

def calculate_deal_score(percent_drop):
    if percent_drop > 15: return 'High'
    if percent_drop > 5: return 'Medium'
    return 'Low'

def scrape_product(url):
    client = ApifyClient(APIFY_TOKEN)
    # logic to call Jumia or Konga actor
    return {'price': 50000, 'title': 'Sample Item'}