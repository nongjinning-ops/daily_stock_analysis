# -*- coding: utf-8 -*-
"""
ORM models for the trading tracking system.
Extends the existing Base from src/storage.py so all tables live in the same DB.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from src.storage import Base


class TradeRecord(Base):
    """Records each individual trade execution."""

    __tablename__ = "trade_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(32), unique=True, nullable=False)  # e.g. T20260411-001
    stock_code = Column(String(20), nullable=False)  # e.g. hk00700 / AAPL
    stock_name = Column(String(50))
    market = Column(String(4), nullable=False)  # HK / US
    direction = Column(String(8), nullable=False)  # buy / sell / add / reduce
    security_type = Column(String(8), default="stock")  # stock / etf / option
    price = Column(Float, nullable=False)  # actual fill price
    quantity = Column(Integer, nullable=False)  # number of shares
    amount = Column(Float, nullable=False)  # price * quantity (original currency)
    commission = Column(Float, default=0.0)  # total fees (brokerage + all levies)
    net_amount = Column(Float, nullable=False)  # amount ± commission (cash flow)
    trade_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    trade_time = Column(String(5))  # HH:MM (optional)
    currency = Column(String(4), default="HKD")  # HKD / USD
    fx_rate_cny = Column(Float, default=1.0)  # exchange rate at trade time → CNY
    analysis_id = Column(Integer)  # FK to analysis_history.id (nullable)
    strategy_tag = Column(String(50))  # e.g. ma_golden_cross
    emotion_score = Column(Integer)  # 1–5 (1=extreme fear, 5=extreme greed)
    confidence = Column(Integer)  # 1–5 entry confidence
    reason = Column(Text)  # free-text trade reason
    tags = Column(String(200))  # comma-separated tags
    raw_order_id = Column(String(50))  # original broker order ID (for dedup on import)
    # Paper trading fields (account_type='paper') — null for real trades
    account_type = Column(String(8), default="real")  # real / paper
    alpaca_order_id = Column(String(50))  # Alpaca order ID returned on placement
    alpaca_status = Column(String(20))  # pending / filled / cancelled / expired
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_trade_stock_date", "stock_code", "trade_date"),
        Index("ix_trade_date", "trade_date"),
    )

    def __repr__(self) -> str:
        return f"<TradeRecord {self.trade_id} {self.stock_code} {self.direction} {self.quantity}@{self.price}>"


class Position(Base):
    """Current open position for each stock."""

    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(20), unique=True, nullable=False)
    stock_name = Column(String(50))
    market = Column(String(4), nullable=False)
    security_type = Column(String(8), default="stock")
    avg_cost = Column(Float, nullable=False)  # weighted average cost (original currency)
    avg_cost_cny = Column(Float)  # weighted average cost in CNY
    quantity = Column(Integer, nullable=False)  # current shares held
    total_cost = Column(Float, nullable=False)  # total cost including all commissions
    total_cost_cny = Column(Float)  # total cost in CNY
    currency = Column(String(4), default="HKD")
    first_buy_date = Column(String(10))  # YYYY-MM-DD
    last_trade_date = Column(String(10))  # YYYY-MM-DD
    last_update = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    status = Column(String(8), default="open")  # open / closed

    def __repr__(self) -> str:
        return f"<Position {self.stock_code} qty={self.quantity} avg={self.avg_cost}>"


class TradeReview(Base):
    """Post-trade review record covering 4 dimensions."""

    __tablename__ = "trade_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(String(32), unique=True, nullable=False)  # R20260411-001
    trade_id = Column(String(32))  # FK to trade_records.trade_id
    stock_code = Column(String(20), nullable=False)
    review_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    review_type = Column(String(20), nullable=False)  # trade / weekly / monthly / position_close

    # Financial dimension
    entry_price = Column(Float)
    exit_price = Column(Float)
    return_pct = Column(Float)  # realized return %
    max_drawdown = Column(Float)  # max drawdown % during holding
    holding_days = Column(Integer)
    realized_pnl_cny = Column(Float)  # realized PnL in CNY

    # Decision quality dimension
    ai_suggestion = Column(String(10))  # buy / hold / sell
    ai_target_price = Column(Float)
    actual_outcome = Column(String(20))  # hit_target / stop_loss / manual_exit / still_holding
    decision_score = Column(Integer)  # self-rating 1–5
    followed_ai = Column(Boolean)
    deviation_reason = Column(Text)

    # Emotion / psychology dimension
    pre_emotion = Column(Text)
    post_emotion = Column(Text)
    bias_detected = Column(String(200))  # comma-separated cognitive biases

    # Strategy attribution dimension
    strategy_worked = Column(Boolean)
    signal_quality = Column(Integer)  # 1–5
    market_context = Column(String(50))  # trend / consolidation / volatile
    lessons = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_review_stock_date", "stock_code", "review_date"),)

    def __repr__(self) -> str:
        return f"<TradeReview {self.review_id} {self.stock_code}>"


class PortfolioSnapshot(Base):
    """Periodic portfolio snapshots for equity curve tracking."""

    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD
    total_cost_cny = Column(Float)  # total invested (CNY)
    market_value_cny = Column(Float)  # market value at close (CNY)
    unrealized_pnl_cny = Column(Float)
    realized_pnl_cny = Column(Float)  # cumulative realized PnL (CNY)
    cash_balance_cny = Column(Float)
    positions_json = Column(Text)  # JSON snapshot of each position
    note = Column(Text)

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot {self.snapshot_date}>"
