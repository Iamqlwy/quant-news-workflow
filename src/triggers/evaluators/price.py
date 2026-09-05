"""价格状态 & 量价关系类原子评估器 (v4) —— 所有技术指标从 history 日线即时计算"""

from __future__ import annotations

import numpy as np

from src.market.compute.indicators import calc_ma
from src.triggers.eval_context import EvalContext


def _get_ticker_data(ticker: str, ctx: EvalContext) -> dict:
    """安全获取 ticker 数据，缺失时返回空 dict。"""
    return ctx.ticker_data.get(ticker, {})


def _get_history(ticker: str, ctx: EvalContext) -> dict:
    tdata = _get_ticker_data(ticker, ctx)
    return tdata.get("history") or {}


def _get_price(ticker: str, ctx: EvalContext) -> dict:
    tdata = _get_ticker_data(ticker, ctx)
    return tdata.get("price") or {}


def _compute_ma(closes: np.ndarray, window: int) -> float | None:
    """从收盘价序列计算最新 MA 值。"""
    ma = calc_ma(closes, window)
    val = float(ma[-1])
    return round(val, 2) if not np.isnan(val) else None


# ═══════════════════════════════════════════════
# 价格状态 (5)
# ═══════════════════════════════════════════════


def eval_price_move(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pct = params["pct"]
    direction = params.get("direction", "up")
    lookback_days = params.get("lookback_days")
    base_date = params.get("base_date")

    tdata = _get_ticker_data(ticker, ctx)

    # 优先使用 base_date（绝对日期），其次 lookback_days（相对天数）
    if base_date:
        # 使用绝对日期作为基准
        history = tdata.get("history") or {}
        data = history.get("data", [])

        if len(data) < 2:
            return {"triggered": False, "reason": "历史数据不足"}

        # 查找指定日期的收盘价
        # data 格式: [{"date": "2024-03-15", "open": ..., "close": ..., ...}, ...]
        # 容错：找到 <= base_date 的最近一个交易日（向后查找，取最新的）
        base_close = None
        base_actual_date = None
        target_date = base_date.replace("-", "")  # 统一转为 YYYYMMDD 比较

        # 从后往前遍历，找到第一个 <= target_date 的日期（即最近的交易日）
        for bar in reversed(data):
            bar_date = bar.get("date", "")  # "2024-03-15" 格式
            bar_date_num = bar_date.replace("-", "")

            # 找到 <= target_date 的最新日期
            if bar_date_num <= target_date:
                base_close = float(bar["close"])
                base_actual_date = bar_date
                break  # 找到最近的就停止

        if base_close is None:
            return {"triggered": False, "reason": f"未找到 {base_date} 或之前的交易日数据"}

        # 获取当前价格
        price_data = tdata.get("price") or {}
        current = float(price_data.get("price", 0))
        if current == 0:
            # 回退到最新一根bar的收盘价
            current = float(data[-1]["close"])

        if base_close == 0:
            return {"triggered": False, "reason": "基准价格为零"}

        actual_pct = round((current - base_close) / base_close * 100, 2)

        triggered = (direction == "up" and actual_pct >= pct) or (direction == "down" and actual_pct <= -pct)
        return {
            "triggered": triggered,
            "actual_pct": actual_pct,
            "target_pct": pct,
            "base_date": base_date,
            "base_actual_date": base_actual_date,  # 实际使用的交易日
            "base_price": round(base_close, 2),
            "current_price": round(current, 2),
        }

    elif lookback_days and lookback_days > 1:
        # 使用相对天数
        history = tdata.get("history") or {}
        data = history.get("data", [])
        if len(data) < lookback_days + 1:
            return {"triggered": False, "reason": f"历史数据不足{lookback_days + 1}天"}
        base_close = float(data[-(lookback_days + 1)]["close"])
        price_data = tdata.get("price") or {}
        current = float(price_data.get("price", 0))
        if current == 0:
            current = float(data[-1]["close"])
        if base_close == 0:
            return {"triggered": False, "reason": "基准价格为零"}
        actual_pct = round((current - base_close) / base_close * 100, 2)

        triggered = (direction == "up" and actual_pct >= pct) or (direction == "down" and actual_pct <= -pct)
        return {"triggered": triggered, "actual_pct": actual_pct, "target_pct": pct, "lookback_days": lookback_days}

    else:
        # 默认：今日涨跌幅
        price_data = tdata.get("price") or {}
        actual_pct = price_data.get("pct_chg", 0)

        triggered = (direction == "up" and actual_pct >= pct) or (direction == "down" and actual_pct <= -pct)
        return {"triggered": triggered, "actual_pct": actual_pct, "target_pct": pct, "lookback_days": 1}


def eval_price_vs_level(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    level = params["level"]
    relation = params.get("relation", "above")
    tolerance_pct = params.get("tolerance_pct", 1)

    # 获取当前价格
    price_data = _get_ticker_data(ticker, ctx).get("price") or {}
    price = price_data.get("price", 0)
    if price == 0:
        history = _get_history(ticker, ctx)
        data = history.get("data", [])
        if data:
            price = float(data[-1]["close"])
    if price == 0:
        return {"triggered": False, "reason": "无法获取价格"}

    if isinstance(level, str) and level.startswith("MA"):
        # 从 history 收盘价计算 MA
        history = _get_history(ticker, ctx)
        data = history.get("data", [])
        if not data:
            return {"triggered": False, "reason": f"{level} 数据不可用——无历史数据"}

        window = int(level[2:])  # "MA5" -> 5
        closes = np.array([float(d["close"]) for d in data], dtype=float)
        target = _compute_ma(closes, window)
        if target is None:
            return {"triggered": False, "reason": f"{level} 数据不可用——收盘价不足{window}根"}
    else:
        target = float(level)

    if target == 0:
        return {"triggered": False, "reason": "参考价位为零"}

    if relation == "above":
        triggered = price > target
    elif relation == "below":
        triggered = price < target
    elif relation == "near":
        diff_pct = abs(price - target) / target * 100
        triggered = diff_pct <= tolerance_pct
        return {
            "triggered": triggered,
            "price": price,
            "level": target,
            "diff_pct": round(diff_pct, 2),
            "tolerance_pct": tolerance_pct,
        }
    else:
        return {"triggered": False, "reason": f"未知关系: {relation}"}

    return {"triggered": triggered, "price": price, "level": target, "relation": relation}


def eval_new_extreme(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    n_days = params["n_days"]
    direction = params.get("direction", "high")

    tdata = _get_ticker_data(ticker, ctx)
    history = tdata.get("history") or {}
    data = history.get("data", [])
    if len(data) < n_days + 1:
        return {"triggered": False, "reason": f"历史数据不足{n_days + 1}天"}

    price_data = tdata.get("price") or {}
    current = float(price_data.get("price", 0))
    if current == 0:
        current = float(data[-1]["close"])

    if direction == "high":
        n_day_high = max(float(d["high"]) for d in data[-(n_days + 1) : -1])
        triggered = current > n_day_high
    else:
        n_day_low = min(float(d["low"]) for d in data[-(n_days + 1) : -1])
        triggered = current < n_day_low

    return {"triggered": triggered, "current": current, "n_days": n_days, "direction": direction}


def eval_gap(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    min_pct = params["min_pct"]
    direction = params.get("direction", "up")

    tdata = _get_ticker_data(ticker, ctx)
    price_data = tdata.get("price") or {}
    prev_close = price_data.get("pre_close", 0)
    if prev_close is None or prev_close == 0:
        # 从 history 获取
        history = tdata.get("history") or {}
        data = history.get("data", [])
        if len(data) >= 2:
            prev_close = float(data[-2]["close"])
        else:
            return {"triggered": False, "reason": "无前收盘数据"}

    open_price = price_data.get("open", 0)
    if open_price == 0:
        return {"triggered": False, "reason": "无开盘价"}

    gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
    triggered = (direction == "up" and gap_pct >= min_pct) or (direction == "down" and gap_pct <= -min_pct)
    return {"triggered": triggered, "gap_pct": gap_pct, "min_pct": min_pct, "direction": direction}


def eval_consecutive_move(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    n_days = params["n_days"]
    direction = params.get("direction", "up")

    tdata = _get_ticker_data(ticker, ctx)
    history = tdata.get("history") or {}
    if history.get("count", 0) < n_days + 1:
        return {"triggered": False, "reason": "历史数据不足"}

    closes = [float(d["close"]) for d in history["data"][-(n_days + 1) :]]
    for i in range(1, len(closes)):
        if direction == "up" and closes[i] <= closes[i - 1]:
            return {"triggered": False, "consecutive_days": n_days, "direction": direction}
        if direction == "down" and closes[i] >= closes[i - 1]:
            return {"triggered": False, "consecutive_days": n_days, "direction": direction}

    return {"triggered": True, "consecutive_days": n_days, "direction": direction}


# ═══════════════════════════════════════════════
# 量价关系 (3)
# ═══════════════════════════════════════════════


# 量比：开盘后多少分钟内不触发（开盘集中放量，均线法会严重高估）
_VOLUME_RATIO_SKIP_MINUTES = 30


def eval_volume_ratio(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    multiplier = params["multiplier"]
    relation = params.get("relation", "above")
    n_days = params.get("n_days", 20)

    # 日内时间
    now = ctx.now
    hour_min = now.hour * 60 + now.minute
    if 570 <= hour_min <= 690:
        elapsed = hour_min - 570
    elif 780 <= hour_min <= 900:
        elapsed = 120 + (hour_min - 780)
    else:
        elapsed = 0

    # 开盘后 skip 分钟内不触发，避免开盘集中放量导致的虚假高量比
    if 0 < elapsed <= _VOLUME_RATIO_SKIP_MINUTES:
        return {"triggered": False, "reason": f"开盘后{elapsed}分钟，跳过量比判定"}

    tdata = _get_ticker_data(ticker, ctx)
    history = tdata.get("history") or {}
    data = history.get("data", [])

    if len(data) < n_days + 1:
        return {"triggered": False, "reason": f"日线数据不足{n_days + 1}天"}

    volumes = [float(d["volume"]) for d in data[-(n_days + 1) :]]
    latest_vol = volumes[-1]
    avg_vol = sum(volumes[:-1]) / n_days
    ratio = round(latest_vol / avg_vol, 2) if avg_vol > 0 else 1.0

    # 日内时间比例修正
    if 0 < elapsed < 240:
        ratio = round(ratio * 240.0 / elapsed, 2)

    triggered = (relation == "above" and ratio >= multiplier) or (relation == "below" and ratio <= multiplier)
    return {"triggered": triggered, "volume_ratio": ratio, "multiplier": multiplier, "n_days": n_days}


def eval_turnover_active(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pct = params["pct"]
    relation = params.get("relation", "above")

    tdata = _get_ticker_data(ticker, ctx)
    turnover = tdata.get("turnover")
    if turnover is None or turnover == 0:
        return {"triggered": False, "reason": "无法计算换手率"}
    triggered = (relation == "above" and turnover >= pct) or (relation == "below" and turnover <= pct)
    return {"triggered": triggered, "turnover_rate": turnover, "target_pct": pct}


def eval_amplitude_wide(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pct = params["pct"]
    relation = params.get("relation", "above")

    tdata = _get_ticker_data(ticker, ctx)
    snap = tdata.get("snapshot") or {}
    if "error" in snap:
        return {"triggered": False, "reason": snap["error"]}
    if not snap:
        return {"triggered": False, "reason": "snapshot 数据不可用"}

    amplitude = snap.get("high_pct", 0) - snap.get("low_pct", 0)
    triggered = (relation == "above" and amplitude >= pct) or (relation == "below" and amplitude <= pct)
    return {"triggered": triggered, "amplitude": round(amplitude, 2), "target_pct": pct}
