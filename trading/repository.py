# -*- coding: utf-8 -*-
"""
Data access layer for the trading tracking system.
All DB operations go through the shared DatabaseManager singleton.
"""

import logging
from datetime import date
from typing import List, Optional

from sqlalchemy import select, and_, desc, func

from src.storage import DatabaseManager
from trading.models import TradeRecord, Position, TradeReview, PortfolioSnapshot

logger = logging.getLogger(__name__)


class TradeRepository:
    """CRUD for trade_records table."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self._db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, record: TradeRecord) -> TradeRecord:
        """Insert or update a trade record."""
        with self._db.session_scope() as session:
            existing = session.execute(
                select(TradeRecord).where(TradeRecord.trade_id == record.trade_id)
            ).scalar_one_or_none()
            if existing:
                # Update mutable fields (reason, tags, emotion, strategy_tag)
                for field in ("reason", "tags", "emotion_score", "confidence", "strategy_tag", "analysis_id"):
                    val = getattr(record, field, None)
                    if val is not None:
                        setattr(existing, field, val)
                return existing
            session.add(record)
            session.flush()
            session.expunge(record)
            return record

    def bulk_save(self, records: List[TradeRecord]) -> int:
        """Bulk insert, skip duplicates by trade_id. Returns count inserted."""
        inserted = 0
        with self._db.session_scope() as session:
            existing_ids = {
                r[0]
                for r in session.execute(
                    select(TradeRecord.trade_id).where(
                        TradeRecord.trade_id.in_([r.trade_id for r in records])
                    )
                ).fetchall()
            }
            for record in records:
                if record.trade_id not in existing_ids:
                    session.add(record)
                    inserted += 1
        return inserted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, trade_id: str) -> Optional[TradeRecord]:
        with self._db.session_scope() as session:
            return session.execute(
                select(TradeRecord).where(TradeRecord.trade_id == trade_id)
            ).scalar_one_or_none()

    def list_by_code(self, stock_code: str, limit: int = 100) -> List[TradeRecord]:
        with self._db.session_scope() as session:
            rows = session.execute(
                select(TradeRecord)
                .where(TradeRecord.stock_code == stock_code)
                .order_by(desc(TradeRecord.trade_date))
                .limit(limit)
            ).scalars().all()
            result = list(rows)
            session.expunge_all()
            return result

    def list_by_month(self, year: int, month: int) -> List[TradeRecord]:
        prefix = f"{year:04d}-{month:02d}"
        with self._db.session_scope() as session:
            rows = session.execute(
                select(TradeRecord)
                .where(TradeRecord.trade_date.like(f"{prefix}%"))
                .order_by(TradeRecord.trade_date)
            ).scalars().all()
            result = list(rows)
            session.expunge_all()
            return result

    def list_all(self, market: Optional[str] = None, limit: int = 500) -> List[TradeRecord]:
        with self._db.session_scope() as session:
            q = select(TradeRecord).order_by(desc(TradeRecord.trade_date)).limit(limit)
            if market:
                q = q.where(TradeRecord.market == market)
            rows = list(session.execute(q).scalars().all())
            session.expunge_all()
            return rows

    def get_by_raw_order_id(self, raw_order_id: str) -> Optional[TradeRecord]:
        """Used for dedup during CSV import."""
        with self._db.session_scope() as session:
            return session.execute(
                select(TradeRecord).where(TradeRecord.raw_order_id == raw_order_id)
            ).scalar_one_or_none()


class PositionRepository:
    """CRUD for positions table."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self._db = db_manager or DatabaseManager.get_instance()

    def upsert(self, position: Position) -> Position:
        with self._db.session_scope() as session:
            existing = session.execute(
                select(Position).where(Position.stock_code == position.stock_code)
            ).scalar_one_or_none()
            if existing:
                for field in (
                    "avg_cost", "avg_cost_cny", "quantity", "total_cost", "total_cost_cny",
                    "last_trade_date", "status", "stock_name",
                ):
                    setattr(existing, field, getattr(position, field, None) or getattr(existing, field))
                return existing
            session.add(position)
            session.flush()
            session.expunge(position)
            return position

    def get(self, stock_code: str) -> Optional[Position]:
        with self._db.session_scope() as session:
            return session.execute(
                select(Position).where(Position.stock_code == stock_code)
            ).scalar_one_or_none()

    def list_open(self) -> List[Position]:
        with self._db.session_scope() as session:
            rows = list(
                session.execute(
                    select(Position).where(Position.status == "open").order_by(Position.stock_code)
                ).scalars().all()
            )
            session.expunge_all()
            return rows

    def list_all(self) -> List[Position]:
        with self._db.session_scope() as session:
            rows = list(
                session.execute(select(Position).order_by(Position.stock_code)).scalars().all()
            )
            session.expunge_all()
            return rows


class ReviewRepository:
    """CRUD for trade_reviews table."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self._db = db_manager or DatabaseManager.get_instance()

    def save(self, review: TradeReview) -> TradeReview:
        with self._db.session_scope() as session:
            session.add(review)
            session.flush()
            session.expunge(review)
            return review

    def get_by_id(self, review_id: str) -> Optional[TradeReview]:
        with self._db.session_scope() as session:
            return session.execute(
                select(TradeReview).where(TradeReview.review_id == review_id)
            ).scalar_one_or_none()

    def list_by_month(self, year: int, month: int) -> List[TradeReview]:
        prefix = f"{year:04d}-{month:02d}"
        with self._db.session_scope() as session:
            rows = list(
                session.execute(
                    select(TradeReview)
                    .where(TradeReview.review_date.like(f"{prefix}%"))
                    .order_by(TradeReview.review_date)
                ).scalars().all()
            )
            session.expunge_all()
            return rows

    def list_all(self, limit: int = 200) -> List[TradeReview]:
        with self._db.session_scope() as session:
            rows = list(
                session.execute(
                    select(TradeReview).order_by(desc(TradeReview.review_date)).limit(limit)
                ).scalars().all()
            )
            session.expunge_all()
            return rows


class SnapshotRepository:
    """CRUD for portfolio_snapshots table."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self._db = db_manager or DatabaseManager.get_instance()

    def save(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        with self._db.session_scope() as session:
            existing = session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.snapshot_date == snapshot.snapshot_date
                )
            ).scalar_one_or_none()
            if existing:
                for field in (
                    "total_cost_cny", "market_value_cny", "unrealized_pnl_cny",
                    "realized_pnl_cny", "cash_balance_cny", "positions_json", "note",
                ):
                    val = getattr(snapshot, field, None)
                    if val is not None:
                        setattr(existing, field, val)
                return existing
            session.add(snapshot)
            session.flush()
            session.expunge(snapshot)
            return snapshot

    def get_latest(self) -> Optional[PortfolioSnapshot]:
        with self._db.session_scope() as session:
            return session.execute(
                select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.snapshot_date)).limit(1)
            ).scalar_one_or_none()

    def list_recent(self, n: int = 30) -> List[PortfolioSnapshot]:
        with self._db.session_scope() as session:
            return list(
                session.execute(
                    select(PortfolioSnapshot)
                    .order_by(desc(PortfolioSnapshot.snapshot_date))
                    .limit(n)
                ).scalars().all()
            )
