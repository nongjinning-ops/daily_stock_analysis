# -*- coding: utf-8 -*-
"""
Futu Securities CSV order history importer.

Handles the special structure of Futu's export:
  1. Split fills: one order → multiple rows (only first row has order header)
  2. Options: strategy summary row + individual leg rows
  3. Cancelled / failed orders: skip them
  4. Auto-detect ETF from code patterns and known list
  5. Auto-fill fx_rate_cny from akshare historical rates
"""

import csv
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

from trading.commission import calc_hk_commission, calc_us_commission
from trading.models import TradeRecord
from trading.repository import TradeRepository
from trading.service import get_fx_rate_cny

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ETF code detection
# ---------------------------------------------------------------------------

# HK ETF codes starting with 02xxx, 03xxx, 07xxx are typically ETFs
_HK_ETF_CODE_PREFIXES = ("02", "03", "07", "08")
_KNOWN_ETF_CODES = {
    "07226", "07231", "07232", "07261", "07266",  # leveraged / inverse
    "02800", "02801", "02823", "02833",  # tracker ETFs
    "03032", "03033", "03037",  # China-themed ETFs
    "QQQ", "SPY", "IWM", "GLD", "SLV", "TLT", "ARKK", "NDAQ",
}


def _is_etf(code: str) -> bool:
    code = code.strip()
    if code in _KNOWN_ETF_CODES:
        return True
    # HK numeric codes: ETF typically starts with 02/03/07/08
    if re.match(r"^\d{5}$", code):
        return code[:2] in _HK_ETF_CODE_PREFIXES
    return False


def _is_option_code(code: str) -> bool:
    """Detect option strategy codes like GOOG260220C300/320 or MIU251230C45/50."""
    return "/" in code or bool(re.search(r"[CP]\d{3,}", code))


