"""Probe MarketDataProvider APIs and trigger atoms at a fixed simulation time.

Default:
    python scripts/probe_market_provider_and_triggers.py

Outputs:
    data/probe/market_provider_trigger_probe_20260525_1030.json
    docs/market_provider_trigger_probe_20260525_1030.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import settings
from src.market import MarketDataProvider
from src.triggers.atoms import ATOM_SCHEMA, normalize_and_validate_tree
import src.triggers.engine as trigger_engine_module
from src.triggers.engine import TriggerEngine
from src.triggers.evaluators import EVALUATORS, evaluate_atom

DEFAULT_TIME = "2026-05-25 14:59:00"
DEFAULT_TICKER = "000001.SZ"
DEFAULT_TICKER_2 = "000002.SZ"
DEFAULT_STOCK_NAME = "平安银行"
DEFAULT_SECTOR = "半导体"
DEFAULT_INDEX_NAME = "上证指数"


class ProbeClock:
    """Simulation clock adapter for code that uses clock.now as an attribute."""

    def __init__(self, simulated_time: str | datetime | pd.Timestamp) -> None:
        ts = pd.Timestamp(simulated_time)
        if pd.isna(ts):
            raise ValueError(f"无效的模拟时间: {simulated_time}")
        self._now = ts.to_pydatetime()

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def today(self) -> date:
        return self._now.date()

    @property
    def today_str(self) -> str:
        return self.today.strftime("%Y%m%d")

    @property
    def minutes_since_midnight(self) -> int:
        return self.now.hour * 60 + self.now.minute

    @property
    def is_trading_session(self) -> bool:
        minutes = self.minutes_since_midnight
        return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)

    @property
    def is_pre_market(self) -> bool:
        return 0 <= self.minutes_since_midnight < 9 * 60 + 30

    @property
    def is_post_market(self) -> bool:
        return self.minutes_since_midnight > 15 * 60

    @property
    def phase(self) -> str:
        if self.is_pre_market:
            return "pre_market"
        if self.is_trading_session:
            return "trading"
        return "post_market"

    @property
    def is_realtime(self) -> bool:
        return False


def _now_value(clock: Any) -> datetime:
    now = clock.now
    return now() if callable(now) else now


async def _ordered_gather_with_progress(
    _label: str,
    coros: list,
    *,
    report_every: int = 10,
    return_exceptions: bool = True,
) -> list:
    """Stable replacement for the probe; preserves order and avoids as_completed key issues."""
    del report_every
    return await asyncio.gather(*coros, return_exceptions=return_exceptions)


def _json_safe(obj: Any, *, max_rows: int = 3, max_list: int = 20, depth: int = 0) -> Any:
    """Convert common market return values into compact JSON-safe summaries."""
    if depth > 5:
        return str(type(obj).__name__)

    if obj is None:
        return None
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if is_dataclass(obj):
        return _json_safe(asdict(obj), max_rows=max_rows, max_list=max_list, depth=depth + 1)
    if isinstance(obj, pd.DataFrame):
        if obj.empty:
            return {"type": "DataFrame", "empty": True, "shape": list(obj.shape), "columns": list(obj.columns)}
        return {
            "type": "DataFrame",
            "shape": list(obj.shape),
            "columns": list(obj.columns),
            "head": json.loads(obj.head(max_rows).to_json(orient="records", date_format="iso")),
            "tail": json.loads(obj.tail(max_rows).to_json(orient="records", date_format="iso")),
        }
    if isinstance(obj, pd.Series):
        return _json_safe(obj.to_dict(), max_rows=max_rows, max_list=max_list, depth=depth + 1)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:max_list]:
            out[str(k)] = _json_safe(v, max_rows=max_rows, max_list=max_list, depth=depth + 1)
        if len(obj) > max_list:
            out["..."] = f"{len(obj) - max_list} more keys"
        return out
    if isinstance(obj, (list, tuple, set)):
        values = list(obj)
        out = [_json_safe(v, max_rows=max_rows, max_list=max_list, depth=depth + 1) for v in values[:max_list]]
        if len(values) > max_list:
            out.append(f"... {len(values) - max_list} more items")
        return out
    if hasattr(obj, "item"):
        try:
            return _json_safe(obj.item(), max_rows=max_rows, max_list=max_list, depth=depth + 1)
        except (TypeError, ValueError):
            pass
    return str(obj)


def _preview(obj: Any) -> str:
    safe = _json_safe(obj, max_rows=2, max_list=6)
    text = json.dumps(safe, ensure_ascii=False, default=str)
    return text if len(text) <= 220 else text[:217] + "..."


def _status_from_result(result: Any) -> str:
    if isinstance(result, dict) and result.get("error"):
        return "error"
    return "ok"


def _timed_sync(api: str, params: dict[str, Any], description: str, fn: Callable[[], Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = fn()
        status = _status_from_result(result)
    except Exception as exc:
        result = {"error": str(exc), "type": type(exc).__name__}
        status = "exception"
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "api": api,
        "params": _json_safe(params),
        "description": description,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "result": _json_safe(result),
        "preview": _preview(result),
    }


async def _timed_async(api: str, params: dict[str, Any], description: str, awaitable: Any) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await awaitable
        status = _status_from_result(result)
    except Exception as exc:
        result = {"error": str(exc), "type": type(exc).__name__}
        status = "exception"
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "api": api,
        "params": _json_safe(params),
        "description": description,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "result": _json_safe(result),
        "preview": _preview(result),
    }


def _first_existing_ticker(provider: MarketDataProvider, preferred: str) -> str:
    if preferred in provider._cache.session.last_daily_ticker:  # noqa: SLF001 - probe script
        return preferred
    if provider._cache.session.last_daily_ticker:  # noqa: SLF001 - probe script
        return next(iter(provider._cache.session.last_daily_ticker.keys()))  # noqa: SLF001
    return preferred


def _first_existing_sector(provider: MarketDataProvider, preferred: str) -> tuple[str, str | None]:
    code = provider.resolve_sector_code(preferred)
    if code:
        return preferred, code

    for item in provider.get_concept_list("all"):
        name = str(item.get("name") or "")
        code = str(item.get("code") or "")
        if name and code:
            return name, code
    return preferred, None


def _first_existing_concept_code(provider: MarketDataProvider, fallback: str | None) -> str:
    if fallback:
        return fallback
    for item in provider.get_concept_list("all"):
        code = str(item.get("code") or "")
        if code:
            return code
    return ""


def _snapshot_from_bars(ticker: str, df: pd.DataFrame | None) -> dict[str, Any]:
    if df is None or df.empty:
        return {"error": f"无1分钟数据: {ticker}", "ticker": ticker}

    open_price = float(df["open"].iloc[0])
    latest_close = float(df["close"].iloc[-1])
    high = float(df["high"].max())
    low = float(df["low"].min())
    latest_pct = round((latest_close - open_price) / open_price * 100, 2) if open_price else 0.0
    high_pct = round((high - open_price) / open_price * 100, 2) if open_price else 0.0
    low_pct = round((low - open_price) / open_price * 100, 2) if open_price else 0.0
    bars = [
        {
            "open": float(r[0]),
            "high": float(r[1]),
            "low": float(r[2]),
            "close": float(r[3]),
            "volume": float(r[4]),
            "amount": float(r[5]),
        }
        for r in df[["open", "high", "low", "close", "volume", "amount"]].itertuples(index=False, name=None)
    ]
    return {
        "ticker": ticker,
        "price": latest_close,
        "open": open_price,
        "high": high,
        "low": low,
        "high_pct": high_pct,
        "low_pct": low_pct,
        "close": latest_close,
        "volume": float(df["volume"].sum()),
        "amount": float(df["amount"].sum()),
        "source": "1m",
        "available": True,
        "latest_pct": latest_pct,
        "bars": bars,
    }


async def probe_provider_apis(
    provider: MarketDataProvider,
    *,
    ticker: str,
    ticker_2: str,
    stock_name: str,
    sector: str,
    sector_code: str,
    concept_code: str,
    index_name: str,
    snapshot_date: str,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def add(api: str, params: dict[str, Any], description: str, fn: Callable[[], Any]) -> None:
        calls.append(_timed_sync(api, params, description, fn))

    async def add_async(api: str, params: dict[str, Any], description: str, awaitable: Any) -> None:
        calls.append(await _timed_async(api, params, description, awaitable))

    index_code = provider.resolve_index_name(index_name) or "000001.SH"

    add("clock", {}, "provider.clock 属性", lambda: {"now": str(_now_value(provider.clock)), "phase": provider.clock.phase})
    add("klines_path", {}, "行情数据根目录", lambda: str(provider.klines_path))
    add("trading_days", {}, "Session 内交易日窗口", lambda: provider.trading_days)
    add("is_trading_day", {}, "模拟日期是否交易日", lambda: provider.is_trading_day)
    add("xt_ready", {}, "XTQuant provider 是否就绪", lambda: provider.xt_ready)
    add("refresh", {"force": False}, "刷新 Session；同日默认跳过", lambda: provider.refresh(False))

    add("get_bars", {"ticker": ticker, "granularity": "1d"}, "个股日线，默认窗口", lambda: provider.get_bars(ticker))
    add(
        "get_bars",
        {"ticker": ticker, "granularity": "1d", "start": "2026-05-01", "end": snapshot_date},
        "个股日线，指定日期范围",
        lambda: provider.get_bars(ticker, "1d", "2026-05-01", snapshot_date),
    )
    add("get_bars", {"ticker": ticker, "granularity": "1m"}, "个股 1m，按模拟时间截断", lambda: provider.get_bars(ticker, "1m"))
    add("get_bars", {"ticker": ticker, "granularity": "5m"}, "个股 5m，基于 1m 重采样", lambda: provider.get_bars(ticker, "5m"))
    add(
        "get_bars",
        {"ticker": index_code, "granularity": "1d", "category": "index"},
        f"指数日线：{index_name}->{index_code}",
        lambda: provider.get_bars(index_code, "1d", None, None, "index"),
    )
    add(
        "get_bars",
        {"ticker": index_code, "granularity": "1m", "category": "index"},
        f"指数 1m：{index_name}->{index_code}",
        lambda: provider.get_bars(index_code, "1m", None, None, "index"),
    )
    add(
        "get_concept_kline",
        {"concept_code": concept_code, "granularity": "1d"},
        "概念/板块日线",
        lambda: provider.get_concept_kline(concept_code),
    )
    add(
        "get_price_history",
        {"ticker": ticker, "from_date": "2026-05-01", "to_date": snapshot_date},
        "历史价格摘要",
        lambda: provider.get_price_history(ticker, "2026-05-01", snapshot_date),
    )

    await add_async("get_realtime_price", {"ticker": ticker}, "单只股票实时/模拟价格", provider.get_realtime_price(ticker))
    await add_async(
        "get_realtime_prices",
        {"tickers": [ticker, ticker_2]},
        "批量实时/模拟价格",
        provider.get_realtime_prices([ticker, ticker_2]),
    )
    await add_async("get_technical_indicators", {"ticker": ticker}, "技术指标", provider.get_technical_indicators(ticker))
    add("get_turnover_rate", {"ticker": ticker}, "估算换手率", lambda: provider.get_turnover_rate(ticker))
    add("get_zdt_record", {"ticker": ticker}, "涨跌停记录", lambda: provider.get_zdt_record(ticker))

    await add_async("get_sector_overview", {"sector": sector}, "板块概览", provider.get_sector_overview(sector))
    add("get_sector_leader", {"sector_code": sector_code}, "板块龙头", lambda: provider.get_sector_leader(sector_code))
    add("get_sector_volume_ratio", {"sector_code": sector_code, "n": 5}, "板块量比", lambda: provider.get_sector_volume_ratio(sector_code, 5))
    add("get_concept_list", {"con_type": "all"}, "概念/行业/地域列表", lambda: provider.get_concept_list("all"))
    add("get_concept_members", {"concept_code": concept_code}, "概念成员", lambda: provider.get_concept_members(concept_code))
    add("get_stock_concepts", {"ticker": ticker}, "个股所属概念", lambda: provider.get_stock_concepts(ticker))
    add(
        "get_sector_intraday",
        {"sector_code": sector_code, "include_bars": True},
        "板块日内走势",
        lambda: provider.get_sector_intraday(sector_code, True),
    )

    add("get_market_snapshot", {"date": snapshot_date}, "全市场快照", lambda: provider.get_market_snapshot(snapshot_date))
    add("get_market_breadth", {}, "市场广度", lambda: provider.get_market_breadth())
    add("get_index_overview", {}, "主要指数概览", lambda: provider.get_index_overview())

    add("resolve_stock_ticker", {"name": stock_name}, "股票名称解析", lambda: provider.resolve_stock_ticker(stock_name))
    add("infer_stock_market", {"name": stock_name}, "股票市场推断", lambda: provider.infer_stock_market(stock_name))
    add("resolve_index_name", {"name": index_name}, "指数名称解析", lambda: provider.resolve_index_name(index_name))
    add("get_stock_name", {"ticker": ticker}, "股票代码转名称", lambda: provider.get_stock_name(ticker))
    add("resolve_sector_code", {"sector": sector}, "板块名称解析", lambda: provider.resolve_sector_code(sector))
    add(
        "get_classification",
        {},
        "分类数据 shape 摘要",
        lambda: {
            k: {"shape": list(v.shape), "columns": list(v.columns)}
            for k, v in provider.get_classification().items()
            if hasattr(v, "shape")
        },
    )

    return calls


def _default_atom_params(atom_name: str, ticker: str, sector: str) -> dict[str, Any]:
    return {
        "price_move": {"ticker": ticker, "direction": "up", "pct": 1, "lookback_days": 1},
        "price_vs_level": {"ticker": ticker, "level": "MA20", "relation": "above", "tolerance_pct": 1},
        "new_extreme": {"ticker": ticker, "direction": "high", "n_days": 20},
        "gap": {"ticker": ticker, "direction": "up", "min_pct": 1},
        "consecutive_move": {"ticker": ticker, "direction": "up", "n_days": 3},
        "volume_ratio": {"ticker": ticker, "multiplier": 1.2, "relation": "above", "n_days": 20},
        "turnover_active": {"ticker": ticker, "pct": 2, "relation": "above"},
        "amplitude_wide": {"ticker": ticker, "pct": 2, "relation": "above"},
        "ma_slope": {"ticker": ticker, "period": "MA20", "direction": "up"},
        "ma_cross": {"ticker": ticker, "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"},
        "ma_alignment": {"ticker": ticker, "pattern": "bullish"},
        "macd_cross": {"ticker": ticker, "direction": "golden"},
        "macd_divergence": {"ticker": ticker, "pattern": "bearish", "lookback_days": 5},
        "intraday_reversal": {"ticker": ticker, "pattern": "shot_up_fall", "move_pct": 2, "retrace_ratio": 50},
        "intraday_round_trip": {"ticker": ticker, "direction": "A", "min_move_pct": 2, "tolerance_pct": 0.5},
        "intraday_trend": {"ticker": ticker, "direction": "up", "minutes": 30, "min_pct": 1},
        "sector_move": {"sector": sector, "direction": "up", "pct": 1, "velocity_minutes": None},
        "sector_breadth": {"sector": sector, "up_ratio_min": 0.5},
        "sector_limit_ratio": {"sector": sector, "direction": "up", "min_count": 1},
        "market_breadth": {"up_down_ratio_min": 1.0, "avg_pct_min": None},
        "market_volume": {"amount_yi": 5000, "relation": "above"},
        "time_after": {"days": 1},
        "time_window": {"days_min": 1, "days_max": 5},
        "time_before": {"days": 5},
    }[atom_name]


def _make_trigger_record(condition: dict[str, Any]) -> Any:
    class ProbeTrigger:
        id = "probe"

        def __init__(self, condition: dict[str, Any]) -> None:
            self.condition = condition

    return ProbeTrigger(condition)


async def probe_trigger_atoms(provider: MarketDataProvider, *, ticker: str, sector: str) -> dict[str, Any]:
    atom_specs: list[dict[str, Any]] = []
    atom_records: list[dict[str, Any]] = []

    for atom_name, schema in ATOM_SCHEMA.items():
        params = _default_atom_params(atom_name, ticker, sector)
        tree, errors = normalize_and_validate_tree({"atom": atom_name, "params": params})
        normalized_params = tree.get("params", params)
        atom_specs.append(
            {
                "atom": atom_name,
                "description": schema.get("description", ""),
                "meta": bool(schema.get("meta")),
                "params": normalized_params,
                "validation_errors": errors,
            }
        )

        if schema.get("meta"):
            atom_records.append(
                {
                    "atom": atom_name,
                    "description": schema.get("description", ""),
                    "params": _json_safe(normalized_params),
                    "status": "meta_skipped",
                    "elapsed_ms": 0.0,
                    "triggered": None,
                    "result": {"reason": "meta atom: compiler extracts it; evaluator does not run it"},
                    "preview": "meta atom: compiler extracts it; evaluator does not run it",
                }
            )

    evaluable_specs = [s for s in atom_specs if not s["meta"] and s["atom"] in EVALUATORS]
    condition = {"logic": "AND", "children": [{"atom": s["atom"], "params": s["params"]} for s in evaluable_specs]}
    trigger = _make_trigger_record(condition)
    trigger_engine_module.gather_with_progress = _ordered_gather_with_progress
    engine = TriggerEngine(provider, on_trigger=lambda _trigger: None)
    tickers, sectors = engine._collect_all_entities([trigger])  # noqa: SLF001 - intentional probe of trigger path
    ticker_needs, sector_needs, member_needs = engine._analyze_atom_requirements([trigger])  # noqa: SLF001

    context_started = time.perf_counter()
    ctx = await engine._build_eval_context(tickers, sectors, ticker_needs, sector_needs, member_needs)  # noqa: SLF001
    context_elapsed_ms = (time.perf_counter() - context_started) * 1000

    for spec in evaluable_specs:
        started = time.perf_counter()
        try:
            result = evaluate_atom(spec["atom"], spec["params"], ctx)
            status = _status_from_result(result)
        except Exception as exc:
            result = {"atom": spec["atom"], "triggered": False, "error": str(exc), "type": type(exc).__name__}
            status = "exception"
        elapsed_ms = (time.perf_counter() - started) * 1000
        atom_records.append(
            {
                "atom": spec["atom"],
                "description": spec["description"],
                "params": _json_safe(spec["params"]),
                "status": status,
                "elapsed_ms": round(elapsed_ms, 2),
                "triggered": result.get("triggered") if isinstance(result, dict) else None,
                "result": _json_safe(result),
                "preview": _preview(result),
            }
        )

    atom_order = {name: i for i, name in enumerate(ATOM_SCHEMA)}
    atom_records.sort(key=lambda item: atom_order.get(item["atom"], 999))

    return {
        "context_elapsed_ms": round(context_elapsed_ms, 2),
        "context_summary": {
            "tickers": sorted(tickers),
            "sectors": sorted(sectors),
            "ticker_needs": {k: sorted(v) for k, v in ticker_needs.items()},
            "sector_needs": {k: sorted(v) for k, v in sector_needs.items()},
            "member_needs": {k: sorted(v) for k, v in member_needs.items()},
            "ticker_data_keys": {k: sorted(v.keys()) for k, v in ctx.ticker_data.items()},
            "sector_data_keys": {k: sorted(v.keys()) for k, v in ctx.sector_data.items()},
            "market_summary": _json_safe(ctx.market_summary),
        },
        "atoms": atom_records,
    }


def _write_markdown_report(result: dict[str, Any], path: Path) -> None:
    meta = result["meta"]
    api_calls = result["market_provider_calls"]
    atom_probe = result["trigger_atom_probe"]
    atoms = atom_probe["atoms"]

    api_ok = sum(1 for c in api_calls if c["status"] == "ok")
    api_errors = len(api_calls) - api_ok
    atom_eval = [a for a in atoms if a["status"] != "meta_skipped"]
    atom_ok = sum(1 for a in atom_eval if a["status"] == "ok")
    atom_errors = sum(1 for a in atom_eval if a["status"] != "ok")
    atom_triggered = sum(1 for a in atom_eval if a["triggered"] is True)

    lines: list[str] = []
    lines.append("# MarketDataProvider 与 Trigger Atom 探针报告")
    lines.append("")
    lines.append("## 运行信息")
    lines.append("")
    lines.append(f"- 模拟时间: `{meta['simulated_time']}`")
    lines.append(f"- 交易阶段: `{meta['phase']}`")
    lines.append(f"- 行情目录: `{meta['klines_path']}`")
    lines.append(f"- 测试股票: `{meta['ticker']}` / `{meta['ticker_2']}`")
    lines.append(f"- 测试板块: `{meta['sector']}` -> `{meta['sector_code']}`")
    lines.append(f"- 测试概念代码: `{meta['concept_code']}`")
    lines.append(f"- 报告生成时间: `{meta['generated_at']}`")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- MarketDataProvider 接口调用: `{len(api_calls)}`，成功 `{api_ok}`，失败/异常 `{api_errors}`")
    lines.append(
        f"- Trigger atom: 总数 `{len(atoms)}`，实际评估 `{len(atom_eval)}`，成功 `{atom_ok}`，失败/异常 `{atom_errors}`，触发 `{atom_triggered}`"
    )
    lines.append(f"- EvalContext 构建耗时: `{atom_probe['context_elapsed_ms']}` ms")
    lines.append("")
    lines.append("## MarketDataProvider 接口")
    lines.append("")
    lines.append("| 接口 | 状态 | 耗时(ms) | 参数 | 返回摘要 |")
    lines.append("|---|---:|---:|---|---|")
    for call in api_calls:
        params = json.dumps(call["params"], ensure_ascii=False)
        lines.append(
            f"| `{call['api']}` | `{call['status']}` | {call['elapsed_ms']} | `{params}` | {call['preview'].replace('|', '\\|')} |"
        )
    lines.append("")
    lines.append("## Trigger EvalContext")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(atom_probe["context_summary"], ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Trigger 原子")
    lines.append("")
    lines.append("| 原子 | 状态 | 触发 | 耗时(ms) | 参数 | 结果摘要 |")
    lines.append("|---|---:|---:|---:|---|---|")
    for atom in atoms:
        params = json.dumps(atom["params"], ensure_ascii=False)
        triggered = "" if atom["triggered"] is None else str(atom["triggered"])
        lines.append(
            f"| `{atom['atom']}` | `{atom['status']}` | `{triggered}` | {atom['elapsed_ms']} | `{params}` | {atom['preview'].replace('|', '\\|')} |"
        )
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- `time_after`、`time_window`、`time_before` 是 v3 meta atom，编译阶段抽取生命周期，不进入 evaluator，因此报告中标为 `meta_skipped`。")
    lines.append("- JSON 原始结果包含完整的接口返回摘要和 atom 评估明细；Markdown 只保留便于阅读的预览。")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    clock = ProbeClock(args.time)
    init_started = time.perf_counter()
    provider = MarketDataProvider(args.klines_path, clock=clock)
    init_elapsed_ms = (time.perf_counter() - init_started) * 1000

    ticker = _first_existing_ticker(provider, args.ticker)
    ticker_2 = _first_existing_ticker(provider, args.ticker_2)
    sector, sector_code = _first_existing_sector(provider, args.sector)
    concept_code = _first_existing_concept_code(provider, sector_code)
    snapshot_date = clock.today_str or args.time[:10]

    api_calls = await probe_provider_apis(
        provider,
        ticker=ticker,
        ticker_2=ticker_2,
        stock_name=args.stock_name,
        sector=sector,
        sector_code=sector_code or concept_code,
        concept_code=concept_code,
        index_name=args.index_name,
        snapshot_date=snapshot_date,
    )
    trigger_atom_probe = await probe_trigger_atoms(provider, ticker=ticker, sector=sector)

    return {
        "meta": {
            "simulated_time": str(_now_value(clock)),
            "today_str": clock.today_str,
            "phase": clock.phase,
            "klines_path": str(provider.klines_path),
            "init_elapsed_ms": round(init_elapsed_ms, 2),
            "ticker": ticker,
            "ticker_2": ticker_2,
            "stock_name": args.stock_name,
            "sector": sector,
            "sector_code": sector_code,
            "concept_code": concept_code,
            "index_name": args.index_name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "market_provider_calls": api_calls,
        "trigger_atom_probe": trigger_atom_probe,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time", default=DEFAULT_TIME, help="Simulation time, e.g. '2026-05-25 14:59:00'.")
    parser.add_argument("--klines-path", default=settings.klines_path)
    parser.add_argument("--ticker", default=DEFAULT_TICKER)
    parser.add_argument("--ticker-2", default=DEFAULT_TICKER_2)
    parser.add_argument("--stock-name", default=DEFAULT_STOCK_NAME)
    parser.add_argument("--sector", default=DEFAULT_SECTOR)
    parser.add_argument("--index-name", default=DEFAULT_INDEX_NAME)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--md-out", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    label = pd.Timestamp(args.time).strftime("%Y%m%d_%H%M")
    json_path = Path(args.json_out) if args.json_out else ROOT / "data" / "probe" / f"market_provider_trigger_probe_{label}.json"
    md_path = Path(args.md_out) if args.md_out else ROOT / "docs" / f"market_provider_trigger_probe_{label}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    result = asyncio.run(run(args))
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_markdown_report(result, md_path)

    api_calls = result["market_provider_calls"]
    atoms = result["trigger_atom_probe"]["atoms"]
    api_failed = sum(1 for c in api_calls if c["status"] != "ok")
    atom_failed = sum(1 for a in atoms if a["status"] not in ("ok", "meta_skipped"))
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    print(f"MarketDataProvider calls: {len(api_calls)}, failed: {api_failed}")
    print(f"Trigger atoms: {len(atoms)}, failed: {atom_failed}")


if __name__ == "__main__":
    main()
