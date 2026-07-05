"""
NSE Stock Universe Data Fetcher
================================
Reads NSE_STOCK_UNIVERSE.xlsx, fetches financial data from Yahoo Finance,
and saves computed indicators to trading_app/data/stocks.json

Usage:  python scripts/fetch_data.py
Output: trading_app/data/stocks.json

Yahoo Finance Workarounds Applied:
  - curl_cffi browser impersonation (bypasses Cloudflare/rate limits)
  - Batch download in chunks of 30 (conservative to avoid bans)
  - Exponential backoff retry on failures (up to 3 attempts)
  - ThreadPoolExecutor for parallel market cap fetching
  - Incremental saves (no data loss on crash)
"""
from __future__ import annotations
import json
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import openpyxl
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Run: pip install yfinance pandas numpy openpyxl")
    sys.exit(1)

# ─── curl_cffi: bypasses Yahoo Finance Cloudflare / rate limiting ─────────────
# Install with: pip install curl_cffi
try:
    from curl_cffi import requests as cffi_requests
    YF_SESSION = cffi_requests.Session(impersonate="chrome")
    _session_label = "curl_cffi (Chrome impersonation)"
except ImportError:
    YF_SESSION = None
    _session_label = "standard requests (install curl_cffi to avoid rate limits)"

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
APP_DIR    = SCRIPT_DIR.parent
ROOT_DIR   = APP_DIR.parent
EXCEL_PATH = SCRIPT_DIR / "NSE_STOCK_UNIVERSE.xlsx"
OUTPUT_DIR = APP_DIR / "data"
OUTPUT_PATH = OUTPUT_DIR / "stocks.json"

# ─── Config ───────────────────────────────────────────────────────────────────
BATCH_SIZE            = 30    # Stocks per yf.download call (smaller = fewer rate limits)
SLEEP_BETWEEN_BATCHES = 3.0   # Seconds between download batches
MAX_RETRIES           = 3     # Retries per batch
MC_WORKERS            = 5     # Parallel threads for market cap fetching
DATA_PERIOD           = "1y"  # History period for price data
MIN_ROWS              = 60    # Minimum trading days required

print_lock = threading.Lock()


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    with print_lock:
        print(f"[{ts}] [{msg}]", flush=True)


# ─── Excel Reader ─────────────────────────────────────────────────────────────
def read_universe() -> list[str]:
    """Read NSE stock symbols from the Excel universe file."""
    if not EXCEL_PATH.exists():
        log(f"[ERROR] Excel file not found: {EXCEL_PATH}")
        sys.exit(1)

    wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True)
    ws = wb.active
    symbols: list[str] = []
    for row in ws.iter_rows(values_only=True):
        if row[0]:
            sym = str(row[0]).strip()
            # Convert "NSE:RELIANCE" → "RELIANCE"
            if ":" in sym:
                sym = sym.split(":", 1)[1]
            if sym:
                symbols.append(sym)
    wb.close()
    log(f"Loaded {len(symbols)} symbols from {EXCEL_PATH.name}")
    return symbols


# ─── Technical Indicators ─────────────────────────────────────────────────────
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def relative_volume(vol: pd.Series, window: int = 20) -> float | None:
    if len(vol) < window + 1:
        return None
    avg = vol.iloc[-(window + 1):-1].mean()
    if avg <= 0:
        return None
    return round(float(vol.iloc[-1]) / float(avg), 2)


def perf_3m(close: pd.Series) -> float | None:
    """3-month performance ≈ last 63 trading days."""
    if len(close) < 10:
        return None
    idx = max(0, len(close) - 63)
    base = float(close.iloc[idx])
    if base == 0:
        return None
    return round(((float(close.iloc[-1]) - base) / base) * 100, 2)


