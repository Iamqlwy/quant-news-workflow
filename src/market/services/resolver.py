"""Resolver —— 名称→代码解析。"""

from __future__ import annotations

from loguru import logger

from src.market.config import INDEX_NAME_TO_CODE
from src.market.data.cache import CacheManager



def _dedup(results: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按 ts_code 去重，重名的用 / 拼接。"""
    merged: dict[str, list[str]] = {}
    for ticker, name in results:
        merged.setdefault(ticker, []).append(name)
    return [(ticker, "/".join(dict.fromkeys(names))) for ticker, names in merged.items()]


class Resolver:
    """股票名称、指数名称、板块名称到代码的解析。"""

    def __init__(self, cache: CacheManager) -> None:
        self._cache = cache

    def resolve_stock_ticker(self, name: str) -> list[tuple[str, str]]:
        """根据名称模糊查找股票代码。

        返回 [(ticker, name), ...] 列表。
        """
        session = self._cache.session

        # 1. stock_basic 精确匹配
        if session.stock_basic is not None and not session.stock_basic.empty:
            basic = session.stock_basic
            if "name" in basic.columns and "ts_code" in basic.columns:
                exact = basic[basic["name"] == name]
                if not exact.empty:
                    return _dedup([(str(r["ts_code"]), str(r["name"])) for _, r in exact.iterrows()])
                # 模糊匹配
                fuzzy = basic[basic["name"].str.contains(name, na=False, regex=False)]
                if not fuzzy.empty:
                    return _dedup([(str(r["ts_code"]), str(r["name"])) for _, r in fuzzy.iterrows()])

        # 2. stock_name_history 兜底
        if session.stock_name_history is not None and not session.stock_name_history.empty:
            hist = session.stock_name_history
            if "name" in hist.columns and "ts_code" in hist.columns:
                fuzzy = hist[hist["name"].str.contains(name, na=False, regex=False)]
                if not fuzzy.empty:
                    return _dedup([(str(r["ts_code"]), str(r["name"])) for _, r in fuzzy.iterrows()])

        logger.debug("resolve_stock_ticker: no match for '{}'", name)
        return []

    # ── 港股 ──────────────────────────────────

    def infer_stock_market(self, name: str) -> tuple[str, str, str] | None:
        """推断股票所属市场。

        Returns (market_type, ticker, resolved_name) 或 None。
        market_type: "a_share" | "hk" | "us_possible"
        """
        # 1. A 股
        a_matches = self.resolve_stock_ticker(name)
        if a_matches:
            ticker, rname = a_matches[0]
            return ("a_share", ticker, rname)

        # 2. 港股
        hk = self._cache.session.hk_basic
        if hk is not None and not hk.empty:
            exact = hk[hk["name"] == name]
            fuzzy = hk[hk["name"].str.contains(name, na=False, regex=False)] if exact.empty else exact
            if not fuzzy.empty:
                r = fuzzy.iloc[0]
                return ("hk", str(r["ts_code"]), str(r["name"]))

        # 3. 美股特征检测（纯英文名）
        import re
        if re.fullmatch(r"[A-Za-z0-9 .&(),-]+", name):
            return ("us_possible", None, name)

        return None

    def resolve_index_name(self, name: str) -> str | None:
        """将指数名称解析为代码。"""
        return INDEX_NAME_TO_CODE.get(name)

    def resolve_sector_code(self, sector: str) -> str | None:
        """将板块名称解析为代码（从 session 预计算索引 O(1) 查找）。"""
        name_to_code: dict = self._cache.session.adhoc.get("sector_name_to_code") or {}
        code = name_to_code.get(sector)
        if code:
            return code
        # 模糊匹配回退
        for name, c in name_to_code.items():
            if sector in name:
                return c
        logger.debug("resolve_sector_code: no match for '{}'", sector)
        return None

    def get_stock_name(self, ticker: str) -> str:
        """根据 ticker 查找股票名称。"""
        session = self._cache.session
        if session.stock_basic is not None and not session.stock_basic.empty:
            basic = session.stock_basic
            if "ts_code" in basic.columns and "name" in basic.columns:
                match = basic[basic["ts_code"] == ticker]
                if not match.empty:
                    return str(match.iloc[0]["name"])
        return ""
