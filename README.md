# Real-time-Alerts 🤖 Cost Intelligence & News Aggregator Bot

**Real-time price tracking, energy cost monitoring, and curated education news for Nigeria**

-----

## 📌 Overview

This is an intelligent automation system that combines **price intelligence**, **energy cost tracking**, and **curated news aggregation** into a single Telegram-based notification platform. It solves critical information gaps for Nigerian consumers and students by delivering actionable insights directly to their phones.

### Why It Exists

Nigerians face three critical information challenges:

#### 1. **Price Chaos** 🛍️

- Products are scattered across multiple e-commerce platforms
- Prices change constantly with no unified visibility
- Consumers waste time comparing prices manually
- Missing out on good deals because there’s no centralized alert system

#### 2. **Energy Cost Blindness** ⛽🔥

- Fuel prices fluctuate unpredictably
- LPG (cooking gas) costs impact household budgets
- No real-time tracking of market movements
- People can’t plan fuel purchases or budget effectively

#### 3. **Information Fragmentation** 📚

- School news scattered across multiple websites
- Students miss important announcements about admissions, exams, results
- Parents can’t track institutional updates efficiently
- No single source for education news

### What This Bot Does

It transforms fragmented, scattered information into **curated, actionable alerts** delivered directly to Telegram.

-----

## 🎯 Core Features

### 1. 💰 **Smart Deal Broadcasting** (v1.0)

Automatically detects and broadcasts the best product deals across all major Nigerian e-commerce platforms.

#### Key Capabilities

- **Intelligent Group Monitoring** — Tracks the same product across Jumia, Konga, Binance, and other platforms
- **First-Time Detection** — Automatically announces new products with `🆕 NEW DEAL!` header on first successful scrape
- **Significant Price Drops** — Only reposts when meaningful price changes occur (configurable thresholds)
- **Best-Price Detection** — Identifies the lowest price across all sources, marks it with `✅ BEST`, and shows store-by-store comparison
- **Telegram-Optimized Formatting**
  - Clear headers (NEW / DROP %)
  - Proper currency formatting
  - Savings amount and percentage
  - Product images when available
  - Direct clickable store links

#### Example Output

```
🆕 NEW DEAL!

iPhone 15 Pro Max
₦850,000 (BEST)

📊 Price Comparison:
✅ Konga: ₦850,000 (BEST)
   Jumia: ₦875,000 (+₦25,000)
  

💾 Save ₦850,000 on the best price!

[View on Konga]
```

#### Technical Highlights

- Persistent posting history (Redis + database)
- Duplicate prevention
- Rate-limited posting with graceful error handling
- Automatic exchange-rate awareness

-----

### 2. ⛽🔥 **Real-Time Energy Cost Tracking** (v1.1)

Monitors and alerts on changes in fuel (petrol) and cooking gas (LPG) prices.

#### Fuel Price Monitoring

- **Live Petrol Prices** — Tracks average PMS prices from verified sources
- **Market Average Calculation** — Computes reliable market averages across multiple sources
- **Meaningful Movement Detection** — Posts updates only when price movements exceed configured thresholds
- **Daily Snapshot** — Prevents duplicate alerts with daily price snapshots
- **Graceful Degradation** — Continues functioning even if some price sources fail

#### Cooking Gas Tracking (LPG)

- **Real-Time Prices** — Monitors LPG depot prices across Nigeria
- **Threshold-Based Alerts** — Alerts only when prices cross configured thresholds (up or down)
- **Retail Estimate** — Calculates expected retail prices based on depot prices + typical margins
- **Household Budget Impact** — Designed to track household cost pressure

#### Intelligent Alert Logic

- **Configurable Movement Thresholds** — Set minimum price change percentage to trigger alerts
- **Cool-Down Periods** — Prevent alert spam with configurable cooldown intervals
- **Change-Focused Notifications** — Only alert on significant changes, not every price check
- **Reliable Scraping** — Defensive scraping with partial data fallback

#### Example Output

```
⛽ FUEL PRICE UPDATE

Current Average: ₦650/litre

📈 +₦25/litre (+4.0%) from last update
🔴 Last 7 days: +₦75/litre

Source: FuelPriceWatch.com
```

-----

### 3. 📚 **School News Aggregation** (v1.2)

Curates and aggregates education news from three authoritative Nigerian sources.

#### Multi-Source Coverage

- **NUC (National Universities Commission)** — University policy changes, accreditation updates, regulatory announcements
- **Punch Newspaper** — In-depth education reporting, institutional developments, sector trends
- **MySchool.ng** — Direct institutional announcements, admissions, registrations, results, timetables

#### Smart Aggregation Features

