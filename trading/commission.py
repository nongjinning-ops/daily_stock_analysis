# -*- coding: utf-8 -*-
"""
Commission calculation for HK and US trades.
Based on Futu Securities standard fee schedule (as of 2025).
"""

import math


# ---------------------------------------------------------------------------
# Hong Kong — Futu Securities standard (stock / ETF)
# ---------------------------------------------------------------------------
# Fee breakdown:
#   brokerage:    0.03%  min HKD 3
#   platform fee: HKD 15 / order (fixed tier, suitable for < 35 orders/month)
#   settlement:   0.0042% (HKSCC)
#   stamp duty:   0.10%  (stocks only, ETF exempt)
#   trading fee:  0.00565% min HKD 0.01 (HKEX)
#   SFC levy:     0.0027%  min HKD 0.01
#   FRC levy:     0.00015%
# ---------------------------------------------------------------------------

HK_BROKERAGE_RATE = 0.0003  # 0.03%
HK_BROKERAGE_MIN = 3.0  # HKD
HK_PLATFORM_FEE = 15.0  # HKD per order
HK_SETTLEMENT_RATE = 0.000042  # 0.0042%
HK_STAMP_DUTY_RATE = 0.001  # 0.10% (stocks only)
HK_TRADING_FEE_RATE = 0.0000565  # 0.00565%
HK_TRADING_FEE_MIN = 0.01  # HKD
HK_SFC_LEVY_RATE = 0.000027  # 0.0027%
HK_SFC_LEVY_MIN = 0.01  # HKD
HK_FRC_LEVY_RATE = 0.0000015  # 0.00015%


def calc_hk_commission(amount: float, security_type: str = "stock") -> float:
    """
    Calculate total HK trade commission using Futu standard rates.

    Args:
        amount: Trade amount in HKD (price × quantity).
        security_type: 'stock' or 'etf'. ETF is exempt from stamp duty.

    Returns:
        Total commission in HKD, rounded to 2 decimal places.
    """
    brokerage = max(amount * HK_BROKERAGE_RATE, HK_BROKERAGE_MIN)
    platform_fee = HK_PLATFORM_FEE
    settlement = amount * HK_SETTLEMENT_RATE
    # Stamp duty: stocks only; not less than HKD 1 (round up)
    if security_type == "stock":
        stamp_raw = amount * HK_STAMP_DUTY_RATE
        stamp_duty = math.ceil(stamp_raw) if stamp_raw < 1 else math.ceil(stamp_raw)
        stamp_duty = max(stamp_duty, 1.0) if stamp_raw > 0 else 0.0
    else:
        stamp_duty = 0.0
    trading_fee = max(amount * HK_TRADING_FEE_RATE, HK_TRADING_FEE_MIN)
    sfc_levy = max(amount * HK_SFC_LEVY_RATE, HK_SFC_LEVY_MIN)
    frc_levy = amount * HK_FRC_LEVY_RATE

    total = brokerage + platform_fee + settlement + stamp_duty + trading_fee + sfc_levy + frc_levy
    return round(total, 2)


# ---------------------------------------------------------------------------
# United States — Futu Securities standard (stock / ETF)
# ---------------------------------------------------------------------------
# Fee breakdown:
#   brokerage:    USD 0.0049/share  min USD 0.99  cap 0.5% of trade value
#   platform fee: USD 0.0049/share  min USD 0.99  cap 0.5% (same structure)
#   SEC fee:      sell only  0.0000278 × trade value
#   TAF:          sell only  USD 0.000166/share  min USD 0.01
# ---------------------------------------------------------------------------

US_BROKERAGE_PER_SHARE = 0.0049  # USD
US_BROKERAGE_MIN = 0.99  # USD
US_BROKERAGE_CAP_RATE = 0.005  # 0.5% of trade value
US_SEC_FEE_RATE = 0.0000278  # sell only
US_TAF_PER_SHARE = 0.000166  # USD, sell only
US_TAF_MIN = 0.01  # USD


def calc_us_commission(price: float, quantity: int, direction: str = "buy") -> float:
    """
    Calculate total US trade commission using Futu standard rates.

    Args:
        price: Fill price in USD.
        quantity: Number of shares.
        direction: 'buy' or 'sell'. SEC fee and TAF apply to sells only.

    Returns:
        Total commission in USD, rounded to 4 decimal places.
    """
    amount = price * quantity

    # Brokerage + platform fee (same structure, charged separately)
    def _leg_fee(qty: int, amt: float) -> float:
        fee = qty * US_BROKERAGE_PER_SHARE
        fee = max(fee, US_BROKERAGE_MIN)
        fee = min(fee, amt * US_BROKERAGE_CAP_RATE)
        return fee

    brokerage = _leg_fee(quantity, amount)
    platform_fee = _leg_fee(quantity, amount)

    sec_fee = amount * US_SEC_FEE_RATE if direction == "sell" else 0.0
    taf = max(quantity * US_TAF_PER_SHARE, US_TAF_MIN) if direction == "sell" else 0.0

    total = brokerage + platform_fee + sec_fee + taf
    return round(total, 4)


def estimate_commission(
    amount: float,
    market: str,
    security_type: str = "stock",
    direction: str = "buy",
    price: float = 0.0,
    quantity: int = 0,
) -> float:
    """
    Unified commission estimator. Dispatches to HK or US calculator.

    Args:
        amount: Trade amount in original currency.
        market: 'HK' or 'US'.
        security_type: 'stock', 'etf', or 'option'.
        direction: 'buy' or 'sell'.
        price: Required for US calculation.
        quantity: Required for US calculation.

    Returns:
        Estimated total commission in original currency.
    """
    if market == "HK":
        return calc_hk_commission(amount, security_type=security_type)
    elif market == "US":
        if quantity and price:
            return calc_us_commission(price, quantity, direction=direction)
        # Fallback: rough estimate from amount only
        qty_est = max(1, round(quantity or 1))
        price_est = price or (amount / qty_est if qty_est else 0)
        return calc_us_commission(price_est, qty_est, direction=direction)
    return 0.0
