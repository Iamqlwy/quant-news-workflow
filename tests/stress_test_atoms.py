"""触发器原子评估压力测试 —— 10000 个原子，使用智能电网全部成员作为股票池"""
from __future__ import annotations

import asyncio
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import evaluate_atom
from src.triggers.engine import TriggerEngine, _ATOM_TICKER_KEYS, _ATOM_SECTOR_KEYS

settings.simulation_enabled = True

# ── 初始化 ──────────────────────────────────

clock_config = TimeConfig(
    start_time=datetime(2026, 5, 27, 11, 30, 0),
    tick_duration=timedelta(minutes=5),
    realtime=False,
)
clock = Clock(clock_config)
market = MarketDataProvider(clock=clock)

TEST_CONCEPT_NAME = "智能电网"
random.seed(42)

# ── 预构建 EvalContext（含全部板块成员） ──────

logger.info("=" * 60)
logger.info("  初始化 MarketDataProvider + 预取板块成员数据")
logger.info("=" * 60)

market.begin_cycle()

# 获取板块成员列表
overview = market.get_sector_overview_cached(TEST_CONCEPT_NAME)
concept_code = overview.get("concept_code", "")
members = market.get_concept_members(concept_code) if concept_code else []
if not members:
    logger.error("板块成员为空！")
    sys.exit(1)

logger.info(f"  智能电网成员数: {len(members)}")

# 用第一个成员作为参考 ticker，预取完整数据
ref_ticker = members[0]
logger.info(f"  参考 ticker: {ref_ticker}")

ref_tech = market.get_technical_indicators_cached(ref_ticker)
ref_snap = market.get_intraday_snapshot_cached(ref_ticker)
ref_price = asyncio.run(market.get_realtime_price(ref_ticker))
ref_turnover = market.get_turnover_rate(ref_ticker)
ref_zdt = market.get_zdt_record(ref_ticker) or {}
ref_history = market.get_price_history(ref_ticker, None, None) or {}

assert isinstance(ref_tech, dict) and ref_tech, "技术指标数据为空"
assert isinstance(ref_snap, dict) and "bars" in ref_snap, "日内快照数据为空"
assert isinstance(ref_price, dict) and ref_price, "实时价格数据为空"

logger.info(f"  ref_price: {ref_price}")
logger.info(f"  ref_tech keys: {sorted(ref_tech.keys())}")
logger.info(f"  snapshot bars: {len(ref_snap.get('bars', []))}")
logger.info(f"  history records: {ref_history.get('count', 0)}")

# 为每个成员构建 ticker_data（共享同一个参考数据，模拟真实 dict 查找）
ticker_data: dict[str, dict] = {}
for t in members:
    ticker_data[t] = {
        "tech": ref_tech,
        "snapshot": ref_snap,
        "price": {**ref_price, "ticker": t},  # 替换 ticker 名，模拟不同股票
        "turnover": ref_turnover,
        "zdt_record": ref_zdt,
        "history": ref_history,
    }

logger.info(f"  ticker_data 条目数: {len(ticker_data)}")

# 预取板块数据
sector_leader = market.get_sector_leader(concept_code) if concept_code else {}
sector_intraday = market.get_sector_intraday(concept_code, True) if concept_code else {}
try:
    sector_vol_ratio = market.get_sector_volume_ratio(concept_code, 5)
except Exception:
    sector_vol_ratio = {}

ms = market.get_today_market_summary()
breadth = ms.get("breadth", {})

# 完整 EvalContext
ctx = EvalContext(
    now=clock.now,
    ticker_data=ticker_data,
    sector_data={
        TEST_CONCEPT_NAME: {
            "overview": overview,
            "members": members,
            "leader": sector_leader,
            "intraday": sector_intraday,
            "volume_ratio": sector_vol_ratio,
        },
    },
    market_summary={
        "up_down_ratio": breadth.get("up_down_ratio", 1.0),
        "avg_pct_chg": breadth.get("avg_pct_chg", 0.0),
        "total_amount_yi": ms.get("total_amount_yi", 0.0),
    },
)

logger.info("  EvalContext 构建完成\n")


# ── 股票池 ──────────────────────────────────

STOCK_POOL = members  # 266 只股票


def random_ticker() -> str:
    return random.choice(STOCK_POOL)


