"""全接口探针 —— 在三个时钟时间点调用所有 MarketDataProvider API，保存结果供检查。

输出格式每条记录包含：
- api: 接口名称
- params: 调用参数
- description: 说明
- result: 返回数据

用法：python -m src.market_new.probe_all_apis
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pandas as pd

from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.market import MarketDataProvider

OUTPUT_DIR = Path(__file__).parent / "probe_output"
KLINES_PATH = "C:/klines"
TIMES = {
    "0900": "2026-06-03 09:00:00",   # 盘前
    "1130": "2026-06-03 11:30:00",   # 盘中
    "1700": "2026-06-02 17:00:00",   # 盘后（前一交易日）
}
TEST_TICKER = "000001.SZ"
TEST_TICKER2 = "000002.SZ"
TEST_INDEX = "上证指数"
TEST_SECTOR = "半导体"


def _df_to_result(df: pd.DataFrame | None) -> dict | None:
    """DataFrame → 可读结果。"""
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    return {
        "shape": list(df.shape),
        "columns": list(df.columns),
        "head_5": json.loads(df.head(5).to_json(orient="records", date_format="iso")),
        "tail_5": json.loads(df.tail(5).to_json(orient="records", date_format="iso")),
    }


def _json_safe(obj: Any) -> Any:
    """递归处理 non-JSON-serializable 值。"""
    import math

    import pandas as pd

    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(i) for i in obj]
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, pd.DataFrame):
        return _df_to_result(obj)
    # numpy float types
    if hasattr(obj, "item"):
        try:
            val = obj.item()
            if isinstance(val, float):
                if math.isnan(val) or math.isinf(val):
                    return None
                return val
            return val
        except (ValueError, TypeError):
            pass
    return str(obj)


def _call(api: str, params: dict, description: str, result: Any) -> dict:
    """构建一条 API 调用记录。"""
    return {
        "api": api,
        "params": params,
        "description": description,
        "result": _json_safe(result),
    }


async def probe_all(clock_label: str, clock: Clock, provider: MarketDataProvider | None = None) -> dict:
    """对所有 API 进行一次完整探针。"""
    p = provider if provider is not None else MarketDataProvider(KLINES_PATH, clock=clock)
    calls: list[dict] = []

    phase_map = {"pre_market": "盘前 (0-9:30)", "trading": "盘中 (9:30-15:00)", "post_market": "盘后 (15:00+)"}

    meta = {
        "clock_label": clock_label,
        "clock_time": clock_label,
        "simulated_datetime": str(clock.now()),
        "phase": clock.phase,
        "phase_desc": phase_map.get(clock.phase, clock.phase),
        "today_str": clock.today_str,
    }

    # ════════════════════════════════════════════════════════════
    # 属性
    # ════════════════════════════════════════════════════════════
    calls.append(_call("trading_days", {}, "交易日窗口", p.trading_days))
    calls.append(_call("klines_path", {}, "数据根目录", str(p.klines_path)))
    calls.append(_call("xt_ready", {}, "xtquant 是否就绪", p.xt_ready))

    # ════════════════════════════════════════════════════════════
    # get_bars — 日线
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_bars",
        {"ticker": TEST_TICKER, "granularity": "1d", "start": None, "end": None},
        "个股日线，默认（无start/end，应返回120个交易日）",
        p.get_bars(TEST_TICKER),
    ))
    calls.append(_call(
        "get_bars",
        {"ticker": TEST_TICKER, "granularity": "1d", "start": "2026-05-01", "end": "2026-06-03"},
        "个股日线，指定 date 范围",
        p.get_bars(TEST_TICKER, start="2026-05-01", end="2026-06-03"),
    ))

    # ════════════════════════════════════════════════════════════
    # get_bars — 分钟
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_bars",
        {"ticker": TEST_TICKER, "granularity": "1m", "start": None, "end": None},
        "个股1m线（忽略start/end，只返回当前交易日数据）",
        p.get_bars(TEST_TICKER, granularity="1m"),
    ))

    # ════════════════════════════════════════════════════════════
    # get_bars — 指数日线
    # ════════════════════════════════════════════════════════════
    idx_code = p.resolve_index_name(TEST_INDEX) or "000001.SH"
    calls.append(_call(
        "get_bars",
        {"ticker": idx_code, "granularity": "1d", "start": None, "end": None},
        f"指数日线 ({TEST_INDEX} → {idx_code})",
        p.get_bars(idx_code),
    ))

    # ════════════════════════════════════════════════════════════
    # get_bars — 指数1m
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_bars",
        {"ticker": idx_code, "granularity": "1m", "start": None, "end": None},
        f"指数1m线 ({TEST_INDEX} → {idx_code})",
        p.get_bars(idx_code, granularity="1m"),
    ))

    # ════════════════════════════════════════════════════════════
    # get_concept_kline
    # ════════════════════════════════════════════════════════════
    sectors = p.get_concept_list()
    if sectors:
        con_code = sectors[0].get("code", "")
        calls.append(_call(
            "get_concept_kline",
            {"concept_code": con_code, "granularity": "1d"},
            f"概念日线 (code={con_code})",
            p.get_concept_kline(con_code),
        ))

    # ════════════════════════════════════════════════════════════
    # get_realtime_price
    # ════════════════════════════════════════════════════════════
    price = await p.get_realtime_price(TEST_TICKER)
    calls.append(_call(
        "get_realtime_price",
        {"ticker": TEST_TICKER},
        "单只股票实时价格",
        price,
    ))

    # ════════════════════════════════════════════════════════════
    # get_realtime_prices
    # ════════════════════════════════════════════════════════════
    prices = await p.get_realtime_prices([TEST_TICKER, TEST_TICKER2])
    calls.append(_call(
        "get_realtime_prices",
        {"tickers": [TEST_TICKER, TEST_TICKER2]},
        "批量实时价格",
        {k: _json_safe(v) for k, v in prices.items()},
    ))

    # ════════════════════════════════════════════════════════════
    # get_turnover_rate
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_turnover_rate",
        {"ticker": TEST_TICKER},
        "估算换手率",
        p.get_turnover_rate(TEST_TICKER),
    ))

    # ════════════════════════════════════════════════════════════
    # get_zdt_record
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_zdt_record",
        {"ticker": TEST_TICKER},
        "涨跌停记录（板型、连板数等）",
        p.get_zdt_record(TEST_TICKER),
    ))

    # ════════════════════════════════════════════════════════════
    # get_sector_overview
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_sector_overview",
        {"sector": TEST_SECTOR},
        f"板块概览 (sector={TEST_SECTOR})",
        await p.get_sector_overview(TEST_SECTOR),
    ))

    # ════════════════════════════════════════════════════════════
    # get_sector_leader / get_sector_volume_ratio
    # ════════════════════════════════════════════════════════════
    sector_code = p.resolve_sector_code(TEST_SECTOR)
    if sector_code:
        calls.append(_call(
            "get_sector_leader",
            {"sector_code": sector_code},
            "板块龙头",
            p.get_sector_leader(sector_code),
        ))
        calls.append(_call(
            "get_sector_volume_ratio",
            {"sector_code": sector_code, "n": 5},
            "板块量比",
            p.get_sector_volume_ratio(sector_code, n=5),
        ))

    # ════════════════════════════════════════════════════════════
    # get_concept_list
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_concept_list",
        {"con_type": "all"},
        "概念列表（全部类型，截取前5条）",
        p.get_concept_list("all")[:5],
    ))

    # ════════════════════════════════════════════════════════════
    # get_concept_members
    # ════════════════════════════════════════════════════════════
    if sectors:
        con_code = sectors[0].get("code", "")
        calls.append(_call(
            "get_concept_members",
            {"concept_code": con_code},
            f"概念成员 (截取前10，concept={con_code})",
            p.get_concept_members(con_code)[:10],
        ))

    # ════════════════════════════════════════════════════════════
    # get_stock_concepts
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_stock_concepts",
        {"ticker": TEST_TICKER},
        f"股票所属概念 ({TEST_TICKER})",
        p.get_stock_concepts(TEST_TICKER),
    ))

    # ════════════════════════════════════════════════════════════
    # get_market_breadth
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_market_breadth",
        {},
        "全市场涨跌统计（涨跌家数、平均涨幅、成交额）",
        p.get_market_breadth(),
    ))

    # ════════════════════════════════════════════════════════════
    # get_index_overview
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_index_overview",
        {},
        "主要指数概览（7大指数涨跌幅）",
        p.get_index_overview(),
    ))

    # ════════════════════════════════════════════════════════════
    # get_today_market_summary
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_today_market_summary",
        {},
        "今日市场摘要（通过 market_snapshot + breadth + index_overview 组合提供）",
        {
            "breadth": p.get_market_breadth(),
            "index_overview": p.get_index_overview(),
            "snapshot": p.get_market_snapshot(p.clock.today_str),
        },
    ))

    # ════════════════════════════════════════════════════════════
    # get_market_snapshot
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_market_snapshot",
        {"date": "2026-06-03"},
        "全市场快照",
        p.get_market_snapshot("2026-06-03"),
    ))

    # ════════════════════════════════════════════════════════════
    # resolve_stock_ticker
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "resolve_stock_ticker",
        {"name": "平安银行"},
        "按名称查找股票代码",
        p.resolve_stock_ticker("平安银行"),
    ))

    # ════════════════════════════════════════════════════════════
    # resolve_index_name
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "resolve_index_name",
        {"name": TEST_INDEX},
        "指数名称→代码",
        p.resolve_index_name(TEST_INDEX),
    ))

    # ════════════════════════════════════════════════════════════
    # get_stock_name
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "get_stock_name",
        {"ticker": TEST_TICKER},
        "股票代码→名称",
        p.get_stock_name(TEST_TICKER),
    ))

    # ════════════════════════════════════════════════════════════
    # resolve_sector_code
    # ════════════════════════════════════════════════════════════
    calls.append(_call(
        "resolve_sector_code",
        {"sector": TEST_SECTOR},
        "板块名称→代码",
        p.resolve_sector_code(TEST_SECTOR),
    ))

    # ════════════════════════════════════════════════════════════
    # get_classification
    # ════════════════════════════════════════════════════════════
    clf = p.get_classification()
    calls.append(_call(
        "get_classification",
        {},
        "板块分类数据（各类别的 shape）",
        {k: list(v.shape) if hasattr(v, "shape") else str(v) for k, v in clf.items()},
    ))

    return {"meta": meta, "total_calls": len(calls), "api_calls": calls}


async def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 复用同一天的 provider 避免重复加载数据
    prev_provider = None
    prev_today = None

    for label, time_str in TIMES.items():
        print(f"\n{'='*60}")
        print(f"探针: clock={time_str} ({label})")
        print(f"{'='*60}")

        clock = Clock(TimeConfig(
            start_time=datetime.fromisoformat(time_str),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        today = clock.today_str
        if prev_provider is not None and today == prev_today:
            # 同一天：复用 provider，只换时钟（服务层持有旧时钟引用，但 Session 数据相同）
            p = prev_provider
            p._clock = clock
            p._bar_svc._clock = clock
            p._price_svc._clock = clock
            p._limit_tracker._clock = clock
            p._breadth_svc._clock = clock
            p._sector_svc._clock = clock
            p._snapshot_svc._clock = clock
        else:
            p = MarketDataProvider(KLINES_PATH, clock=clock)
            prev_provider = p
            prev_today = today

        results = await probe_all(label, clock, provider=p)

        out_path = OUTPUT_DIR / f"probe_{label}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"→ 保存到 {out_path} ({results['total_calls']} 个接口调用)")

    # 汇总
    summary = {
        "description": "market_new 全接口探针汇总",
        "data_path": KLINES_PATH,
        "test_ticker": TEST_TICKER,
        "test_index": TEST_INDEX,
        "test_sector": TEST_SECTOR,
        "times": TIMES,
        "output_files": {label: str(OUTPUT_DIR / f"probe_{label}.json") for label in TIMES},
    }
    with open(OUTPUT_DIR / "probe_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n汇总 → {OUTPUT_DIR / 'probe_summary.json'}")
    print("完成。")


if __name__ == "__main__":
    asyncio.run(main())
