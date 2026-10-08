"""
models.py
SQLAlchemy ORM models for portracker-v2.
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean,
    DateTime, Date, ForeignKey, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class PlaidItem(Base):
    """
    One row per linked brokerage account (Plaid Item).
    An Item can contain multiple accounts (e.g. Individual + Roth IRA at Fidelity).
    """
    __tablename__ = "plaid_items"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    item_id         = Column(String, unique=True, nullable=False)   # Plaid's item ID
    access_token    = Column(String, nullable=False)                 # keep encrypted at rest
    institution_id  = Column(String)
    institution_name = Column(String)
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_synced_at  = Column(DateTime)

    accounts = relationship("Account", back_populates="item", cascade="all, delete-orphan")


class Account(Base):
    """
    One row per brokerage account (e.g. Individual -1973, Roth IRA -2701).
    """
    __tablename__ = "accounts"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    item_id         = Column(Integer, ForeignKey("plaid_items.id"), nullable=False)
    account_id      = Column(String, unique=True, nullable=False)   # Plaid account ID
    name            = Column(String)                                 # e.g. "Individual"
    official_name   = Column(String)
    slug            = Column(String, nullable=False)                 # e.g. "individual"
    account_type    = Column(String)                                 # investment, etc.
    subtype         = Column(String)                                 # brokerage, ira, etc.
    current_balance = Column(Float)
    iso_currency    = Column(String, default="USD")
    is_active       = Column(Boolean, default=True)

    item     = relationship("PlaidItem", back_populates="accounts")
    holdings = relationship("Holding", back_populates="account", cascade="all, delete-orphan")


class Security(Base):
    """
    Security metadata returned by Plaid alongside holdings.
    """
    __tablename__ = "securities"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    security_id         = Column(String, unique=True, nullable=False)  # Plaid security ID
    ticker_symbol       = Column(String, index=True)
    name                = Column(String)
    security_type       = Column(String)   # equity, mutual fund, etf, etc.
    is_cash_equivalent  = Column(Boolean, default=False)
    close_price         = Column(Float)
    close_price_as_of   = Column(Date)

    holdings = relationship("Holding", back_populates="security")


class Holding(Base):
    """
    Current holdings snapshot — replaced wholesale on each Plaid sync.
    """
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("account_id", "security_id", name="uq_account_security"),
    )

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    account_id          = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    security_id         = Column(Integer, ForeignKey("securities.id"), nullable=False)
    quantity            = Column(Float, nullable=False)
    cost_basis          = Column(Float)          # total cost basis (not per share)
    institution_value   = Column(Float)          # broker's reported value
    institution_price   = Column(Float)          # broker's reported price
    institution_price_as_of = Column(Date)

    account  = relationship("Account", back_populates="holdings")
    security = relationship("Security", back_populates="holdings")


class Transaction(Base):
    """
    Investment transactions (buys, sells, dividends, etc.).
    Append-only — never deleted, new ones added on each sync.
    """
    __tablename__ = "transactions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    investment_transaction_id = Column(String, unique=True, nullable=False)
    account_id          = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    security_id         = Column(Integer, ForeignKey("securities.id"), nullable=True)
    date                = Column(Date, nullable=False)
    name                = Column(String)
    quantity            = Column(Float)
    amount              = Column(Float)          # negative = money out (buy), positive = money in (sell/div)
    price               = Column(Float)
    fees                = Column(Float)
    type                = Column(String)         # buy, sell, dividend, cash, transfer
    subtype             = Column(String)
    iso_currency        = Column(String, default="USD")


class PortfolioHistory(Base):
    """
    Time-series of portfolio value snapshots.
    Migrated from portfolio_history.csv + appended by history_logger.
    """
    __tablename__ = "portfolio_history"
    __table_args__ = (
        UniqueConstraint("snapshot_at", "slug", name="uq_snapshot_slug"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_at  = Column(DateTime, nullable=False, index=True)
    slug         = Column(String, nullable=False, index=True)   # e.g. "individual"
    pct_gain     = Column(Float)     # daily % gain at time of snapshot
    dollar_value = Column(Float)     # total portfolio value in USD
    twr          = Column(Float)     # time-weighted return (cumulative)
    source       = Column(String, default="live")  # "live" | "backfill" | "csv_import"



class LivePrice(Base):
    """
    Latest fetched price per ticker (from yfinance).
    Used by renderer — updated by refresh_prices.py every 5 min.
    """
    __tablename__ = "live_prices"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    ticker          = Column(String, unique=True, nullable=False)
    price           = Column(Float)
    prev_close      = Column(Float)
    daily_pct       = Column(Float)
    change_pct_30d  = Column(Float)
    change_pct_ytd  = Column(Float)
    fetched_at      = Column(DateTime, default=datetime.utcnow)