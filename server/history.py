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

    # Monthly/YTD: downsample to last snapshot per trading day
    if mode in ("monthly", "ytd"):
        for slug in series:
            by_day: dict[date, tuple] = {}
            for ts, pct, dv in series[slug]:
                by_day[ts.date()] = (ts, pct, dv)
            sorted_days = [by_day[d] for d in sorted(by_day)]

            # Neutralize cash inflow days
            # If dollar_value jumps more than 5% AND pct_gain is flat/small,
            # it's a purchase — use previous day's value to keep chart flat
            cleaned = []
            for i, (ts, pct, dv) in enumerate(sorted_days):
                if i == 0:
                    cleaned.append((ts, pct, dv))
                    continue
                prev_dv = cleaned[-1][2]
                raw_chg = (dv - prev_dv) / prev_dv * 100 if prev_dv else 0
                # If raw dollar change >> pct_gain, it's a cash inflow
                if abs(raw_chg) > abs(pct) + 2.0:
                    # Neutralize: scale dollar_value to remove the inflow
                    adjusted_dv = prev_dv * (1 + pct / 100)
                    cleaned.append((ts, pct, round(adjusted_dv, 2)))
                else:
                    cleaned.append((ts, pct, dv))

            series[slug] = cleaned
            
    # Daily: fetch yesterday's closing value per slug as baseline
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
                # Prepend yesterday's close as anchor at 0%
                # Use pct_gain=0 so today's moves are relative to open
                series[slug].insert(0, (cutoff_utc, 0.0, prev.dollar_value if prev else series[slug][0][2]))

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
