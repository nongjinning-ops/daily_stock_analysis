# -*- coding: utf-8 -*-
"""
Paper trading automation: receives AI AnalysisResult and places bracket orders
on Alpaca paper account when conditions are met.

Trigger conditions:
  - market == US  (stock code is not an HK code)
  - operation_advice in ("买入", "加仓", "强烈买入")
  - analysis result has valid sniper_points (ideal_buy, stop_loss, take_profit)

Order type: bracket limit order
  - entry:      limit @ ideal_buy
  - take-profit: limit @ take_profit
  - stop-loss:   stop market @ stop_loss

Position sizing: floor(PAPER_POSITION_SIZE_USD / ideal_buy), min 1 share
"""

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from trading.alpaca_client import AlpacaClient
from trading.models import TradeRecord
from trading.repository import TradeRepository
from trading.service import next_trade_id

logger = logging.getLogger(__name__)

# Advice strings that trigger a paper buy order
_BUY_ADVICES = {"买入", "加仓", "强烈买入", "小仓位买入", "逢低买入", "逢低加仓", "轻仓买入"}

# HK stock code patterns (numeric 5-digit, or starts with hk/HK)
_HK_PREFIXES = ("hk", "HK")


def _is_us_stock(code: str) -> bool:
    """Return True if the stock code looks like a US ticker."""
    code = code.strip()
    if code[:2].lower() == "hk":
        return False
    if code.isdigit():
        return False  # pure numeric = HK
    # US tickers are alpha or alpha+numeric (AAPL, GOOG, NDAQ, QQQ …)
    return True


