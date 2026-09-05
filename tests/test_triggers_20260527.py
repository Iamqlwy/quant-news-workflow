"""触发器系统完整测试 —— 时钟固定在 2026-05-27 11:30

Part 1: 全部 23 个原子独立测试 (v2)
Part 2: 条件树组合测试
Part 3: _collect_atoms 算法测试
"""
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
from src.triggers.atoms import ATOM_DEFINITIONS, evaluate_condition_tree
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import EVALUATORS, evaluate_atom
from src.triggers.engine import TriggerEngine

# 强制模拟模式，避免 xtquant 连接阻塞
settings.simulation_enabled = True

# ── 测试参数 ──────────────────────────────────

TEST_TICKER = "000001.SZ"
TEST_CONCEPT_CODE = "885311.TI"       # 智能电网 (260 members)
TEST_CONCEPT_NAME = "智能电网"
TEST_CONCEPT_CODE_SMALL = "700046.TI" # 23 members
TEST_SECTOR_B = "物联网"

# ── 初始化 ──────────────────────────────────

logger.info("=" * 60)
logger.info("Setting up MarketDataProvider at 2026-05-27 11:30:00")
logger.info("=" * 60)

clock_config = TimeConfig(
    start_time=datetime(2026, 5, 27, 11, 30, 0),
    tick_duration=timedelta(minutes=5),
    realtime=False,
)
clock = Clock(clock_config)
market = MarketDataProvider(clock=clock)

logger.info(f"clock.now = {clock.now}")
logger.info(f"clock.today = {clock.today}")
logger.info(f"_xt_ready = {market._xt_ready}")
logger.info(f"Trading days: {len(market._cache.get('daily_window', []))} days in window")
logger.info("")

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
        elif key == "n_days":
            filled[key] = 3
        elif key == "lookback_days":
            filled[key] = 1
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
        elif key == "min_count":
            filled[key] = 1
        elif key == "ratio_min":
            filled[key] = 0.3
        elif key == "up_ratio_min":
            filled[key] = 0.5
        elif key == "up_down_ratio_min":
            filled[key] = 1.0
        elif key == "consecutive_days":
            filled[key] = 3
        elif key == "min_pct":
            filled[key] = 1.0
        elif key == "move_pct":
            filled[key] = 3
        elif key == "retrace_ratio":
            filled[key] = 50
        elif key == "min_move_pct":
            filled[key] = 2
        elif key == "tolerance_pct":
            filled[key] = 1.0
        elif key == "velocity_minutes":
            filled[key] = 5
        elif key in ("direction", "relation", "signal", "position", "pattern", "limit_type",
                      "price_position", "mode", "slope_direction"):
            options = desc.split("/")
            filled[key] = options[0].strip()
        elif key == "fast_period":
            filled[key] = "MA5"
        elif key == "slow_period":
            filled[key] = "MA20"
        elif key == "period":
            filled[key] = "MA20"
        elif key == "ma":
            filled[key] = "MA20"
        else:
            filled[key] = 0
    return filled


# ── 分类定义 (v3, 5 classes, 22 atoms, time atoms are meta) ──

ATOM_CATEGORIES = {
    "价格状态": [
        "price_move", "price_vs_level", "new_extreme", "gap", "consecutive_move",
    ],
    "量价关系": [
        "volume_ratio", "turnover_active", "amplitude_wide",
    ],
    "趋势结构": [
        "ma_slope", "ma_cross", "ma_alignment",
    ],
    "日内动态": [
        "intraday_reversal", "intraday_round_trip", "intraday_trend",
    ],
    "板块与市场": [
        "sector_move", "sector_breadth", "sector_limit_ratio",
        "market_breadth", "market_volume",
    ],
}


# ── EvalContext 构建辅助 ───────────────────────

