# -*- coding: utf-8 -*-
"""
Alpaca REST API client for paper trading.
Reads credentials from environment variables:
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
"""

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://paper-api.alpaca.markets/v2"


class AlpacaClient:
    """
    Thin wrapper around Alpaca REST v2 API.
    Always targets the paper trading endpoint unless explicitly overridden.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self.base_url = (base_url or os.environ.get("ALPACA_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")

        if not self.api_key or not self.secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

        self._session = requests.Session()
        self._session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.post(url, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.delete(url, timeout=10)
        if resp.status_code == 204:
            return {}
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def _patch(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.patch(url, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """Return account info (balance, buying power, status)."""
        return self._get("/account")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_order(self, symbol: str, qty: int, side: str = "buy") -> Dict[str, Any]:
        """Place a simple market order."""
        body = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        return self._post("/orders", body)

    def place_limit_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        side: str = "buy",
        time_in_force: str = "gtc",
    ) -> Dict[str, Any]:
        """Place a limit order (GTC by default)."""
        body = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": time_in_force,
        }
        return self._post("/orders", body)

    def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        time_in_force: str = "gtc",
    ) -> Dict[str, Any]:
        """
        Place a bracket order: limit buy + take-profit limit + stop-loss market.

        Args:
            symbol: Ticker (e.g. 'AAPL').
            qty: Number of shares (>= 1).
            limit_price: Entry limit price.
            take_profit_price: Target price for take-profit leg.
            stop_loss_price: Stop price for stop-loss leg.
            time_in_force: 'gtc' (default) or 'day'.

        Returns:
            Alpaca order response dict.
        """
        body = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "take_profit": {
                "limit_price": str(round(take_profit_price, 2)),
            },
            "stop_loss": {
                "stop_price": str(round(stop_loss_price, 2)),
                # Trailing to market once stop is hit
            },
        }
        logger.info(
            f"Placing bracket order: {symbol} BUY {qty} @ limit={limit_price:.2f} "
            f"TP={take_profit_price:.2f} SL={stop_loss_price:.2f}"
        )
        return self._post("/orders", body)

    def get_orders(
        self,
        status: str = "open",
        limit: int = 100,
        direction: str = "desc",
    ) -> List[Dict[str, Any]]:
        """
        Retrieve orders.

        Args:
            status: 'open', 'closed', or 'all'.
            limit: Max records (max 500).
            direction: 'asc' or 'desc' by timestamp.
        """
        return self._get("/orders", params={"status": status, "limit": limit, "direction": direction})

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._get(f"/orders/{order_id}")

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._delete(f"/orders/{order_id}")

    def cancel_all_orders(self) -> List[Dict[str, Any]]:
        return self._delete("/orders")

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return all open positions."""
        return self._get("/positions")

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return position for a single symbol, or None if not held."""
        try:
            return self._get(f"/positions/{symbol.upper()}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close entire position for a symbol at market."""
        return self._delete(f"/positions/{symbol.upper()}")

    def close_all_positions(self) -> List[Dict[str, Any]]:
        """Liquidate all open positions."""
        return self._delete("/positions")

    # ------------------------------------------------------------------
    # Portfolio history
    # ------------------------------------------------------------------

    def get_portfolio_history(
        self,
        period: str = "1M",
        timeframe: str = "1D",
    ) -> Dict[str, Any]:
        """
        Get portfolio equity curve.

        Args:
            period: '1D', '1W', '1M', '3M', '1A' (1 year).
            timeframe: '1Min', '5Min', '15Min', '1H', '1D'.
        """
        return self._get("/account/portfolio/history", params={"period": period, "timeframe": timeframe})

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "AlpacaClient":
        """Factory: create client from environment variables."""
        return cls()

    def is_market_open(self) -> bool:
        """Check if US market is currently open."""
        try:
            clock = self._get("/clock")
            return bool(clock.get("is_open", False))
        except Exception:
            return False
