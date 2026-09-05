"""纯技术指标计算函数 —— 无副作用，可单独测试。

所有函数接受 numpy 数组或列表，返回 numpy 数组或标量。
不依赖 DataFrame、缓存或任何外部状态。
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def calc_ma(data: np.ndarray, window: int) -> np.ndarray:
    """简单移动平均。前 window-1 个位置为 NaN。"""
    if len(data) == 0:
        return np.array([])
    # 验证输入数据不包含 NaN/inf
    if not np.all(np.isfinite(data)):
        raise ValueError("calc_ma: 输入数据包含 NaN 或 inf")
    if len(data) < window:
        return np.full(len(data), np.nan)
    kernel = np.ones(window) / window
    result = np.convolve(data, kernel, mode="full")[: len(data)]
    result[: window - 1] = np.nan
    return result


def calc_ema(data: np.ndarray, window: int) -> np.ndarray:
    """指数移动平均。

    时间复杂度：O(N)。
    """
    if len(data) == 0:
        return np.array([])
    # 验证输入数据不包含 NaN/inf
    if not np.all(np.isfinite(data)):
        raise ValueError("calc_ema: 输入数据包含 NaN 或 inf")

    alpha = 2.0 / (window + 1)

    result = np.empty(len(data), dtype=np.float64)
    result[0] = data[0]

    # 手动优化的循环（已经足够快，编译器会优化）
    one_minus_alpha = 1.0 - alpha
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + one_minus_alpha * result[i - 1]

    return result


def calc_rsi(closes: np.ndarray, window: int = 14) -> np.ndarray:
    """RSI（Wilder's smoothing）。

    时间复杂度：O(N)。
    """
    n = len(closes)
    if n < 2:
        return np.full(n, np.nan)

    # 验证输入数据不包含 NaN/inf
    if not np.all(np.isfinite(closes)):
        raise ValueError("calc_rsi: 输入数据包含 NaN 或 inf")

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    result = np.full(n, np.nan)
    # 第一个窗口：简单平均
    if n >= window + 1:
        avg_gain = gains[:window].mean()
        avg_loss = losses[:window].mean()
        result[window] = _rsi_from_avgs(avg_gain, avg_loss)

        # 使用 Wilder's smoothing: avg[i] = (avg[i-1] * (window-1) + new_value) / window
        for i in range(window + 1, n):
            avg_gain = (avg_gain * (window - 1) + gains[i - 1]) / window
            avg_loss = (avg_loss * (window - 1) + losses[i - 1]) / window
            result[i] = _rsi_from_avgs(avg_gain, avg_loss)

    return result


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    """从平均盈亏计算 RSI。"""
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    hist_scale: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD 指标。

    返回 (DIF, DEA, HIST)。HIST = hist_scale * (DIF - DEA)。
    时间复杂度：O(N)。
    """
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    hist = hist_scale * (dif - dea)
    return dif, dea, hist


def calc_bollinger(
    closes: np.ndarray,
    window: int = 20,
    num_std: float = 2.0,
    ddof: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """布林带指标。

    返回 (upper, mid, lower)。
    使用 sliding_window_view 实现完全向量化计算，O(N)。
    """
    n = len(closes)
    mid = calc_ma(closes, window)

    if n < window:
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)
        return upper, mid, lower

    windows = sliding_window_view(closes, window)
    stds = np.std(windows, axis=1, ddof=ddof)
    upper_vals = mid[window - 1 :] + num_std * stds
    lower_vals = mid[window - 1 :] - num_std * stds

    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    upper[window - 1 :] = upper_vals
    lower[window - 1 :] = lower_vals
    return upper, mid, lower


def calc_bollinger_position(close: float, upper: float, lower: float) -> str:
    """判断价格在布林带中的位置。

    返回：'above' / 'inside_upper' / 'inside_lower' / 'below'
    """

    eps = abs(close) * 1e-4
    if close >= upper - eps:
        return "above"
    if close <= lower + eps:
        return "below"
    mid = (upper + lower) / 2.0
    return "inside_upper" if close >= mid else "inside_lower"


def calc_kdj(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    n: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """KDJ 指标 - 向量化实现。

    返回 (K, D, J)。时间复杂度：O(N)。
    使用 sliding_window_view 实现滚动窗口最大最小值的向量化计算。
    """
    length = len(closes)
    k = np.full(length, np.nan, dtype=np.float64)
    d = np.full(length, np.nan, dtype=np.float64)
    j = np.full(length, np.nan, dtype=np.float64)

    if length < n:
        return k, d, j

    # 验证输入数据不包含 NaN/inf
    if not (np.all(np.isfinite(highs)) and np.all(np.isfinite(lows)) and np.all(np.isfinite(closes))):
        raise ValueError("calc_kdj: 输入数据包含 NaN 或 inf")

    # 使用 sliding_window_view 向量化计算滚动窗口的 max/min
    high_windows = sliding_window_view(highs, n)
    low_windows = sliding_window_view(lows, n)
    close_windows = sliding_window_view(closes, n)

    # 向量化计算 RSV
    high_n = high_windows.max(axis=1)
    low_n = low_windows.min(axis=1)
    close_n = close_windows[:, -1]  # 取每个窗口的最后一个值

    # RSV 计算，避免除零
    denominator = high_n - low_n
    rsv = np.where(denominator != 0, (close_n - low_n) / denominator * 100.0, 50.0)

    # K, D 递推计算（这部分仍需循环，因为是递推关系）
    # K[i] = 2/3 * K[i-1] + 1/3 * RSV[i]
    # D[i] = 2/3 * D[i-1] + 1/3 * K[i]

    k_prev = 50.0
    d_prev = 50.0

    k_values = np.empty(len(rsv), dtype=np.float64)
    d_values = np.empty(len(rsv), dtype=np.float64)
    j_values = np.empty(len(rsv), dtype=np.float64)

    for i in range(len(rsv)):
        k_prev = 2.0 / 3.0 * k_prev + 1.0 / 3.0 * rsv[i]
        d_prev = 2.0 / 3.0 * d_prev + 1.0 / 3.0 * k_prev
        j_val = 3.0 * k_prev - 2.0 * d_prev

        k_values[i] = k_prev
        d_values[i] = d_prev
        j_values[i] = j_val

    # 填充结果
    k[n - 1:] = k_values
    d[n - 1:] = d_values
    j[n - 1:] = j_values

    return k, d, j


def calc_volume_ratio(
    volumes: np.ndarray,
    window: int = 5,
) -> tuple[float | None, float | None]:
    """量比计算。

    返回 (5日均量比, 10日均量比)。
    量比 = 当日成交量 / 过去N日平均成交量。
    """
    if len(volumes) < 2:
        return None, None

    today_vol = volumes[-1]
    avg_5 = volumes[-6:-1].mean() if len(volumes) >= 6 else volumes[:-1].mean()
    avg_10 = volumes[-11:-1].mean() if len(volumes) >= 11 else volumes[:-1].mean()

    vr = today_vol / avg_5 if avg_5 > 0 else None
    vr10 = today_vol / avg_10 if avg_10 > 0 else None
    return vr, vr10
