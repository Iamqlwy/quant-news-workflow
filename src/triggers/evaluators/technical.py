"""趋势结构类原子评估器 (v4) —— 所有指标从 history 日线收盘价即时计算"""

from __future__ import annotations

import numpy as np

from src.market.compute.indicators import calc_ma, calc_macd
from src.triggers.eval_context import EvalContext

_MA_MAP = {"MA5": 5, "MA10": 10, "MA20": 20, "MA60": 60}


def _get_history(ticker: str, ctx: EvalContext) -> dict:
    tdata = ctx.ticker_data.get(ticker, {})
    return tdata.get("history") or {}


def _compute_ma(closes: np.ndarray, window: int) -> float | None:
    """从收盘价序列计算最新 MA 值。"""
    ma = calc_ma(closes, window)
    val = float(ma[-1])
    return round(val, 2) if not np.isnan(val) else None


def _compute_prev_ma(closes: np.ndarray, window: int) -> float | None:
    """计算前一 bar 的 MA 值：SMA(closes[-(window+1):-1])。"""
    if len(closes) < window + 1:
        return None
    return round(float(closes[-(window + 1) : -1].mean()), 4)


# ═══════════════════════════════════════════════
# MA 评估器
# ═══════════════════════════════════════════════


def eval_ma_slope(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    period = params["period"]
    direction = params.get("direction", "up")

    history = _get_history(ticker, ctx)
    data = history.get("data", [])
    if len(data) < 2:
        return {"triggered": False, "reason": "历史数据不足"}

    closes = np.array([float(d["close"]) for d in data], dtype=float)
    cur_ma = _compute_ma(closes, _MA_MAP[period])
    if cur_ma is None:
        return {"triggered": False, "reason": f"{period} 数据不可用"}

    prev_ma = _compute_prev_ma(closes, _MA_MAP[period])
    if prev_ma is None:
        # 回退：用 close 方向做代理
        cur_close = closes[-1]
        prev_close = closes[-2]

        if direction == "up":
            triggered = bool(cur_close > prev_close)
        elif direction == "down":
            triggered = bool(cur_close < prev_close)
        elif direction == "flat":
            pct_change = abs(cur_close - prev_close) / prev_close * 100 if prev_close else 0
            triggered = pct_change < 0.1
        else:
            return {"triggered": False, "reason": f"未知的 direction: {direction}"}

        return {"triggered": triggered, "period": period, "direction": direction,
                "ma_val": cur_ma, "note": "history 不足，使用 close 近似"}

    if direction == "up":
        triggered = bool(cur_ma > prev_ma)
    elif direction == "down":
        triggered = bool(cur_ma < prev_ma)
    elif direction == "flat":
        pct_change = abs(cur_ma - prev_ma) / prev_ma * 100 if prev_ma else 0
        triggered = pct_change < 0.1
    else:
        return {"triggered": False, "reason": f"未知的 direction: {direction}"}

    return {"triggered": triggered, "period": period, "direction": direction,
            "cur_ma": round(cur_ma, 2), "prev_ma": round(prev_ma, 2)}


def eval_ma_cross(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    fast_period = params["fast_period"]
    slow_period = params["slow_period"]
    direction = params.get("direction", "golden")

    history = _get_history(ticker, ctx)
    data = history.get("data", [])
    if len(data) < 3:
        return {"triggered": False, "reason": "历史数据不足以判断交叉"}

    closes = np.array([float(d["close"]) for d in data], dtype=float)

    fast_window = _MA_MAP.get(fast_period, 0)
    slow_window = _MA_MAP.get(slow_period, 0)
    if fast_window <= 0 or slow_window <= 0:
        return {"triggered": False, "reason": "均线周期无效"}

    cur_fast = _compute_ma(closes, fast_window)
    cur_slow = _compute_ma(closes, slow_window)
    if cur_fast is None or cur_slow is None:
        return {"triggered": False, "reason": "均线数据不足"}

    prev_fast = _compute_prev_ma(closes, fast_window)
    prev_slow = _compute_prev_ma(closes, slow_window)

    if prev_fast is None or prev_slow is None:
        # 回退：用 close 方向做代理
        prev_close = closes[-2]
        prev2_close = closes[-3]

        is_golden = cur_fast > cur_slow and prev_close >= prev2_close
        is_death = cur_fast < cur_slow and prev_close <= prev2_close

        triggered = (direction == "golden" and is_golden) or (direction == "death" and is_death)
        return {"triggered": triggered, "fast": fast_period, "slow": slow_period, "direction": direction,
                "note": "history 不足，使用 close 近似"}

    if direction == "golden":
        triggered = bool(cur_fast > cur_slow and prev_fast <= prev_slow)
    elif direction == "death":
        triggered = bool(cur_fast < cur_slow and prev_fast >= prev_slow)
    else:
        return {"triggered": False, "reason": f"未知的 direction: {direction}"}

    return {"triggered": triggered, "fast": fast_period, "slow": slow_period, "direction": direction}


def eval_ma_alignment(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pattern = params.get("pattern", "bullish")

    history = _get_history(ticker, ctx)
    data = history.get("data", [])
    if len(data) < 60:
        return {"triggered": False, "reason": "均线数据不足（需要MA5/MA10/MA20/MA60）"}

    closes = np.array([float(d["close"]) for d in data], dtype=float)
    periods = ["MA5", "MA10", "MA20", "MA60"]
    values = [_compute_ma(closes, _MA_MAP[p]) for p in periods]
    if any(v is None for v in values):
        return {"triggered": False, "reason": "均线数据不足（需要MA5/MA10/MA20/MA60）"}

    if pattern == "bullish":
        triggered = all(values[i] > values[i + 1] for i in range(len(values) - 1))  # type: ignore[operator]
    elif pattern == "bearish":
        triggered = all(values[i] < values[i + 1] for i in range(len(values) - 1))  # type: ignore[operator]
    else:
        return {"triggered": False, "reason": f"未知的 pattern: {pattern}"}

    return {
        "triggered": triggered,
        "pattern": pattern,
        **{p: round(v, 2) for p, v in zip(periods, values, strict=False) if v is not None},
    }


# ═══════════════════════════════════════════════
# MACD 评估器
# ═══════════════════════════════════════════════


def eval_macd_cross(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    direction = params.get("direction", "golden")

    history = _get_history(ticker, ctx)
    data = history.get("data", [])

    if len(data) < 35:
        return {"triggered": False, "reason": "历史数据不足（MACD需要至少35根K线）"}

    closes = np.array([float(d["close"]) for d in data], dtype=float)
    if not np.all(np.isfinite(closes)):
        return {"triggered": False, "reason": "历史数据包含无效值（NaN 或 inf）"}

    dif_series, dea_series, hist_series = calc_macd(closes)

    if len(hist_series) < 2:
        return {"triggered": False, "reason": "MACD序列长度不足"}

    if np.isnan(hist_series[-2]):
        return {"triggered": False, "reason": "前一日MACD数据不足（NaN）"}

    prev_hist = float(hist_series[-2])
    cur_hist = float(hist_series[-1])

    if direction == "golden":
        triggered = prev_hist <= 0 and cur_hist > 0
    elif direction == "death":
        triggered = prev_hist >= 0 and cur_hist < 0
    else:
        return {"triggered": False, "reason": f"未知的 direction: {direction}"}

    return {
        "triggered": triggered,
        "direction": direction,
        "cur_dif": round(float(dif_series[-1]), 4),
        "cur_dea": round(float(dea_series[-1]), 4),
        "cur_hist": round(cur_hist, 4),
        "prev_hist": round(prev_hist, 4),
    }


def eval_macd_divergence(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pattern = params.get("pattern", "bullish")
    lookback_days = params.get("lookback_days", 5)

    history = _get_history(ticker, ctx)
    data = history.get("data", [])

    if len(data) < lookback_days + 35:
        return {"triggered": False, "reason": f"历史数据不足（需要至少{lookback_days + 35}根K线）"}

    closes = np.array([float(d["close"]) for d in data], dtype=float)
    highs = np.array([float(d["high"]) for d in data], dtype=float)
    lows = np.array([float(d["low"]) for d in data], dtype=float)

    if not (np.all(np.isfinite(closes)) and np.all(np.isfinite(highs)) and np.all(np.isfinite(lows))):
        return {"triggered": False, "reason": "历史数据包含无效值（NaN 或 inf）"}

    dif_series, dea_series, hist_series = calc_macd(closes)

    _ = closes[-(lookback_days + 1):]
    recent_difs = dif_series[-(lookback_days + 1):]
    recent_highs = highs[-(lookback_days + 1):]
    recent_lows = lows[-(lookback_days + 1):]

    if pattern == "bearish":
        price_max_idx = int(np.argmax(recent_highs))
        dif_max_idx = int(np.argmax(recent_difs))

        if price_max_idx == len(recent_highs) - 1 and dif_max_idx < len(recent_difs) - 1:
            price_high = float(recent_highs[price_max_idx])
            prev_high = float(np.max(recent_highs[:-1]))
            dif_high = float(recent_difs[-1])
            prev_dif_high = float(np.max(recent_difs[:-1]))

            triggered = price_high > prev_high and dif_high < prev_dif_high

            return {
                "triggered": triggered,
                "pattern": pattern,
                "price_high": round(price_high, 2),
                "prev_high": round(prev_high, 2),
                "dif_cur": round(dif_high, 4),
                "dif_prev_max": round(prev_dif_high, 4),
            }
        else:
            return {"triggered": False, "reason": "未检测到顶背离形态"}

    elif pattern == "bullish":
        price_min_idx = int(np.argmin(recent_lows))
        dif_min_idx = int(np.argmin(recent_difs))

        if price_min_idx == len(recent_lows) - 1 and dif_min_idx < len(recent_difs) - 1:
            price_low = float(recent_lows[price_min_idx])
            prev_low = float(np.min(recent_lows[:-1]))
            dif_low = float(recent_difs[-1])
            prev_dif_low = float(np.min(recent_difs[:-1]))

            triggered = price_low < prev_low and dif_low > prev_dif_low

            return {
                "triggered": triggered,
                "pattern": pattern,
                "price_low": round(price_low, 2),
                "prev_low": round(prev_low, 2),
                "dif_cur": round(dif_low, 4),
                "dif_prev_min": round(prev_dif_low, 4),
            }
        else:
            return {"triggered": False, "reason": "未检测到底背离形态"}
    else:
        return {"triggered": False, "reason": f"未知的 pattern: {pattern}"}
