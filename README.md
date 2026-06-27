# NSE Market Dashboard

A professional trading dashboard for the Indian NSE market, featuring:
- **3 smart stock filters** (Uptrend, 3M Performance, Volume Spike)
- **TradingView Lightweight Charts** with EMA 10/20/50/200 and Volume MA-50
- **Live data refresh** from Yahoo Finance
- **Sortable stock table** with price, daily change, and market cap

---

## 🚀 Quick Start

### Step 1 — Install Python dependencies

```bash
pip install yfinance pandas numpy openpyxl
```

### Step 2 — Fetch market data (first time)

```bash
cd trading_app
python scripts/fetch_data.py
```

> This will read `NSE_STOCK_UNIVERSE.xlsx` (2,610 stocks) and fetch financial data from Yahoo Finance.
> It may take **15–30 minutes** to complete due to API rate limits.

### Step 3 — Start the local server

**Double-click `start_server.bat`**  
or run from command prompt:

```bash
python server.py
```

### Step 4 — Open your browser

Navigate to: **http://localhost:8080**

---

## 📊 Filters

| Filter | Criteria |
|--------|----------|
| **Uptrend** | Price > ₹30, EMA(20) > EMA(50) > EMA(200), Market Cap > ₹800 Cr |
| **3M Performance** | Price > ₹30, Market Cap > ₹800 Cr, 3M Return > 30% |
| **Volume Spike** | Price > ₹30, Daily Change > 3%, Relative Volume > 3× |

---

## 📈 Chart Features

- **Candlestick chart** with 1M / 3M / 6M / 1Y / 2Y / 5Y timeframes
- **EMA 10** (orange), **EMA 20** (cyan), **EMA 50** (purple), **EMA 200** (amber)
- **Volume histogram** (green/red) with **50-day Volume MA** (blue)
- Synchronized scroll and crosshair between price and volume panes

---

## 🔄 Refreshing Data

Click the **"Refresh Market Data"** button in the dashboard to re-fetch live data.
A modal window will show the real-time log output from the Python script.

You can also schedule automatic refreshes using **Windows Task Scheduler**:
- Action: `python C:\Trade\Dashboard\market_dashboard\trading_app\scripts\fetch_data.py`
- Schedule: Daily before market open (e.g., 9:00 AM IST)

---

## 📁 File Structure

```
trading_app/
├── index.html          ← Dashboard UI
├── styles.css          ← Premium dark theme
├── app.js              ← Frontend logic & charts
├── server.py           ← Local HTTP server + API proxy
├── start_server.bat    ← Windows launcher (double-click to start)
├── data/
│   └── stocks.json     ← Generated stock data (created by fetch_data.py)
└── scripts/
    └── fetch_data.py   ← Yahoo Finance data fetcher
```

---

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `R` | Trigger data refresh |
| `↑ / ↓` | Navigate stock list |
| `Enter` | Open chart for selected stock |
| `Esc` | Close refresh modal |

---

## ⚠️ Notes

- Yahoo Finance may rate-limit requests during bulk downloads. The script includes automatic retry logic.
- Market cap data in Yahoo Finance for NSE stocks is in **INR** (not USD).
- The chart data is fetched live from Yahoo Finance each time you click a stock.
- `stocks.json` is NOT committed to git (add it to `.gitignore`).
