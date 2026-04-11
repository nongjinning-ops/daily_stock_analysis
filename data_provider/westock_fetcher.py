# -*- coding: utf-8 -*-
"""
===================================
WeStockFetcher - 腾讯自选股数据源（实时行情增量字段）
===================================

数据来源：腾讯自选股行情接口（通过 westock-data CLI）

特点：
- 免费、无需 Token
- 支持 A股、港股、美股
- 提供现有数据源缺失的增量字段：
  * pe_fwd（预测PE）、ps_ttm、pcf_ttm、dividend_ratio_ttm
  * chg_5d / chg_10d / chg_20d / chg_ytd（多周期涨跌幅）
  * high_52w / low_52w（52周高低）

用途：
- 作为实时行情的补充数据源（增量字段 Enricher）
- 不替代主数据源，只在 merge 阶段补充缺失字段
"""

import json
import logging
import os
import subprocess
import time
from typing import Optional, Dict, Any

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import UnifiedRealtimeQuote, RealtimeSource, safe_float, safe_int

logger = logging.getLogger(__name__)

# westock-data CLI 路径
_WESTOCK_SCRIPT = os.path.expanduser('~/.workbuddy/skills/westock-data/scripts/index.js')

# 股票代码格式转换：daily_stock 内部格式 → westock-data 格式
# 内部格式：600519 / hk00700 / AAPL / GOOGL / IXIC
# westock格式：sh600519 / hk00700 / usAAPL / usGOOGL / us.IXIC

def _to_westock_code(code: str) -> Optional[str]:
    """将 daily_stock 内部代码转换为 westock-data 格式"""
    if not code:
        return None
    c = code.strip()
    upper = c.upper()

    # 已带前缀的港股: hk00700 → 直接用
    if upper.startswith('HK') and len(c) >= 4:
        return c.lower()[:2] + c[2:]  # 保持 hk 小写

    # 美股指数: IXIC / HSI / HSTECH 等（由 us_index_mapping 处理的）
    from .us_index_mapping import US_INDEX_MAPPING
    if upper in US_INDEX_MAPPING:
        yf_sym, _ = US_INDEX_MAPPING[upper]
        # ^IXIC → us.IXIC, ^HSI → hkHSI
        if yf_sym.startswith('^'):
            sym = yf_sym[1:]
            # 港股指数
            if sym in ('HSI', 'HSTECH', 'HSCE'):
                return f'hk{sym}'
            # 美股指数
            return f'us.{sym}'
        return None

    # 美股: AAPL / GOOGL / TSLA / NVDA 等（纯字母）
    if c.isalpha() and len(c) <= 5:
        return f'us{c.upper()}'

    # A股: 6位数字 → 识别交易所
    if c.isdigit() and len(c) == 6:
        if c.startswith('6') or c.startswith('9'):
            return f'sh{c}'
        elif c.startswith(('0', '2', '3')):
            return f'sz{c}'
        elif c.startswith('4') or c.startswith('8') or c.startswith('43'):
            return f'bj{c}'

    return None