def _parse_hk_datetime(dt_str: str) -> Tuple[str, str]:
    """
    Parse Futu HK datetime like '2025/01/06 10:32:12(香港)'.
    Returns (date_str 'YYYY-MM-DD', time_str 'HH:MM').
    """
    dt_str = dt_str.replace("(香港)", "").replace("(美东)", "").strip()
    try:
        dt = datetime.strptime(dt_str, "%Y/%m/%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        return dt_str[:10].replace("/", "-"), ""


def _safe_float(val: str) -> float:
    """Parse numeric string, removing commas."""
    if not val or not val.strip():
        return 0.0
    try:
        return float(val.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_direction(direction_str: str) -> str:
    mapping = {"买入": "buy", "卖出": "sell", "卖空": "sell"}
    return mapping.get(direction_str.strip(), direction_str.strip().lower())


def _parse_market(market_str: str) -> str:
    if "港股" in market_str or "HK" in market_str:
        return "HK"
    if "美股" in market_str or "US" in market_str:
        return "US"
    return market_str.strip()


def _parse_currency(currency_str: str) -> str:
    mapping = {"港元": "HKD", "美元": "USD"}
    return mapping.get(currency_str.strip(), currency_str.strip())


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

class FutuCSVImporter:
    """
    Parse a Futu order history CSV and convert to TradeRecord list.

    Usage:
        importer = FutuCSVImporter()
        records = importer.parse("/path/to/orders.csv")
        inserted = importer.import_to_db(records, trade_repo)
    """

    def __init__(self, fetch_fx: bool = True):
        self.fetch_fx = fetch_fx
        self._fx_cache: dict = {}

    def _get_fx(self, currency: str, trade_date: str) -> float:
        key = f"{currency}:{trade_date}"
        if key not in self._fx_cache:
            self._fx_cache[key] = get_fx_rate_cny(currency, trade_date) if self.fetch_fx else 1.0
        return self._fx_cache[key]

    def parse(self, csv_path: str) -> List[TradeRecord]:
        """
        Parse a Futu CSV export file and return a list of TradeRecord objects.
        Only includes fully executed (全部成交 / 部分成交) trades.
        Options are stored as a single record per strategy (net cash flow).

        Args:
            csv_path: Absolute path to the CSV file.

        Returns:
            List of TradeRecord objects (not yet saved to DB).
        """
        records = []
        seen_order_ids: set = set()

        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # Group rows: each "real" order starts with a non-empty 代码 and non-empty 交易状态
        # Sub-rows (split fills, option legs) have empty 代码
        i = 0
        while i < len(rows):
            row = rows[i]
            code = row.get("代码", "").strip()
            status = row.get("交易状态", "").strip()

            # Skip rows that are sub-legs (empty code) or have no relevant status
            if not code:
                i += 1
                continue

            # Skip cancelled, failed, waiting
            if status in ("已撤单", "下单失败", "等待提交", ""):
                i += 1
                continue

            # Skip partially-matched rows (only header, 0 filled)
            filled = row.get("已成交", "0").strip()
            if filled == "0" or not filled:
                i += 1
                continue

            # --- Collect all sub-rows belonging to this order ---
            sub_rows = []
            j = i + 1
            while j < len(rows) and not rows[j].get("代码", "").strip():
                sub_rows.append(rows[j])
                j += 1

            try:
                record = self._build_record(row, sub_rows, seen_order_ids)
                if record:
                    records.append(record)
            except Exception as e:
                logger.warning(f"Skipping row {i+2} ({code}): {e}")

            i = j  # advance past sub_rows

        logger.info(f"Parsed {len(records)} trade records from {csv_path}")
        return records

    def _build_record(
        self,
        row: dict,
        sub_rows: list,
        seen_order_ids: set,
    ) -> Optional[TradeRecord]:
        code = row["代码"].strip()
        name = row.get("名称", "").strip()
        direction_raw = row.get("方向", "").strip()
        order_time_raw = row.get("下单时间", "").strip()
        market_raw = row.get("市场", "").strip()
        currency_raw = row.get("币种", "").strip()

        market = _parse_market(market_raw)
        currency = _parse_currency(currency_raw)
        direction = _parse_direction(direction_raw)

        # --- Detect security type ---
        if _is_option_code(code):
            security_type = "option"
        elif _is_etf(code):
            security_type = "etf"
        else:
            security_type = "stock"

        # --- Parse date/time from order time (used as dedup key) ---
        trade_date, trade_time = _parse_hk_datetime(order_time_raw)

        # --- Build unique dedup key from order header ---
        # Use down-order-time + code + direction as the natural key
        dedup_key = f"{code}:{order_time_raw}:{direction_raw}"
        if dedup_key in seen_order_ids:
            return None
        seen_order_ids.add(dedup_key)

        # --- Aggregate fills (main row + sub_rows) ---
        total_qty = 0
        total_amount = 0.0
        fill_time = ""

        # Main row fill
        main_qty = int(_safe_float(row.get("成交数量", "0")))
        main_price = _safe_float(row.get("成交价格", "0"))
        main_amount = _safe_float(row.get("成交金额", "0"))
        main_fill_time_raw = row.get("成交时间", "").strip()

        if main_qty > 0:
            total_qty += main_qty
            total_amount += main_amount
            if main_fill_time_raw:
                fill_date, fill_time = _parse_hk_datetime(main_fill_time_raw)
                trade_date = fill_date  # prefer actual fill date

        # Sub-rows (split fills or option legs)
        for sr in sub_rows:
            sr_qty = int(_safe_float(sr.get("成交数量", "0")))
            sr_amount = _safe_float(sr.get("成交金额", "0"))
            sr_fill_time_raw = sr.get("成交时间", "").strip()
            if sr_qty > 0:
                total_qty += sr_qty
                total_amount += sr_amount
                if sr_fill_time_raw and not fill_time:
                    fd, fill_time = _parse_hk_datetime(sr_fill_time_raw)

        if total_qty == 0 and security_type != "option":
            return None  # no fill data

        # For options, use net amount from header row (the "订单金额" is net debit/credit)
        if security_type == "option":
            order_amount_str = row.get("订单金额", "").strip()
            if "/" in code:
                # Strategy net: buy → debit (net_amount negative), sell → credit (positive)
                order_amount = _safe_float(order_amount_str)
                total_amount = order_amount
                total_qty = int(_safe_float(row.get("已成交", "1")))  # number of contracts/sets
                # Use the fill amounts if available
                if main_amount > 0:
                    total_amount = main_amount
            else:
                # Individual option leg
                total_amount = main_amount if main_amount > 0 else _safe_float(order_amount_str)
                total_qty = main_qty if main_qty > 0 else 1

        avg_price = round(total_amount / total_qty, 6) if total_qty > 0 else main_price

        # --- Fees ---
        commission = _safe_float(row.get("合计费用", "0"))
        if commission == 0.0 and total_amount > 0:
            # Auto-estimate if not in CSV
            commission = calc_hk_commission(total_amount, security_type) if market == "HK" else calc_us_commission(avg_price, total_qty, direction)

        # --- Net cash flow ---
        # Buy/add: outflow (negative)  Sell/reduce: inflow (positive)
        if direction in ("buy", "add"):
            net_amount = -(total_amount + commission)
        else:
            net_amount = total_amount - commission

        # --- FX rate ---
        fx_rate_cny = self._get_fx(currency, trade_date)

        # --- Build raw_order_id for dedup on re-import ---
        raw_order_id = dedup_key[:50]

        record = TradeRecord(
            trade_id="",  # will be assigned in import_to_db
            stock_code=code,
            stock_name=name or code,
            market=market,
            direction=direction,
            security_type=security_type,
            price=avg_price,
            quantity=total_qty,
            amount=round(total_amount, 4),
            commission=commission,
            net_amount=round(net_amount, 4),
            trade_date=trade_date,
            trade_time=fill_time or trade_time,
            currency=currency,
            fx_rate_cny=fx_rate_cny,
            raw_order_id=raw_order_id,
        )
        return record

    def import_to_db(
        self,
        records: List[TradeRecord],
        trade_repo: TradeRepository,
        start_date: Optional[str] = None,
    ) -> Tuple[int, int]:
        """
        Save parsed records to DB, assigning trade IDs and skipping duplicates.

        Args:
            records: List from parse().
            trade_repo: TradeRepository instance.
            start_date: Only import trades on or after this date (YYYY-MM-DD).

        Returns:
            (inserted_count, skipped_count)
        """
        # Filter by date if requested
        if start_date:
            records = [r for r in records if r.trade_date >= start_date]

        # Sort chronologically before assigning IDs
        records.sort(key=lambda r: (r.trade_date, r.trade_time or ""))

        # Assign trade IDs (skip duplicates by raw_order_id)
        date_seq: dict = {}
        to_save = []
        for rec in records:
            # Check duplicate
            if trade_repo.get_by_raw_order_id(rec.raw_order_id):
                continue
            d = rec.trade_date
            date_seq[d] = date_seq.get(d, 0) + 1
            rec.trade_id = f"T{d.replace('-', '')}-{date_seq[d]:03d}"
            to_save.append(rec)

        inserted = trade_repo.bulk_save(to_save)
        skipped = len(records) - inserted
        logger.info(f"Import complete: {inserted} inserted, {skipped} skipped")
        return inserted, skipped