# ── 参数生成 ──────────────────────────────────

def make_params(atom_name: str, ticker: str | None = None) -> dict:
    """为给定 atom 构造有效参数，ticker 从股票池中随机选取。"""
    t = ticker or random_ticker()
    return {
        # 价格状态 (v2)
        "price_move": {"ticker": t, "pct": 1.0, "direction": "up"},
        "price_vs_level": {"ticker": t, "level": 5.0, "relation": "above"},
        "new_extreme": {"ticker": t, "direction": "high", "n_days": 20},
        "gap": {"ticker": t, "direction": "up", "min_pct": 2.0},
        "consecutive_move": {"ticker": t, "direction": "up", "n_days": 3},
        # 量价关系 (v2)
        "volume_ratio": {"ticker": t, "multiplier": 1.5, "relation": "above"},
        "volume_price_direction": {"ticker": t, "mode": "confirmed"},
        "turnover_active": {"ticker": t, "pct": 5.0, "relation": "above"},
        "amplitude_wide": {"ticker": t, "pct": 3.0, "relation": "above"},
        # 趋势结构 (v2)
        "ma_status": {"ticker": t, "period": "MA20", "price_position": "above"},
        "ma_cross": {"ticker": t, "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"},
        "ma_alignment": {"ticker": t, "pattern": "bullish"},
        # 日内动态 (v2)
        "intraday_reversal": {"ticker": t, "pattern": "shot_up_fall", "move_pct": 3, "retrace_ratio": 50},
        "intraday_round_trip": {"ticker": t, "direction": "A", "min_move_pct": 2, "tolerance_pct": 0.5},
        "intraday_trend": {"ticker": t, "direction": "up", "minutes": 5, "min_pct": 1},
        # 板块与市场 (v2)
        "sector_move": {"sector": TEST_CONCEPT_NAME, "pct": 1.0, "direction": "up"},
        "sector_breadth": {"sector": TEST_CONCEPT_NAME, "up_ratio_min": 0.5},
        "sector_limit_ratio": {"sector": TEST_CONCEPT_NAME, "direction": "up", "min_count": 1},
        "market_breadth": {"up_down_ratio_min": 1.0},
        "market_volume": {"amount_yi": 5000, "relation": "above"},
        # 时间 (v2)
        "time_after": {"days": 3, "created_at": "2026-05-24T00:00:00"},
        "time_window": {"days_min": 3, "days_max": 10, "created_at": "2026-05-24T00:00:00"},
        "time_before": {"days": 10, "created_at": "2026-05-24T00:00:00"},
    }.get(atom_name, {"ticker": t, "pct": 1.0, "direction": "up"})


# ═══════════════════════════════════════════════════════════
# 场景 1: 10000 个简单 atom（纯 dict 读取，266 只不同股票）
# ═══════════════════════════════════════════════════════════

def scenario_1_simple():
    """10000 个 price_move，尽量用不同股票。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 1: 10000 个简单 atom（price_move，{len(STOCK_POOL)} 只股票）")
    logger.info("=" * 60)

    N = 10000
    atoms = []
    for i in range(N):
        t = STOCK_POOL[i % len(STOCK_POOL)]  # 循环使用全部 266 只股票
        atoms.append((
            f"price_move__{i}",
            "price_move",
            {"ticker": t, "pct": 0.1 + (i % 100) * 0.1, "direction": "up" if i % 2 == 0 else "down"},
        ))

    t0 = time.perf_counter()
    for _, name, params in atoms:
        evaluate_atom(name, params, ctx)
    elapsed = time.perf_counter() - t0

    unique_tickers = len(set(p["ticker"] for _, _, p in atoms))
    ops = N / elapsed
    logger.info(f"  总数: {N}, 不同股票: {unique_tickers}")
    logger.info(f"  耗时: {elapsed*1000:.1f}ms")
    logger.info(f"  吞吐: {ops:,.0f} atoms/s")
    logger.info(f"  单次: {elapsed / N * 1_000_000:.1f} μs")
    return elapsed


# ═══════════════════════════════════════════════════════════
# 场景 2: 10000 个复杂 atom（遍历 1m bars，不同股票）
# ═══════════════════════════════════════════════════════════

def scenario_2_complex():
    """10000 个 intraday 形态 atom，不同股票。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 2: 10000 个复杂 atom（intraday 形态，{len(STOCK_POOL)} 只股票）")
    logger.info("=" * 60)

    N = 10000
    intraday_types = [
        "intraday_reversal", "intraday_round_trip", "intraday_trend",
    ]
    atoms = []
    for i in range(N):
        name = intraday_types[i % 3]
        t = STOCK_POOL[i % len(STOCK_POOL)]
        atoms.append((f"intra_{i}", name, make_params(name, t)))

    t0 = time.perf_counter()
    for _, name, params in atoms:
        evaluate_atom(name, params, ctx)
    elapsed = time.perf_counter() - t0

    unique_tickers = len(set(p["ticker"] for _, _, p in atoms))
    ops = N / elapsed
    logger.info(f"  总数: {N}, 不同股票: {unique_tickers}")
    logger.info(f"  耗时: {elapsed*1000:.1f}ms")
    logger.info(f"  吞吐: {ops:,.0f} atoms/s")
    logger.info(f"  单次: {elapsed / N * 1_000_000:.1f} μs")
    return elapsed


