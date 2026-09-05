"""板块与市场类原子评估器 —— 板块涨跌异动、涨跌比、涨停家数、全市场广度、成交额

注意：文件名 sentiment 是早期的命名，实际评估维度为市场/板块广度与量价结构，不涉及情绪分析。
"""

from __future__ import annotations

from src.triggers.eval_context import EvalContext


def _get_sector_data(sector: str, ctx: EvalContext) -> dict:
    return ctx.sector_data.get(sector, {})


# ═══════════════════════════════════════════════
# 板块 (3)
# ═══════════════════════════════════════════════


def eval_sector_move(params: dict, ctx: EvalContext) -> dict:
    sector = params["sector"]
    pct = params["pct"]
    direction = params.get("direction", "up")
    velocity_minutes = params.get("velocity_minutes")

    sec = _get_sector_data(sector, ctx)
    overview = sec.get("overview") or {}
    if "error" in overview:
        return {"triggered": False, "reason": overview["error"]}

    if velocity_minutes is not None:
        intraday = sec.get("intraday") or {}
        if "error" in intraday:
            return {"triggered": False, "reason": intraday["error"]}
        bars = intraday.get("bars", [])
        if len(bars) < velocity_minutes + 1:
            return {"triggered": False, "reason": f"板块分钟 bar 数据不足{velocity_minutes + 1}"}
        recent = bars[-(velocity_minutes + 1) :]
        start_close = recent[0]["close"]
        end_close = recent[-1]["close"]
        velocity = round((end_close - start_close) / start_close * 100, 2) if start_close else 0
        velocity_ok = (direction == "up" and velocity >= pct) or (direction == "down" and velocity <= -pct)
        if not velocity_ok:
            return {
                "triggered": False,
                "reason": f"最近{velocity_minutes}分钟涨速{velocity}%，未达{pct}%",
                "velocity_pct": velocity,
            }

    pct_chg = overview.get("pct_chg", 0)
    triggered = (direction == "up" and pct_chg >= pct) or (direction == "down" and pct_chg <= -pct)
    return {"triggered": triggered, "pct_chg": pct_chg, "target_pct": pct, "velocity_minutes": velocity_minutes}


def eval_sector_breadth(params: dict, ctx: EvalContext) -> dict:
    sec = _get_sector_data(params["sector"], ctx)
    overview = sec.get("overview") or {}
    if "error" in overview:
        return {"triggered": False, "reason": overview["error"]}
    up_count = overview.get("up_count", 0)
    down_count = overview.get("down_count", 0)
    total = up_count + down_count
    up_ratio = up_count / total if total > 0 else 0
    triggered = up_ratio >= params.get("up_ratio_min", 0.5)
    return {"triggered": triggered, "up_ratio": round(up_ratio, 2), "up_count": up_count, "down_count": down_count}


def eval_sector_limit_ratio(params: dict, ctx: EvalContext) -> dict:
    sector = params["sector"]
    direction = params.get("direction", "up")
    min_count = params.get("min_count", 1)

    sec = _get_sector_data(sector, ctx)
    overview = sec.get("overview") or {}
    if "error" in overview:
        return {"triggered": False, "reason": overview["error"]}

    members = sec.get("members") or []
    limit_count = 0
    for ticker in members:
        tdata = ctx.ticker_data.get(ticker, {})
        status = tdata.get("zdt_record") or {}
        if not status.get("is_limit"):
            continue
        limit_type = status.get("limit_type", "")
        if direction == "up" and limit_type == "涨停" or direction == "down" and limit_type == "跌停":
            limit_count += 1

    triggered = limit_count >= min_count
    total = len(members) if members else 1
    return {
        "triggered": triggered,
        "limit_count": limit_count,
        "total_members": len(members),
        "ratio": round(limit_count / total, 2),
        "min_count": min_count,
    }


# ═══════════════════════════════════════════════
# 市场 (2)
# ═══════════════════════════════════════════════


def eval_market_breadth(params: dict, ctx: EvalContext) -> dict:
    data = ctx.market_summary or {}
    ratio = data.get("up_down_ratio", 1.0)
    triggered = ratio >= params.get("up_down_ratio_min", 1.0)
    if params.get("avg_pct_min") is not None:
        triggered = triggered and data.get("avg_pct_chg", 0) >= params["avg_pct_min"]
    return {"triggered": triggered, "up_down_ratio": ratio}


