"""Resolver 测试。"""

import pandas as pd

from src.market.data.cache import CacheManager
from src.market.services.resolver import Resolver


class TestResolver:
    def test_resolve_stock_exact_match(self) -> None:
        cache = CacheManager()
        cache.session.stock_basic = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["平安银行", "万科A"],
        })
        resolver = Resolver(cache)
        result = resolver.resolve_stock_ticker("平安银行")
        assert result == [("000001.SZ", "平安银行")]

    def test_resolve_stock_fuzzy_match(self) -> None:
        cache = CacheManager()
        cache.session.stock_basic = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["平安银行", "万科A"],
        })
        resolver = Resolver(cache)
        result = resolver.resolve_stock_ticker("银行")
        assert len(result) >= 1
        assert result[0][0] == "000001.SZ"

    def test_resolve_stock_not_found(self) -> None:
        cache = CacheManager()
        resolver = Resolver(cache)
        assert resolver.resolve_stock_ticker("不存在的股票") == []

    def test_resolve_index_name(self) -> None:
        cache = CacheManager()
        resolver = Resolver(cache)
        assert resolver.resolve_index_name("上证指数") == "000001.SH"

    def test_get_stock_name(self) -> None:
        cache = CacheManager()
        cache.session.stock_basic = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "name": ["平安银行"],
        })
        resolver = Resolver(cache)
        assert resolver.get_stock_name("000001.SZ") == "平安银行"

    def test_get_stock_name_missing(self) -> None:
        cache = CacheManager()
        resolver = Resolver(cache)
        assert resolver.get_stock_name("XXXXXX.SZ") == ""
