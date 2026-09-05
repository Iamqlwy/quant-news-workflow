"""市场快照图表测试 —— 验证 get_market_snapshot 返回图片 + 文本摘要"""
from __future__ import annotations
from loguru import logger

import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import sys

sys.path.append(str(Path(__file__).parent.parent))

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.market.charts import generate_market_snapshot_chart, generate_sector_snapshot_chart


OUT_DIR = "tests/output"
DATE = "2026-05-25"


def _pick_available_snapshot_date(requested_date: str) -> str:
    date_compact = requested_date.replace("-", "")
    root = Path(settings.klines_path) / "extra" / "all_stocks_daily"
    if (root / f"{date_compact}.csv").exists():
        return requested_date

    if not root.exists():
        raise RuntimeError(f"无快照目录: {root}")

    files = sorted(root.glob("*.csv"))
    if not files:
        raise RuntimeError(f"无快照文件: {root}")

    eligible = [p for p in files if p.stem <= date_compact]
    chosen = (eligible[-1] if eligible else files[-1]).stem
    return f"{chosen[:4]}-{chosen[4:6]}-{chosen[6:]}"


def _assert_png_bytes(png: bytes) -> None:
    assert isinstance(png, (bytes, bytearray))
    assert len(png) > 10_000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def setup(snapshot_date: str, now_h: int = 15, now_m: int = 0):
    settings.simulation_enabled = True
    y, m, d = (int(x) for x in snapshot_date.split("-"))
    clock = Clock(TimeConfig(
        start_time=datetime(y, m, d, now_h, now_m, 0),
        tick_duration=timedelta(minutes=1),
        realtime=False,
    ))
    market = MarketDataProvider(clock=clock)
    return clock, market


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    snapshot_date = "2026-05-25"
    clock, market = setup(snapshot_date, now_h=15, now_m=0)
    logger.info(f"clock.today = {clock.today.isoformat()}")
    logger.info(f"clock.now   = {clock.now.isoformat()}\n")

    if snapshot_date != DATE:
        logger.info(f"requested date {DATE} not found, fallback to {snapshot_date}\n")

    snap = market.get_market_snapshot(snapshot_date)
    assert isinstance(snap, dict) and "error" not in snap
    assert snap.get("date") == snapshot_date
    assert isinstance(snap.get("up_count"), int)
    assert isinstance(snap.get("down_count"), int)

    market.refresh()
    sector_code = "885311.TI"
    date_compact = snapshot_date.replace("-", "")
    with market._cache_lock:
        sector_bars = market._cache.get("_sector_bars", {})
    df_sector = sector_bars.get(sector_code)
    logger.info(f"\nsector_bars keys = {len(sector_bars)}")
    if df_sector is None or df_sector.empty:
        members = market.get_concept_members(sector_code)
        logger.info(f"sector_bars[{sector_code}] = None/empty, members={len(members)}")
    else:
        logger.info(f"sector_bars[{sector_code}] rows={len(df_sector):,}, cols={list(df_sector.columns)}")
        logger.info(f"timestamp dtype={df_sector['timestamp'].dtype}")
        logger.info(f"timestamp head={df_sector['timestamp'].head(3).tolist()}")
        logger.info(f"timestamp tail={df_sector['timestamp'].tail(3).tolist()}")
        ts = df_sector["timestamp"]
        if pd.api.types.is_datetime64_any_dtype(ts):
            ts2 = ts
        else:
            numeric = pd.to_numeric(ts, errors="coerce")
            if numeric.notna().any():
                maxv = float(numeric.max())
                if maxv > 1e14:
                    ts2 = pd.to_datetime(numeric, unit="ns", errors="coerce")
                elif maxv > 1e11:
                    ts2 = pd.to_datetime(numeric, unit="ms", errors="coerce")
                elif maxv > 1e9:
                    ts2 = pd.to_datetime(numeric, unit="s", errors="coerce")
                else:
                    ts2 = pd.to_datetime(ts, errors="coerce")
            else:
                ts2 = pd.to_datetime(ts, errors="coerce")
        logger.info(f"parsed timestamp non_na={int(ts2.notna().sum()):,}/{len(ts2):,}")
        if ts2.notna().any():
            logger.info(f"parsed timestamp min={ts2.min()} max={ts2.max()}")
            mask_day = ts2.dt.strftime('%Y%m%d') == date_compact
            logger.info(f"rows on {date_compact} = {int(mask_day.sum()):,}")
            if mask_day.any():
                sample = df_sector.loc[mask_day, ["timestamp", "close"]].head(5)
                logger.info("sample day head:")
                logger.info(sample.to_string(index=False))

    png = generate_market_snapshot_chart(market, date=snapshot_date)
    _assert_png_bytes(png)

    out_path = os.path.join(OUT_DIR, f"sector_snapshot_{snapshot_date.replace('-', '')}.png")
    with open(out_path, "wb") as f:
        f.write(png)
    logger.info(f"chart -> {out_path} ({len(png):,} bytes)")
    logger.info("snapshot + chart -> ok")


if __name__ == "__main__":
    main()