@dataclass
class PaperTradingConfig:
    enabled: bool = False
    position_size_usd: float = 1000.0  # dollars per trade
    min_sentiment_score: int = 0  # optional score gate (0 = no gate)
    alpaca_api_key: str = field(default_factory=lambda: os.environ.get("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.environ.get("ALPACA_SECRET_KEY", ""))
    alpaca_base_url: str = field(
        default_factory=lambda: os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    )

    @classmethod
    def from_env(cls) -> "PaperTradingConfig":
        """Build config from environment variables (loaded from .env by src/config.py)."""
        return cls(
            enabled=os.environ.get("PAPER_TRADING_ENABLED", "false").lower() in ("true", "1", "yes"),
            position_size_usd=float(os.environ.get("PAPER_POSITION_SIZE_USD", "1000")),
            min_sentiment_score=int(os.environ.get("PAPER_MIN_SENTIMENT_SCORE", "0")),
        )


class PaperTrader:
    """
    Singleton that listens for AnalysisResult events and auto-places paper trades.
    """

    _instance: Optional["PaperTrader"] = None

    def __init__(self, config: Optional[PaperTradingConfig] = None):
        self.config = config or PaperTradingConfig.from_env()
        self._alpaca: Optional[AlpacaClient] = None
        self._trade_repo: Optional[TradeRepository] = None

    @classmethod
    def get_instance(cls) -> "PaperTrader":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    @property
    def alpaca(self) -> AlpacaClient:
        if self._alpaca is None:
            self._alpaca = AlpacaClient(
                api_key=self.config.alpaca_api_key,
                secret_key=self.config.alpaca_secret_key,
                base_url=self.config.alpaca_base_url,
            )
        return self._alpaca

    @property
    def trade_repo(self) -> TradeRepository:
        if self._trade_repo is None:
            from src.storage import DatabaseManager
            import trading.models  # noqa: F401 — ensure tables registered

            self._trade_repo = TradeRepository(DatabaseManager.get_instance())
        return self._trade_repo

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def on_analysis_result(self, result, analysis_db_id: Optional[int] = None) -> Optional[TradeRecord]:
        """
        Called after each AI analysis completes.
        Places a paper bracket order if conditions are met.

        Args:
            result: AnalysisResult instance from src/analyzer.py.
            analysis_db_id: ID in analysis_history table (for linking).

        Returns:
            TradeRecord if an order was placed, else None.
        """
        if not self.config.enabled:
            logger.debug("Paper trading disabled, skipping.")
            return None

        # --- Gate 1: US stocks only ---
        if not _is_us_stock(result.code):
            logger.debug(f"Skipping {result.code}: not a US stock.")
            return None

        # --- Gate 2: Buy signal ---
        # AI often returns compound advice like "加仓/持有" or "持有/小仓位买入"
        # Match if ANY part of the advice contains a buy keyword
        advice = (result.operation_advice or "").strip()
        advice_parts = {p.strip() for p in advice.replace("/", "|").replace("、", "|").split("|")}
        if not advice_parts.intersection(_BUY_ADVICES):
            logger.debug(f"Skipping {result.code}: advice='{advice}' is not a buy signal.")
            return None
        logger.info(f"{result.code}: buy signal detected in advice='{advice}'")

        # --- Gate 3: Dedup — skip if there is already an open Alpaca order for this symbol ---
        try:
            open_orders = self.alpaca.get_orders(status="open", limit=100)
            open_buy_symbols = {o["symbol"] for o in open_orders if o["side"] == "buy"}
            if result.code.upper() in open_buy_symbols:
                logger.info(f"Skipping {result.code}: already has an open buy order on Alpaca.")
                return None
        except Exception as e:
            logger.warning(f"{result.code}: failed to check open orders for dedup: {e}")

        # --- Gate 4: Optional sentiment gate ---
        if self.config.min_sentiment_score > 0 and result.sentiment_score < self.config.min_sentiment_score:
            logger.info(
                f"Skipping {result.code}: sentiment {result.sentiment_score} < "
                f"threshold {self.config.min_sentiment_score}"
            )
            return None

        # --- Extract prices from AI dashboard ---
        # Try multiple sources: sniper_points dict → direct result attributes → current_price
        sniper = {}
        try:
            sniper = result.get_sniper_points() or {}
        except Exception:
            pass

        ideal_buy = (
            _parse_price(sniper.get("ideal_buy"))
            or _parse_price(sniper.get("理想买点"))
            or _parse_price(sniper.get("入场价"))
            or _parse_price(getattr(result, "ideal_buy", None))  # DB field on AnalysisResult
        )
        stop_loss = (
            _parse_price(sniper.get("stop_loss"))
            or _parse_price(sniper.get("止损位"))
            or _parse_price(sniper.get("止损价"))
            or _parse_price(getattr(result, "stop_loss", None))
        )
        take_profit = (
            _parse_price(sniper.get("take_profit"))
            or _parse_price(sniper.get("目标价"))
            or _parse_price(sniper.get("止盈位"))
            or _parse_price(sniper.get("目标位"))
            or _parse_price(getattr(result, "take_profit", None))
        )

        # Fallback to current_price if ideal_buy still not available
        if not ideal_buy and getattr(result, "current_price", None):
            ideal_buy = result.current_price
            logger.info(f"{result.code}: ideal_buy not found, using current_price {ideal_buy}")

        # Last resort: fetch latest close price from data_provider
        if not ideal_buy:
            ideal_buy = _fetch_last_close(result.code)
            if ideal_buy:
                logger.info(f"{result.code}: using last close price as entry: {ideal_buy}")

        if not ideal_buy:
            logger.warning(f"Skipping {result.code}: cannot determine entry price from analysis.")
            return None

        # Derive missing stop/take from ideal_buy if AI didn't provide them
        if not stop_loss:
            stop_loss = round(ideal_buy * 0.92, 2)  # -8% default
            logger.info(f"{result.code}: stop_loss not found, using -8% default: {stop_loss}")
        if not take_profit:
            take_profit = round(ideal_buy * 1.15, 2)  # +15% default
            logger.info(f"{result.code}: take_profit not found, using +15% default: {take_profit}")

        # Sanity check: stop < entry < take_profit
        if stop_loss >= ideal_buy or take_profit <= ideal_buy:
            logger.warning(
                f"Skipping {result.code}: invalid bracket prices "
                f"SL={stop_loss} entry={ideal_buy} TP={take_profit}"
            )
            return None

        # --- Position sizing ---
        qty = max(1, math.floor(self.config.position_size_usd / ideal_buy))

        # --- Place bracket order on Alpaca ---
        try:
            order = self.alpaca.place_bracket_order(
                symbol=result.code,
                qty=qty,
                limit_price=ideal_buy,
                take_profit_price=take_profit,
                stop_loss_price=stop_loss,
            )
        except Exception as e:
            logger.error(f"Alpaca bracket order failed for {result.code}: {e}")
            return None

        order_id = order.get("id", "")
        order_status = order.get("status", "pending_new")
        fill_price = float(order.get("filled_avg_price") or ideal_buy)

        logger.info(
            f"Paper order placed: {result.code} BUY {qty}@{ideal_buy:.2f} "
            f"TP={take_profit:.2f} SL={stop_loss:.2f} | order_id={order_id}"
        )

        # --- Record to local DB (account_type='paper') ---
        today = date.today().strftime("%Y-%m-%d")
        trade_id = next_trade_id(self.trade_repo, today)
        amount = round(fill_price * qty, 4)

        record = TradeRecord(
            trade_id=trade_id,
            stock_code=result.code,
            stock_name=result.name or result.code,
            market="US",
            direction="buy",
            security_type="stock",
            price=ideal_buy,
            quantity=qty,
            amount=amount,
            commission=0.0,  # Alpaca paper has no commissions
            net_amount=-amount,
            trade_date=today,
            currency="USD",
            fx_rate_cny=7.25,  # approximate; updated later if needed
            analysis_id=analysis_db_id,
            strategy_tag=f"ai_paper:{advice}",
            reason=f"AI建议: {advice} | 评分: {result.sentiment_score} | {result.get_core_conclusion()[:80]}",
            confidence=_score_to_confidence(result.sentiment_score),
            account_type="paper",
            alpaca_order_id=order_id,
            alpaca_status=order_status,
        )
        try:
            self.trade_repo.save(record)
            logger.info(f"Paper trade recorded: {trade_id}")
        except Exception as e:
            logger.error(f"Failed to save paper trade record: {e}")

        return record


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_price(value) -> Optional[float]:
    """Parse a price value that may be a float, int, or string like '$168.5' or '168-170'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip().replace("$", "").replace(",", "").replace("￥", "")
    # Range like "168-170" → take the lower bound
    if "-" in s and not s.startswith("-"):
        s = s.split("-")[0].strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _score_to_confidence(sentiment_score: int) -> int:
    """Map 0-100 sentiment score to 1-5 confidence scale."""
    if sentiment_score >= 80:
        return 5
    elif sentiment_score >= 65:
        return 4
    elif sentiment_score >= 55:
        return 3
    elif sentiment_score >= 45:
        return 2
    return 1


def _fetch_last_close(code: str) -> Optional[float]:
    """Fetch the most recent close price for a US stock from data_provider or DB."""
    # Try data_provider first
    try:
        from data_provider.base import DataProviderManager
        provider = DataProviderManager()
        df = provider.get_stock_data(code, days=5)
        if df is not None and not df.empty:
            return float(df.iloc[-1]["close"])
    except Exception:
        pass
    # Fallback: query stock_daily table
    try:
        from src.storage import DatabaseManager
        from sqlalchemy import text
        db = DatabaseManager.get_instance()
        with db.session_scope() as s:
            row = s.execute(text(
                "SELECT close FROM stock_daily WHERE code = :code ORDER BY date DESC LIMIT 1"
            ), {"code": code}).fetchone()
            if row and row[0]:
                return float(row[0])
    except Exception:
        pass
    return None