# ═══════════════════════════════════════════════════════════
# 场景 3: 10000 个混合 atom（全部 266 只股票的 n× 遍历）
# ═══════════════════════════════════════════════════════════

def scenario_3_mixed():
    """10000 个混合 atom，20 种类型 × 不同股票。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 3: 10000 个混合 atom（{len(STOCK_POOL)} 只股票）")
    logger.info("=" * 60)

    N = 10000
    all_types = [
        "price_move", "volume_ratio", "gap",
        "ma_status", "ma_cross", "ma_alignment",
        "volume_price_direction", "amplitude_wide",
        "sector_move", "sector_breadth", "market_breadth", "market_volume",
        "intraday_reversal", "intraday_round_trip", "intraday_trend",
        "new_extreme", "consecutive_move", "price_vs_level",
        "turnover_active",
        "time_after", "time_window",
    ]

    atoms = []
    for i in range(N):
        name = all_types[i % len(all_types)]
        t = STOCK_POOL[i % len(STOCK_POOL)]
        atoms.append((f"a{i}", name, make_params(name, t)))

    t0 = time.perf_counter()
    for _, name, params in atoms:
        evaluate_atom(name, params, ctx)
    elapsed = time.perf_counter() - t0

    unique_tickers = len(set(
        p.get("ticker", p.get("sector", ""))
        for _, _, p in atoms
    ))
    ops = N / elapsed
    logger.info(f"  总数: {N}, 不同股票+板块: {unique_tickers}")
    logger.info(f"  耗时: {elapsed*1000:.1f}ms")
    logger.info(f"  吞吐: {ops:,.0f} atoms/s")
    logger.info(f"  单次: {elapsed / N * 1_000_000:.1f} μs")
    return elapsed


# ═══════════════════════════════════════════════════════════
# 场景 4: 去重 —— 10000 个 atom，不同股票 × 不同参数
# ═══════════════════════════════════════════════════════════

def scenario_4_dedup():
    """模拟引擎去重：10000 次 dedup_key 计算 + 不同股票组合。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 4: 10000 个 atom + 去重（{len(STOCK_POOL)} 只股票 × 200 种参数）")
    logger.info("=" * 60)

    N = 10000
    UNIQUE_SPECS = 200

    all_atom_types = [
        "price_move", "volume_ratio", "ma_status", "ma_cross", "ma_alignment",
        "volume_price_direction", "gap", "amplitude_wide",
        "intraday_reversal", "intraday_round_trip", "intraday_trend",
        "new_extreme", "consecutive_move", "price_vs_level",
        "sector_move", "sector_breadth", "turnover_active",
    ]

    # 生成 200 种不同的参数组合
    unique_pool = []
    for i in range(UNIQUE_SPECS):
        name = all_atom_types[i % len(all_atom_types)]
        t = STOCK_POOL[i % len(STOCK_POOL)]  # 用不同股票增加唯一性
        params = make_params(name, t)
        if "pct" in params:
            params = dict(params)
            params["pct"] = 0.1 + (i % 50) * 0.2
        if "multiplier" in params:
            params = dict(params)
            params["multiplier"] = 1.0 + (i % 30) * 0.1
        unique_pool.append((name, params))

    # 从池中重复选取 10000 次
    atoms = [(f"a{i}", unique_pool[i % UNIQUE_SPECS][0], unique_pool[i % UNIQUE_SPECS][1])
             for i in range(N)]

    # 模拟引擎的去重流程
    t0 = time.perf_counter()

    # Phase A: 计算 dedup key
    unique_map: dict[tuple, bool] = {}
    for _, name, params in atoms:
        key = (name, frozenset(params.items()))
        if key not in unique_map:
            unique_map[key] = False

    dedup_time = time.perf_counter() - t0

    # Phase B: 评估唯一 atom
    t1 = time.perf_counter()
    for key in list(unique_map):
        name, _ = key
        params_dict = dict(key[1])
        result = evaluate_atom(name, params_dict, ctx)
        unique_map[key] = result.get("triggered", False)
    eval_time = time.perf_counter() - t1

    # Phase C: 查表
    t2 = time.perf_counter()
    for _, name, params in atoms:
        key = (name, frozenset(params.items()))
        _ = unique_map[key]
    lookup_time = time.perf_counter() - t2

    total_time = time.perf_counter() - t0
    logger.info(f"  总数: {N}, 唯一组合: {len(unique_map)}")
    logger.info(f"  去重计算: {dedup_time*1000:.1f}ms")
    logger.info(f"  实际评估: {eval_time*1000:.1f}ms ({len(unique_map)} 次)")
    logger.info(f"  结果查询: {lookup_time*1000:.1f}ms")
    logger.info(f"  总耗时:   {total_time*1000:.1f}ms")
    logger.info(f"  等效吞吐: {N / total_time:,.0f} atoms/s")
    return total_time