def _build_eval_ctx(tickers: set[str], sectors: set[str]) -> EvalContext:
    """为测试构建 EvalContext，预取所有需要的数据。"""
    market.begin_cycle()

    ticker_data: dict[str, dict] = {}
    for t in tickers:
        try:
            tech = market.get_technical_indicators_cached(t)
            snap = market.get_intraday_snapshot_cached(t)
            price = market.get_realtime_price(t)
            ticker_data[t] = {
                "tech": tech if isinstance(tech, dict) else {},
                "snapshot": snap if isinstance(snap, dict) else {},
                "price": price if isinstance(price, dict) else {},
                "turnover": market.get_turnover_rate(t),
                "zdt_record": market.get_zdt_record(t) or {},
                "history": market.get_price_history(t, None, None) or {},
                "bars_1d": market.get_price_history(t, None, None) or {},
            }
        except Exception as e:
            ticker_data[t] = {"tech": {}, "snapshot": {"error": str(e)}, "price": {}}

    sector_data: dict[str, dict] = {}
    for s in sectors:
        try:
            overview = market.get_sector_overview_cached(s)
            concept_code = overview.get("concept_code", "")
            members: list[str] = []
            leader: dict = {}
            intraday: dict = {}
            volume_ratio: dict = {}
            if concept_code:
                members = market.get_concept_members(concept_code)
                leader = market.get_sector_leader(concept_code)
                intraday = market.get_sector_intraday(concept_code, True)
                try:
                    volume_ratio = market.get_sector_volume_ratio(concept_code, 5)
                except Exception:
                    pass
                # 预取板块成员股票的价格和涨跌停数据
                for member_ticker in members:
                    if member_ticker not in ticker_data:
                        try:
                            price = market.get_realtime_price(member_ticker)
                            ticker_data[member_ticker] = {
                                "price": price if isinstance(price, dict) else {},
                                "zdt_record": market.get_zdt_record(member_ticker),
                                "snapshot": market.get_intraday_snapshot_cached(member_ticker) or {},
                                "zdt_record": market.get_zdt_record(member_ticker),
                                "tech": {},
                                "turnover": None,
                                "history": {},
                            }
                        except Exception:
                            ticker_data[member_ticker] = {"price": {}, "zdt_record": {}}
            sector_data[s] = {
                "overview": overview,
                "members": members if isinstance(members, list) else [],
                "leader": leader if isinstance(leader, dict) else {},
                "intraday": intraday if isinstance(intraday, dict) else {},
                "volume_ratio": volume_ratio if isinstance(volume_ratio, dict) else {},
            }
        except Exception as e:
            sector_data[s] = {"overview": {"error": str(e)}, "members": [], "leader": {}, "intraday": {}, "volume_ratio": {}}

    ms = market.get_today_market_summary()
    breadth = ms.get("breadth", {})
    market_summary = {
        "up_down_ratio": breadth.get("up_down_ratio", ms.get("up_down_ratio", 1.0)),
        "avg_pct_chg": breadth.get("avg_pct_chg", ms.get("avg_pct_chg", 0.0)),
        "total_amount_yi": ms.get("total_amount_yi", 0.0),
    }

    return EvalContext(
        now=clock.now,
        ticker_data=ticker_data,
        sector_data=sector_data,
        market_summary=market_summary,
    )


# ═══════════════════════════════════════════════════════════
# Part 1: 全部原子独立测试
# ═══════════════════════════════════════════════════════════

