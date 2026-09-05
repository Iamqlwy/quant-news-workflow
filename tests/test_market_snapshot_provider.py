"""市场快照 API 测试 —— 覆盖 clock.today=2026-05-27 且 now=15:00 的分支"""

from __future__ import annotations
from loguru import logger
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider


def _make_market(now: datetime) -> tuple[Clock, MarketDataProvider]:
    settings.simulation_enabled = True
    clock = Clock(TimeConfig(
        start_time=now,
        tick_duration=timedelta(minutes=1),
        realtime=False,
    ))
    market = MarketDataProvider(clock=clock)
    return clock, market


def main() -> None:
    clock, market = _make_market(datetime(2026, 5, 27, 15, 1, 0))
    assert clock.today.strftime("%Y-%m-%d") == "2026-05-27"
    assert clock.now.strftime("%Y-%m-%d %H:%M") == "2026-05-27 15:01"
    start = time.time()
    snap = market.get_market_snapshot("2026-05-27")
    end = time.time()
    logger.info(f"get_market_snapshot time={end-start}")
    assert isinstance(snap, dict) and "error" not in snap, snap
    assert snap.get("date") == "2026-05-27", snap
    assert snap.get("source") in {"intraday_1m", "csv", "xtquant"}, snap
    assert isinstance(snap.get("top_industries"), list), snap
    assert isinstance(snap.get("top_sectors"), list), snap
    if snap.get("source") == "csv":
        assert len(snap["top_industries"]) == 6, snap
        assert len(snap["top_sectors"]) == 6, snap

    assert isinstance(snap.get("total_stocks"), int) and snap["total_stocks"] > 0, snap
    assert isinstance(snap.get("up_count"), int), snap
    assert isinstance(snap.get("down_count"), int), snap
    assert isinstance(snap.get("avg_pct_chg"), (int, float)), snap

    if snap.get("source") == "intraday_1m":
        assert snap.get("total_amount") is None or float(snap["total_amount"]) >= 0

    logger.info("ok:", {k: snap.get(k) for k in ["date", "source", "total_stocks", "up_count", "down_count", "avg_pct_chg","top_sectors","top_industries"]})


if __name__ == "__main__":
    main()