- **Unified Feed** — Single source instead of checking 3+ websites
- **Timestamp-Based Deduplication** — Prevents duplicate articles from appearing
- **Source Attribution** — Always shows where news originated
- **Category Filtering** — Filter by type (admissions, policy, events, rankings, results)
- **Cross-Source Correlation** — Identifies trending topics covered by multiple sources

#### Real-Time Notifications

- **Instant Alerts** — Breaking news delivered immediately
- **Digest Mode** — Get daily digest of non-urgent updates
- **Zero Duplicates** — Same news never appears twice

#### Technical Excellence

- **Parallel Scraping** — Fetches from all 3 sources simultaneously
- **Intelligent Caching** — Reduces API load while keeping data fresh
- **Optimized Ranking** — Most recent and relevant news first
- **Sub-Second Updates** — Feed refreshes in under 1 second for new articles
- **Source Health Monitoring** — Tracks availability of each news source
- **Graceful Degradation** — Continues working if one source is unavailable
- **Auto-Retry Logic** — Exponential backoff for temporary failures
- **Comprehensive Logging** — Detailed error tracking and alerting

#### Example Output

```
📚 NEW EDUCATION NEWS

🎓 JAMB Releases 2026 UTME Results

Students can now check their JAMB results on the JAMB
portal. Results show scores and qualifying status for
university admission...

📰 Source: Punch Newspaper
🕐 2 hours ago
🔗 [Read Full Story]

---
Related Stories:
• NUC Approves 50 New Academic Programs
• MySchool Portal: Check Your Admission Status
```

-----

## 🔧 Technical Architecture

### Core Components

```
┌─────────────────────────────────────────────────────────┐
│                  Telegram Bot Interface                  │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│              Smart Alert Engine                          │
│  • Duplicate Detection  • Deal Scoring  • Aggregation   │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│         Data Scrapers & Fetchers (3 Modules)            │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────┐ │
│  │ E-Commerce Sites │  │ Energy Trackers  │  │ News │ │
│  │ • Konga          │  │ • Fuel Watch     │  │      │ │
│  │ • Jumia          │  │ • LPG Nigeria    │  │ • NUC│ │
│  │ • Binance        │  │                  │  │ • PND│ │
│  └──────────────────┘  └──────────────────┘  │ • MSCL│ │
│                                               └──────┘ │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│        Data Processing & Enrichment Layer               │
│  • Price Parsing  • Date Extraction  • HTML Processing │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                  Persistence Layer                      │
│         • Redis Cache       • PostgreSQL Database       │
└─────────────────────────────────────────────────────────┘
```

### Key Technologies

|Layer                 |Technology                              |
|----------------------|----------------------------------------|
|**Bot Framework**     |python-telegram-bot                     |
|**Web Scraping**      |Playwright, Beautiful Soup, CloudScraper|
|**Browser Automation**|Playwright (for JS-heavy sites)         |
|**Data Processing**   |Pandas, Regex, Date/Time utilities      |
|**Caching**           |Redis                                   |
|**Database**          |PostgreSQL                              |
|**Async Runtime**     |asyncio                                 |
|**Logging**           |Python logging with structured records  |

-----

## 📊 Data Sources

### E-Commerce Platforms

- **Konga.com** — Nigeria’s leading marketplace
- **Jumia.com.ng** — Pan-African e-commerce giant
- **Binance** — Cryptocurrency exchange (for crypto prices)

### Energy Price Sources

- **FuelPriceWatch.com** — Real-time fuel prices
- **LPGinNigeria.com** — Depot prices and market data

### Education News Sources

- **NUC.edu.ng** — National Universities Commission official announcements
- **PunchNG.com** — Punch Newspaper education section
- **MySchool.ng** — School news and updates portal

-----

## 🚀 Getting Started

### Prerequisites

- Python 3.9+
- Redis server
- PostgreSQL database
- Telegram Bot Token (from @BotFather)

### Installation

```bash
# Clone repository
git clone https://github.com/yourusername/cost-intelligence-bot.git
cd cost-intelligence-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your Telegram token, Redis URL, etc.

# Initialize database
python -m bot.models.init_db

# Start the bot
python -m bot.main
```

### Configuration

```bash
# .env file
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql://user:password@localhost/bot_db

# Price tracking
MIN_CHANGE_TO_ALERT=5           # Minimum % change
HIGH_DEAL_THRESHOLD=50          # >= 50% discount = high deal
MEDIUM_DEAL_THRESHOLD=25        # >= 25% discount = medium
LOW_DEAL_THRESHOLD=10           # >= 10% discount = low

# Energy tracking
FUEL_CHECK_INTERVAL=3600        # Check every hour
LPG_CHECK_INTERVAL=7200         # Check every 2 hours

# News tracking
NEWS_CHECK_INTERVAL=1800        # Check every 30 minutes
NEWS_ALERT_COOLDOWN=7200        # 2 hours between same news
```