# ═══════════════════════════════════════════════════════════
# 场景 5: sector_limit_ratio —— 遍历全部 266 个成员
# ═══════════════════════════════════════════════════════════

def scenario_5_sector():
    """sector_limit_ratio，context 中已预取全部 266 个成员的 zdt_record。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 5: sector_limit_ratio（遍历 {len(members)} 个成员）")
    logger.info("=" * 60)

    N = 500  # 板块类原子较慢
    atoms = [(f"s{i}", "sector_limit_ratio",
              {"sector": TEST_CONCEPT_NAME, "limit_type": "涨停池" if i % 2 == 0 else "跌停池", "count": 1})
             for i in range(N)]

    # 预热
    evaluate_atom("sector_limit_ratio",
                  {"sector": TEST_CONCEPT_NAME, "limit_type": "涨停池", "count": 1}, ctx)

    t0 = time.perf_counter()
    for _, name, params in atoms:
        evaluate_atom(name, params, ctx)
    elapsed = time.perf_counter() - t0

    ops = N / elapsed
    logger.info(f"  总数: {N}, 成员数: {len(members)}")
    logger.info(f"  耗时: {elapsed*1000:.1f}ms")
    logger.info(f"  吞吐: {ops:,.0f} atoms/s")
    logger.info(f"  单次: {elapsed / N * 1_000_000:.0f} μs")
    logger.info(f"  等效 10000 耗时: {elapsed / N * 10000:.1f}s")
    return elapsed


# ═══════════════════════════════════════════════════════════
# 场景 6: 引擎需求分析 —— 10000 个 atom 节点分布在不同股票上
# ═══════════════════════════════════════════════════════════

def scenario_6_engine_flow():
    """模拟引擎完整需求分析：10000 个 atom 节点分布在 266 只股票上。"""
    logger.info("\n" + "=" * 60)
    logger.info(f"  场景 6: 引擎需求分析（10000 atom 节点, {len(STOCK_POOL)} 只股票）")
    logger.info("=" * 60)

    from src.triggers.atoms import ATOM_DEFINITIONS

    engine = TriggerEngine(market, lambda _t: None)

    all_atom_names = list(ATOM_DEFINITIONS.keys())
    # 只选用有 ticker 参数的 atom 类型
    ticker_atoms = [a for a in all_atom_names
                    if "ticker" in ATOM_DEFINITIONS[a]["params"]]

    # 模拟 3000 个 trigger，每个 ~3 个 atom ≈ 10000 个 atom 节点
    N_TRIGGERS = 3000
    atoms_per_trigger_range = (2, 5)

    trees = []
    total_nodes = 0
    for _ in range(N_TRIGGERS):
        n_atoms = random.randint(*atoms_per_trigger_range)
        children = []
        for _ in range(n_atoms):
            name = random.choice(ticker_atoms)
            t = random_ticker()
            params = make_params(name, t)
            children.append({"atom": name, "params": params})
        total_nodes += n_atoms
        trees.append({"logic": "AND", "children": children})

    logger.info(f"  模拟: {N_TRIGGERS} 个 trigger, 共 {total_nodes} 个 atom 节点")

    # 测量 _collect_atoms + 需求分析
    t0 = time.perf_counter()

    all_tickers: set[str] = set()
    all_sectors: set[str] = set()
    ticker_needs: dict[str, set[str]] = {}
    sector_needs: dict[str, set[str]] = {}

    for tree in trees:
        atoms = engine._collect_atoms(tree)
        for atom_name, params in atoms.values():
            ticker = params.get("ticker")
            if ticker:
                all_tickers.add(ticker)
                if ticker not in ticker_needs:
                    ticker_needs[ticker] = set()
                ticker_needs[ticker] |= _ATOM_TICKER_KEYS.get(atom_name, set())
            for sk in ("sector", "sector_a", "sector_b"):
                s = params.get(sk)
                if s:
                    all_sectors.add(s)
                    if s not in sector_needs:
                        sector_needs[s] = set()
                    sector_needs[s] |= _ATOM_SECTOR_KEYS.get(atom_name, set())

    collect_time = time.perf_counter() - t0

    # 统计每个 ticker 需要什么数据
    need_counts: dict[str, int] = {}
    for t, needs in ticker_needs.items():
        key = "+".join(sorted(needs))
        need_counts[key] = need_counts.get(key, 0) + 1

    logger.info(f"  唯一 ticker 数: {len(all_tickers)}, sector 数: {len(all_sectors)}")
    logger.info(f"  需求分布: {need_counts}")
    logger.info(f"  收集+分析耗时: {collect_time*1000:.1f}ms")
    logger.info(f"  等效每节点: {collect_time/total_nodes*1_000_000:.1f}μs")
    return collect_time


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info(f"  触发器原子评估压力测试")
    logger.info(f"  Clock: {clock.now}")
    logger.info(f"  股票池: 智能电网 ({len(STOCK_POOL)} 只)")
    logger.info("=" * 60)

    results = {}

    results["简单"] = scenario_1_simple()
    results["复杂"] = scenario_2_complex()
    results["混合"] = scenario_3_mixed()
    results["去重"] = scenario_4_dedup()
    results["板块"] = scenario_5_sector()
    results["引擎流"] = scenario_6_engine_flow()

    # ── 汇总 ──
    logger.info("\n" + "=" * 60)
    logger.info("  压力测试汇总（智能电网 {} 只股票）".format(len(STOCK_POOL)))
    logger.info("=" * 60)
    logger.info(f"  {'场景':<15} {'耗时':>10} {'备注'}")
    logger.info(f"  {'─' * 15} {'─' * 10} {'─' * 40}")
    logger.info(f"  {'简单(10000)':<15} {results['简单']*1000:>7.1f}ms   price_move, {len(STOCK_POOL)} 只不同股票")
    logger.info(f"  {'复杂(10000)':<15} {results['复杂']*1000:>7.1f}ms   intraday 遍历 1m bars, {len(STOCK_POOL)} 只")
    logger.info(f"  {'混合(10000)':<15} {results['混合']*1000:>7.1f}ms   20 种 atom × {len(STOCK_POOL)} 只股票")
    logger.info(f"  {'去重(10000)':<15} {results['去重']*1000:>7.1f}ms   {len(STOCK_POOL)} 只股票 × 200 参数组合")
    logger.info(f"  {'板块(500)':<15} {results['板块']*1000:>7.1f}ms   遍历 {len(members)} 成员, 等效10000={results['板块']/500*10000*1000:.0f}ms")
    logger.info(f"  {'引擎流(~{10000})':<15} {results['引擎流']*1000:>7.1f}ms   ~10000 atom 节点需求分析")
    logger.info("=" * 60)
