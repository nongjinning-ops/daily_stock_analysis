# -*- coding: utf-8 -*-
"""
Business logic layer: position avg-cost calculation, CNY PnL, FX rate fetching.
"""

import json
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from trading.commission import calc_hk_commission, calc_us_commission, estimate_commission
from trading.models import Position, TradeRecord
from trading.repository import PositionRepository, TradeRepository

logger = logging.getLogger(__name__)

# USD → CNY and HKD → CNY approximate rates (fallback if live fetch fails)
_FALLBACK_FX = {"USD": 7.25, "HKD": 0.93}


def get_fx_rate_cny(currency: str, trade_date: Optional[str] = None) -> float:
    """
    Fetch CNY exchange rate for a given currency on a given date.
    Falls back to akshare historical data, then hardcoded fallback.

    Args:
        currency: 'HKD' or 'USD'.
        trade_date: 'YYYY-MM-DD'. Uses today if None.

    Returns:
        1 unit of currency = ? CNY.
    """
    if currency == "CNY":
        return 1.0
    try:
        import akshare as ak

        if currency == "HKD":
            # Try multiple possible akshare API names
            for fn in ("currency_hist", "fx_spot_quote"):
                try:
                    func = getattr(ak, fn, None)
                    if func is None:
                        continue
                    if fn == "currency_hist":
                        df = func(symbol="HKDCNY")
                    else:
                        df = func()
                        if df is not None:
                            row = df[df["货币对"] == "HKDCNY"]
                            if not row.empty:
                                return float(row.iloc[0]["最新价"])
                            continue
                    break
                except Exception:
                    continue
        elif currency == "USD":
            for fn in ("currency_hist", "fx_spot_quote"):
                try:
                    func = getattr(ak, fn, None)
                    if func is None:
                        continue
                    if fn == "currency_hist":
                        df = func(symbol="USDCNY")
                    else:
                        df = func()
                        if df is not None:
                            row = df[df["货币对"] == "USDCNY"]
                            if not row.empty:
                                return float(row.iloc[0]["最新价"])
                            continue
                    break
                except Exception:
                    continue
        else:
            return _FALLBACK_FX.get(currency, 1.0)

        if df is not None and not df.empty:
            if trade_date:
                row = df[df["date"] <= trade_date].tail(1)
            else:
                row = df.tail(1)
            if not row.empty:
                return float(row.iloc[0]["close"])
    except Exception as e:
        logger.warning(f"FX rate fetch failed for {currency}: {e}")

    return _FALLBACK_FX.get(currency, 1.0)


def _gen_trade_id(trade_date: str, seq: int) -> str:
    """Generate a deterministic trade ID like T20260411-001."""
    d = trade_date.replace("-", "")
    return f"T{d}-{seq:03d}"


def next_trade_id(repo: TradeRepository, trade_date: str) -> str:
    """Find the next available trade ID for a given date."""
    existing = repo.list_by_month(
        int(trade_date[:4]), int(trade_date[5:7])
    )
    same_day = [r for r in existing if r.trade_date == trade_date]
    return _gen_trade_id(trade_date, len(same_day) + 1)


def _gen_review_id(review_date: str, seq: int) -> str:
    d = review_date.replace("-", "")
    return f"R{d}-{seq:03d}"


