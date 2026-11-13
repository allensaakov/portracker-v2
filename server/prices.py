"""
prices.py
Hybrid price fetching:
  - Finnhub: live quotes for all held tickers + indices (every 5 min)
  - yfinance: 30d/YTD index data only (every 5 min, 3 calls only)
  - yfinance: dividends for held tickers (once daily)

Environment variables required:
    FINNHUB_API_KEY
"""

import os
import time
from datetime import datetime, timedelta, date

import requests as req_lib
import yfinance as yf
from sqlalchemy.orm import Session

from models import LivePrice, Holding, Security, Account

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE    = "https://finnhub.io/api/v1"

INDICES = {
    "SPY": "SPY",
    "DOW": "DIA",
    "NDQ": "QQQ",
}

REQUEST_DELAY = 1.2  # seconds between Finnhub calls


# ── Finnhub helpers ───────────────────────────────────────────────────────────

def _finnhub_get(endpoint: str, params: dict = {}) -> dict:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not set")
    params["token"] = FINNHUB_API_KEY
    url = f"{FINNHUB_BASE}/{endpoint}"
    for attempt in range(3):
        try:
            r = req_lib.get(url, params=params, timeout=10)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"  Finnhub 429 on {endpoint}, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r.json()
        except req_lib.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
            else:
                print(f"  Finnhub failed: {endpoint} — {e}")
                return {}
    return {}


def fetch_quote(ticker: str) -> dict | None:
    data = _finnhub_get("quote", {"symbol": ticker})
    if not data or not data.get("c"):
        return None
    price      = round(float(data["c"]), 4)
    prev_close = round(float(data["pc"]), 4)
    daily_pct  = round((price - prev_close) / prev_close * 100, 4) if prev_close else 0.0
    return {"price": price, "prev_close": prev_close, "daily_pct": daily_pct}


def fetch_earnings(ticker: str) -> list[dict]:
    today   = date.today()
    to_date = today + timedelta(days=90)
    data    = _finnhub_get("calendar/earnings", {
        "symbol": ticker,
        "from":   today.strftime("%Y-%m-%d"),
        "to":     to_date.strftime("%Y-%m-%d"),
    })
    events = []
    for e in data.get("earningsCalendar", []):
        try:
            earn_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
            if earn_date >= today:
                events.append({
                    "symbol": ticker, "kind": "ERN",
                    "date": earn_date,
                    "detail": earn_date.strftime("%-m/%-d"),
                })
        except Exception:
            pass
    return events


# ── yfinance helpers (indices + dividends only) ───────────────────────────────

def fetch_index_extended(etf_ticker: str, price: float) -> tuple[float | None, float | None]:
    """
    Fetch 30d and YTD % change for an index ETF via yfinance.
    Only called for SPY, DIA, QQQ — 3 calls per refresh cycle.
    """
    try:
        hist = yf.Ticker(etf_ticker).history(period="ytd", interval="1d")
        if hist.empty:
            return None, None

        today     = date.today()
        ytd_start = date(today.year, 1, 1)
        month_ago = today - timedelta(days=30)

        chg_ytd = None
        ytd_rows = hist[hist.index.date >= ytd_start]
        if not ytd_rows.empty:
            base    = float(ytd_rows["Close"].iloc[0])
            chg_ytd = round((price - base) / base * 100, 2) if base else None

        chg_30d = None
        rows_30d = hist[hist.index.date >= month_ago]
        if not rows_30d.empty:
            base    = float(rows_30d["Close"].iloc[0])
            chg_30d = round((price - base) / base * 100, 2) if base else None

        return chg_30d, chg_ytd
    except Exception as e:
        print(f"  yfinance extended data failed for {etf_ticker}: {e}")
        return None, None


