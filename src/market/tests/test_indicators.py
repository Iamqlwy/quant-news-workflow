"""技术指标计算函数测试。"""

import numpy as np
import pytest

from src.market.compute.indicators import (
    calc_bollinger,
    calc_bollinger_position,
    calc_ema,
    calc_kdj,
    calc_ma,
    calc_macd,
    calc_rsi,
    calc_volume_ratio,
)


class TestMA:
    def test_ma5_basic(self) -> None:
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        result = calc_ma(data, 5)
        assert np.isnan(result[0])
        assert np.isnan(result[3])
        assert result[4] == pytest.approx(3.0)  # (1+2+3+4+5)/5
        assert result[6] == pytest.approx(5.0)  # (3+4+5+6+7)/5

    def test_ma_too_short(self) -> None:
        data = np.array([1.0, 2.0, 3.0])
        result = calc_ma(data, 5)
        assert np.all(np.isnan(result))

    def test_ma_empty(self) -> None:
        result = calc_ma(np.array([]), 5)
        assert len(result) == 0


class TestEMA:
    def test_ema_basic(self) -> None:
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = calc_ema(data, 5)
        # EMA(5) = price * (2/6) + prev_ema * (4/6)
        assert result[0] == 1.0  # first = data[0]
        expected1 = 2.0 * (2/6) + 1.0 * (4/6)
        assert result[1] == pytest.approx(expected1, abs=0.01)

    def test_ema_empty(self) -> None:
        result = calc_ema(np.array([]), 5)
        assert len(result) == 0


class TestRSI:
    def test_rsi_all_gains(self) -> None:
        closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0])
        result = calc_rsi(closes, 14)
        # 全部上涨 → RSI close to 100
        assert result[-1] > 90

    def test_rsi_too_short(self) -> None:
        closes = np.array([10.0, 11.0, 12.0])
        result = calc_rsi(closes, 14)
        assert np.all(np.isnan(result))


class TestMACD:
    def test_macd_basic(self) -> None:
        closes = np.array([10.0 + i * 0.1 for i in range(50)])
        dif, dea, hist = calc_macd(closes)
        assert len(dif) == len(closes)
        assert len(dea) == len(closes)
        assert len(hist) == len(closes)
        assert not np.isnan(dif[-1])
        assert not np.isnan(dea[-1])
        assert not np.isnan(hist[-1])

    def test_macd_hist_scale(self) -> None:
        closes = np.array([10.0 + i * 0.1 for i in range(50)])
        _, _, hist1 = calc_macd(closes, hist_scale=2.0)
        _, _, hist2 = calc_macd(closes, hist_scale=1.0)
        assert hist1[-1] == pytest.approx(hist2[-1] * 2.0, abs=0.01)


class TestBollinger:
    def test_bollinger_basic(self) -> None:
        closes = np.array([10.0 + np.sin(i * 0.5) * 2 for i in range(50)])
        upper, mid, lower = calc_bollinger(closes, window=20)
        assert np.isnan(upper[18])
        assert not np.isnan(upper[19])
        assert upper[-1] >= mid[-1] >= lower[-1]

    def test_bollinger_position(self) -> None:
        assert calc_bollinger_position(11.0, 10.0, 9.0) == "above"
        assert calc_bollinger_position(9.5, 10.0, 9.0) == "inside_upper"
        assert calc_bollinger_position(8.0, 10.0, 9.0) == "below"


class TestKDJ:
    def test_kdj_basic(self) -> None:
        n = 30
        highs = np.array([10.0 + i * 0.5 for i in range(n)])
        lows = np.array([8.0 + i * 0.5 for i in range(n)])
        closes = np.array([9.0 + i * 0.5 for i in range(n)])
        k, d, j = calc_kdj(highs, lows, closes, n=9)
        assert len(k) == n
        assert not np.isnan(k[-1])
        assert not np.isnan(d[-1])
        assert not np.isnan(j[-1])

    def test_kdj_too_short(self) -> None:
        highs = np.array([10.0, 11.0, 12.0])
        lows = np.array([9.0, 10.0, 11.0])
        closes = np.array([9.5, 10.5, 11.5])
        k, d, j = calc_kdj(highs, lows, closes, n=9)
        assert np.all(np.isnan(k))


class TestVolumeRatio:
    def test_volume_ratio(self) -> None:
        volumes = np.array([100.0] * 20 + [200.0])
        vr, vr10 = calc_volume_ratio(volumes, window=5)
        assert vr == pytest.approx(2.0)  # 200 / 100
        assert vr10 == pytest.approx(2.0)

    def test_volume_ratio_too_short(self) -> None:
        volumes = np.array([100.0])
        vr, vr10 = calc_volume_ratio(volumes)
        assert vr is None
        assert vr10 is None