# ═══════════════════════════════════════════════
# 市场情绪综合评分 (market_sentiment)
# ═══════════════════════════════════════════════


def _score_breadth(up_down_ratio: float) -> int:
    """涨跌广度 → 0-30 分"""
    if up_down_ratio >= 3.0:
        return 30
    elif up_down_ratio >= 2.0:
        return 24
    elif up_down_ratio >= 1.5:
        return 18
    elif up_down_ratio >= 1.0:
        return 12
    elif up_down_ratio >= 0.7:
        return 6
    return 0


def _score_index_alignment(index_overview: dict) -> int:
    """指数协同 → 0-25 分"""
    if not index_overview:
        return 10  # 无数据时给中性分

    # 6 大指数：上证50、沪深300、中证500、中证1000、创业板指、科创50
    key_indices = ["上证指数", "沪深300", "中证500", "中证1000", "创业板指", "科创50"]
    up_count = 0
    for name in key_indices:
        info = index_overview.get(name, {})
        pct = info.get("pct_chg")
        if pct is not None and pct > 0:
            up_count += 1

    mapping = {6: 25, 5: 20, 4: 15, 3: 10, 2: 5, 1: 0, 0: 0}
    return mapping.get(up_count, 0)


def _score_avg_pct(avg_pct_chg: float) -> int:
    """平均涨幅 → 0-20 分"""
    if avg_pct_chg >= 2.0:
        return 20
    elif avg_pct_chg >= 1.0:
        return 16
    elif avg_pct_chg >= 0.5:
        return 12
    elif avg_pct_chg >= 0:
        return 8
    elif avg_pct_chg >= -0.5:
        return 4
    return 0


def _score_volume(amount_ratio: float) -> int:
    """量能 → 0-15 分"""
    if amount_ratio >= 1.5:
        return 15
    elif amount_ratio >= 1.2:
        return 12
    elif amount_ratio >= 1.0:
        return 9
    elif amount_ratio >= 0.8:
        return 6
    return 0


def _score_zdt_follow(zdt_follow_pct: float) -> int:
    """昨日涨停今日表现 → 0-10 分"""
    if zdt_follow_pct >= 3.0:
        return 10
    elif zdt_follow_pct >= 1.5:
        return 8
    elif zdt_follow_pct >= 0:
        return 5
    elif zdt_follow_pct >= -2.0:
        return 3
    return 0


def eval_market_sentiment(params: dict, ctx: EvalContext) -> dict:
    data = ctx.market_summary or {}

    ratio = data.get("up_down_ratio", 1.0)
    index_ov = data.get("index_overview") or {}
    avg_pct = data.get("avg_pct_chg", 0.0)
    amount_ratio = data.get("amount_ratio", 1.0)
    zdt_follow_pct = data.get("zdt_follow_pct", 0.0)

    breadth_score = _score_breadth(ratio)
    alignment_score = _score_index_alignment(index_ov)
    avg_score = _score_avg_pct(avg_pct)
    volume_score = _score_volume(amount_ratio)
    limit_score = _score_zdt_follow(zdt_follow_pct)

    total = breadth_score + alignment_score + avg_score + volume_score + limit_score

    min_score = params["min_score"]
    direction = params.get("direction", "bullish")
    triggered = total >= min_score if direction == "bullish" else total <= min_score

    return {
        "triggered": triggered,
        "score": total,
        "breadth_score": breadth_score,
        "alignment_score": alignment_score,
        "avg_score": avg_score,
        "volume_score": volume_score,
        "limit_score": limit_score,
        "up_down_ratio": ratio,
        "avg_pct_chg": avg_pct,
        "amount_ratio": amount_ratio,
        "zdt_follow_pct": zdt_follow_pct,
        "zdt_follow_rate": data.get("zdt_follow_rate"),
        "zdt_consecutive_rate": data.get("zdt_consecutive_rate"),
        "index_count": sum(1 for v in index_ov.values() if (v.get("pct_chg") or 0) > 0) if index_ov else 0,
    }


# market_volume 原子已删除，不再需要 eval_market_volume