def fetch_dividends_yf(tickers: list[str]) -> list[dict]:
    events  = []
    today   = date.today()
    import calendar
    last_day  = calendar.monthrange(today.year, today.month)[1]
    month_end = date(today.year, today.month, last_day)

    for sym in tickers:
        try:
            info          = yf.Ticker(sym).info
            ex_date_ts    = info.get("exDividendDate")   # ex-dividend date
            pay_date_ts   = info.get("dividendDate")      # payment date
            div_rate      = info.get("dividendRate")
            div_frequency = info.get("dividendFrequency") or 4
            per_payment   = round(float(div_rate) / div_frequency, 4) if div_rate else None

            if ex_date_ts:
                ex_date = date.fromtimestamp(ex_date_ts)
                if today <= ex_date <= month_end:
                    events.append({
                        "symbol": sym,
                        "kind":   "EX-DIV",
                        "date":   ex_date,
                        "detail": f"${per_payment:.2f}" if per_payment else "--",
                    })

            if pay_date_ts and per_payment:
                pay_date = date.fromtimestamp(pay_date_ts)
                if today <= pay_date <= month_end:
                    events.append({
                        "symbol": sym,
                        "kind":   "DIV",
                        "date":   pay_date,
                        "detail": f"${per_payment:.2f}",
                    })
        except Exception:
            pass

    return events


# ── DB helpers ────────────────────────────────────────────────────────────────

def _upsert_price(db: Session, ticker: str, price: float, prev_close: float,
                  daily_pct: float, chg_30d: float | None = None,
                  chg_ytd: float | None = None):
    row = db.query(LivePrice).filter_by(ticker=ticker).first()
    if not row:
        row = LivePrice(ticker=ticker)
        db.add(row)
    row.price      = price
    row.prev_close = prev_close
    row.daily_pct  = daily_pct
    # Only update extended fields if we have new values — preserve cached ones
    if chg_30d is not None:
        row.change_pct_30d = chg_30d
    if chg_ytd is not None:
        row.change_pct_ytd = chg_ytd
    row.fetched_at = datetime.utcnow()