async def test_part1_all_atoms():
    """逐个测试全部 23 个原子，验证无异常"""
    results: dict[str, dict] = {}
    total = 0
    passed = 0
    errors = 0

    ctx = _build_eval_ctx(
        {TEST_TICKER},
        {TEST_CONCEPT_NAME},
    )

    logger.info("=" * 60)
    logger.info("  Part 1: 全部 22 个原子独立测试 (v3)")
    logger.info(f"  Clock: {clock.now}")
    logger.info("=" * 60)

    for category, atoms in ATOM_CATEGORIES.items():
        logger.info(f"\n{'─' * 60}")
        logger.info(f"  {category} ({len(atoms)} atoms)")
        logger.info(f"{'─' * 60}")

        for atom_name in atoms:
            total += 1
            params = atom_params(atom_name)

            if atom_name in ("time_after", "time_window", "time_before"):
                params["created_at"] = "2026-05-24T00:00:00"
                params["now"] = clock.now.isoformat()

            try:
                result = evaluate_atom(atom_name, params, ctx)
            except Exception as e:
                result = {"atom": atom_name, "triggered": False, "error": str(e)}

            triggered = result.get("triggered", False)
            has_error = "error" in result
            detail = result.get("detail", result.get("error", ""))

            if has_error:
                errors += 1
                status = "ERROR"
            else:
                passed += 1
                status = "PASS"

            if isinstance(detail, dict):
                info = ", ".join(
                    f"{k}={v}" for k, v in detail.items()
                    if k in ("triggered", "reason", "pct_chg", "actual_pct", "ratio", "limit_count",
                             "volume_ratio", "up_down_ratio", "position", "pattern", "mode",
                             "price_position", "slope_direction", "monotonic", "direction")
                )
            else:
                info = str(detail)

            trig = "TRIG" if triggered else "    "
            logger.info(f"  [{status}] {trig} {atom_name:30s} | {info[:100]}")

            results[atom_name] = result

    logger.info(f"\n{'─' * 60}")
    logger.info(f"  Part 1 Summary: {passed} passed, {errors} errors (total {total})")
    logger.info(f"{'─' * 60}")

    return results, passed, errors


# ═══════════════════════════════════════════════════════════
# Part 2: 条件树组合测试 (v2 atoms)
# ═══════════════════════════════════════════════════════════

def _collect_atoms(tree: dict) -> dict[str, tuple[str, dict]]:
    """内联版 _collect_atoms（路径匹配，不修改原树），方便 Part 2 和 Part 3 使用"""
    atoms: dict[str, tuple[str, dict]] = {}

    def _walk(node, prefix: str):
        if "atom" in node:
            atoms[prefix] = (node["atom"], node.get("params", {}))
        for i, child in enumerate(node.get("children", [])):
            child_prefix = f"{prefix}.{i}" if prefix else str(i)
            _walk(child, child_prefix)

    if "logic" in tree and "children" in tree:
        for i, child in enumerate(tree["children"]):
            _walk(child, str(i))
    elif "atom" in tree:
        _walk(tree, "0")

    return atoms


