from __future__ import annotations

import pandas as pd

from src.market.data import MarketDataProvider


def test_build_intraday_snapshot_returns_1m_bars() -> None:
    provider = object.__new__(MarketDataProvider)
    df = pd.DataFrame(
        [
            {"timestamp": "2026-05-25 09:30:00", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
            {"timestamp": "2026-05-25 09:31:00", "open": 10.0, "high": 10.5, "low": 10.0, "close": 10.4},
        ]
    )

    snap = provider._build_intraday_snapshot_from_df("000001.SZ", df, source="csv")

    assert "bars" in snap
    assert len(snap["bars"]) == 2
    assert snap["bars"][0]["timestamp"] == "2026-05-25T09:30:00"
    assert snap["bars"][1]["pct_from_open"] == 4.0


def test_build_intraday_snapshot_accepts_time_column() -> None:
    provider = object.__new__(MarketDataProvider)
    df = pd.DataFrame(
        [
            {"time": "2026-05-25 09:30:00", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1},
            {"time": "2026-05-25 09:31:00", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2},
        ]
    )

    snap = provider._build_intraday_snapshot_from_df("000001.SZ", df, source="xtquant_aggregated")

    assert snap["count"] == 2
    assert snap["source"] == "xtquant_aggregated"
    assert snap["bars"][0]["timestamp"] == "2026-05-25T09:30:00"
