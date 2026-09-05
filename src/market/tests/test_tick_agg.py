"""Tick 聚合器测试。"""


from src.market.compute.tick_agg import TickAggregator
from src.market.data.cache import CacheManager


class TestTickAggregator:
    def test_basic_aggregation(self) -> None:
        import time
        cache = CacheManager()
        agg = TickAggregator(cache)

        # 喂入同一分钟的 tick
        ticks = {
            "000001.SZ": {
                "time": 1737000000000,
                "lastPrice": 10.0,
                "pvolume": 1000000,
                "amount": 10000000,
            }
        }
        agg.on_tick(ticks)

        # 第二 tick
        ticks2 = {
            "000001.SZ": {
                "time": 1737000030000,
                "lastPrice": 10.5,
                "pvolume": 1100000,
                "amount": 11000000,
            }
        }
        agg.on_tick(ticks2)

        # 等待 worker 消费队列
        time.sleep(0.6)

        # 切换到下一分钟触发 flush
        ticks3 = {
            "000001.SZ": {
                "time": 1737000090000,
                "lastPrice": 11.0,
                "pvolume": 1200000,
                "amount": 12000000,
            }
        }
        agg.on_tick(ticks3)

        # 等待 worker 处理
        time.sleep(0.6)

        bars = agg.get_bars("000001.SZ")
        assert bars is not None
        assert len(bars) >= 1

    def test_filter_invalid_prices(self) -> None:
        """零或负价格应被过滤。"""
        cache = CacheManager()
        agg = TickAggregator(cache)

        # 所有 tick 价格无效 → 返回 None
        bar = agg._aggregate_ticks([
            {"lastPrice": 0, "pvolume": 1000, "amount": 10000},
            {"lastPrice": -1, "pvolume": 1100, "amount": 11000},
        ])
        assert bar is None

        # 只有有效价格的 tick 才参与聚合
        bar = agg._aggregate_ticks([
            {"lastPrice": 10.0, "pvolume": 1000, "amount": 10000},
        ])
        assert bar is not None
        assert bar["open"] == 10.0
        assert bar["close"] == 10.0

    def test_empty_ticks(self) -> None:
        cache = CacheManager()
        agg = TickAggregator(cache)
        bar = agg._aggregate_ticks([])
        assert bar is None

    def test_minute_from_tick_int(self) -> None:
        """测试整数时间戳（毫秒）。"""
        import datetime
        # 使用当前时间的 10:30 来避免时区问题
        now = datetime.datetime.now()
        target = now.replace(hour=10, minute=30, second=0, microsecond=0)
        ts_ms = int(target.timestamp() * 1000)
        minute_key, time_str = TickAggregator._minute_from_tick(ts_ms)
        assert minute_key is not None
        assert minute_key == 10 * 60 + 30
        assert "10:30" in time_str

    def test_minute_from_tick_float(self) -> None:
        import datetime
        now = datetime.datetime.now()
        target = now.replace(hour=10, minute=30, second=0, microsecond=0)
        ts_sec = target.timestamp()
        minute_key, time_str = TickAggregator._minute_from_tick(ts_sec)
        assert minute_key is not None

    def test_minute_from_tick_invalid(self) -> None:
        minute_key, time_str = TickAggregator._minute_from_tick("invalid")
        assert minute_key is None