def test_part2_trees():
    """测试条件树组合 (v2 atoms)"""
    logger.info("\n" + "=" * 60)
    logger.info("  Part 2: 条件树组合测试 (v3)")
    logger.info(f"  Clock: {clock.now}")
    logger.info("=" * 60)

    test_num = 0
    passed = 0

    def eval_tree(tree: dict) -> tuple[bool, dict]:
        atoms_in_tree = _collect_atoms(tree)
        eval_tickers: set[str] = set()
        eval_sectors: set[str] = set()
        for atom_name, params in atoms_in_tree.values():
            if "ticker" in params:
                eval_tickers.add(params["ticker"])
            for sk in ("sector", "sector_a", "sector_b"):
                if sk in params:
                    eval_sectors.add(params[sk])

        ctx = _build_eval_ctx(eval_tickers, eval_sectors)

        atom_results: dict[str, bool] = {}
        detail: dict[str, dict] = {}

        for path, (atom_name, params) in atoms_in_tree.items():
            result = evaluate_atom(atom_name, params, ctx)
            atom_results[path] = result.get("triggered", False)
            detail[path] = result

        # 标注路径并评估
        from src.triggers.engine import _annotate_paths
        annotated_tree = _annotate_paths(tree)
        tree_result = evaluate_condition_tree(annotated_tree, atom_results)
        return tree_result, detail

    # ── 测试 1: 简单 AND (v2 atoms) ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Simple AND — price_vs_level above 5.0 AND volume_ratio above 1.5x")
    tree_and = {
        "logic": "AND",
        "children": [
            {"atom": "price_vs_level", "params": {"ticker": TEST_TICKER, "level": 5.0, "relation": "above"}},
            {"atom": "volume_ratio", "params": {"ticker": TEST_TICKER, "multiplier": 1.5, "relation": "above"}},
        ],
    }
    triggered, detail = eval_tree(tree_and)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 2: 简单 OR ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Simple OR — price_move up OR down")
    tree_or = {
        "logic": "OR",
        "children": [
            {"atom": "price_move", "params": {"ticker": TEST_TICKER, "pct": 1.0, "direction": "up"}},
            {"atom": "price_move", "params": {"ticker": TEST_TICKER, "pct": 1.0, "direction": "down"}},
        ],
    }
    triggered, detail = eval_tree(tree_or)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 3: 嵌套 AND+OR (v3) ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Nested — (price_vs_level AND volume_ratio) OR (ma_slope down AND ma_alignment bearish)")
    tree_nested = {
        "logic": "OR",
        "children": [
            {
                "logic": "AND",
                "children": [
                    {"atom": "price_vs_level", "params": {"ticker": TEST_TICKER, "level": 5.0, "relation": "above"}},
                    {"atom": "volume_ratio", "params": {"ticker": TEST_TICKER, "multiplier": 1.5, "relation": "above"}},
                ],
            },
            {
                "logic": "AND",
                "children": [
                    {"atom": "ma_slope", "params": {"ticker": TEST_TICKER, "period": "MA20", "direction": "down"}},
                    {"atom": "ma_alignment", "params": {"ticker": TEST_TICKER, "pattern": "bearish"}},
                ],
            },
        ],
    }
    triggered, detail = eval_tree(tree_nested)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 4: 时间 + 价格 ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Time + Price — time_window AND price_move")
    tree_time = {
        "logic": "AND",
        "children": [
            {"atom": "time_window", "params": {"days_min": 3, "days_max": 10}},
            {"atom": "price_move", "params": {"ticker": TEST_TICKER, "pct": 3.0, "direction": "up"}},
        ],
    }
    triggered, detail = eval_tree(tree_time)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 5: 市场 + 板块 (v2 atoms) ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Multi — market_breadth AND sector_move AND sector_breadth")
    tree_market = {
        "logic": "AND",
        "children": [
            {"atom": "market_breadth", "params": {"up_down_ratio_min": 0.5}},
            {"atom": "sector_move", "params": {"sector": TEST_CONCEPT_NAME, "pct": 1.0, "direction": "up"}},
            {"atom": "sector_breadth", "params": {"sector": TEST_CONCEPT_NAME, "up_ratio_min": 0.3}},
        ],
    }
    triggered, detail = eval_tree(tree_market)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 6: 量价组合 + 均线方向 (v3) ──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: price_move up AND volume_ratio above 1.0 AND ma_slope up")
    tree_semantic = {
        "logic": "AND",
        "children": [
            {"atom": "price_move", "params": {"ticker": TEST_TICKER, "pct": 0.5, "direction": "up"}},
            {"atom": "volume_ratio", "params": {"ticker": TEST_TICKER, "multiplier": 1.0, "relation": "above"}},
            {"atom": "ma_slope", "params": {"ticker": TEST_TICKER, "period": "MA5", "direction": "up"}},
        ],
    }
    triggered, detail = eval_tree(tree_semantic)
    for k, v in detail.items():
        trig = "TRIG" if v.get("triggered") else "    "
        logger.info(f"    {trig} {k}: {v.get('detail', v.get('error', ''))}")
    logger.info(f"  Tree result: triggered={triggered}")
    if "error" not in str(detail):
        passed += 1
        logger.info(f"  [PASS]")

    # ── 测试 7: 边缘情况（纯逻辑）──
    test_num += 1
    logger.info(f"\n  Test 2.{test_num}: Edge cases — empty children, unknown logic, missing atom_key")

    r = evaluate_condition_tree({"logic": "AND", "children": []}, {})
    assert r is False, f"empty children should be False, got {r}"

    r = evaluate_condition_tree({"logic": "XOR", "children": [{"atom_key": "a"}]}, {"a": True})
    assert r is False, f"unknown logic should be False, got {r}"

    r = evaluate_condition_tree({"atom_key": "missing"}, {})
    assert r is False, f"missing atom_key should be False, got {r}"

    passed += 1
    logger.info(f"  [PASS] All 3 edge cases correct (all returned False)")

    logger.info(f"\n{'─' * 60}")
    logger.info(f"  Part 2 Summary: {passed}/{test_num} test groups passed")
    logger.info(f"{'─' * 60}")
    return passed == test_num


