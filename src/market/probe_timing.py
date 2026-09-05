"""API 耗时探针 —— 对指定模拟时间调用所有 MarketDataProvider 接口，记录耗时。

用法：
    python -m src.market_new.probe_timing 1700 "2026-06-02 17:00:00"
    python -m src.market_new.probe_timing 0900 "2026-06-03 09:00:00"
    python -m src.market_new.probe_timing 1130 "2026-06-03 11:30:00"
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pandas as pd

from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.market import MarketDataProvider

OUTPUT_DIR = Path(__file__).parent / "probe_output"
KLINES_PATH = "C:/klines"
TEST_TICKER = "000001.SZ"
TEST_TICKER2 = "000002.SZ"
TEST_INDEX = "上证指数"
TEST_SECTOR = "半导体"


def _json_safe(obj: Any) -> Any:
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
        if obj.empty:
            return None
        return {
            "shape": list(obj.shape),
            "columns": list(obj.columns),
            "head_3": json.loads(obj.head(3).to_json(orient="records", date_format="iso")),
            "tail_3": json.loads(obj.tail(3).to_json(orient="records", date_format="iso")),
        }
    if hasattr(obj, "item"):
        try:
            val = obj.item()
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return None
            return val
        except (ValueError, TypeError):
            pass
    return str(obj)


def _call(api: str, params: dict, description: str, elapsed_ms: float, result: Any) -> dict:
    return {
        "api": api,
        "params": params,
        "description": description,
        "elapsed_ms": round(elapsed_ms, 2),
        "result": _json_safe(result),
    }


async def probe_all(clock_label: str, time_str: str) -> dict:
    clock = Clock(TimeConfig(
        start_time=datetime.fromisoformat(time_str),
        tick_duration=timedelta(minutes=1),
        realtime=False,
    ))
    t0_init = time.perf_counter()
    p = MarketDataProvider(KLINES_PATH, clock=clock)
    init_ms = (time.perf_counter() - t0_init) * 1000

    phase_map = {"pre_market": "盘前 (0-9:30)", "trading": "盘中 (9:30-15:00)", "post_market": "盘后 (15:00+)"}

    meta = {
        "clock_label": clock_label,
        "simulated_datetime": str(clock.now()),
        "phase": clock.phase,
        "phase_desc": phase_map.get(clock.phase, clock.phase),
        "today_str": clock.today_str,
    }

    calls: list[dict] = []

    def timed(api: str, params: dict, description: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            result = {"error": str(e), "type": type(e).__name__}
        elapsed = (time.perf_counter() - t0) * 1000
        calls.append(_call(api, params, description, elapsed, result))

    async def timed_async(api: str, params: dict, description: str, coro: Awaitable[Any]) -> None:
        t0 = time.perf_counter()
        try:
            result = await coro
        except Exception as e:
            result = {"error": str(e), "type": type(e).__name__}
        elapsed = (time.perf_counter() - t0) * 1000
        calls.append(_call(api, params, description, elapsed, result))

    # ════════════════════════════════════════════════════════════
    # 属性
    # ════════════════════════════════════════════════════════════
    timed("trading_days", {}, "交易日窗口", lambda: p.trading_days)
    timed("klines_path", {}, "数据根目录", lambda: str(p.klines_path))
    timed("xt_ready", {}, "xtquant 是否就绪", lambda: p.xt_ready)

    # ════════════════════════════════════════════════════════════
    # get_bars — 日线
    # ════════════════════════════════════════════════════════════
    timed("get_bars(1d,default)", {"ticker": TEST_TICKER, "granularity": "1d"},
          "个股日线，默认参数（无start/end）",
          p.get_bars, TEST_TICKER)

    timed("get_bars(1d,range)", {"ticker": TEST_TICKER, "granularity": "1d", "start": "2026-05-01", "end": "2026-06-03"},
          "个股日线，指定范围",
          p.get_bars, TEST_TICKER, "1d", "2026-05-01", "2026-06-03")

    # ════════════════════════════════════════════════════════════
    # get_bars — 分钟
    # ════════════════════════════════════════════════════════════
    timed("get_bars(1m)", {"ticker": TEST_TICKER, "granularity": "1m"},
          "个股1m线",
          p.get_bars, TEST_TICKER, "1m")

    timed("get_bars(5m)", {"ticker": TEST_TICKER, "granularity": "5m"},
          "个股5m线（重采样）",
          p.get_bars, TEST_TICKER, "5m")

    # ════════════════════════════════════════════════════════════
    # get_bars — 指数
    # ════════════════════════════════════════════════════════════
    idx_code = p.resolve_index_name(TEST_INDEX) or "000001.SH"
    timed("get_bars(index,1d)", {"ticker": idx_code, "granularity": "1d"},
          f"指数日线 ({TEST_INDEX})",
          p.get_bars, idx_code, "1d", None, None)

    timed("get_bars(index,1m)", {"ticker": idx_code, "granularity": "1m"},
          f"指数1m线 ({TEST_INDEX})",
          p.get_bars, idx_code, "1m", None, None)

    # ════════════════════════════════════════════════════════════
    # get_concept_kline
    # ════════════════════════════════════════════════════════════
    sectors = p.get_concept_list()
    if sectors:
        con_code = sectors[0].get("code", "")
        timed("get_concept_kline", {"concept_code": con_code},
              f"概念日线 (code={con_code})",
              p.get_concept_kline, con_code)

    # ════════════════════════════════════════════════════════════
    # 实时价格
    # ════════════════════════════════════════════════════════════
    await timed_async("get_realtime_price", {"ticker": TEST_TICKER},
                      "单只股票实时价格",
                      p.get_realtime_price(TEST_TICKER))

    await timed_async("get_realtime_prices", {"tickers": [TEST_TICKER, TEST_TICKER2]},
                      "批量实时价格",
                      p.get_realtime_prices([TEST_TICKER, TEST_TICKER2]))

    # ════════════════════════════════════════════════════════════
    # 涨跌停
    # ════════════════════════════════════════════════════════════
    timed("get_zdt_record", {"ticker": TEST_TICKER},
          "涨跌停记录",
          p.get_zdt_record, TEST_TICKER)

    # ════════════════════════════════════════════════════════════
    # 板块
    # ════════════════════════════════════════════════════════════
    await timed_async("get_sector_overview", {"sector": TEST_SECTOR},
                      f"板块概览 ({TEST_SECTOR})",
                      p.get_sector_overview(TEST_SECTOR))

    sector_code = p.resolve_sector_code(TEST_SECTOR)
    if sector_code:
        timed("get_sector_leader", {"sector_code": sector_code},
              "板块龙头",
              p.get_sector_leader, sector_code)

        timed("get_sector_volume_ratio", {"sector_code": sector_code, "n": 5},
              "板块量比",
              p.get_sector_volume_ratio, sector_code, 5)

    timed("get_concept_list", {"con_type": "all"},
          "概念列表（截取前5条）",
          lambda: p.get_concept_list("all")[:5])

    if sectors:
        con_code = sectors[0].get("code", "")
        timed("get_concept_members", {"concept_code": con_code},
              f"概念成员 (截取前10, code={con_code})",
              lambda c=con_code: p.get_concept_members(c)[:10])

    timed("get_stock_concepts", {"ticker": TEST_TICKER},
          f"股票所属概念 ({TEST_TICKER})",
          p.get_stock_concepts, TEST_TICKER)

    # ════════════════════════════════════════════════════════════
    # 快照
    # ════════════════════════════════════════════════════════════
    timed("get_market_snapshot", {"date": "2026-06-03"},
          "全市场快照",
          p.get_market_snapshot, "2026-06-03")

    # ════════════════════════════════════════════════════════════
    # 解析
    # ════════════════════════════════════════════════════════════
    timed("resolve_stock_ticker", {"name": "平安银行"},
          "按名称查找股票",
          p.resolve_stock_ticker, "平安银行")

    timed("resolve_index_name", {"name": TEST_INDEX},
          "指数名称→代码",
          p.resolve_index_name, TEST_INDEX)

    timed("get_stock_name", {"ticker": TEST_TICKER},
          "股票代码→名称",
          p.get_stock_name, TEST_TICKER)

    timed("resolve_sector_code", {"sector": TEST_SECTOR},
          "板块名称→代码",
          p.resolve_sector_code, TEST_SECTOR)

    timed("resolve_sector_code(miss)", {"sector": "不存在的板块"},
          "板块名称→代码 (miss)",
          p.resolve_sector_code, "不存在的板块")

    # ════════════════════════════════════════════════════════════
    # 分类
    # ════════════════════════════════════════════════════════════
    timed("get_classification", {},
          "板块分类数据",
          lambda: {k: list(v.shape) if hasattr(v, "shape") else str(v)
                   for k, v in p.get_classification().items()})

    # ── 汇总 ──
    total_ms = sum(c["elapsed_ms"] for c in calls)
    return {
        "meta": meta,
        "init_ms": round(init_ms, 2),
        "total_api_ms": round(total_ms, 2),
        "total_calls": len(calls),
        "api_calls": calls,
    }


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python -m src.market_new.probe_timing <label> <time_str>")
        print("示例: python -m src.market_new.probe_timing 1700 \"2026-06-02 17:00:00\"")
        sys.exit(1)

    label = sys.argv[1]
    time_str = sys.argv[2]

    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"[{label}] 开始探测 clock={time_str}")
    result = asyncio.run(probe_all(label, time_str))

    out_path = OUTPUT_DIR / f"probe_{label}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    # 打印摘要
    print(f"[{label}] init={result['init_ms']:.0f}ms  total_api={result['total_api_ms']:.0f}ms  calls={result['total_calls']}")
    for c in result["api_calls"]:
        ms = c["elapsed_ms"]
        flag = " ⚠️" if ms > 100 else ""
        has_err = isinstance(c["result"], dict) and "error" in (c["result"] or {})
        err_flag = " ❌" if has_err else ""
        print(f"  {c['api']:40s} {ms:8.2f}ms{flag}{err_flag}")
    print(f"[{label}] → {out_path}")


if __name__ == "__main__":
    main()
