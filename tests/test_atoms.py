"""测试全部 34 个触发原子 —— 时钟固定在 2026-05-26 11:00"""
from __future__ import annotations
from loguru import logger

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.triggers.atoms import ATOM_DEFINITIONS
from src.triggers.evaluators import EVALUATORS, evaluate_atom

# 强制模拟模式，避免 xtquant 连接阻塞
settings.simulation_enabled = True

# ── 测试参数 ──────────────────────────────────

TEST_TICKER = "000001.SZ"
TEST_CONCEPT_CODE = "885311.TI"       # 智能电网 (260 members)
TEST_CONCEPT_NAME = "智能电网"
TEST_CONCEPT_CODE_SMALL = "700046.TI" # 23 members, fast for scans
TEST_SECTOR_B = "物联网"               # for sector_relative_strength

# ── 初始化 ──────────────────────────────────

logger.info("=" * 60)
logger.info("Setting up MarketDataProvider at 2026-05-26 11:00:00")
logger.info("=" * 60)

clock_config = TimeConfig(
    start_time=datetime(2026, 5, 26, 11, 0, 0),
    tick_duration=timedelta(minutes=5),
    realtime=False,
)
clock = Clock(clock_config)
market = MarketDataProvider(clock=clock)

logger.info(f"clock.now = {clock.now}")
logger.info(f"clock.today = {clock.today}")
logger.info(f"_xt_ready = {market._xt_ready}")
logger.info(f"Trading days: {len(market._cache.get('daily_window', []))} days in window")
logger.info()

# ── 参数构建辅助 ──────────────────────────────

def atom_params(atom_name: str) -> dict:
    """根据原子定义自动构建测试参数"""
    params_def = ATOM_DEFINITIONS[atom_name]["params"]

    filled: dict = {}
    for key, desc in params_def.items():
        if key == "ticker":
            filled[key] = TEST_TICKER
        elif key == "leader_ticker":
            filled[key] = TEST_TICKER
        elif key == "sector":
            filled[key] = TEST_CONCEPT_NAME
        elif key == "sector_a":
            filled[key] = TEST_CONCEPT_NAME
        elif key == "sector_b":
            filled[key] = TEST_SECTOR_B
        elif key == "level":
            filled[key] = 10.0
        elif key == "pct":
            filled[key] = 1.0
        elif key == "value":
            filled[key] = 50
        elif key == "multiplier":
            filled[key] = 1.5
        elif key == "amount_yi":
            filled[key] = 5000
        elif key == "n":
            filled[key] = 3
        elif key == "days":
            filled[key] = 3
        elif key == "days_min" or key == "duration_minutes":
            filled[key] = 3
        elif key == "days_max":
            filled[key] = 10
        elif key == "minutes":
            filled[key] = 5
        elif key == "strength_pct":
            filled[key] = 5
        elif key == "count":
            filled[key] = 1
        elif key == "ratio_min":
            filled[key] = 0.3
        elif key == "up_ratio_min":
            filled[key] = 0.5
        elif key == "up_down_ratio_min":
            filled[key] = 1.0
        elif key == "consecutive_days":
            filled[key] = 3
        elif key in ("direction", "relation", "signal", "position", "pattern", "limit_type"):
            # 选第一个可用值
            options = desc.split("/")
            filled[key] = options[0]
        elif key == "fast":
            filled[key] = "MA5"
        elif key == "slow":
            filled[key] = "MA20"
        elif key == "ma":
            filled[key] = "MA20"
        else:
            filled[key] = 0  # fallback
    return filled


# ── 分类定义 ──────────────────────────────────

ATOM_CATEGORIES = {
    "价格与成交量": [
        "price_level", "price_change_pct", "price_velocity", "volume_spike",
        "consecutive_days", "turnover_rate", "open_gap", "intraday_amplitude",
    ],
    "技术指标": [
        "ma_position", "ma_cross", "macd", "rsi", "bollinger", "kdj", "volume_ratio",
    ],
    "市场情绪与资金": [
        "sector_index_change", "sector_breadth", "sector_limit_ratio",
        "sector_volume_ratio", "sector_index_velocity", "sector_up_down_ratio",
        "sector_leader_strength", "market_breadth", "market_volume",
        "sector_relative_strength", "leader_divergence",
    ],
    "日内分时形态": [
        "intraday_shot_up_fall", "intraday_dip_recover",
        "intraday_A_shape", "intraday_V_shape", "intraday_trend",
    ],
    "时间": [
        "time_after", "time_window", "time_before",
    ],
}


# ── 测试执行 ──────────────────────────────────

async def test_all_atoms():
    results: dict[str, dict] = {}
    total = 0
    passed = 0
    failed = 0
    errors = 0

    for category, atoms in ATOM_CATEGORIES.items():
        logger.info(f"\n{'─' * 60}")
        logger.info(f"  {category}")
        logger.info(f"{'─' * 60}")

        for atom_name in atoms:
            total += 1
            params = atom_params(atom_name)

            # 时间原子注入 created_at / now
            if atom_name in ("time_after", "time_window", "time_before"):
                params["created_at"] = "2026-05-23T00:00:00"
                params["now"] = clock.now.isoformat()

            try:
                result = await evaluate_atom(atom_name, params, market)
            except Exception as e:
                result = {"atom": atom_name, "triggered": False, "error": str(e)}

            triggered = result.get("triggered", False)
            has_error = "error" in result
            detail = result.get("detail", result.get("error", ""))

            # 判断是否正常工作（不崩溃即为通过，stub/error 单独标记）
            if has_error:
                errors += 1
                status = "ERROR"
            else:
                passed += 1
                status = "PASS"

            # 提取关键信息展示
            if isinstance(detail, dict):
                info = ", ".join(
                    f"{k}={v}" for k, v in detail.items()
                    if k in ("triggered", "reason", "pct_chg", "ratio", "count",
                             "leader_ticker", "deviation", "diff", "velocity_pct",
                             "volume_ratio", "up_down_ratio", "position", "rsi")
                )
            else:
                info = str(detail)

            trig = "🔥" if triggered else "  "
            logger.info(f"  [{status}] {trig} {atom_name:30s} | {info[:90]}")

            results[atom_name] = result

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  Summary: {passed} passed, {errors} errors (total {total})")
    logger.info(f"{'=' * 60}")
    return results


if __name__ == "__main__":
    asyncio.run(test_all_atoms())
