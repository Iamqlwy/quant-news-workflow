"""触发原子评估器注册表 (v3) —— 从 ATOM_SCHEMA 派生"""

from __future__ import annotations

from typing import Any

from src.triggers.eval_context import EvalContext

from .intraday import (
    eval_intraday_reversal,
    eval_intraday_round_trip,
    eval_intraday_trend,
)
from .price import (
    eval_amplitude_wide,
    eval_consecutive_move,
    eval_gap,
    eval_new_extreme,
    eval_price_move,
    eval_price_vs_level,
    eval_turnover_active,
    eval_volume_ratio,
)
from .sentiment import (
    eval_market_breadth,
    eval_market_sentiment,
    eval_sector_breadth,
    eval_sector_limit_ratio,
    eval_sector_move,
)
from .technical import (
    eval_ma_alignment,
    eval_ma_cross,
    eval_ma_slope,
    eval_macd_cross,
    eval_macd_divergence,
)

# ── 注册表 ───────────────────────────────────

EVALUATORS: dict[str, Any] = {
    # 价格状态 (5)
    "price_move": eval_price_move,
    "price_vs_level": eval_price_vs_level,
    "new_extreme": eval_new_extreme,
    "gap": eval_gap,
    "consecutive_move": eval_consecutive_move,
    # 量价关系 (3)
    "volume_ratio": eval_volume_ratio,
    "turnover_active": eval_turnover_active,
    "amplitude_wide": eval_amplitude_wide,
    # 趋势结构 (3)
    "ma_slope": eval_ma_slope,
    "ma_cross": eval_ma_cross,
    "ma_alignment": eval_ma_alignment,
    # MACD 指标 (2)
    "macd_cross": eval_macd_cross,
    "macd_divergence": eval_macd_divergence,
    # 日内动态 (3)
    "intraday_reversal": eval_intraday_reversal,
    "intraday_round_trip": eval_intraday_round_trip,
    "intraday_trend": eval_intraday_trend,
    # 板块与市场 (5)
    "sector_move": eval_sector_move,
    "sector_breadth": eval_sector_breadth,
    "sector_limit_ratio": eval_sector_limit_ratio,
    "market_breadth": eval_market_breadth,
    "market_sentiment": eval_market_sentiment,
}


def evaluate_atom(atom_name: str, params: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    if atom_name not in EVALUATORS:
        return {"atom": atom_name, "triggered": False, "reason": "unknown_atom"}
    try:
        result = EVALUATORS[atom_name](params, ctx)
        return {"atom": atom_name, "triggered": result.get("triggered", False), "detail": result}
    except Exception as e:
        return {"atom": atom_name, "triggered": False, "error": str(e)}