class WeStockFetcher(BaseFetcher):
    """
    腾讯自选股数据源 Fetcher

    仅实现 get_realtime_quote()，作为增量字段 Enricher。
    历史行情数据由其他 Fetcher 负责。
    """

    name = "WeStockFetcher"
    priority = 5  # 最低优先级，作为补充

    def __init__(self):
        super().__init__()
        self._available = os.path.exists(_WESTOCK_SCRIPT)
        if not self._available:
            logger.warning(f"[WeStock] CLI 不存在: {_WESTOCK_SCRIPT}，WeStockFetcher 不可用")

    # ──────────────────────────────────────────────
    # BaseFetcher 必须实现的抽象方法（历史行情）
    # ──────────────────────────────────────────────

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str):
        """历史行情不由本 Fetcher 负责"""
        import pandas as pd
        return pd.DataFrame()

    def _normalize_data(self, df, stock_code: str):
        """历史行情不由本 Fetcher 负责"""
        return df

    def fetch_stock_data(self, stock_code: str, start_date: str, end_date: str):
        """历史行情不由本 Fetcher 负责"""
        return None

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        return None

    # ──────────────────────────────────────────────
    # 核心：实时行情（带增量字段）
    # ──────────────────────────────────────────────

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        通过 westock-data CLI 获取实时行情

        返回包含增量字段的 UnifiedRealtimeQuote：
        - pe_fwd, ps_ttm, pcf_ttm, dividend_ratio_ttm
        - chg_5d, chg_10d, chg_20d, chg_ytd
        - high_52w, low_52w
        """
        if not self._available:
            return None

        ws_code = _to_westock_code(stock_code)
        if not ws_code:
            logger.debug(f"[WeStock] 无法转换代码: {stock_code}")
            return None

        try:
            result = self._call_cli('quote', ws_code)
            if not result:
                return None

            q = self._extract_quote_data(result, ws_code)
            if not q:
                return None

            return self._parse_quote(stock_code, q)

        except Exception as e:
            logger.warning(f"[WeStock] {stock_code} 实时行情获取失败: {e}")
            return None

    def _extract_quote_data(self, result: Dict[str, Any], ws_code: str) -> Optional[Dict]:
        """从 CLI 返回的 JSON 中提取行情数据，兼容两种格式：
        1. BatchResult: { data: [ {symbol, data: {...}} ] }
        2. 单股: { data: { ws_code: {...} } }
        """
        data = result.get('data')
        if not data:
            return None

        # 格式1：BatchResult - data 是列表
        if isinstance(data, list):
            if data:
                item = data[0]
                return item.get('data') if isinstance(item, dict) else None
            return None

        # 格式2：单股 - data 是 dict，key 为股票代码
        if isinstance(data, dict):
            # 直接命中 ws_code
            if ws_code in data:
                return data[ws_code]
            # 只有一个 key，直接取
            values = list(data.values())
            if len(values) == 1 and isinstance(values[0], dict):
                return values[0]

        return None

    def _call_cli(self, cmd: str, *args) -> Optional[Dict[str, Any]]:
        """调用 westock-data CLI，返回解析后的 JSON"""
        cli_args = ['node', _WESTOCK_SCRIPT, cmd] + list(args)
        try:
            proc = subprocess.run(
                cli_args,
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = proc.stdout
            if not output.strip():
                return None

            # 跳过首行状态文本，找到 JSON 起始位置
            lines = output.splitlines()
            json_start = next(
                (i for i, l in enumerate(lines) if l.strip().startswith('{')),
                None
            )
            if json_start is None:
                return None

            raw = '\n'.join(lines[json_start:])
            return json.loads(raw)

        except subprocess.TimeoutExpired:
            logger.warning(f"[WeStock] CLI 超时: {cmd} {args}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"[WeStock] JSON 解析失败: {e}")
            return None
        except Exception as e:
            logger.warning(f"[WeStock] CLI 调用失败: {e}")
            return None

    def _parse_quote(self, original_code: str, q: Dict[str, Any]) -> Optional[UnifiedRealtimeQuote]:
        """将 westock API 返回的字段映射到 UnifiedRealtimeQuote"""
        price = safe_float(q.get('price'))
        if not price:
            return None

        return UnifiedRealtimeQuote(
            code=original_code,
            name=q.get('name', ''),
            source=RealtimeSource.FALLBACK,  # 标记为补充源

            # 基础价格（已有数据源覆盖，这里也填充以备回退）
            price=price,
            change_pct=safe_float(q.get('change_percent')),
            change_amount=safe_float(q.get('change')),
            volume=safe_int(q.get('volume')),
            amount=safe_float(q.get('amount')),
            volume_ratio=safe_float(q.get('volume_ratio')),
            turnover_rate=safe_float(q.get('turnover_rate')),
            amplitude=safe_float(q.get('range_pct')),
            open_price=safe_float(q.get('open')),
            high=safe_float(q.get('high')),
            low=safe_float(q.get('low')),
            pre_close=safe_float(q.get('prev_close')),

            # 估值指标
            pe_ratio=safe_float(q.get('pe_ratio')),
            pb_ratio=safe_float(q.get('pb_ratio')),
            total_mv=safe_float(q.get('total_market_cap')),
            circ_mv=safe_float(q.get('circulating_market_cap')),

            # 52周区间
            high_52w=safe_float(q.get('high_52week')),
            low_52w=safe_float(q.get('low_52week')),

            # === 增量字段 ===
            pe_fwd=safe_float(q.get('pe_fwd')),
            ps_ttm=safe_float(q.get('ps_ttm')),
            pcf_ttm=safe_float(q.get('pcf_ttm')),
            dividend_ratio_ttm=safe_float(q.get('dividend_ratio_ttm')),
            chg_5d=safe_float(q.get('chg_5d')),
            chg_10d=safe_float(q.get('chg_10d')),
            chg_20d=safe_float(q.get('chg_20d')),
            change_60d=safe_float(q.get('chg_60d')),  # 复用已有字段
            chg_ytd=safe_float(q.get('chg_ytd')),
        )