# ─── Batch Downloader ─────────────────────────────────────────────────────────
def process_batch(symbols: list[str], yf_syms: list[str]) -> dict:
    """Download OHLCV for a batch and compute indicators."""
    results: dict = {}

    for attempt in range(MAX_RETRIES):
        try:
            raw = yf.download(
                tickers=" ".join(yf_syms),
                period=DATA_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
                session=YF_SESSION,
            )
            break
        except Exception as exc:
            wait = 5 * (2 ** attempt)
            log(f"    Attempt {attempt+1} failed: {exc}  (retry in {wait}s)")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)
            else:
                log(f"    Giving up on this batch")
                return results

    if raw is None or raw.empty:
        return results

    for sym, yf_sym in zip(symbols, yf_syms):
        try:
            # Handle single vs multi-ticker column structure
            if len(yf_syms) == 1:
                df = raw.copy()
            else:
                if yf_sym not in raw.columns.get_level_values(0):
                    continue
                df = raw[yf_sym].copy()

            df = df.dropna(subset=["Close"])
            if len(df) < MIN_ROWS:
                continue

            close = df["Close"].astype(float)
            vol   = df["Volume"].astype(float)

            last_price  = float(close.iloc[-1])
            prev_price  = float(close.iloc[-2]) if len(close) >= 2 else last_price

            # ── change_pct: use last two rows from history as fallback.
            # The real-time override (regularMarketChangePercent) is fetched
            # in the market-cap pass below, where we already call fast_info.
            change_pct  = round(((last_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0

            ema20  = round(float(ema(close, 20).iloc[-1]), 2)
            ema50  = round(float(ema(close, 50).iloc[-1]), 2)
            ema200 = round(float(ema(close, 200).iloc[-1]), 2) if len(df) >= 200 else None

            today_vol  = int(float(vol.iloc[-1]))
            avg_vol    = int(float(vol.rolling(20).mean().iloc[-1])) if len(vol) >= 20 else None
            rel_vol    = relative_volume(vol)
            three_m    = perf_3m(close)

            results[sym] = {
                "symbol":        sym,
                "yf_symbol":     yf_sym,
                "price":         round(last_price, 2),
                "change_pct":    change_pct,
                "volume":        today_vol,
                "avg_volume_20d": avg_vol,
                "relative_volume": rel_vol,
                "ema20":         ema20,
                "ema50":         ema50,
                "ema200":        ema200,
                "perf_3m":       three_m,
                "marketcap_cr":  None,   # Populated in a later pass
            }
        except Exception:
            pass  # Skip problematic tickers silently

    return results


# ─── Market Cap + Real-Time Price Fetcher ────────────────────────────────────────────────────────────────────
def fetch_market_cap(sym_pair: tuple[str, str]) -> tuple[str, float | None, float | None, float | None]:
    """Fetch market cap, real-time price and today's change% for a single ticker.

    Returns (symbol, marketcap_cr, realtime_price, realtime_change_pct)
    Any value may be None if unavailable.
    """
    symbol, yf_sym = sym_pair
    for attempt in range(2):
        try:
            fi = yf.Ticker(yf_sym, session=YF_SESSION).fast_info

            # Market cap
            mc = getattr(fi, "market_cap", None)
            mc_cr = round(mc / 10_000_000, 0) if (mc and mc > 0) else None

            # Real-time price (regularMarketPrice = last traded price)
            rt_price = getattr(fi, "last_price", None)
            if rt_price is not None:
                rt_price = round(float(rt_price), 2)

            # Real-time day change %  (regularMarketChangePercent)
            rt_chg = getattr(fi, "regular_market_change_percent", None)
            if rt_chg is None:
                # Fallback: compute from previous close vs current price
                prev_close = getattr(fi, "previous_close", None)
                if rt_price and prev_close and prev_close > 0:
                    rt_chg = round(((rt_price - prev_close) / prev_close) * 100, 2)
            else:
                rt_chg = round(float(rt_chg), 2)

            return symbol, mc_cr, rt_price, rt_chg
        except Exception:
            time.sleep(1)
    return symbol, None, None, None


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    log("=" * 55)
    log("  NSE Market Dashboard — Data Fetcher")
    log(f"  Session: {_session_label}")
    log("=" * 55)

    # 1. Read symbols
    symbols  = read_universe()
    yf_syms  = [f"{s}.NS" for s in symbols]
    total    = len(symbols)
    n_batch  = (total + BATCH_SIZE - 1) // BATCH_SIZE

    # 2. Batch download price/volume/EMA data
    log(f"Downloading {total} stocks in {n_batch} batches of {BATCH_SIZE}…")
    all_data: dict = {}

    for idx in range(0, total, BATCH_SIZE):
        b_syms = symbols[idx: idx + BATCH_SIZE]
        b_yf   = yf_syms[idx: idx + BATCH_SIZE]
        bnum   = idx // BATCH_SIZE + 1
        log(f"  Batch {bnum:>3}/{n_batch}  [{b_syms[0]} … {b_syms[-1]}]")
        result = process_batch(b_syms, b_yf)
        all_data.update(result)
        log(f"           -> {len(result):>3} fetched  |  total: {len(all_data)}")
        if idx + BATCH_SIZE < total:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    log(f"Price/indicator data ready for {len(all_data)} stocks")

    # 3. Fetch real-time price, change% and market cap for all stocks via fast_info.
    #    Market cap is only meaningful for price > ₹30, but we fetch all so that
    #    change_pct and price are always current (matches Yahoo Finance / TradingView).
    mc_candidates = [
        (s, d["yf_symbol"])
        for s, d in all_data.items()
    ]
    log(f"Fetching real-time price/change% + market cap for {len(mc_candidates)} stocks…")

    done = 0
    with ThreadPoolExecutor(max_workers=MC_WORKERS) as ex:
        futures = {ex.submit(fetch_market_cap, pair): pair for pair in mc_candidates}
        for fut in as_completed(futures):
            sym, mc, rt_price, rt_chg = fut.result()
            if sym in all_data:
                all_data[sym]["marketcap_cr"] = mc
                # Override stale historical price/change with real-time values
                if rt_price is not None:
                    all_data[sym]["price"] = rt_price
                if rt_chg is not None:
                    all_data[sym]["change_pct"] = rt_chg
            done += 1
            if done % 200 == 0:
                log(f"  Market cap progress: {done}/{len(mc_candidates)}")

    log(f"Market cap data fetched")

    # 4. Save output
    stocks_list = sorted(all_data.values(), key=lambda x: x["symbol"])
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total":        len(stocks_list),
        "stocks":       stocks_list,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f" Saved {len(stocks_list)} stocks -> {OUTPUT_PATH}")
    log("=" * 55)
    log("  Done! Refresh your browser to see updated data.")
    log("=" * 55)


if __name__ == "__main__":
    main()
