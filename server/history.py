"""
history.py
Handles writing and reading PortfolioHistory from PostgreSQL.
Replaces history_logger.py and the CSV-reading parts of render_display.py.
"""

from datetime import datetime, timedelta, date
from sqlalchemy.orm import Session
from sqlalchemy import func
import os
from models import PortfolioHistory, Account
import pytz

PORTFOLIOS = [
    slug.strip()
    for slug in os.getenv("PORTFOLIOS", "").split(",")
    if slug.strip()
]


def log_snapshot(db: Session, slug: str, pct_gain: float, dollar_value: float,
                 source: str = "live", snapshot_at: datetime | None = None):
    """
    Append a portfolio snapshot. Skips if an identical timestamp+slug exists.
    """
    ts = snapshot_at or datetime.utcnow()

    exists = db.query(PortfolioHistory).filter_by(
        snapshot_at=ts, slug=slug
    ).first()
    if exists:
        return

    row = PortfolioHistory(
        snapshot_at  =ts,
        slug         =slug,
        pct_gain     =pct_gain,
        dollar_value =dollar_value,
        source       =source,
    )
    db.add(row)


def log_all_snapshots(db: Session, portfolio_values: dict[str, dict]):
    """
    Log a snapshot for each slug in portfolio_values.
    portfolio_values: {slug: {daily_pct, total_value}}
    """
    ts = datetime.utcnow()
    for slug, pf in portfolio_values.items():
        if pf.get("daily_pct") is None or pf.get("total_value") is None:
            continue
        log_snapshot(db, slug, pf["daily_pct"], pf["total_value"], snapshot_at=ts)

def get_purchase_dates(db: Session, slug: str, start: datetime) -> set[date]:
    """Returns set of dates where a buy transaction occurred for this slug."""
    from models import Transaction, Account
    account = db.query(Account).filter_by(slug=slug).first()
    if not account:
        return set()
    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id == account.id)
        .filter(Transaction.date >= start.date())
        .filter(Transaction.type == "buy")
        .all()
    )
    return {t.date for t in txns}

def compute_daily_twr(db: Session, slug: str, date_to_compute: date) -> float | None:
    """
    Computes Time-Weighted Return for a single day using the modified Dietz method.
    
    TWR_day = (End_Value - Start_Value - Cash_Flows) / (Start_Value + Weighted_Cash_Flows)
    
    Cash flow weight = (T - t) / T  where T = total minutes in day, t = minutes since open
    """
    from models import Account, Transaction
    from datetime import datetime, timedelta

    account = db.query(Account).filter_by(slug=slug).first()
    if not account:
        return None

    # Get start of day and end of day in UTC
    day_start_utc = datetime.combine(date_to_compute, datetime.min.time())
    day_end_utc   = datetime.combine(date_to_compute + timedelta(days=1), datetime.min.time())
    T_minutes     = 390.0  # 6.5 hour trading day in minutes

    # Start value — last snapshot before today
    start_row = (
        db.query(PortfolioHistory)
        .filter(PortfolioHistory.slug == slug)
        .filter(PortfolioHistory.snapshot_at < day_start_utc)
        .order_by(PortfolioHistory.snapshot_at.desc())
        .first()
    )
    if not start_row:
        return None
    start_value = start_row.dollar_value

    # End value — last snapshot of today
    end_row = (
        db.query(PortfolioHistory)
        .filter(PortfolioHistory.slug == slug)
        .filter(PortfolioHistory.snapshot_at >= day_start_utc)
        .filter(PortfolioHistory.snapshot_at < day_end_utc)
        .order_by(PortfolioHistory.snapshot_at.desc())
        .first()
    )
    if not end_row:
        return None
    end_value = end_row.dollar_value

    # Cash flows today from transactions (buys = positive, sells = negative)
    # Plaid: amount is negative for buys (money out), positive for sells
    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id == account.id)
        .filter(Transaction.date == date_to_compute)
        .filter(Transaction.type.in_(["buy", "sell"]))
        .all()
    )

    weighted_cf = 0.0
    total_cf    = 0.0
    market_open = datetime.combine(date_to_compute, datetime.min.time()).replace(hour=14, minute=30)  # 8:30 CT = 14:30 UTC

    for txn in txns:
        # Plaid amount: negative = buy (cash out), positive = sell (cash in)
        # For TWR: cash inflow (buy) = positive CF
        cf = -txn.amount if txn.amount else 0.0

        # Time weight: earlier in day = higher weight
        txn_dt  = datetime.combine(txn.date, datetime.min.time()).replace(hour=14, minute=30)
        minutes_since_open = max(0.0, (txn_dt - market_open).total_seconds() / 60)
        weight  = (T_minutes - minutes_since_open) / T_minutes

        weighted_cf += cf * weight
        total_cf    += cf

    denominator = start_value + weighted_cf
    if not denominator:
        return None

    twr_day = (end_value - start_value - total_cf) / denominator
    return round(twr_day, 6)


