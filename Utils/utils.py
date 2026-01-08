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

def compute_diff(old_data, new_data):
    """Compute changes between snapshots."""
    changed = False
    what_changed = []
    if old_data['price'] != new_data['price']:
        changed = True
        what_changed.append('price')
    # Add stock, etc.
    return {
        'changed': changed,
        'what_changed': what_changed,
        'old': old_data,
        'new': new_data
    }

def calculate_deal_score(percent_drop, historical_avg):
    """Simple intelligence: high if drop >10% below avg."""
    if percent_drop > 10 and new_data['price'] < historical_avg:
        return 'high'
    return 'medium'

def scrape_product(url, site):
    """Wrapper for Apify scraping."""
    client = ApifyClient('YOUR_APIFY_TOKEN')  # From .env
    actor_id = 'jumia-scraper' if 'jumia' in url else 'konga-scraper'
    run_input = {'urls': [url]}
    run = client.actor(actor_id).call(run_input=run_input)
    # Parse output to get price, stock, etc.
    return {'price': 750000, 'stock': 'limited'}  # Placeholder

# Add more: fuel_scrape(), tariff_scrape() using requests/BeautifulSoup if no Apify actor.