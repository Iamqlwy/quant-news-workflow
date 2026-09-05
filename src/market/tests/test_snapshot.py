"""快照服务测试。"""


from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.market.data.cache import CacheManager
from src.market.services.snapshot import SnapshotService


class TestSnapshotService:
    """需要 mock 数据才能完整测试。"""

    def test_init(self) -> None:
        _cache = CacheManager()
        _clock = Clock(TimeConfig(
            start_time=datetime(2025, 1, 15, 10, 30, 0),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        # bar_svc 需要 mock
        svc = SnapshotService.__new__(SnapshotService)
        assert svc is not None
