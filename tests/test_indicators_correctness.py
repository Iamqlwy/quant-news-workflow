from __future__ import annotations

import math

import numpy as np

from src.market.indicators import (
    calc_bollinger_position,
    calc_bollinger_series,
    calc_kdj_series,
    calc_macd_series,
    calc_rsi_series,
    calc_volume_ratio,
)


def test_rsi_hits_100_on_monotonic_rise() -> None:
    closes = np.arange(1, 31, dtype=float)
    rsi = calc_rsi_series(closes, window=14)
    assert not np.isnan(rsi[-1])
    assert math.isclose(float(rsi[-1]), 100.0, rel_tol=0.0, abs_tol=1e-6)


def test_macd_returns_valid_latest_values_after_warmup() -> None:
    closes = np.linspace(10.0, 30.0, 60)
    dif, dea, hist = calc_macd_series(closes, fast=12, slow=26, signal=9, hist_scale=2.0)
    assert not np.isnan(dif[-1])
    assert not np.isnan(dea[-1])
    assert not np.isnan(hist[-1])
    assert math.isclose(float(hist[-1]), 2.0 * (float(dif[-1]) - float(dea[-1])), rel_tol=0.0, abs_tol=1e-9)


def test_bollinger_supports_touch_positions() -> None:
    closes = np.array([10.0] * 19 + [12.0], dtype=float)
    upper, _, lower = calc_bollinger_series(closes, window=20, num_std=2.0, ddof=1)
    assert calc_bollinger_position(float(upper[-1]), float(upper[-1]), float(lower[-1])) == "upper_touch"
    assert calc_bollinger_position(float(lower[-1]), float(upper[-1]), float(lower[-1])) == "lower_touch"


def test_kdj_produces_previous_values_for_cross_detection() -> None:
    highs = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], dtype=float)
    lows = np.array([8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13], dtype=float)
    closes = np.array([9, 10, 10.5, 11, 12, 13, 14, 15, 14.5, 16, 17], dtype=float)
    k, d, j = calc_kdj_series(highs, lows, closes, n=9)
    assert not np.isnan(k[-1])
    assert not np.isnan(d[-1])
    assert not np.isnan(j[-1])
    assert not np.isnan(k[-2])
    assert not np.isnan(d[-2])


def test_volume_ratio_requires_n_plus_one_bars() -> None:
    ratio, latest_volume, avg_volume = calc_volume_ratio(np.array([100.0, 120.0, 140.0]), window=3)
    assert ratio is None
    assert latest_volume is None
    assert avg_volume is None

    ratio, latest_volume, avg_volume = calc_volume_ratio(np.array([100.0, 120.0, 140.0, 210.0]), window=3)
    assert ratio == 1.75
    assert latest_volume == 210.0
    assert avg_volume == 120.0
