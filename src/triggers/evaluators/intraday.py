"""日内动态类原子评估器 (v3) —— 3个"""

from __future__ import annotations

from src.triggers.eval_context import EvalContext


def _snapshot_bars(snap: dict) -> list[dict]:
    bars = snap.get("bars", [])
    return bars if isinstance(bars, list) else []


def _pct_from_base(price: float, base: float) -> float:
    return round((price - base) / base * 100, 2) if base else 0.0


def _get_snap(ticker: str, ctx: EvalContext) -> dict:
    tdata = ctx.ticker_data.get(ticker, {})
    return tdata.get("snapshot") or {}


def eval_intraday_reversal(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    pattern = params.get("pattern", "shot_up_fall")
    move_pct = params.get("move_pct", 3)
    retrace_ratio = params.get("retrace_ratio", 50)

    snap = _get_snap(ticker, ctx)
    if "error" in snap:
        return {"triggered": False, "reason": snap["error"]}
    if not snap:
        return {"triggered": False, "reason": "snapshot 数据不可用"}

    bars = _snapshot_bars(snap)
    if len(bars) < 2:
        return {"triggered": False, "reason": "1分钟数据不足"}
    open_price = float(snap.get("open", 0))
    latest_pct = float(snap.get("latest_pct", 0))

    if pattern == "shot_up_fall":
        high_idx, high_bar = max(enumerate(bars), key=lambda item: float(item[1]["high"]))
        extreme_pct = _pct_from_base(float(high_bar["high"]), open_price)
        if extreme_pct < move_pct:
            return {"triggered": False, "reason": f"最高涨幅{extreme_pct}%未达{move_pct}%"}
        if high_idx >= len(bars) - 1:
            return {"triggered": False, "reason": "最高点出现在最后一根1m，尚未形成回落"}
        fall_amount = extreme_pct - latest_pct
        fall_ratio = (fall_amount / extreme_pct * 100) if extreme_pct > 0 else 0
        triggered = fall_ratio >= retrace_ratio
        return {
            "triggered": triggered,
            "pattern": pattern,
            "high_pct": extreme_pct,
            "latest_pct": latest_pct,
            "fall_ratio": round(fall_ratio, 1),
        }
    else:
        # dip_recover
        low_idx, low_bar = min(enumerate(bars), key=lambda item: float(item[1]["low"]))
        extreme_pct = _pct_from_base(float(low_bar["low"]), open_price)
        if extreme_pct > -move_pct:
            return {"triggered": False, "reason": f"最低跌幅{abs(extreme_pct)}%未达{move_pct}%"}
        if low_idx >= len(bars) - 1:
            return {"triggered": False, "reason": "最低点出现在最后一根1m，尚未形成回升"}
        recovered = latest_pct - extreme_pct
        total_dip = abs(extreme_pct)
        recovery_ratio = (recovered / total_dip * 100) if total_dip > 0 else 0
        triggered = recovery_ratio >= retrace_ratio
        return {
            "triggered": triggered,
            "pattern": pattern,
            "low_pct": extreme_pct,
            "latest_pct": latest_pct,
            "recovery_ratio": round(recovery_ratio, 1),
        }


def eval_intraday_round_trip(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    direction = params.get("direction", "A")
    min_move_pct = params.get("min_move_pct", 2)
    tolerance_pct = params.get("tolerance_pct", 0.5)

    snap = _get_snap(ticker, ctx)
    if "error" in snap:
        return {"triggered": False, "reason": snap["error"]}
    if not snap:
        return {"triggered": False, "reason": "snapshot 数据不可用"}

    bars = _snapshot_bars(snap)
    if len(bars) < 2:
        return {"triggered": False, "reason": "1分钟数据不足"}
    open_price = float(snap.get("open", 0))
    latest_pct = float(snap.get("latest_pct", 0))

    if direction == "A":
        high_idx, high_bar = max(enumerate(bars), key=lambda item: float(item[1]["high"]))
        extreme_pct = _pct_from_base(float(high_bar["high"]), open_price)
        if extreme_pct < min_move_pct:
            return {"triggered": False, "reason": f"最高涨幅{extreme_pct}%未达{min_move_pct}%"}
        if high_idx >= len(bars) - 1:
            return {"triggered": False, "reason": "最高点出现在最后一根1m，未形成A字回落"}
        triggered = abs(latest_pct) <= tolerance_pct
    else:
        # V
        low_idx, low_bar = min(enumerate(bars), key=lambda item: float(item[1]["low"]))
        extreme_pct = _pct_from_base(float(low_bar["low"]), open_price)
        if extreme_pct > -min_move_pct:
            return {"triggered": False, "reason": f"最低跌幅{abs(extreme_pct)}%未达{min_move_pct}%"}
        if low_idx >= len(bars) - 1:
            return {"triggered": False, "reason": "最低点出现在最后一根1m，未形成V字回升"}
        triggered = abs(latest_pct) <= tolerance_pct

    return {"triggered": triggered, "direction": direction, "extreme_pct": extreme_pct, "latest_pct": latest_pct}


def eval_intraday_trend(params: dict, ctx: EvalContext) -> dict:
    ticker = params["ticker"]
    direction = params.get("direction", "up")
    minutes = params.get("minutes", 30)
    min_pct = params.get("min_pct", 3)

    snap = _get_snap(ticker, ctx)
    if "error" in snap:
        return {"triggered": False, "reason": snap["error"]}
    if not snap:
        return {"triggered": False, "reason": "snapshot 数据不可用"}

    bars = _snapshot_bars(snap)
    if len(bars) < minutes:
        return {"triggered": False, "reason": "1分钟数据不足"}

    recent = bars[-minutes:]
    closes = [float(b["close"]) for b in recent]
    start_close = closes[0]
    end_close = closes[-1]
    move_pct = _pct_from_base(end_close, start_close)

    # 单边性：反向回撤幅度不超过总移动的 40%
    total_move = abs(move_pct)
    if direction == "up":
        high_idx = closes.index(max(closes))
        max_retrace = max(closes[high_idx:]) - min(closes)
        retrace_ratio = max_retrace / total_move if total_move > 0 else 1.0
        triggered = move_pct >= min_pct and retrace_ratio <= 0.4
    else:
        low_idx = closes.index(min(closes))
        max_retrace = max(closes[low_idx:]) - min(closes)
        retrace_ratio = max_retrace / total_move if total_move > 0 else 1.0
        triggered = move_pct <= -min_pct and retrace_ratio <= 0.4

    return {
        "triggered": triggered,
        "direction": direction,
        "move_pct": move_pct,
        "retrace_ratio": round(retrace_ratio, 2),
        "minutes": minutes,
    }
