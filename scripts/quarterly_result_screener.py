#!/usr/bin/env python3
"""
quarterly_result_screener.py

Pipeline:
  REQ-1: Get today's result-announcing stocks (reuses get_results_mintbyte.py logic)
  REQ-2/3: For each stock, scrape screener.in for Market Cap, P/E, and
           YoY/QoQ Sales% and Profit% (from the quarterly results table)
  REQ-4: Append/save all fetched records to unfiltered_stock.json
  REQ-5: Filter records with Market Cap >= 500 Cr, append/save to filtered_stock.json
  REQ-6: Uses screener.in scraping (requests + BeautifulSoup)

Usage:
    python3 quarterly_result_screener.py
    python3 quarterly_result_screener.py --date 2026-07-29
    python3 quarterly_result_screener.py --start-date 2026-07-27 --end-date 2026-07-30   # date range, inclusive
    python3 quarterly_result_screener.py --days 5 --results-only
    python3 quarterly_result_screener.py --input my_results.xlsx           # use an Excel file instead of MintByte
    python3 quarterly_result_screener.py --input my_results.xlsx --start-date 2026-07-27 --end-date 2026-07-30
    python3 quarterly_result_screener.py --min-mcap 500
    python3 quarterly_result_screener.py --limit 10          # debug: only process first 10 tickers

Notes / caveats:
  - screener.in is scraped (no official API). It occasionally gates data behind
    login for anonymous requests, and its HTML changes over time. If you have a
    screener.in account, you can export your logged-in session cookie and set it
    as the SCREENER_COOKIE environment variable to get fuller/more reliable data:
        export SCREENER_COOKIE="sessionid=xxxx; csrftoken=yyyy"
  - IP-friendly by default:
      * jittered delay between requests (--delay, default 2s, up to 1.6x jitter)
      * exponential backoff + Retry-After handling on 429/503/5xx (--backoff, --max-retries)
      * 403 is treated as a hard "no" and is NOT retried (retrying a 403 fast just
        confirms to screener that you're a bot)
      * circuit breaker: after N consecutive blocked responses (--circuit-breaker,
        default 3) the whole run stops and saves partial results, instead of
        continuing to hammer a server that's actively refusing you
      * periodic longer cooldown every N tickers (--cooldown-every / --cooldown-seconds)
      * skips tickers already fetched cleanly for the same date on prior runs
        (use --force-refetch to override)
      * checks robots.txt once at startup and warns (non-fatal) if disallowed
      * uses a persistent requests.Session (fewer TCP handshakes than a fresh
        connection per request)
  - Still be sensible: don't drop --delay near 0, don't disable the circuit
    breaker, and if you're doing this daily consider a screener.in account /
    their paid API instead of scraping.
  - The MintByte earnings-calendar feed (used for REQ-1) is an unofficial,
    community-run mirror of NSE's corporate event calendar, not an exchange or
    licensed data-vendor API. Spot-check its output occasionally.

CHANGELOG (fix for silently-empty records on valid tickers, e.g. Apcotex):
  - _fetch_screener_page used to accept ANY 200 response containing the
    substring "screener" as a valid company page. Every page on the site
    (redirects, search pages, the homepage) contains that word somewhere
    (title/logo/footer), so a slightly-off ticker slug would return a 200,
    parse "successfully", but yield no ratios/quarters -- and get silently
    saved with error=None and all-None numeric fields. That record then
    disappears from filtered_stock.json because market_cap_cr is None.
  - Fixed by requiring the page to actually contain a #quarters section and
    a top-ratios list before treating it as a hit (_is_valid_company_page).
  - When a 200 response fails that stricter check, we now record a
    descriptive "unexpected_page: <title>" error (with the URL) instead of
    silently treating it as a clean miss, so future issues are visible in
    unfiltered_stock.json instead of just vanishing.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# REQ-1: existing MintByte earnings-calendar logic (adapted from
# get_results_mintbyte.py into an importable function)
# ----------------------------------------------------------------------------

MINTBYTE_FEED_URL = "https://mintbyte.com/api/v1/earnings-calendar/today.json"


def fetch_mintbyte_feed(days: int):
    params = {"days": days} if days else {}
    resp = requests.get(MINTBYTE_FEED_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def daterange_inclusive(start_date: str, end_date: str):
    """Return list of 'YYYY-MM-DD' strings from start_date to end_date, inclusive of both."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")
    days = []
    d = start
    while d <= end:
        days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def get_result_candidates_for_range(start_date: str, end_date: str, days: int = 7,
                                     results_only: bool = False):
    """
    Returns list of dicts: {Ticker, Company, Date, Purpose} for every
    board-meeting/result event whose event_date falls between start_date and
    end_date, INCLUSIVE of both endpoints.
    """
    target_dates = set(daterange_inclusive(start_date, end_date))

    feed = fetch_mintbyte_feed(days)
    print(f"[MintByte] as_of={feed.get('as_of')}  data_through={feed.get('data_through')}")

    events = []
    events.extend(feed.get("today", []))
    events.extend(feed.get("upcoming", []))
    events.extend(feed.get("recent_reported", []))

    rows = []
    seen = set()
    for e in events:
        event_date = e.get("event_date")
        if event_date not in target_dates:
            continue
        purpose = e.get("purpose", "")
        if results_only and "financial results" not in purpose.lower():
            continue
        symbol = e.get("symbol", "")
        # dedupe key includes the date now, since the same ticker can
        # legitimately appear on different dates within the range
        key = (symbol, purpose, event_date)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "Ticker": symbol,
            "Company": e.get("company_name", ""),
            "Date": event_date,
            "Purpose": purpose,
        })
    # stable order: earliest date first, then ticker
    rows.sort(key=lambda r: (r["Date"], r["Ticker"]))
    return rows


