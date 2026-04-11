# -*- coding: utf-8 -*-
"""
Trading system CLI entry point.

Usage:
    python -m trading.cli trade buy hk01810 33.10 1800
    python -m trading.cli trade import trading/订单历史.csv
    python -m trading.cli position list
    python -m trading.cli review new T20260411-001
    python -m trading.cli stats summary
"""

import argparse
import json
import sys
from datetime import date, datetime
from typing import Optional

# Ensure project root is on path when running as module
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import DatabaseManager
from trading.commission import estimate_commission
from trading.importer import FutuCSVImporter
from trading.models import TradeRecord, TradeReview
from trading.repository import PositionRepository, ReviewRepository, SnapshotRepository, TradeRepository
from trading.service import (
    get_fx_rate_cny,
    next_review_id,
    next_trade_id,
    rebuild_positions,
    calc_position_pnl,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_db() -> DatabaseManager:
    """Initialize DB (creates tables if not exist) and return manager."""
    # Importing models registers them on Base before create_all is called
    import trading.models  # noqa: F401 — side-effect: registers models on Base

    db = DatabaseManager.get_instance()
    return db


def _market_from_code(code: str) -> str:
    code = code.upper()
    if code.startswith("HK") or code[:5].isdigit():
        return "HK"
    return "US"


def _currency_from_market(market: str) -> str:
    return "HKD" if market == "HK" else "USD"


def _fmt_cny(val: float) -> str:
    prefix = "+" if val >= 0 else ""
    return f"{prefix}¥{val:,.2f}"


def _fmt_pct(val: float) -> str:
    prefix = "+" if val >= 0 else ""
    return f"{prefix}{val:.2f}%"


def _try_get_realtime_price(code: str) -> Optional[float]:
    """Try to get real-time quote via existing data_provider."""
    try:
        from data_provider.westock_fetcher import WeStockFetcher

        fetcher = WeStockFetcher()
        quote = fetcher.get_realtime_quote(code)
        if quote:
            return quote.price
    except Exception:
        pass
    try:
        import akshare as ak

        # HK stock
        if code[:2].isdigit() or code.startswith("hk"):
            hk_code = code.replace("hk", "").lstrip("0") or "700"
            df = ak.stock_hk_spot_em()
            row = df[df["代码"] == hk_code.zfill(5)]
            if not row.empty:
                return float(row.iloc[0]["最新价"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Sub-commands: trade
# ---------------------------------------------------------------------------

def cmd_trade_buy(args: argparse.Namespace):
    """Record a buy trade."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    pos_repo = PositionRepository(db)

    market = _market_from_code(args.code)
    currency = _currency_from_market(market)
    amount = args.price * args.qty
    security_type = args.type or ("etf" if args.etf else "stock")

    commission = estimate_commission(
        amount=amount, market=market, security_type=security_type,
        direction="buy", price=args.price, quantity=args.qty,
    )
    net_amount = -(amount + commission)

    today = date.today().strftime("%Y-%m-%d")
    trade_date = args.date or today
    fx = get_fx_rate_cny(currency, trade_date)
    trade_id = next_trade_id(trade_repo, trade_date)

    record = TradeRecord(
        trade_id=trade_id,
        stock_code=args.code,
        stock_name=args.name or args.code,
        market=market,
        direction="buy",
        security_type=security_type,
        price=args.price,
        quantity=args.qty,
        amount=amount,
        commission=commission,
        net_amount=net_amount,
        trade_date=trade_date,
        trade_time=args.time,
        currency=currency,
        fx_rate_cny=fx,
        reason=args.reason,
        tags=args.tags,
        emotion_score=args.emotion,
        confidence=args.confidence,
    )
    trade_repo.save(record)
    rebuild_positions(trade_repo, pos_repo)

    print(f"✅ Recorded: {trade_id}  {args.code} BUY {args.qty}@{args.price} {currency}")
    print(f"   Commission: {commission:.2f} {currency}  |  Net outflow: {abs(net_amount):.2f} {currency}")
    print(f"   FX rate: 1 {currency} = {fx} CNY")


def cmd_trade_sell(args: argparse.Namespace):
    """Record a sell trade."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    pos_repo = PositionRepository(db)

    market = _market_from_code(args.code)
    currency = _currency_from_market(market)
    amount = args.price * args.qty
    security_type = args.type or "stock"

    commission = estimate_commission(
        amount=amount, market=market, security_type=security_type,
        direction="sell", price=args.price, quantity=args.qty,
    )
    net_amount = amount - commission

    today = date.today().strftime("%Y-%m-%d")
    trade_date = args.date or today
    fx = get_fx_rate_cny(currency, trade_date)
    trade_id = next_trade_id(trade_repo, trade_date)

    record = TradeRecord(
        trade_id=trade_id,
        stock_code=args.code,
        stock_name=args.name or args.code,
        market=market,
        direction="sell",
        security_type=security_type,
        price=args.price,
        quantity=args.qty,
        amount=amount,
        commission=commission,
        net_amount=net_amount,
        trade_date=trade_date,
        trade_time=args.time,
        currency=currency,
        fx_rate_cny=fx,
        reason=args.reason,
        tags=args.tags,
    )
    trade_repo.save(record)
    rebuild_positions(trade_repo, pos_repo)

    print(f"✅ Recorded: {trade_id}  {args.code} SELL {args.qty}@{args.price} {currency}")
    print(f"   Commission: {commission:.2f} {currency}  |  Net inflow: {net_amount:.2f} {currency}")


def cmd_trade_import(args: argparse.Namespace):
    """Import historical trades from Futu CSV."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    pos_repo = PositionRepository(db)

    csv_path = args.file
    print(f"Parsing {csv_path} ...")
    importer = FutuCSVImporter(fetch_fx=not args.no_fx)
    records = importer.parse(csv_path)

    print(f"Parsed {len(records)} records. Saving to DB ...")
    inserted, skipped = importer.import_to_db(records, trade_repo, start_date=args.from_date)
    print(f"✅ Import done: {inserted} inserted, {skipped} skipped (duplicates or filtered)")

    if inserted > 0:
        print("Rebuilding positions from trade history ...")
        open_pos = rebuild_positions(trade_repo, pos_repo)
        print(f"Positions rebuilt: {len(open_pos)} open positions")


def cmd_trade_list(args: argparse.Namespace):
    """List trade history."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)

    if args.code:
        trades = trade_repo.list_by_code(args.code)
    elif args.date:
        parts = args.date.split("-")
        trades = trade_repo.list_by_month(int(parts[0]), int(parts[1]))
    else:
        trades = trade_repo.list_all(limit=50)

    if not trades:
        print("No trades found.")
        return

    print(f"{'ID':<18} {'Date':<12} {'Code':<10} {'Dir':<6} {'Qty':>6} {'Price':>10} {'Currency':<5} {'Net':>12} {'Type':<8}")
    print("-" * 95)
    for t in trades:
        net_str = f"{t.net_amount:+.2f}"
        print(
            f"{t.trade_id:<18} {t.trade_date:<12} {t.stock_code:<10} {t.direction:<6} "
            f"{t.quantity:>6} {t.price:>10.4f} {t.currency:<5} {net_str:>12} {t.security_type:<8}"
        )
    print(f"\nTotal: {len(trades)} trades")


# ---------------------------------------------------------------------------
# Sub-commands: position
# ---------------------------------------------------------------------------

def cmd_position_list(args: argparse.Namespace):
    """Show current open positions with real-time PnL."""
    db = _ensure_db()
    pos_repo = PositionRepository(db)
    positions = pos_repo.list_open()

    if not positions:
        print("No open positions.")
        return

    print(f"\n{'Code':<10} {'Name':<16} {'Mkt':<4} {'Qty':>6} {'AvgCost':>10} {'CurPrice':>10} {'PnL%':>8} {'PnL CNY':>14}")
    print("-" * 90)

    total_cost_cny = 0.0
    total_value_cny = 0.0

    for pos in positions:
        cur_price = _try_get_realtime_price(pos.stock_code)
        if cur_price:
            fx = get_fx_rate_cny(pos.currency)
            pnl = calc_position_pnl(pos, cur_price, fx)
            pnl_pct = f"{pnl['unrealized_pct']:+.2f}%"
            pnl_cny = _fmt_cny(pnl["unrealized_pnl_cny"])
            cur_price_str = f"{cur_price:.4f}"
            total_cost_cny += pos.total_cost_cny or 0
            total_value_cny += pnl["market_value_cny"]
        else:
            pnl_pct = "N/A"
            pnl_cny = "N/A"
            cur_price_str = "N/A"
            total_cost_cny += pos.total_cost_cny or 0

        print(
            f"{pos.stock_code:<10} {(pos.stock_name or ''):<16} {pos.market:<4} "
            f"{pos.quantity:>6} {pos.avg_cost:>10.4f} {cur_price_str:>10} "
            f"{pnl_pct:>8} {pnl_cny:>14}"
        )

    print("-" * 90)
    if total_value_cny:
        total_pnl_cny = total_value_cny - total_cost_cny
        print(f"{'TOTAL':<10} {'':16} {'':4} {'':6} {'':10} {'':10} {_fmt_pct(total_pnl_cny/total_cost_cny*100) if total_cost_cny else '':>8} {_fmt_cny(total_pnl_cny):>14}")


def cmd_position_snapshot(args: argparse.Namespace):
    """Take a portfolio snapshot for today."""
    db = _ensure_db()
    pos_repo = PositionRepository(db)
    snap_repo = SnapshotRepository(db)

    from trading.models import PortfolioSnapshot

    positions = pos_repo.list_open()
    today = date.today().strftime("%Y-%m-%d")

    total_cost_cny = sum(p.total_cost_cny or 0 for p in positions)
    pos_data = [
        {
            "code": p.stock_code,
            "name": p.stock_name,
            "qty": p.quantity,
            "avg_cost": p.avg_cost,
            "currency": p.currency,
        }
        for p in positions
    ]

    snap = PortfolioSnapshot(
        snapshot_date=today,
        total_cost_cny=total_cost_cny,
        positions_json=json.dumps(pos_data, ensure_ascii=False),
        note=args.note or "",
    )
    snap_repo.save(snap)
    print(f"✅ Portfolio snapshot saved for {today}  ({len(positions)} positions, cost ¥{total_cost_cny:,.2f})")


# ---------------------------------------------------------------------------
# Sub-commands: review
# ---------------------------------------------------------------------------

def cmd_review_new(args: argparse.Namespace):
    """Interactive review wizard."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    review_repo = ReviewRepository(db)

    # Resolve trade
    trade = trade_repo.get_by_id(args.ref) if args.ref.startswith("T") else None
    stock_code = trade.stock_code if trade else args.ref
    stock_name = trade.stock_name if trade else stock_code

    today = date.today().strftime("%Y-%m-%d")
    print(f"\n--- Review Wizard: {stock_code} ({stock_name}) ---")
    if trade:
        print(f"  Trade: {trade.direction.upper()} {trade.quantity}@{trade.price} on {trade.trade_date}")
    print()

    def _ask(prompt: str, default: str = "") -> str:
        val = input(f"{prompt} [{default}]: ").strip()
        return val or default

    def _ask_int(prompt: str, default: int = 3) -> Optional[int]:
        val = input(f"{prompt} (1–5) [{default}]: ").strip()
        try:
            return int(val) if val else default
        except ValueError:
            return default

    entry_price = float(_ask("[1/8] Entry price", str(trade.price) if trade else ""))
    exit_price_raw = _ask("[2/8] Exit price (leave blank if still holding)", "")
    exit_price = float(exit_price_raw) if exit_price_raw else None

    return_pct = None
    if exit_price and entry_price:
        direction = trade.direction if trade else "buy"
        if direction == "buy":
            return_pct = (exit_price - entry_price) / entry_price * 100
        else:
            return_pct = (entry_price - exit_price) / entry_price * 100
        print(f"  Return: {return_pct:+.2f}%")

    followed_ai_raw = _ask("[3/8] Followed AI suggestion? (y/n)", "y")
    followed_ai = followed_ai_raw.lower() in ("y", "yes", "1")
    deviation_reason = ""
    if not followed_ai:
        deviation_reason = _ask("  Why did you deviate from AI?", "")

    decision_score = _ask_int("[4/8] Decision quality self-score", 3)
    emotion_pre = _ask("[5/8] Pre-trade emotion (free text)", "")
    emotion_post = _ask("[6/8] Post-trade emotion (free text)", "")
    bias = _ask("[7/8] Cognitive biases detected (comma-sep, e.g. 锚定效应,过度自信)", "")
    lessons = _ask("[8/8] Key lesson from this trade", "")

    actual_outcome = "still_holding"
    if exit_price:
        if return_pct and return_pct > 0:
            actual_outcome = "hit_target"
        else:
            actual_outcome = "manual_exit"

    review_id = next_review_id(review_repo, today)
    review = TradeReview(
        review_id=review_id,
        trade_id=args.ref if trade else None,
        stock_code=stock_code,
        review_date=today,
        review_type="trade",
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=round(return_pct, 2) if return_pct is not None else None,
        actual_outcome=actual_outcome,
        decision_score=decision_score,
        followed_ai=followed_ai,
        deviation_reason=deviation_reason or None,
        pre_emotion=emotion_pre or None,
        post_emotion=emotion_post or None,
        bias_detected=bias or None,
        lessons=lessons or None,
    )
    review_repo.save(review)
    print(f"\n✅ Review saved: {review_id}")


def cmd_review_list(args: argparse.Namespace):
    db = _ensure_db()
    review_repo = ReviewRepository(db)

    if args.month:
        parts = args.month.split("-")
        reviews = review_repo.list_by_month(int(parts[0]), int(parts[1]))
    else:
        reviews = review_repo.list_all(limit=30)

    if not reviews:
        print("No reviews found.")
        return

    print(f"\n{'ID':<18} {'Date':<12} {'Code':<10} {'Type':<16} {'Return':>8} {'Score':>6} {'Outcome':<20}")
    print("-" * 90)
    for r in reviews:
        ret = f"{r.return_pct:+.2f}%" if r.return_pct is not None else "N/A"
        score = str(r.decision_score) if r.decision_score else "-"
        print(f"{r.review_id:<18} {r.review_date:<12} {r.stock_code:<10} {r.review_type:<16} {ret:>8} {score:>6} {(r.actual_outcome or ''):20}")


def cmd_review_show(args: argparse.Namespace):
    db = _ensure_db()
    review_repo = ReviewRepository(db)
    r = review_repo.get_by_id(args.review_id)
    if not r:
        print(f"Review {args.review_id} not found.")
        return
    print(f"\n=== {r.review_id} — {r.stock_code} ({r.review_date}) ===")
    fields = [
        ("Entry price", r.entry_price), ("Exit price", r.exit_price),
        ("Return %", f"{r.return_pct:+.2f}%" if r.return_pct is not None else None),
        ("Outcome", r.actual_outcome), ("Decision score", r.decision_score),
        ("Followed AI", r.followed_ai), ("Deviation reason", r.deviation_reason),
        ("Pre-trade emotion", r.pre_emotion), ("Post-trade emotion", r.post_emotion),
        ("Biases detected", r.bias_detected), ("Lessons", r.lessons),
    ]
    for label, val in fields:
        if val is not None and val != "":
            print(f"  {label:<22}: {val}")


# ---------------------------------------------------------------------------
# Sub-commands: stats
# ---------------------------------------------------------------------------

def cmd_stats_summary(args: argparse.Namespace):
    """Print financial summary statistics."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    trades = trade_repo.list_all(limit=5000)

    sells = [t for t in trades if t.direction in ("sell", "reduce")]
    buys = [t for t in trades if t.direction in ("buy", "add")]

    total_invested_cny = sum((t.amount * (t.fx_rate_cny or 1)) for t in buys)
    total_commission_cny = sum((t.commission * (t.fx_rate_cny or 1)) for t in trades)

    # PnL approximation: sum of net_amount * fx for sells minus cost of those shares
    realized_pnl_cny = 0.0
    win_count = 0
    loss_count = 0

    # Group buys by code to get avg cost
    cost_basis: dict = {}
    for t in sorted(trades, key=lambda x: x.trade_date):
        code = t.stock_code
        if t.direction in ("buy", "add"):
            if code not in cost_basis:
                cost_basis[code] = {"qty": 0, "cost": 0.0, "cost_cny": 0.0}
            b = cost_basis[code]
            fx = t.fx_rate_cny or 1.0
            b["qty"] += t.quantity
            b["cost"] += t.amount
            b["cost_cny"] += t.amount * fx
        elif t.direction in ("sell", "reduce") and code in cost_basis:
            b = cost_basis[code]
            if b["qty"] > 0:
                avg_cost_cny = b["cost_cny"] / b["qty"]
                fx = t.fx_rate_cny or 1.0
                proceeds_cny = t.amount * fx - t.commission * fx
                cost_sold_cny = avg_cost_cny * t.quantity
                pnl = proceeds_cny - cost_sold_cny
                realized_pnl_cny += pnl
                if pnl >= 0:
                    win_count += 1
                else:
                    loss_count += 1
                sold_qty = min(t.quantity, b["qty"])
                b["qty"] -= sold_qty
                b["cost_cny"] -= avg_cost_cny * sold_qty
                b["cost"] -= (b["cost"] / (b["qty"] + sold_qty)) * sold_qty if b["qty"] + sold_qty else 0

    total_closed = win_count + loss_count
    win_rate = win_count / total_closed * 100 if total_closed else 0

    print("\n=== Stats Summary ===")
    print(f"  Total trades:        {len(trades)}")
    print(f"  Total invested:      ¥{total_invested_cny:,.0f}")
    print(f"  Total commission:    ¥{total_commission_cny:,.0f}")
    print(f"  Realized PnL:        {_fmt_cny(realized_pnl_cny)}")
    print(f"  Closed trades:       {total_closed}  (win: {win_count}, loss: {loss_count})")
    print(f"  Win rate:            {win_rate:.1f}%")
    print(f"  Return on invested:  {_fmt_pct(realized_pnl_cny / total_invested_cny * 100) if total_invested_cny else 'N/A'}")


def cmd_stats_winrate(args: argparse.Namespace):
    """Win rate by strategy tag."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    trades = trade_repo.list_all(limit=5000)

    tag_stats: dict = {}
    for t in trades:
        tag = t.strategy_tag or "untagged"
        if tag not in tag_stats:
            tag_stats[tag] = {"trades": 0}
        tag_stats[tag]["trades"] += 1

    print("\n=== Win Rate by Strategy Tag ===")
    print(f"  {'Tag':<30} {'Trades':>8}")
    print("  " + "-" * 40)
    for tag, s in sorted(tag_stats.items(), key=lambda x: -x[1]["trades"]):
        print(f"  {tag:<30} {s['trades']:>8}")
    print("\n  (Attach strategy_tag to trades via `trade buy --tags` to enable breakdown)")


def cmd_stats_ai_accuracy(args: argparse.Namespace):
    """Show AI suggestion accuracy from review records."""
    db = _ensure_db()
    review_repo = ReviewRepository(db)
    reviews = review_repo.list_all(limit=500)

    followed = [r for r in reviews if r.followed_ai is True and r.return_pct is not None]
    deviated = [r for r in reviews if r.followed_ai is False and r.return_pct is not None]

    def _win_rate(lst):
        wins = sum(1 for r in lst if (r.return_pct or 0) > 0)
        return wins / len(lst) * 100 if lst else 0

    print("\n=== AI Suggestion Accuracy ===")
    print(f"  Reviews with outcome data:  {len([r for r in reviews if r.return_pct is not None])}")
    print(f"  Followed AI  ({len(followed)} trades):  win rate {_win_rate(followed):.1f}%")
    print(f"  Deviated AI  ({len(deviated)} trades):  win rate {_win_rate(deviated):.1f}%")


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading",
        description="Trading tracking & review system CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- trade ---
    trade_p = sub.add_parser("trade", help="Trade record commands")
    trade_sub = trade_p.add_subparsers(dest="subcommand", required=True)

    # trade buy
    buy_p = trade_sub.add_parser("buy", help="Record a buy trade")
    buy_p.add_argument("code", help="Stock code, e.g. hk01810 or AAPL")
    buy_p.add_argument("price", type=float, help="Fill price")
    buy_p.add_argument("qty", type=int, help="Number of shares")
    buy_p.add_argument("--type", choices=["stock", "etf", "option"], help="Security type")
    buy_p.add_argument("--etf", action="store_true", help="Mark as ETF (shorthand for --type etf)")
    buy_p.add_argument("--date", help="Trade date YYYY-MM-DD (default: today)")
    buy_p.add_argument("--time", help="Fill time HH:MM")
    buy_p.add_argument("--name", help="Stock name")
    buy_p.add_argument("--reason", help="Trade reason")
    buy_p.add_argument("--tags", help="Comma-separated strategy tags")
    buy_p.add_argument("--emotion", type=int, choices=range(1, 6), metavar="1-5", help="Emotion score")
    buy_p.add_argument("--confidence", type=int, choices=range(1, 6), metavar="1-5", help="Entry confidence")

    # trade sell
    sell_p = trade_sub.add_parser("sell", help="Record a sell trade")
    sell_p.add_argument("code")
    sell_p.add_argument("price", type=float)
    sell_p.add_argument("qty", type=int)
    sell_p.add_argument("--type", choices=["stock", "etf", "option"])
    sell_p.add_argument("--date")
    sell_p.add_argument("--time")
    sell_p.add_argument("--name")
    sell_p.add_argument("--reason")
    sell_p.add_argument("--tags")

    # trade import
    imp_p = trade_sub.add_parser("import", help="Import Futu CSV order history")
    imp_p.add_argument("file", help="Path to the CSV file")
    imp_p.add_argument("--from-date", dest="from_date", help="Only import trades from this date YYYY-MM-DD")
    imp_p.add_argument("--no-fx", action="store_true", help="Skip live FX rate fetch (use fallback rates)")

    # trade list
    list_p = trade_sub.add_parser("list", help="List trade history")
    list_p.add_argument("--code", help="Filter by stock code")
    list_p.add_argument("--date", help="Filter by YYYY-MM (month)")
    list_p.add_argument("--market", choices=["HK", "US"])

    # --- position ---
    pos_p = sub.add_parser("position", help="Position commands")
    pos_sub = pos_p.add_subparsers(dest="subcommand", required=True)

    pos_list_p = pos_sub.add_parser("list", help="Show open positions with PnL")

    pos_snap_p = pos_sub.add_parser("snapshot", help="Save portfolio snapshot")
    pos_snap_p.add_argument("--note", help="Market context note")

    # --- review ---
    rev_p = sub.add_parser("review", help="Review commands")
    rev_sub = rev_p.add_subparsers(dest="subcommand", required=True)

    rev_new_p = rev_sub.add_parser("new", help="Start an interactive review")
    rev_new_p.add_argument("ref", help="Trade ID (T...) or stock code")

    rev_list_p = rev_sub.add_parser("list", help="List reviews")
    rev_list_p.add_argument("--month", help="Filter by YYYY-MM")

    rev_show_p = rev_sub.add_parser("show", help="Show a review")
    rev_show_p.add_argument("review_id")

    # --- stats ---
    stats_p = sub.add_parser("stats", help="Statistics commands")
    stats_sub = stats_p.add_subparsers(dest="subcommand", required=True)

    sum_p = stats_sub.add_parser("summary", help="Financial summary")
    sum_p.add_argument("--period", choices=["month", "year"], default="all")

    wr_p = stats_sub.add_parser("win-rate", help="Win rate by strategy")
    wr_p.add_argument("--strategy")

    ai_p = stats_sub.add_parser("ai-accuracy", help="AI suggestion accuracy")

    # --- paper ---
    paper_p = sub.add_parser("paper", help="Paper trading commands (Alpaca)")
    paper_sub = paper_p.add_subparsers(dest="subcommand", required=True)

    paper_sub.add_parser("status", help="Show Alpaca paper account summary")
    paper_sub.add_parser("positions", help="List Alpaca paper positions")
    paper_sub.add_parser("orders", help="List open/recent Alpaca orders")
    paper_sub.add_parser("history", help="Show local DB paper trade history")
    paper_sub.add_parser("stats", help="Paper trading performance statistics")
    paper_sub.add_parser("sync", help="Sync filled Alpaca orders to local DB")

    return parser


# ---------------------------------------------------------------------------
# Paper trading sub-commands
# ---------------------------------------------------------------------------

def _alpaca() -> "AlpacaClient":
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    from trading.alpaca_client import AlpacaClient
    return AlpacaClient()


def cmd_paper_status(args):
    """Show Alpaca paper account summary."""
    client = _alpaca()
    acct = client.get_account()
    print("\n=== Alpaca Paper Account ===")
    print(f"  Account:       {acct['account_number']}  ({acct['status']})")
    print(f"  Cash:          ${float(acct['cash']):>12,.2f}")
    print(f"  Buying Power:  ${float(acct['buying_power']):>12,.2f}")
    print(f"  Portfolio Val: ${float(acct['portfolio_value']):>12,.2f}")
    pnl = float(acct['portfolio_value']) - float(acct.get('last_equity', acct['portfolio_value']))
    print(f"  Day PnL:       {_fmt_cny(pnl * 7.25)} (approx CNY)")
    clock = client._get("/clock")
    status = "OPEN" if clock.get("is_open") else "CLOSED"
    print(f"  Market:        {status}  next open: {clock.get('next_open', 'N/A')[:16]}")


def cmd_paper_positions(args):
    """List Alpaca paper positions."""
    client = _alpaca()
    positions = client.get_positions()
    if not positions:
        print("No open paper positions.")
        return
    print(f"\n{'Symbol':<10} {'Qty':>6} {'AvgCost':>10} {'CurPrice':>10} {'PnL%':>8} {'MktVal':>12}")
    print("-" * 65)
    for p in positions:
        pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
        print(
            f"{p['symbol']:<10} {float(p['qty']):>6.0f} {float(p['avg_entry_price']):>10.4f} "
            f"{float(p['current_price']):>10.4f} {pnl_pct:>+8.2f}% "
            f"${float(p['market_value']):>12,.2f}"
        )


def cmd_paper_orders(args):
    """List open and recent Alpaca orders."""
    client = _alpaca()
    orders = client.get_orders(status="all", limit=20)
    if not orders:
        print("No recent orders.")
        return
    print(f"\n{'Symbol':<10} {'Side':<5} {'Type':<10} {'Qty':>6} {'Limit':>10} {'Status':<20} {'Submitted':<20}")
    print("-" * 90)
    for o in orders:
        limit = o.get("limit_price") or "-"
        print(
            f"{o['symbol']:<10} {o['side']:<5} {o['type']:<10} {o.get('qty','?'):>6} "
            f"{str(limit):>10} {o['status']:<20} {o['submitted_at'][:19]:<20}"
        )


def cmd_paper_history(args):
    """Show local DB paper trade history."""
    db = _ensure_db()
    trade_repo = TradeRepository(db)
    from sqlalchemy import select
    from trading.models import TradeRecord
    with db.session_scope() as session:
        rows = list(
            session.execute(
                select(TradeRecord)
                .where(TradeRecord.account_type == "paper")
                .order_by(TradeRecord.trade_date.desc())
                .limit(50)
            ).scalars().all()
        )
        session.expunge_all()

    if not rows:
        print("No paper trades recorded yet.")
        return
    print(f"\n{'ID':<18} {'Date':<12} {'Code':<10} {'Dir':<5} {'Qty':>5} {'Price':>10} {'Status':<20} {'AlpacaID'}")
    print("-" * 95)
    for t in rows:
        print(
            f"{t.trade_id:<18} {t.trade_date:<12} {t.stock_code:<10} {t.direction:<5} "
            f"{t.quantity:>5} {t.price:>10.4f} {(t.alpaca_status or 'pending'):<20} "
            f"{(t.alpaca_order_id or '')[:12]}"
        )
    print(f"\nTotal: {len(rows)} paper trades")


def cmd_paper_stats(args):
    """Paper trading performance statistics."""
    db = _ensure_db()
    from sqlalchemy import select
    from trading.models import TradeRecord
    with db.session_scope() as session:
        rows = list(
            session.execute(
                select(TradeRecord).where(TradeRecord.account_type == "paper")
            ).scalars().all()
        )
        session.expunge_all()

    if not rows:
        print("No paper trades to analyze.")
        return

    buys = [r for r in rows if r.direction == "buy"]
    sells = [r for r in rows if r.direction == "sell"]
    total_invested_usd = sum(r.amount for r in buys)
    tags = {}
    for r in rows:
        t = r.strategy_tag or "untagged"
        tags[t] = tags.get(t, 0) + 1

    print("\n=== Paper Trading Stats ===")
    print(f"  Total paper trades:  {len(rows)}")
    print(f"  Buy orders:          {len(buys)}")
    print(f"  Sell orders:         {len(sells)}")
    print(f"  Total invested:      ${total_invested_usd:,.2f} USD")
    print(f"\n  Strategy breakdown:")
    for tag, count in sorted(tags.items(), key=lambda x: -x[1]):
        print(f"    {tag:<40} {count} trades")
    print("\n  (Run `paper sync` to update fill status from Alpaca)")


def cmd_paper_sync(args):
    """Sync filled Alpaca orders into local DB."""
    db = _ensure_db()
    client = _alpaca()
    from sqlalchemy import select, update
    from trading.models import TradeRecord

    # Get all closed orders from Alpaca
    filled_orders = client.get_orders(status="closed", limit=200)
    if not filled_orders:
        print("No closed orders found on Alpaca.")
        return

    filled_map = {o["id"]: o for o in filled_orders if o.get("status") == "filled"}
    print(f"Found {len(filled_map)} filled orders on Alpaca.")

    updated = 0
    with db.session_scope() as session:
        paper_trades = list(
            session.execute(
                select(TradeRecord).where(
                    TradeRecord.account_type == "paper",
                    TradeRecord.alpaca_order_id.isnot(None),
                )
            ).scalars().all()
        )
        for t in paper_trades:
            order = filled_map.get(t.alpaca_order_id)
            if order and t.alpaca_status != "filled":
                fill_price = float(order.get("filled_avg_price") or t.price)
                t.alpaca_status = "filled"
                t.price = fill_price
                t.amount = fill_price * t.quantity
                t.net_amount = -(fill_price * t.quantity)
                updated += 1
                print(f"  Updated {t.trade_id} {t.stock_code}: filled @ ${fill_price:.4f}")

    print(f"\n✅ Sync done: {updated} trades updated to 'filled'")


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        ("trade", "buy"): cmd_trade_buy,
        ("trade", "sell"): cmd_trade_sell,
        ("trade", "import"): cmd_trade_import,
        ("trade", "list"): cmd_trade_list,
        ("position", "list"): cmd_position_list,
        ("position", "snapshot"): cmd_position_snapshot,
        ("review", "new"): cmd_review_new,
        ("review", "list"): cmd_review_list,
        ("review", "show"): cmd_review_show,
        ("stats", "summary"): cmd_stats_summary,
        ("stats", "win-rate"): cmd_stats_winrate,
        ("stats", "ai-accuracy"): cmd_stats_ai_accuracy,
        ("paper", "status"): cmd_paper_status,
        ("paper", "positions"): cmd_paper_positions,
        ("paper", "orders"): cmd_paper_orders,
        ("paper", "history"): cmd_paper_history,
        ("paper", "stats"): cmd_paper_stats,
        ("paper", "sync"): cmd_paper_sync,
    }

    key = (args.command, getattr(args, "subcommand", None))
    handler = dispatch.get(key)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