def _get_held_tickers(db: Session) -> set[str]:
    rows = (
        db.query(Security.ticker_symbol)
        .join(Holding, Holding.security_id == Security.id)
        .filter(Security.is_cash_equivalent == False)
        .filter(Security.ticker_symbol.isnot(None))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


# ── Main refresh (every 5 min) ────────────────────────────────────────────────

def refresh_prices(db: Session) -> dict:
    """
    Every 5 min:
      1. Finnhub quotes for all held tickers + index ETFs
      2. yfinance 30d/YTD for 3 index ETFs only
    """
    held_tickers  = _get_held_tickers(db)
    index_tickers = set(INDICES.values())
    all_tickers   = list(held_tickers | index_tickers)
    updated       = 0

    print(f"Refreshing {len(all_tickers)} tickers...")

    for ticker in all_tickers:
        quote = fetch_quote(ticker)
        if not quote:
            print(f"  No Finnhub quote for {ticker} — skipping")
            continue

        # yfinance 30d/YTD only for the 3 index ETFs
        chg_30d = chg_ytd = None
        if ticker in index_tickers:
            chg_30d, chg_ytd = fetch_index_extended(ticker, quote["price"])

        _upsert_price(db, ticker, quote["price"], quote["prev_close"],
                      quote["daily_pct"], chg_30d, chg_ytd)
        updated += 1

    print(f"Refresh complete — {updated}/{len(all_tickers)} updated")
    return {"updated": updated, "tickers": all_tickers}


# ── Daily dividend refresh ────────────────────────────────────────────────────

def refresh_dividends(db: Session) -> list[dict]:
    """
    Called once daily — fetches dividend data via yfinance for held tickers.
    Returns list of upcoming dividend events (stored in memory, not DB).
    """
    held_tickers = list(_get_held_tickers(db))
    return fetch_dividends_yf(held_tickers)


# ── Portfolio value computation ───────────────────────────────────────────────

def get_portfolio_value(db: Session, slug: str) -> dict:
    account = db.query(Account).filter_by(slug=slug, is_active=True).first()
    if not account:
        return {"total_value": None, "daily_gain": None, "daily_pct": None, "positions": []}

    total_value = total_prev_value = 0.0
    positions = []

    for holding in account.holdings:
        sec    = holding.security
        ticker = sec.ticker_symbol

        if sec.is_cash_equivalent or not ticker:
            cv = holding.institution_value or 0.0
            total_value      += cv
            total_prev_value += cv
            positions.append({
                "symbol": ticker or "CASH", "quantity": holding.quantity,
                "price": None, "value": cv,
                "daily_pct": 0.0, "daily_gain": 0.0, "is_cash": True,
            })
            continue

        lp = db.query(LivePrice).filter_by(ticker=ticker).first()
        if not lp or lp.price is None:
            fv = holding.institution_value or 0.0  # broker's last value, don't drop the position
            total_value      += fv
            total_prev_value += fv
            continue

        value      = round(lp.price * holding.quantity, 2)
        prev_value = round(lp.prev_close * holding.quantity, 2) if lp.prev_close else value
        daily_gain = round(value - prev_value, 2)
        total_value      += value
        total_prev_value += prev_value
        positions.append({
            "symbol": ticker, "quantity": holding.quantity, "price": lp.price,
            "value": value, "daily_pct": lp.daily_pct, "daily_gain": daily_gain,
            "is_cash": False,
        })

    total_value      = round(total_value, 2)
    total_daily_gain = round(total_value - total_prev_value, 2)
    daily_pct        = round(
        (total_daily_gain / total_prev_value * 100) if total_prev_value else 0.0, 4
    )
    return {
        "total_value": total_value, "daily_gain": total_daily_gain,
        "daily_pct": daily_pct, "positions": positions,
    }


def get_indices(display_mode: str) -> list[dict]:
    """
    Get indices defined in INDICES, depending on display_mode.
    """

    tickers = list(INDICES.values())

    # Determine how much historical data we need
    if display_mode == "daily":
        period = "5d"
    elif display_mode == "monthly":
        period = "1mo"
    elif display_mode == "ytd":
        period = "1y"
    else:
        period = "5d"

    raw = yf.download(
        tickers=tickers,
        period=period,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
    )

    result = []

    for label, ticker in INDICES.items():
        try:
            close = raw[ticker]["Close"].dropna()
            if len(close) < 2:
                change_pct = None
                price = None
            else:
                price = float(close.iloc[-1])

                if display_mode == "daily":
                    # Today's close vs previous trading day's close
                    previous = float(close.iloc[-2])

                elif display_mode == "monthly":
                    # Current price vs ~30 days ago
                    previous = float(close.iloc[0])

                elif display_mode == "ytd":
                    # Find first available trading day of current year
                    current_year = close.index[-1].year
                    ytd_data = close[close.index.year == current_year]

                    if len(ytd_data) > 0:
                        previous = float(ytd_data.iloc[0])
                    else:
                        previous = None

                else:
                    previous = float(close.iloc[-2])

                change_pct = (
                    ((price - previous) / previous) * 100
                    if previous
                    else None
                )

            result.append({
                "symbol": label,
                "price": price,
                "change_pct": change_pct,
            })

        except Exception as e:
            print(f"Could not fetch index {ticker}: {e}")

            result.append({
                "symbol": label,
                "price": None,
                "change_pct": None,
            })

    return result


def get_upcoming_events(db: Session, slugs: list[str]) -> dict[str, list[dict]]:
    """
    Returns upcoming earnings (Finnhub) + dividends (yfinance, cached daily).
    For dividends, reads from a simple in-memory cache keyed by date.
    """
    tickers: set[str] = set()
    for slug in slugs:
        acct = db.query(Account).filter_by(slug=slug, is_active=True).first()
        if not acct:
            continue
        for h in acct.holdings:
            if h.security.ticker_symbol and not h.security.is_cash_equivalent:
                tickers.add(h.security.ticker_symbol)

    events = {"earn": [], "exdiv": [], "div": []}
    for sym in tickers:
        for event in fetch_earnings(sym):
            events["earn"].append(event)

    # Dividends from yfinance (lightweight — one call per ticker, cached by caller)
    for event in fetch_dividends_yf(list(tickers)):
        bucket = "exdiv" if event["kind"] == "EX-DIV" else "div"
        events[bucket].append(event)

    for bucket in events.values():
        bucket.sort(key=lambda e: e["date"])
    return events