# ═══════════════════════════════════════════════════════════
# Part 3: _collect_atoms 算法测试 (v2 atoms)
# ═══════════════════════════════════════════════════════════

def test_part3_collect_atoms():
    """测试 _collect_atoms 的 key 分配逻辑"""
    logger.info("\n" + "=" * 60)
    logger.info("  Part 3: _collect_atoms 算法测试")
    logger.info("=" * 60)

    test_num = 0
    passed = 0

    # ── 3.1: 基本 key 分配 ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Basic key assignment (v2 atoms)")
    tree_simple = {
        "logic": "AND",
        "children": [
            {"atom": "price_vs_level", "params": {"ticker": "000001.SZ"}},
            {"atom": "volume_ratio", "params": {"ticker": "000001.SZ"}},
        ],
    }
    atoms = _collect_atoms(tree_simple)
    keys = sorted(atoms.keys())
    assert len(keys) == 2, f"expected 2 atoms, got {len(keys)}"
    # path-based keys: "0", "1"
    assert keys[0] == "0", f"expected '0', got {keys[0]}"
    assert keys[1] == "1", f"expected '1', got {keys[1]}"
    assert atoms["0"] == ("price_vs_level", {"ticker": "000001.SZ"})
    assert atoms["1"] == ("volume_ratio", {"ticker": "000001.SZ"})
    passed += 1
    logger.info(f"  [PASS] keys={keys}")

    # ── 3.2: 同名原子不同参数 → 不同 key ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Same atom name, different params → different keys")
    tree_dup = {
        "logic": "AND",
        "children": [
            {"atom": "price_move", "params": {"ticker": "000001.SZ", "pct": 1.0, "direction": "up"}},
            {"atom": "price_move", "params": {"ticker": "000001.SZ", "pct": 1.0, "direction": "down"}},
        ],
    }
    atoms = _collect_atoms(tree_dup)
    keys = sorted(atoms.keys())
    assert len(keys) == 2, f"expected 2 atoms, got {len(keys)}"
    assert keys[0] == "0"
    assert keys[1] == "1"
    assert atoms["0"][1]["direction"] == "up"
    assert atoms["1"][1]["direction"] == "down"
    assert keys[0] != keys[1], "same atom name should get different keys"
    passed += 1
    logger.info(f"  [PASS] {atoms[keys[0]]} / {atoms[keys[1]]}")

    # ── 3.3: 嵌套树遍历完整 (v3 atoms) ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Nested tree full traversal (v3 atoms)")
    tree_nested = {
        "logic": "OR",
        "children": [
            {
                "logic": "AND",
                "children": [
                    {"atom": "price_vs_level", "params": {"level": 10.0}},
                    {"atom": "volume_ratio", "params": {"multiplier": 1.5}},
                ],
            },
            {
                "logic": "AND",
                "children": [
                    {"atom": "ma_slope", "params": {"period": "MA20"}},
                    {"atom": "ma_alignment", "params": {"pattern": "bullish"}},
                ],
            },
        ],
    }
    atoms = _collect_atoms(tree_nested)
    assert len(atoms) == 4, f"expected 4 atoms, got {len(atoms)}"
    atom_names = sorted(name for name, _ in atoms.values())
    assert atom_names == ["ma_alignment", "ma_slope", "price_vs_level", "volume_ratio"]
    # path-based: "0.0", "0.1", "1.0", "1.1"
    assert atoms["0.0"] == ("price_vs_level", {"level": 10.0})
    assert atoms["0.1"] == ("volume_ratio", {"multiplier": 1.5})
    assert atoms["1.0"] == ("ma_slope", {"period": "MA20"})
    assert atoms["1.1"] == ("ma_alignment", {"pattern": "bullish"})
    passed += 1
    logger.info(f"  [PASS] Found all 4 atoms across nested tree: {atom_names}")

    # ── 3.4: 单叶子 ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Single leaf node")
    tree_leaf = {"atom": "price_vs_level", "params": {"ticker": "000001.SZ", "level": 10.0}}
    atoms = _collect_atoms(tree_leaf)
    assert len(atoms) == 1
    assert "0" in atoms
    passed += 1
    logger.info(f"  [PASS] Single leaf: {atoms}")

    # ── 3.5: 深层嵌套 ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Deeply nested tree")
    tree_deep = {
        "logic": "AND",
        "children": [
            {"atom": "gap", "params": {}},
            {
                "logic": "OR",
                "children": [
                    {"atom": "consecutive_move", "params": {}},
                    {
                        "logic": "AND",
                        "children": [
                            {"atom": "intraday_reversal", "params": {}},
                            {"atom": "intraday_trend", "params": {}},
                        ],
                    },
                ],
            },
            {"atom": "new_extreme", "params": {}},
        ],
    }
    atoms = _collect_atoms(tree_deep)
    assert len(atoms) == 5
    all_keys = sorted(atoms.keys())
    assert all_keys == ["0", "1.0", "1.1.0", "1.1.1", "2"]  # path order
    passed += 1
    logger.info(f"  [PASS] Found all 5 atoms in deep tree: {all_keys}")

    # ── 3.6: _collect_atoms 与 TriggerEngine 内部一致 ──
    test_num += 1
    logger.info(f"\n  Test 3.{test_num}: Consistency with TriggerEngine._collect_atoms (path-based)")
    tree = {
        "logic": "AND",
        "children": [
            {"atom": "price_vs_level", "params": {"ticker": "000001.SZ", "level": 5.0}},
            {"atom": "volume_ratio", "params": {"ticker": "000001.SZ"}},
        ],
    }
    dummy_on_trigger = lambda t: None
    engine = TriggerEngine(market, dummy_on_trigger)
    engine_atoms = engine._collect_atoms(tree)

    tree2 = {
        "logic": "AND",
        "children": [
            {"atom": "price_vs_level", "params": {"ticker": "000001.SZ", "level": 5.0}},
            {"atom": "volume_ratio", "params": {"ticker": "000001.SZ"}},
        ],
    }
    our_atoms = _collect_atoms(tree2)

    assert len(engine_atoms) == len(our_atoms) == 2
    assert set(engine_atoms.keys()) == {"0", "1"}
    assert set(our_atoms.keys()) == {"0", "1"}
    assert engine_atoms["0"] == our_atoms["0"] == ("price_vs_level", {"ticker": "000001.SZ", "level": 5.0})
    assert engine_atoms["1"] == our_atoms["1"] == ("volume_ratio", {"ticker": "000001.SZ"})
    passed += 1
    logger.info(f"  [PASS] Inline _collect_atoms matches TriggerEngine._collect_atoms (path-based)")

    logger.info(f"\n{'─' * 60}")
    logger.info(f"  Part 3 Summary: {passed}/{test_num} test groups passed")
    logger.info(f"{'─' * 60}")
    return passed == test_num


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

async def main():
    # Part 1
    part1_results, p1_passed, p1_errors = await test_part1_all_atoms()
    part1_ok = p1_errors == 0

    # Part 2
    part2_ok = test_part2_trees()

    # Part 3
    part3_ok = test_part3_collect_atoms()

    # ── 最终总结 ──
    logger.info("\n" + "=" * 60)
    logger.info("  FINAL SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Part 1 (22 atoms):  {'PASS' if part1_ok else 'FAIL'}  ({p1_passed} ok, {p1_errors} errors)")
    logger.info(f"  Part 2 (trees):     {'PASS' if part2_ok else 'FAIL'}")
    logger.info(f"  Part 3 (collect):   {'PASS' if part3_ok else 'FAIL'}")
    logger.info(f"{'=' * 60}")
    all_ok = part1_ok and part2_ok and part3_ok
    logger.info(f"  OVERALL: {'ALL PASSED' if all_ok else 'SOME FAILED'}")
    logger.info(f"{'=' * 60}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