-----

## 📈 Usage Examples

### Add Products to Track

```python
from bot.scrapers import ecommerce

# Track iPhone price across platforms
await ecommerce.add_product_group(
    name="iPhone 15 Pro Max",
    urls=[
        "https://konga.com/...",
        "https://jumia.com.ng/...",
    ]
)
```

### Monitor Fuel Prices

```python
from bot.scrapers import energy

prices = await energy.scrape_fuel_prices()
# Output: {"avg_petrol": "₦650/litre", "change": "+₦25"}
```

### Get Latest Education News

```python
from bot.scrapers import news

articles = await news.scrape_school_news(
    urls={
        "nuc": "https://www.nuc.edu.ng",
        "punch": "https://punchng.com",
        "myschool": "https://myschool.ng"
    }
)
# Output: List of 10 most recent education articles
```

-----

## 📋 Features Matrix

|Feature             |E-Commerce|Energy|News|
|--------------------|----------|------|----|
|Real-time Tracking  |✅         |✅     |✅   |
|Multi-Source        |✅         |✅     |✅   |
|Duplicate Prevention|✅         |✅     |✅   |
|Telegram Formatted  |✅         |✅     |✅   |
|Error Recovery      |✅         |✅     |✅   |

-----

## 🏗️ Architecture Improvements

Recent releases focused on **modular architecture**, **reliability**, and **scalability**:

### Modular Code Structure

```
bot/
├── browser/              # Browser & HTTP fetching
├── scrapers/             # Scraping modules
├── persistent/           # Data structures
├── settings.py/          # Configuration parsing
├── utils/                # URL, utilities
├── news_scrapers/        # News scrapers
├── persistent/           # Data structures
└── scrapers/             # E-commerce, energy
```

### Benefits

- **Reusable Components** — Use parsers in other projects
- **Easy Testing** — Test individual modules in isolation
- **Zero Circular Imports** — Clear dependency hierarchy
- **Team Development** — Multiple developers work independently
- **Easier Maintenance** — Changes are localized

-----

## 📊 Performance Metrics

- **Price Scraping** — 1-30 seconds per site (concurrent)
- **Fuel Price Update** — <20 seconds (parallel scraping)
- **News Aggregation** — 1-50 seconds (all 3 sources simultaneously)
- **Alert Distribution** — <100ms per Telegram message
- **Duplicate Detection** — <50ms (Redis lookup)
- **Database Writes** — <200ms per transaction

-----

## 🔒 Reliability & Safety

### Duplicate Prevention

- Redis-based deduplication
- Time-window matching (prevent re-posting same item within 24h)
- Hash-based content comparison

### Rate Limiting

- Exponential backoff on failures
- Graceful degradation if sources unavailable
- Cool-down periods prevent spam

### Data Integrity

- Transaction-based database writes
- Automatic retry with exponential backoff
- Comprehensive error logging
- Source health monitoring

### Anti-Detection

- Rotating user agents
- Realistic request patterns
- Cloudflare bypass strategies
- Headless browser automation
- Request timing

-----

## 🐛 Troubleshooting

### Telegram Not Sending Messages

```bash
# Check bot token
python -c "from telegram import Bot; Bot('YOUR_TOKEN').get_me()"

# Verify chat ID
python -c "print('Chat ID correct: {your_id}')"
```

### Scraper Blocked

- Check for Cloudflare blocks
- Check for structure changes

### Missing News Articles

- Check source availability
- Verify selectors matching

-----

## 🤝 Contributing

We welcome contributions! Please:

1. Fork the repository
1. Create a feature branch (`git checkout -b feature/amazing-feature`)
1. Commit changes (`git commit -m 'Add amazing feature'`)
1. Push to branch (`git push origin feature/amazing-feature`)
1. Open an issue before submitting a Pull Request

-----

## 👤 Author

**smintech**  
[GitHub](https://github.com/smintech) · [LinkedIn](https://www.linkedin.com/in/israel-timi-99b339360?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=ios_app)

-----

## 📄 License

This project is licensed under the NON-COMMERCIAL license — see `LICENSE` file for details.

-----

## 🙏 Acknowledgments

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Telegram integration
- [Playwright](https://playwright.dev/) — Browser automation
- [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- The Nigerian developer community for feedback and ideas

-----

## ⭐ Show Your Support

If this project helps you, please consider:

- ⭐ Starring the repository
- 🐦 Sharing on social media
- 💬 Giving feedback
- 🤝 Contributing improvements

-----

**Made with ❤️ for Nigerians who value their time and money**