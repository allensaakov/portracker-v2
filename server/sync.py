"""
sync.py
Orchestrates syncing Plaid data into PostgreSQL.

Called by:
  - POST /plaid/webhook  (Plaid notifies us of changes)
  - GET  /plaid/sync     (manual trigger)
  - Cron (fallback polling)

Account slug mapping — edit this to match your accounts:
"""

import re
from datetime import datetime, date, timedelta

from sqlalchemy.orm import Session

import plaid_client as pc
from models import PlaidItem, Account, Security, Holding, Transaction

# Map Plaid account names/subtypes → display slugs
# Adjust these to match what Plaid returns for your Fidelity accounts
SLUG_MAP: dict[str, str] = {
    "plaid ira":  "roth_ira",
    "plaid 401k": "individual",
    "ira":        "roth_ira",
    "401k":       "individual",
    "individual":  "individual",
    "roth":        "roth_ira",
    "roth ira":    "roth_ira",
}

ALLOWED_TYPES = {"investment", "credit"}

def _slug_for_account(account: dict) -> str:
    """Derive a slug from account name or subtype."""
    name    = (account.get("name") or "").lower()
    subtype = (account.get("subtype") or "").lower()

    for key, slug in SLUG_MAP.items():
        if key in name or key == subtype:
            return slug

    # Fallback: sanitize the name
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"


def sync_item(db: Session, plaid_item: PlaidItem) -> dict:
    """
    Full sync for one Plaid Item:
      1. Fetch holdings → upsert accounts, securities, holdings
      2. Fetch new transactions → append to transactions table
      3. Update last_synced_at

    Returns a summary dict.
    """
    token = plaid_item.access_token

    # ── Holdings ──────────────────────────────────────────────────────────────
    holdings_data = pc.get_holdings(token)

    # Upsert accounts
    account_map: dict[str, Account] = {}  # plaid_account_id → Account ORM obj
    for acct in holdings_data["accounts"]:
        if acct.get("type") not in ALLOWED_TYPES:
            continue
        slug    = _slug_for_account(acct)
        db_acct = db.query(Account).filter_by(account_id=acct["account_id"]).first()
        if not db_acct:
            db_acct = Account(
                item_id    =plaid_item.id,
                account_id =acct["account_id"],
                name       =acct.get("name"),
                official_name=acct.get("official_name"),
                slug       =slug,
                account_type=acct["type"],
                subtype    =acct.get("subtype"),
            )
            db.add(db_acct)
            db.flush()
        db_acct.current_balance = acct["balances"].get("current")
        db_acct.is_active       = True
        account_map[acct["account_id"]] = db_acct

    # Upsert securities
    security_map: dict[str, Security] = {}  # plaid_security_id → Security ORM obj
    for sec in holdings_data.get("securities", []):
        db_sec = db.query(Security).filter_by(security_id=sec["security_id"]).first()
        if not db_sec:
            db_sec = Security(security_id=sec["security_id"])
            db.add(db_sec)
            db.flush()
        db_sec.ticker_symbol      = sec.get("ticker_symbol")
        db_sec.name               = sec.get("name")
        db_sec.security_type      = sec.get("type")
        db_sec.is_cash_equivalent = sec.get("is_cash_equivalent", False)
        db_sec.close_price        = sec.get("close_price")
        cop = sec.get("close_price_as_of")
        db_sec.close_price_as_of  = cop if isinstance(cop, date) else None
        security_map[sec["security_id"]] = db_sec

    # Replace holdings wholesale (delete + reinsert per account)
    for db_acct in account_map.values():
        db.query(Holding).filter_by(account_id=db_acct.id).delete()

    for h in holdings_data.get("holdings", []):
        db_acct = account_map.get(h["account_id"])
        db_sec  = security_map.get(h["security_id"])
        if not db_acct or not db_sec:
            continue
        db_h = Holding(
            account_id          =db_acct.id,
            security_id         =db_sec.id,
            quantity            =h.get("quantity", 0),
            cost_basis          =h.get("cost_basis"),
            institution_value   =h.get("institution_value"),
            institution_price   =h.get("institution_price"),
        )
        iop = h.get("institution_price_as_of")
        db_h.institution_price_as_of = iop if isinstance(iop, date) else None
        db.add(db_h)

    # ── Transactions (new only) ────────────────────────────────────────────────
    last_txn = (
        db.query(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .join(PlaidItem, Account.item_id == PlaidItem.id)
        .filter(PlaidItem.id == plaid_item.id)
        .order_by(Transaction.date.desc())
        .first()
    )
    since = (last_txn.date - timedelta(days=30)) if last_txn else None
    txns  = pc.get_transactions(token, start_date=since)

    new_txns = 0
    for t in txns:
        exists = db.query(Transaction).filter_by(
            investment_transaction_id=t["investment_transaction_id"]
        ).first()
        if exists:
            continue
        db_acct = account_map.get(t["account_id"])
        db_sec  = security_map.get(t.get("security_id", "")) if t.get("security_id") else None
        db_t = Transaction(
            investment_transaction_id=t["investment_transaction_id"],
            account_id  =db_acct.id if db_acct else None,
            security_id =db_sec.id  if db_sec  else None,
            date        =t["date"],
            name        =t.get("name"),
            quantity    =t.get("quantity"),
            amount      =t.get("amount"),
            price       =t.get("price"),
            fees        =t.get("fees"),
            type        =t.get("type"),
            subtype     =t.get("subtype"),
        )
        db.add(db_t)
        new_txns += 1

    # Update sync timestamp
    plaid_item.last_synced_at = datetime.utcnow()

    return {
        "accounts":    len(account_map),
        "securities":  len(security_map),
        "holdings":    len(holdings_data.get("holdings", [])),
        "new_txns":    new_txns,
    }


def sync_all(db: Session) -> list[dict]:
    """Sync every PlaidItem in the database."""
    items   = db.query(PlaidItem).all()
    results = []
    for item in items:
        try:
            summary = sync_item(db, item)
            results.append({"item_id": item.item_id, "status": "ok", **summary})
        except Exception as e:
            results.append({"item_id": item.item_id, "status": "error", "error": str(e)})
    return results