def update_daily_twr(db: Session, slug: str, date_to_compute: date):
    """
    Computes daily TWR and stores cumulative TWR on the last snapshot of the day.
    Cumulative TWR chains from the earliest record: (1+r1)(1+r2)... - 1
    """
    twr_day = compute_daily_twr(db, slug, date_to_compute)
    if twr_day is None:
        return

    # Get previous cumulative TWR
    day_start_utc = datetime.combine(date_to_compute, datetime.min.time())
    prev = (
        db.query(PortfolioHistory)
        .filter(PortfolioHistory.slug == slug)
        .filter(PortfolioHistory.snapshot_at < day_start_utc)
        .filter(PortfolioHistory.twr.isnot(None))
        .order_by(PortfolioHistory.snapshot_at.desc())
        .first()
    )
    prev_cumulative = prev.twr if prev else 0.0

    # Chain: (1 + prev) * (1 + today) - 1
    cumulative_twr = (1 + prev_cumulative) * (1 + twr_day) - 1

    # Store on last snapshot of today
    day_end_utc = datetime.combine(date_to_compute + timedelta(days=1), datetime.min.time())
    last_today  = (
        db.query(PortfolioHistory)
        .filter(PortfolioHistory.slug == slug)
        .filter(PortfolioHistory.snapshot_at >= day_start_utc)
        .filter(PortfolioHistory.snapshot_at < day_end_utc)
        .order_by(PortfolioHistory.snapshot_at.desc())
        .first()
    )
    if last_today:
        last_today.twr = round(cumulative_twr, 6)

def load_history(db: Session, mode: str) -> dict[str, list[tuple[datetime, float, float]]]:
    ct  = pytz.timezone("America/Chicago")
    now = datetime.now(ct)

    if mode == "daily":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "monthly":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = datetime(now.year, 1, 1, tzinfo=ct)

    cutoff_utc = cutoff.astimezone(pytz.utc).replace(tzinfo=None)

    rows = (
        db.query(PortfolioHistory)
        .filter(PortfolioHistory.snapshot_at >= cutoff_utc)
        .filter(PortfolioHistory.slug.in_(PORTFOLIOS))
        .order_by(PortfolioHistory.snapshot_at)
        .all()
    )

    series: dict[str, list] = {}
    for row in rows:
        if row.slug not in series:
            series[row.slug] = []
        series[row.slug].append((row.snapshot_at, row.pct_gain, row.dollar_value))

    # Monthly/YTD: downsample + neutralize purchase days
    if mode in ("monthly", "ytd"):
        for slug in series:
            # Only keep end-of-day rows that have TWR computed
            by_day: dict[date, tuple] = {}
            for ts, pct, dv in series[slug]:
                by_day[ts.date()] = (ts, pct, dv)

            # Fetch TWR values for these dates
            twr_rows = (
                db.query(PortfolioHistory)
                .filter(PortfolioHistory.slug == slug)
                .filter(PortfolioHistory.snapshot_at >= cutoff_utc)
                .filter(PortfolioHistory.twr.isnot(None))
                .order_by(PortfolioHistory.snapshot_at)
                .all()
            )

            if twr_rows:
                # Use TWR for clean performance — $100 peg
                base_twr = twr_rows[0].twr
                series[slug] = [
                    (row.snapshot_at, row.twr, 100 * (1 + row.twr - base_twr))
                    for row in twr_rows
                ]
            else:
                # Fallback to dollar_value until TWR accumulates
                sorted_days = [by_day[d] for d in sorted(by_day)]
                series[slug] = sorted_days

    # Daily: prepend yesterday's close as anchor
    if mode == "daily":
        for slug in list(series.keys()):
            prev = (
                db.query(PortfolioHistory)
                .filter(PortfolioHistory.snapshot_at < cutoff_utc)
                .filter(PortfolioHistory.slug == slug)
                .order_by(PortfolioHistory.snapshot_at.desc())
                .first()
            )
            if prev:
                series[slug].insert(0, (cutoff_utc, 0.0, prev.dollar_value))

    return series

def import_from_csv(db: Session, csv_path: str) -> int:
    """
    One-time import of portfolio_history.csv into PostgreSQL.
    Skips rows that already exist (by timestamp + slug).
    Returns count of imported rows.
    """
    import csv
    from datetime import datetime

    imported = 0
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        has_dollar = "dollar_value" in (reader.fieldnames or [])

        for row in reader:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            slug        = row["portfolio_slug"]
            pct_gain    = float(row["pct_gain"])
            dollar_val  = float(row["dollar_value"]) if has_dollar else float(row.get("total_value", 0))

            exists = db.query(PortfolioHistory).filter_by(
                snapshot_at=ts, slug=slug
            ).first()
            if exists:
                continue

            db.add(PortfolioHistory(
                snapshot_at  =ts,
                slug         =slug,
                pct_gain     =pct_gain,
                dollar_value =dollar_val,
                source       ="csv_import",
            ))
            imported += 1

            # Commit in batches to avoid huge transactions
            if imported % 500 == 0:
                db.commit()
                print(f"  Imported {imported} rows so far...")

    db.commit()
    return imported