def get_today_result_candidates(target_date: str, days: int = 7, results_only: bool = False):
    """
    Backward-compatible single-date wrapper: returns list of dicts
    {Ticker, Company, Date, Purpose} for companies with a board-meeting/result
    event on target_date only.
    """
    return get_result_candidates_for_range(target_date, target_date, days, results_only)


# ----------------------------------------------------------------------------
# REQ-1 (alternative source): Excel file with Symbol/Date columns, used
# instead of the MintByte feed when --input <file.xlsx> is passed.
# ----------------------------------------------------------------------------

def _normalize_excel_date(value):
    """Coerce a pandas cell (Timestamp, date, or date-like string) to 'YYYY-MM-DD', or None if unparseable."""
    import pandas as pd
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def get_result_candidates_from_excel(path: str, date_filter=None, results_only: bool = False):
    """
    Read result candidates from an Excel file instead of the MintByte feed.

    Expects a 'Symbol' (or 'Ticker') column and a 'Date' (or any column with
    "date" in its name, e.g. 'Date of Result') column. Optional 'Company'
    and 'Purpose' columns are used if present; Purpose defaults to
    "Financial Results" when the column is absent so --results-only still
    behaves sensibly.

    date_filter: optional set of 'YYYY-MM-DD' strings. If given, only rows
    whose date falls in this set are kept (used when the user also passed
    --date/--start-date/--end-date alongside --input). If None, every row
    in the file is used as-is.

    Returns the same shape as get_result_candidates_for_range: a list of
    dicts {Ticker, Company, Date, Purpose}, sorted by (Date, Ticker).
    """
    import pandas as pd

    try:
        df = pd.read_excel(path)
    except Exception as e:
        raise ValueError(f"could not read Excel file '{path}': {e}") from e

    colmap = {str(c).strip().lower(): c for c in df.columns}
    symbol_col = next((colmap[k] for k in ("symbol", "ticker") if k in colmap), None)
    date_col = next((orig for key, orig in colmap.items() if "date" in key), None)
    company_col = next((colmap[k] for k in ("company", "company name", "name") if k in colmap), None)
    purpose_col = next((orig for key, orig in colmap.items() if "purpose" in key), None)

    if symbol_col is None or date_col is None:
        raise ValueError(
            f"Excel file '{path}' must have a 'Symbol' (or 'Ticker') column and a "
            f"'Date' column (e.g. 'Date of Result'). Found columns: {list(df.columns)}"
        )

    rows = []
    skipped_bad_date = 0
    for _, r in df.iterrows():
        raw_symbol = r[symbol_col]
        if pd.isna(raw_symbol):
            continue
        symbol = str(raw_symbol).strip().upper()
        if not symbol:
            continue

        date_str = _normalize_excel_date(r[date_col])
        if date_str is None:
            skipped_bad_date += 1
            continue

        if date_filter is not None and date_str not in date_filter:
            continue

        company = ""
        if company_col is not None and pd.notna(r[company_col]):
            company = str(r[company_col]).strip()

        purpose = "Financial Results"
        if purpose_col is not None and pd.notna(r[purpose_col]):
            purpose = str(r[purpose_col]).strip()

        if results_only and "financial results" not in purpose.lower():
            continue

        rows.append({"Ticker": symbol, "Company": company, "Date": date_str, "Purpose": purpose})

    if skipped_bad_date:
        print(f"  [warn] skipped {skipped_bad_date} row(s) in '{path}' with an unparseable date")

    # dedupe on (Ticker, Date), same as the MintByte path
    seen = set()
    deduped = []
    for row in rows:
        key = (row["Ticker"], row["Date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    deduped.sort(key=lambda r: (r["Date"], r["Ticker"]))
    return deduped


# ----------------------------------------------------------------------------
# REQ-2/3/6: screener.in scraping
# ----------------------------------------------------------------------------

SCREENER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Referer": "https://www.screener.in/",
}

SCREENER_COOKIE = os.environ.get("SCREENER_COOKIE", "")
if SCREENER_COOKIE:
    SCREENER_HEADERS["Cookie"] = SCREENER_COOKIE

SCREENER_SESSION = requests.Session()
SCREENER_SESSION.headers.update(SCREENER_HEADERS)

_SESSION_WARMED_UP = False


def _warm_up_session():
    """
    Visit the homepage once before scraping any company pages. Fetching a
    company page cold (as the very first request of a session, with no
    referer/cookie history) is a much more bot-like traffic pattern than a
    real visitor navigating in from the homepage or a search, and appears to
    be what triggers screener.in to serve a blanked-out data skeleton (200
    OK, but every ratio/quarterly value empty) instead of the real page --
    confirmed by comparing a raw `requests` fetch against a normal browser
    fetch of the identical URL, where only the former came back empty.
    This also picks up any session cookies screener's homepage sets.
    """
    global _SESSION_WARMED_UP
    if _SESSION_WARMED_UP:
        return
    try:
        SCREENER_SESSION.get("https://www.screener.in/", timeout=15)
    except requests.RequestException as e:
        print(f"  [warn] session warm-up request failed (continuing anyway): {e}")
    _SESSION_WARMED_UP = True


BLOCKED_STATUS_CODES = {403, 429, 503}


def check_robots_allowed(path: str = "/company/") -> bool:
    """
    Best-effort robots.txt check. Returns True if scraping is allowed (or if
    robots.txt can't be read, in which case we assume allowed but the caller
    should still scrape conservatively). This is advisory, not a hard gate --
    but if it comes back False you should stop and reconsider.
    """
    try:
        import urllib.robotparser
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url("https://www.screener.in/robots.txt")
        rp.read()
        return rp.can_fetch(SCREENER_HEADERS["User-Agent"], f"https://www.screener.in{path}")
    except Exception:
        return True


def _clean_number(text: str):
    """'1,23,456' / '12.4%' / '₹ 1,869 Cr.' -> float, or None if not parseable."""
    if text is None:
        return None
    t = text.replace(",", "").replace("₹", "").replace("%", "").strip()
    t = re.sub(r"[^0-9.\-]", "", t)
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


RETRY_SETTINGS = {"max_retries": 3, "base_backoff": 5.0}


def _request_with_retry(url: str, max_retries: int = None, base_backoff: float = None):
    max_retries = RETRY_SETTINGS["max_retries"] if max_retries is None else max_retries
    base_backoff = RETRY_SETTINGS["base_backoff"] if base_backoff is None else base_backoff
    """
    GET url with exponential backoff on network errors / 5xx, and honors the
    Retry-After header (or backs off exponentially) on 429 rate-limit responses.
    Returns the final requests.Response, or raises on repeated network failure.
    """
    resp = None
    for attempt in range(max_retries + 1):
        try:
            resp = SCREENER_SESSION.get(url, timeout=15)
        except requests.RequestException as e:
            if attempt == max_retries:
                raise
            wait = base_backoff * (2 ** attempt)
            print(f"    [retry] network error ({e}); backing off {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        if resp.status_code in (429, 503):
            if attempt == max_retries:
                return resp
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else base_backoff * (2 ** attempt)
            print(f"    [retry] rate limited/unavailable ({resp.status_code}); backing off {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            # Don't hammer retries on a 403 -- that's screener actively refusing
            # us, and retrying fast just confirms to them that we're a bot.
            return resp

        if resp.status_code >= 500:
            if attempt == max_retries:
                return resp
            wait = base_backoff * (2 ** attempt)
            print(f"    [retry] server error {resp.status_code}; backing off {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        return resp
    return resp


_GENERIC_TITLE_MARKERS = (
    "stock analysis tool",   # screener.in homepage title
    "search results",
    "page not found",
    "404",
)


def _is_company_page(soup: BeautifulSoup) -> bool:
    """
    Confirm we actually landed on a company page (as opposed to a redirect
    to the homepage, a search-results page, or a 404-styled page) using the
    page <title>, which screener always renders server-side as
    "<Company> share price | About <Company> | Key Insights - Screener".
    This is deliberately independent of the ratios/quarterly-table markup:
    that markup can be legitimately absent (anonymous-request gating, or
    screener changing its HTML) even on a perfectly valid company page, so
    checking for it here would wrongly reject good pages.
    """
    if not soup.title:
        return False
    title = soup.title.get_text(strip=True).lower()
    if any(marker in title for marker in _GENERIC_TITLE_MARKERS):
        return False
    return "share price" in title or "screener" in title


def _page_has_data(soup: BeautifulSoup) -> bool:
    """
    Cheap check for whether a (title-valid) company page actually carries
    real numbers, as opposed to a blanked skeleton -- e.g. the /consolidated/
    URL variant for a single-entity company that has no consolidated
    financials, which can 200 with the right title but every value empty.
    """
    top_ratios = soup.find(id="top-ratios") or soup.find("ul", class_="company-ratios")
    if top_ratios:
        for num_span in top_ratios.find_all(class_="number"):
            if num_span.get_text(strip=True):
                return True
    section = soup.find("section", id="quarters")
    if section:
        table = section.find("table")
        if table:
            header_row = table.find("tr")
            if header_row and len(header_row.find_all(["th", "td"])) > 1:
                return True
    return False


def _fetch_screener_page(ticker: str):
    """
    Try consolidated page first, fall back to standalone if consolidated
    doesn't exist -- OR if it exists but is a blanked skeleton, which
    happens for single-entity companies with no consolidated financials
    (the /consolidated/ URL still 200s with the right title, but every
    ratio/quarterly value comes back empty; the bare URL for the same
    company has the real standalone data). A variant is only accepted once
    it both looks like the right company page AND actually has data; if a
    variant is valid-but-empty we remember it and keep trying the other
    variant before giving up.

    Returns (soup, url, blocked, unexpected_title) where:
      - soup/url are set on success (or on the best-effort blank page found,
        if neither variant had data -- so downstream diagnostics still have
        something to inspect)
      - blocked=True if screener actively refused us (403/429/503 after retries),
        as opposed to a plain 404 (ticker doesn't have that page variant).
      - unexpected_title is set if we got a 200 response that did NOT look
        like a real company page (e.g. symbol mismatch/redirect), so the
        caller can surface a useful error instead of a silent miss.
    """
    blocked = False
    unexpected_title = None
    blank_fallback = None  # (soup, url) of a title-valid but data-empty page
    for variant in ("consolidated/", ""):
        url = f"https://www.screener.in/company/{ticker}/{variant}"
        try:
            resp = _request_with_retry(url)
        except requests.RequestException as e:
            print(f"  [warn] request error for {ticker} ({url}): {e}")
            continue
        if resp is None:
            continue
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if _is_company_page(soup):
                if _page_has_data(soup):
                    return soup, url, False, None
                # Right company, but this variant is a blanked skeleton --
                # keep it as a fallback and try the other variant first.
                if blank_fallback is None:
                    blank_fallback = (soup, url)
                continue
            # Got a 200, but it's a generic/redirect page, not this company's
            # -- capture why, but keep trying the other URL variant first.
            title = soup.title.get_text(strip=True) if soup.title else "no <title>"
            unexpected_title = f"{title} ({url})"
            continue
        if resp.status_code in BLOCKED_STATUS_CODES:
            blocked = True
        # 404/redirect -> try next variant

    if blank_fallback:
        # Neither variant had data; return the blank one we found so callers
        # (fetch_screener_data's diagnostics) can still report specifics
        # instead of a bare page_not_found.
        soup, url = blank_fallback
        return soup, url, blocked, None

    return None, None, blocked, unexpected_title


def _extract_top_ratios(soup: BeautifulSoup):
    """
    Screener shows a list of top ratios (Market Cap, Current Price, Stock P/E,
    etc.) as <li> items with a name span and a number span. Grab Market Cap
    and Stock P/E from there.

    Falls back to a text-regex scan of the whole page if the structured
    <li> lookup finds nothing -- this makes extraction survive screener
    renaming/restructuring its ratio-list markup (class names, tag types)
    without needing this scraper updated in lockstep.
    """
    result = {
        "market_cap_cr": None, "pe_ratio": None,
        "market_cap_label_seen": False, "pe_label_seen": False,
    }
    for li in soup.find_all("li"):
        name_el = li.find(class_="name")
        value_el = li.find(class_="number") or li.find(class_="value")
        if not name_el or not value_el:
            continue
        name = name_el.get_text(strip=True).lower()
        raw_value_text = value_el.get_text(strip=True)
        value = _clean_number(raw_value_text)
        if "market cap" in name:
            result["market_cap_label_seen"] = True
            result["market_cap_cr"] = value
        elif "stock p/e" in name or name == "p/e":
            result["pe_label_seen"] = True
            result["pe_ratio"] = value

    text = None
    if result["market_cap_cr"] is None or result["pe_ratio"] is None:
        text = soup.get_text(separator=" ", strip=True)
        if result["market_cap_cr"] is None:
            m = re.search(r"market\s*cap\s*₹?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
            if m:
                result["market_cap_cr"] = _clean_number(m.group(1))
        if result["pe_ratio"] is None:
            m = re.search(r"stock\s*p\s*/\s*e\s*([\-\d,]+\.?\d*)", text, re.IGNORECASE)
            if m:
                result["pe_ratio"] = _clean_number(m.group(1))

    # Even if we never found a structured <li>, note whether the plain text
    # "market cap" / "stock p/e" appears anywhere on the page at all -- this
    # tells us whether the label truly doesn't exist (structural/wording
    # mismatch) versus exists but its value never parsed (more consistent
    # with the number being injected by client-side JS after the initial
    # HTML we fetched, or screener serving blanked placeholders).
    if not result["market_cap_label_seen"] or not result["pe_label_seen"]:
        if text is None:
            text = soup.get_text(separator=" ", strip=True)
        if not result["market_cap_label_seen"] and re.search(r"market\s*cap", text, re.IGNORECASE):
            result["market_cap_label_seen"] = True
        if not result["pe_label_seen"] and re.search(r"stock\s*p\s*/\s*e", text, re.IGNORECASE):
            result["pe_label_seen"] = True

    return result


SALES_ROW_LABELS = ["sales", "revenue", "total income", "income from operations", "interest earned"]
PROFIT_ROW_LABELS = ["net profit", "profit after tax", "pat"]


def _find_row(table_rows, labels):
    """
    Find a <tr> whose first cell text matches one of the given labels.
    Uses a normalized, whole-word substring match (not just startswith) so
    labels like "Total Revenue", "Sales +", or "Income From Operations"
    still match even when they don't begin exactly with the plain label --
    screener's row wording varies by sector (manufacturing vs financials vs
    banks) and has changed over time.
    """
    for tr in table_rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row_label = cells[0].get_text(strip=True).lower()
        row_label = row_label.replace("\xa0", " ").replace("+", " ")
        row_label = re.sub(r"\s+", " ", row_label).strip()
        for label in labels:
            if re.search(r"\b" + re.escape(label) + r"\b", row_label):
                return cells
    return None


def _find_quarterly_table(soup: BeautifulSoup):
    """
    Locate the Quarterly Results table. Prefers the section id="quarters"
    (screener's current markup), but falls back to scanning every <table> on
    the page for one that actually has a Sales/Revenue row -- this way, if
    screener renames or drops that section id, we still find the right table
    instead of silently grabbing the first (possibly unrelated) table.
    Returns None if no table anywhere on the page has a recognizable
    sales/revenue row.
    """
    section = soup.find("section", id="quarters")
    if section:
        table = section.find("table")
        if table and _find_row(table.find_all("tr"), SALES_ROW_LABELS):
            return table

    for table in soup.find_all("table"):
        if _find_row(table.find_all("tr"), SALES_ROW_LABELS):
            return table

    return None


def _extract_quarterly_growth(soup: BeautifulSoup):
    """
    Parse the Quarterly Results table and compute YoY (vs same quarter, 4
    columns back) and QoQ (vs previous column) growth for Sales and Net
    Profit. Also returns the label of the most recent quarter column (e.g.
    "Jun 2026"), taken from the table header.

    Also returns diagnostic flags (table_found, sales_row_found,
    profit_row_found) so the caller can tell a genuine "no data" apart from
    "we found the table but couldn't match one of the two rows" -- both look
    like all-None output otherwise, but need different error messages.
    """
    out = {
        "sales_yoy_pct": None,
        "sales_qoq_pct": None,
        "profit_yoy_pct": None,
        "profit_qoq_pct": None,
        "quarters_available": 0,
        "quarter": None,
        "table_found": False,
        "sales_row_found": False,
        "profit_row_found": False,
        "header_columns_found": False,
        "row_values_blank": False,
    }

    table = _find_quarterly_table(soup)
    if not table:
        return out
    out["table_found"] = True

    header_row = table.find("tr")
    if header_row:
        header_cells = header_row.find_all(["th", "td"])
        # first header cell is the row-label column ("Quarterly Results" / blank);
        # the rest are quarter labels, oldest -> newest
        if len(header_cells) > 1:
            out["header_columns_found"] = True
            out["quarter"] = header_cells[-1].get_text(strip=True) or None

    rows = table.find_all("tr")
    sales_cells = _find_row(rows, SALES_ROW_LABELS)
    profit_cells = _find_row(rows, PROFIT_ROW_LABELS)
    out["sales_row_found"] = sales_cells is not None
    out["profit_row_found"] = profit_cells is not None

    def to_series(cells):
        if not cells:
            return []
        # cells[0] is the row label, rest are quarterly values (oldest -> newest)
        return [_clean_number(c.get_text(strip=True)) for c in cells[1:]]

    sales_vals = to_series(sales_cells)
    profit_vals = to_series(profit_cells)
    out["quarters_available"] = len(sales_vals)

    # The row(s) and/or header columns exist structurally, but every cell
    # came back unparseable/blank -- this pattern (labels/columns present,
    # numbers absent) is the signature of client-side-rendered values that
    # never appear in the raw HTML `requests` fetches, as opposed to a
    # structural mismatch.
    all_sales_blank = bool(sales_cells) and len(sales_cells) > 1 and all(v is None for v in sales_vals)
    all_profit_blank = bool(profit_cells) and len(profit_cells) > 1 and all(v is None for v in profit_vals)
    out["row_values_blank"] = all_sales_blank or all_profit_blank

    def pct_change(vals, back):
        if len(vals) <= back:
            return None
        latest, prior = vals[-1], vals[-1 - back]
        if prior in (None, 0) or latest is None:
            return None
        return round((latest - prior) / abs(prior) * 100, 2)

    out["sales_qoq_pct"] = pct_change(sales_vals, 1)
    out["sales_yoy_pct"] = pct_change(sales_vals, 4)
    out["profit_qoq_pct"] = pct_change(profit_vals, 1)
    out["profit_yoy_pct"] = pct_change(profit_vals, 4)
    return out


def get_expected_quarter(date_str: str):
    """
    Given a result-announcement date (YYYY-MM-DD), return the quarter-end
    label (e.g. "Jun 2026") that we'd EXPECT to be the latest column on
    screener.in once the just-announced results are reflected there.

    Indian companies report with a lag after quarter-end, roughly:
      Jan-Mar (Q4/annual) results announced Apr-Jun     -> expect "Mar {y}"
      Apr-Jun (Q1)        results announced Jul-Sep      -> expect "Jun {y}"
      Jul-Sep (Q2)        results announced Oct-Dec      -> expect "Sep {y}"
      Oct-Dec (Q3)        results announced Jan-Mar      -> expect "Dec {y-1}"
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    m, y = dt.month, dt.year
    if m in (1, 2, 3):
        return f"Dec {y - 1}"
    if m in (4, 5, 6):
        return f"Mar {y}"
    if m in (7, 8, 9):
        return f"Jun {y}"
    return f"Sep {y}"


def _normalize_quarter_label(label):
    """Loose-normalize a quarter label for comparison ('Jun 2026', 'JUN 2026' -> 'jun 2026')."""
    if not label:
        return None
    return re.sub(r"\s+", " ", label).strip().lower()


DEBUG_DUMP_DIR = None  # set via --debug-dir; when set, pages that yield zero
                       # extracted data get their raw HTML saved for inspection


def fetch_screener_data(ticker: str):
    """
    Returns a dict with market_cap_cr, pe_ratio, sales_yoy_pct, sales_qoq_pct,
    profit_yoy_pct, profit_qoq_pct, quarter, screener_url, a 'blocked' bool,
    and an 'error' key if something went wrong.
    """
    soup, url, blocked, unexpected_title = _fetch_screener_page(ticker)
    if soup is None:
        if unexpected_title:
            error = f"unexpected_page: {unexpected_title}"
        elif blocked:
            error = "blocked_by_screener"
        else:
            error = "page_not_found"
        return {
            "market_cap_cr": None, "pe_ratio": None,
            "sales_yoy_pct": None, "sales_qoq_pct": None,
            "profit_yoy_pct": None, "profit_qoq_pct": None,
            "quarter": None,
            "screener_url": None,
            "blocked": blocked,
            "error": error,
        }

    data = {"screener_url": url, "blocked": False, "error": None}
    try:
        data.update(_extract_top_ratios(soup))
    except Exception as e:
        data["error"] = f"ratio_parse_error: {e}"
        data.setdefault("market_cap_cr", None)
        data.setdefault("pe_ratio", None)

    try:
        data.update(_extract_quarterly_growth(soup))
    except Exception as e:
        prev_err = data.get("error")
        data["error"] = (prev_err + "; " if prev_err else "") + f"quarterly_parse_error: {e}"
        data.setdefault("sales_yoy_pct", None)
        data.setdefault("sales_qoq_pct", None)
        data.setdefault("profit_yoy_pct", None)
        data.setdefault("profit_qoq_pct", None)

    # Surface *partial* failures too, not just total ones, and distinguish
    # "the label/structure genuinely isn't on the page" from "the label or
    # column structure IS there, but every value came back blank/unparseable"
    # -- the second pattern points to client-side-rendered numbers that
    # never appear in the raw HTML `requests` fetches (as opposed to a
    # markup/wording mismatch that broader label-matching could fix).
    diagnostics = []
    hydration_suspected = False

    if data.get("market_cap_cr") is None and data.get("pe_ratio") is None:
        if data.get("market_cap_label_seen") or data.get("pe_label_seen"):
            diagnostics.append(
                "ratios_label_found_but_value_missing: the 'Market Cap'/'Stock "
                "P/E' label(s) are present on the page, but no parseable number "
                "follows them"
            )
            hydration_suspected = True
        else:
            diagnostics.append(
                "ratios_not_found: neither the 'Market Cap' nor 'Stock P/E' "
                "label appears anywhere on the page"
            )

    if not data.get("table_found"):
        diagnostics.append("quarterly_table_not_found: no table on the page had "
                            "a recognizable Sales/Revenue row")
    elif not (data.get("sales_row_found") and data.get("profit_row_found")):
        missing = []
        if not data.get("sales_row_found"):
            missing.append("Sales/Revenue")
        if not data.get("profit_row_found"):
            missing.append("Net Profit")
        diagnostics.append(
            "quarterly_row_not_found: table located, but couldn't match a "
            f"row for: {', '.join(missing)} (label wording on this page may "
            "differ from the known variants)"
        )
    elif not data.get("header_columns_found"):
        diagnostics.append(
            "quarterly_header_columns_missing: rows for Sales/Net Profit were "
            "found, but the header row has no quarter-label columns (e.g. "
            "'Jun 2026') to attach values to"
        )
        hydration_suspected = True
    elif data.get("row_values_blank") or data.get("quarters_available", 0) == 0:
        diagnostics.append(
            "quarterly_values_blank: the Sales/Net Profit rows and quarter "
            "columns exist structurally, but every cell's value is blank or "
            "unparseable"
        )
        hydration_suspected = True

    if diagnostics and not data.get("error"):
        if hydration_suspected:
            likely_cause = (
                "This specific pattern (structure/labels present, numeric "
                "values blank) has been confirmed to be screener.in serving "
                "a stripped-down skeleton page (still HTTP 200) to requests "
                "that look bot-like, while an identical URL fetched normally "
                "returns full data -- not a universal move to client-side "
                "rendering. This scraper now warms up the session with a "
                "homepage visit and sends fuller browser-like headers before "
                "hitting company pages, which should resolve most cases. If "
                "it still happens: try SCREENER_COOKIE (a logged-in session "
                "cookie), add more delay between requests, or as a last "
                "resort a JS-capable fetch (Playwright/Selenium)."
            )
        else:
            likely_cause = (
                "Likely an anonymous-request gating by screener.in (try "
                "setting SCREENER_COOKIE) or a change in screener's HTML/"
                "label wording."
            )
        data["error"] = (
            f"partial_or_empty_extraction on {url} -- " + "; ".join(diagnostics) +
            f". {likely_cause} Inspect the dumped HTML if --debug-dir was "
            "set, or fetch the URL manually to compare."
        )
        if DEBUG_DUMP_DIR:
            try:
                os.makedirs(DEBUG_DUMP_DIR, exist_ok=True)
                dump_path = os.path.join(DEBUG_DUMP_DIR, f"{ticker}.html")
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(str(soup))
                print(f"    [debug] dumped raw HTML to {dump_path}")
            except OSError as e:
                print(f"    [warn] could not write debug dump for {ticker}: {e}")

    # Internal-only flags; keep them out of the persisted record.
    for flag in ("table_found", "sales_row_found", "profit_row_found",
                 "header_columns_found", "row_values_blank",
                 "market_cap_label_seen", "pe_label_seen"):
        data.pop(flag, None)

    return data


# ----------------------------------------------------------------------------
# REQ-4/5: JSON append/save helpers
# ----------------------------------------------------------------------------

def load_json_list(path: str):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = json.load(f)
        return content if isinstance(content, list) else []
    except (json.JSONDecodeError, OSError):
        print(f"  [warn] could not read existing {path}, starting fresh")
        return []


def append_and_save(path: str, new_records: list, dedupe_keys=("Ticker", "Date")):
    existing = load_json_list(path)
    existing_keys = {tuple(rec.get(k) for k in dedupe_keys) for rec in existing}

    added = 0
    for rec in new_records:
        key = tuple(rec.get(k) for k in dedupe_keys)
        if key in existing_keys:
            continue
        existing.append(rec)
        existing_keys.add(key)
        added += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    print(f"  Saved {added} new record(s) to {path} (total now {len(existing)})")
    return existing


# ----------------------------------------------------------------------------
# Main pipeline
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch today's results, enrich with screener.in data, filter by market cap")
    parser.add_argument("--date", default=None,
                         help="Single date in YYYY-MM-DD format (default: today). "
                              "Ignored if --start-date/--end-date are given.")
    parser.add_argument("--start-date", default=None,
                         help="Start of date range (YYYY-MM-DD), inclusive. Must be used "
                              "together with --end-date.")
    parser.add_argument("--end-date", default=None,
                         help="End of date range (YYYY-MM-DD), inclusive. Must be used "
                              "together with --start-date.")
    parser.add_argument("--input", default=None,
                         help="Path to an Excel (.xlsx) file with 'Symbol'/'Ticker' and "
                              "'Date' (e.g. 'Date of Result') columns, used INSTEAD of the "
                              "MintByte feed as the source of result candidates. Optional "
                              "'Company' and 'Purpose' columns are used if present. If you "
                              "also pass --date/--start-date/--end-date, rows are additionally "
                              "filtered to that range; otherwise every row in the file is used.")
    parser.add_argument("--days", type=int, default=7,
                         help="How many days ahead the MintByte feed should fetch (1-14, "
                              "default 7; auto-increased if your date range needs more coverage)")
    parser.add_argument("--results-only", action="store_true",
                         help="Only keep 'Financial Results' events (drop dividend-only/fund-raising events)")
    parser.add_argument("--min-mcap", type=float, default=500,
                         help="Minimum market cap in Cr for the filtered output (default 500)")
    parser.add_argument("--delay", type=float, default=2.0,
                         help="Base seconds to sleep between screener.in requests; actual "
                              "sleep is jittered up to 1.6x this to look less bot-like (default 2.0)")
    parser.add_argument("--max-retries", type=int, default=3,
                         help="Max retries per screener.in request on 429/5xx/network errors (default 3)")
    parser.add_argument("--backoff", type=float, default=5.0,
                         help="Base seconds for exponential backoff on retry (default 5.0)")
    parser.add_argument("--circuit-breaker", type=int, default=3,
                         help="Stop the whole run after this many CONSECUTIVE blocked "
                              "(403/429/503) responses, instead of continuing to hammer "
                              "screener.in (default 3, 0 disables)")
    parser.add_argument("--cooldown-every", type=int, default=15,
                         help="Take a longer cooldown pause every N tickers (default 15, 0 disables)")
    parser.add_argument("--cooldown-seconds", type=float, default=30.0,
                         help="Length of the periodic cooldown pause in seconds (default 30)")
    parser.add_argument("--force-refetch", action="store_true",
                         help="Re-fetch tickers even if already present for their Date in "
                              "unfiltered_stock.json (default: skip already-fetched tickers)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process first N tickers (useful for testing)")
    parser.add_argument("--unfiltered-out", default="unfiltered_stock.json")
    parser.add_argument("--filtered-out", default="filtered_stock.json")
    parser.add_argument("--debug-dir", default=None,
                         help="If set, dump raw HTML for any ticker whose page loads "
                              "successfully but yields zero ratios/quarterly data, so you "
                              "can inspect why extraction failed (e.g. anonymous-gating vs "
                              "changed HTML structure).")
    args = parser.parse_args()

    global DEBUG_DUMP_DIR
    DEBUG_DUMP_DIR = args.debug_dir

    # Resolve the effective date range: --start-date/--end-date takes
    # precedence; otherwise fall back to single-day --date (or today, only
    # when no --input file is given -- an Excel input with no explicit date
    # args should use every row in the file, not just "today").
    explicit_date_range = bool(args.start_date or args.end_date or args.date)
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            parser.error("--start-date and --end-date must be provided together")
        start_date, end_date = args.start_date, args.end_date
    elif args.date:
        start_date = end_date = args.date
    elif args.input:
        start_date = end_date = None  # no filtering; use every row in the file
    else:
        single = datetime.now().strftime("%Y-%m-%d")
        start_date = end_date = single

    if start_date is not None:
        try:
            target_dates = daterange_inclusive(start_date, end_date)
        except ValueError as e:
            parser.error(str(e))
    else:
        target_dates = None

    # Auto-widen the MintByte feed's lookahead window to cover the requested
    # range, unless the user explicitly overrode --days themselves. Not
    # applicable when reading from --input.
    if not args.input and args.days == 7 and target_dates and len(target_dates) > 5:
        args.days = min(14, len(target_dates) + 2)
        print(f"  [info] auto-adjusted --days to {args.days} to cover the {start_date}..{end_date} range")

    if args.input:
        range_label = (f"Excel file '{args.input}'"
                        + (f" filtered to {start_date}..{end_date}" if explicit_date_range else " (all rows)"))
    else:
        range_label = start_date if start_date == end_date else f"{start_date} to {end_date} (inclusive)"

    # REQ-1
    print(f"Fetching result candidates from {'Excel input' if args.input else 'MintByte feed'} "
          f"for {range_label} ...")
    try:
        if args.input:
            date_filter = set(target_dates) if (explicit_date_range and target_dates) else None
            candidates = get_result_candidates_from_excel(args.input, date_filter, args.results_only)
        else:
            candidates = get_result_candidates_for_range(start_date, end_date, args.days, args.results_only)
    except Exception as e:
        source = f"Excel file '{args.input}'" if args.input else "MintByte feed"
        print(f"Error reading {source}: {e}", file=sys.stderr)
        sys.exit(1)

    if not candidates:
        print(f"No companies found reporting between {range_label}. Nothing to enrich.")
        return

    if args.limit:
        candidates = candidates[: args.limit]

    RETRY_SETTINGS["max_retries"] = args.max_retries
    RETRY_SETTINGS["base_backoff"] = args.backoff

    if not check_robots_allowed():
        print("  [warn] screener.in's robots.txt disallows this path for our user-agent. "
              "Proceeding is your call, but consider stopping here.")

    # Skip tickers we've already fetched cleanly for their date (unless
    # forced) -- avoids re-hitting screener.in for no reason on repeated runs.
    already_fetched = set()
    if not args.force_refetch:
        # When no explicit date range was given (e.g. --input with no
        # --date/--start-date/--end-date), target_dates is None -- derive
        # the relevant date set from the candidates actually loaded instead.
        target_date_set = set(target_dates) if target_dates else {c["Date"] for c in candidates}
        for rec in load_json_list(args.unfiltered_out):
            if rec.get("Date") in target_date_set and not rec.get("error"):
                already_fetched.add((rec.get("Ticker"), rec.get("Date")))
        if already_fetched:
            print(f"Skipping {len(already_fetched)} ticker-date pair(s) already fetched cleanly "
                  f"for {range_label} (use --force-refetch to redo them)")

    print(f"Found {len(candidates)} candidate(s). Enriching with screener.in data ...")
    print("  [info] warming up session with a homepage visit before scraping company pages ...")
    _warm_up_session()

    unfiltered_records = []
    consecutive_blocked = 0
    fetch_count = 0

    for i, cand in enumerate(candidates, 1):
        ticker = cand["Ticker"]

        if (ticker, cand["Date"]) in already_fetched:
            print(f"[{i}/{len(candidates)}] {ticker} ({cand['Date']}): already fetched, skipping")
            continue

        print(f"[{i}/{len(candidates)}] {ticker} ({cand['Company']}) ...")
        screener_data = fetch_screener_data(ticker)
        fetch_count += 1

        expected_q = get_expected_quarter(cand["Date"])
        actual_q = screener_data.get("quarter")
        actual_q_norm = _normalize_quarter_label(actual_q)
        expected_q_norm = _normalize_quarter_label(expected_q)
        stale = None if (actual_q_norm is None or expected_q_norm is None) else (actual_q_norm != expected_q_norm)

        record = {
            "Ticker": ticker,
            "Company": cand["Company"],
            "Date": cand["Date"],
            "Purpose": cand["Purpose"],
            "market_cap_cr": screener_data.get("market_cap_cr"),
            "pe_ratio": screener_data.get("pe_ratio"),
            "sales_yoy_pct": screener_data.get("sales_yoy_pct"),
            "sales_qoq_pct": screener_data.get("sales_qoq_pct"),
            "profit_yoy_pct": screener_data.get("profit_yoy_pct"),
            "profit_qoq_pct": screener_data.get("profit_qoq_pct"),
            "quarter": actual_q,
            "expected_quarter": expected_q,
            "stale": stale,
            "screener_url": screener_data.get("screener_url"),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "error": screener_data.get("error"),
        }
        unfiltered_records.append(record)

        if screener_data.get("error"):
            print(f"    [warn] {screener_data['error']}")

        # Circuit breaker: if screener.in is actively blocking us, stop the
        # whole run rather than continuing to hammer it with retries.
        if screener_data.get("blocked"):
            consecutive_blocked += 1
        else:
            consecutive_blocked = 0

        if args.circuit_breaker and consecutive_blocked >= args.circuit_breaker:
            print(f"\n[circuit breaker] {consecutive_blocked} consecutive blocked responses "
                  f"from screener.in -- stopping here to avoid getting the IP flagged.\n"
                  f"Saving what we've got so far. Try again later (or lower request rate / "
                  f"add SCREENER_COOKIE) before re-running.")
            break

        if i < len(candidates):
            if args.cooldown_every and fetch_count % args.cooldown_every == 0:
                print(f"    [cooldown] pausing {args.cooldown_seconds:.0f}s after "
                      f"{fetch_count} requests ...")
                time.sleep(args.cooldown_seconds)
            else:
                time.sleep(random.uniform(args.delay, args.delay * 1.6))

    # REQ-4
    print("\nSaving unfiltered results ...")
    append_and_save(args.unfiltered_out, unfiltered_records)

    # REQ-5: filter by market cap, and keep only the requested fields
    FILTERED_FIELDS = (
        "Ticker", "Company", "Date",
        "market_cap_cr", "pe_ratio",
        "sales_yoy_pct", "sales_qoq_pct",
        "profit_yoy_pct", "profit_qoq_pct",
        "quarter", "expected_quarter", "stale",
    )
    filtered = [
        {k: r.get(k) for k in FILTERED_FIELDS}
        for r in unfiltered_records
        if r.get("market_cap_cr") is not None and r["market_cap_cr"] >= args.min_mcap
    ]
    print(f"\n{len(filtered)}/{len(unfiltered_records)} stock(s) have Market Cap >= {args.min_mcap} Cr")
    print("Saving filtered results ...")
    append_and_save(args.filtered_out, filtered)


if __name__ == "__main__":
    main()