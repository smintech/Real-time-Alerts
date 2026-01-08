from datetime import datetime

def format_alert(data):
    return (f"🚨 {data['title']} dropped to ₦{data['current_price']:,}\n"
            f"Deal score: {data['deal_score']} 🔥\n"
            f"Buy: {data['product_url']}")

def format_json_response(data):
    return {
        'price': data['price'],
        'timestamp': datetime.now().isoformat()
    }