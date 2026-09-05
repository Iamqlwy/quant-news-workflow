"""时间类原子评估器 —— 引擎注入 created_at 到 params，now 从 EvalContext 读取"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.core.timezone import BEIJING_TZ
from src.triggers.eval_context import EvalContext


def _ensure_beijing(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt


def eval_time_after(params: dict, ctx: EvalContext) -> dict:
    created = _ensure_beijing(datetime.fromisoformat(params["created_at"]))
    now_val = (
        _ensure_beijing(ctx.now)
        if isinstance(ctx.now, datetime)
        else _ensure_beijing(datetime.fromisoformat(str(ctx.now)))
    )
    target = created + timedelta(days=params["days"])
    triggered = now_val >= target
    return {"triggered": triggered, "target": target.isoformat(), "now": now_val.isoformat()}


def eval_time_window(params: dict, ctx: EvalContext) -> dict:
    created = _ensure_beijing(datetime.fromisoformat(params["created_at"]))
    now_val = (
        _ensure_beijing(ctx.now)
        if isinstance(ctx.now, datetime)
        else _ensure_beijing(datetime.fromisoformat(str(ctx.now)))
    )
    days_min = params.get("days_min", 0)
    days_max = params.get("days_max", 0)
    start = created + timedelta(days=days_min)
    end = created + timedelta(days=days_max)
    triggered = start <= now_val <= end
    return {"triggered": triggered, "start": start.isoformat(), "end": end.isoformat(), "now": now_val.isoformat()}


def eval_time_before(params: dict, ctx: EvalContext) -> dict:
    created = _ensure_beijing(datetime.fromisoformat(params["created_at"]))
    now_val = (
        _ensure_beijing(ctx.now)
        if isinstance(ctx.now, datetime)
        else _ensure_beijing(datetime.fromisoformat(str(ctx.now)))
    )
    target = created + timedelta(days=params["days"])
    triggered = now_val <= target
    return {"triggered": triggered, "target": target.isoformat(), "now": now_val.isoformat()}
