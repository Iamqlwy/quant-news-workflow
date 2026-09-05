from __future__ import annotations

import sys
from unittest.mock import AsyncMock, Mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.triggers.evaluators.sentiment import (
    eval_leader_divergence,
    eval_sector_breadth,
    eval_sector_up_down_ratio,
    eval_sector_volume_ratio,
)


@pytest.mark.asyncio
async def test_sector_breadth_uses_up_down_counts() -> None:
    market = Mock()
    market.get_sector_overview = AsyncMock(return_value={"up_count": 6, "down_count": 4})

    result = await eval_sector_breadth({"sector": "智能电网", "up_ratio_min": 0.5}, market)

    assert result["triggered"] is True
    assert result["up_ratio"] == 0.6


@pytest.mark.asyncio
async def test_sector_volume_ratio_returns_amount_fields() -> None:
    market = Mock()
    market.get_sector_overview = AsyncMock(return_value={"concept_code": "885311.TI"})
    market.get_sector_volume_ratio = Mock(return_value={"ratio": 1.8, "today_amount": 1200, "avg_amount": 700})

    result = await eval_sector_volume_ratio({"sector": "智能电网", "multiplier": 1.5}, market)

    assert result["triggered"] is True
    assert result["today_amount"] == 1200
    assert result["avg_amount"] == 700


@pytest.mark.asyncio
async def test_sector_up_down_ratio_uses_intraday_shape_evaluator() -> None:
    market = Mock()
    market.get_sector_overview = AsyncMock(return_value={"concept_code": "885311.TI"})
    market.get_concept_members = Mock(return_value=["A", "B"])
    market.get_intraday_snapshot = AsyncMock(
        side_effect=[
            {
                "open": 10.0,
                "latest_pct": 1.0,
                "bars": [
                    {"high": 10.0, "close": 10.0},
                    {"high": 10.5, "close": 10.4},
                    {"high": 10.3, "close": 10.1},
                ],
            },
            {
                "open": 10.0,
                "latest_pct": 2.0,
                "bars": [
                    {"high": 10.0, "close": 10.0},
                    {"high": 10.2, "close": 10.2},
                    {"high": 10.3, "close": 10.2},
                ],
            },
        ]
    )

    result = await eval_sector_up_down_ratio(
        {"sector": "智能电网", "pattern": "shot_up_fall", "ratio_min": 0.5},
        market,
    )

    assert result["triggered"] is True
    assert result["match_count"] == 1
    assert result["total_evaluated"] == 2
    assert result["ratio"] == 0.5


@pytest.mark.asyncio
async def test_leader_divergence_uses_followers_average() -> None:
    market = Mock()
    market.get_sector_overview = AsyncMock(return_value={"concept_code": "885311.TI"})
    market.get_concept_members = Mock(return_value=["LEADER", "A", "B"])
    market.get_realtime_price = AsyncMock(
        side_effect=[
            {"pct_chg": 8.0},
            {"pct_chg": 2.0},
            {"pct_chg": 3.0},
        ]
    )

    result = await eval_leader_divergence(
        {"leader_ticker": "LEADER", "sector": "智能电网", "pct": 3.0},
        market,
    )

    assert result["triggered"] is True
    assert result["sector_avg_pct"] == 2.5
    assert result["deviation"] == 5.5