def next_review_id(review_repo, review_date: str) -> str:
    existing = review_repo.list_by_month(
        int(review_date[:4]), int(review_date[5:7])
    )
    same_day = [r for r in existing if r.review_date == review_date]
    return _gen_review_id(review_date, len(same_day) + 1)


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def rebuild_positions(trade_repo: TradeRepository, pos_repo: PositionRepository) -> Dict[str, Position]:
    """
    Rebuild all positions from trade history (FIFO, no lot tracking).
    Called after bulk import or whenever positions need to be recalculated.

    Returns dict of stock_code → Position.
    """
    all_trades = trade_repo.list_all(limit=10000)
    # Sort chronologically
    all_trades.sort(key=lambda t: (t.trade_date, t.trade_time or ""))

    # Aggregate per stock
    agg: Dict[str, dict] = {}

    for t in all_trades:
        code = t.stock_code
        if code not in agg:
            agg[code] = {
                "stock_code": code,
                "stock_name": t.stock_name,
                "market": t.market,
                "security_type": t.security_type,
                "currency": t.currency,
                "quantity": 0,
                "total_cost": 0.0,
                "total_cost_cny": 0.0,
                "first_buy_date": None,
                "last_trade_date": t.trade_date,
            }
        rec = agg[code]
        fx = t.fx_rate_cny or _FALLBACK_FX.get(t.currency, 1.0)

        if t.direction in ("buy", "add"):
            rec["quantity"] += t.quantity
            rec["total_cost"] += t.amount + t.commission  # cash outflow (positive cost)
            rec["total_cost_cny"] += (t.amount + t.commission) * fx
            if rec["first_buy_date"] is None:
                rec["first_buy_date"] = t.trade_date
        elif t.direction in ("sell", "reduce"):
            qty_sold = min(t.quantity, rec["quantity"])
            if rec["quantity"] > 0:
                cost_per_share = rec["total_cost"] / rec["quantity"]
                cost_per_share_cny = rec["total_cost_cny"] / rec["quantity"]
                rec["total_cost"] -= cost_per_share * qty_sold
                rec["total_cost_cny"] -= cost_per_share_cny * qty_sold
            rec["quantity"] -= qty_sold

        rec["last_trade_date"] = t.trade_date

    positions = {}
    for code, rec in agg.items():
        qty = rec["quantity"]
        pos = Position(
            stock_code=code,
            stock_name=rec["stock_name"],
            market=rec["market"],
            security_type=rec["security_type"],
            currency=rec["currency"],
            quantity=qty,
            avg_cost=round(rec["total_cost"] / qty, 4) if qty > 0 else 0.0,
            avg_cost_cny=round(rec["total_cost_cny"] / qty, 4) if qty > 0 else 0.0,
            total_cost=round(rec["total_cost"], 2),
            total_cost_cny=round(rec["total_cost_cny"], 2),
            first_buy_date=rec["first_buy_date"],
            last_trade_date=rec["last_trade_date"],
            status="open" if qty > 0 else "closed",
        )
        pos_repo.upsert(pos)
        if qty > 0:
            positions[code] = pos
    return positions


# ---------------------------------------------------------------------------
# PnL calculation (real-time)
# ---------------------------------------------------------------------------

def calc_position_pnl(
    position: Position,
    current_price: float,
    current_fx_rate_cny: Optional[float] = None,
) -> Dict[str, float]:
    """
    Calculate unrealized PnL for an open position.

    Returns dict with keys:
        market_value, market_value_cny,
        unrealized_pnl, unrealized_pnl_cny, unrealized_pct
    """
    fx = current_fx_rate_cny or _FALLBACK_FX.get(position.currency, 1.0)
    market_value = current_price * position.quantity
    market_value_cny = market_value * fx
    unrealized_pnl = market_value - position.total_cost
    unrealized_pnl_cny = unrealized_pnl * fx
    cost = position.total_cost or 1  # avoid div/0
    unrealized_pct = unrealized_pnl / cost * 100

    return {
        "market_value": round(market_value, 2),
        "market_value_cny": round(market_value_cny, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_cny": round(unrealized_pnl_cny, 2),
        "unrealized_pct": round(unrealized_pct, 2),
    }


def calc_realized_pnl(
    trade_repo: TradeRepository,
    stock_code: Optional[str] = None,
) -> Dict[str, float]:
    """
    Calculate cumulative realized PnL from closed trades.
    If stock_code is given, only for that stock.

    Returns dict with total_pnl_cny, win_count, loss_count.
    """
    trades = trade_repo.list_by_code(stock_code) if stock_code else trade_repo.list_all()
    sells = [t for t in trades if t.direction in ("sell", "reduce")]

    total_pnl_cny = 0.0
    wins = 0
    losses = 0

    # For each sell, we need matching buy cost — simplification: use avg_cost_cny
    # This requires positions data; for now compute from raw amounts
    # A proper FIFO lot-match would require more state
    for t in sells:
        fx = t.fx_rate_cny or _FALLBACK_FX.get(t.currency, 1.0)
        # net_amount for sells is positive (cash in) minus commission
        # We approximate pnl per sell as: net_amount - avg_cost * qty (stored in position)
        # Here we just sum net_amounts for a quick total; full FIFO in rebuild_positions
        total_pnl_cny += t.net_amount * fx

    return {
        "total_pnl_cny": round(total_pnl_cny, 2),
        "sell_count": len(sells),
    }
