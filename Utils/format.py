from datetime import datetime

def format_alert(data):
    return (f"🚨 {data['title']} dropped to ₦{data['current_price']:,}\n"
            f"Deal score: {data['deal_score']} 🔥\n"
            f"Buy: {data['product_url']}")


def format_alert(data):
    """Clean Telegram alert string."""
    return f"🚨 {data['title']} dropped to ₦{data['current_price']} (was ₦{data['previous_price']})\n" \
           f"Deal score: {data['deal_score']} 🔥\n" \
           f"Action: {data['suggested_action']}\n" \
           f"Buy: {data['product_url']}"

def format_json_response(data):
    """API response JSON."""
    return {
        'product_url': data['url'],
        'current_price': data['price'],
        'changed': data['changed'],
        'deal_score': data['deal_score'],
        'suggested_action': 'Buy now if budget ready',
        'timestamp': datetime.now().isoformat()
    }