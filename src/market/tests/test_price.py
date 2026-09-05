"""PriceService 测试。"""

from unittest.mock import MagicMock

from src.market.data.cache import CacheManager
from src.market.services.price import PriceService
from src.market.types import DailyTicker


def _make_dt(ticker: str = "000001.SZ", close: float = 10.0) -> DailyTicker:
    return DailyTicker(
        ts_code=ticker, open=close - 0.5, close=close, high=close + 0.5,
        low=close - 1.0, pre_close=close, volume=1000.0, amount=10000.0,
        volume_ratio=1.0, turnover_rate=1.0, turnover_rate_f=1.0,
        pe=10.0, pe_ttm=10.0, pb=1.0, ps=1.0, ps_ttm=1.0,
        dv_ratio=0.0, dv_ttm=0.0, total_share=10000.0, float_share=8000.0,
        free_share=5000.0, total_mv=105000.0, circ_mv=84000.0,
        timestamp=1700000000000,
    )


def _make_price_service(cache: CacheManager, is_realtime: bool = False) -> PriceService:
    clock = MagicMock()
    clock.is_realtime = is_realtime
    bar_svc = MagicMock()
    return PriceService(cache, bar_svc, clock)


class TestPriceServiceErrorHandling:
    def test_batch_prices_with_error_returns_unavailable(self) -> None:
        """批量获取价格时单个失败不应影响其他。"""
        cache = CacheManager()
        svc = _make_price_service(cache, is_realtime=True)

        # 构造一个会导致 _get_price_sync 返回 unavailable 的场景
        # 无 tick、无 daily → available=False
        import asyncio

        async def run():
            return await svc.get_realtime_prices(["000001.SZ", "MISSING"])

        result = asyncio.get_event_loop().run_until_complete(run())
        assert "000001.SZ" in result
        assert "MISSING" in result
        assert result["MISSING"]["available"] is False

    def test_price_from_tick(self) -> None:
        tick = {
            "lastPrice": 10.5, "lastClose": 10.0,
            "open": 10.0, "high": 10.8, "low": 9.9,
            "volume": 500000, "amount": 5250000,
        }
        result = PriceService._price_from_tick("000001.SZ", tick)
        assert result["price"] == 10.5
        assert result["pre_close"] == 10.0
        assert result["source"] == "xtquant"
        assert result["available"] is True
