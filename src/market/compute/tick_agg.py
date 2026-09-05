"""Tick→1m 聚合器 —— 从 xtquant 全推 Tick 合成 1m K 线 + 实时日线。

每根 1m bar 写入 session.today_1m_ticker
同时合成当日日线写入 session.today_daily_ticker
同时更新受影响板块的 1m 和当日日线（统一 key 存到 session）
refresh 时调用 clear() 清空 buffer。

架构：on_tick() 仅做轻量入队，后台 worker 线程消费并执行重计算，
避免阻塞 xtquant 回调线程导致 tick 丢失。
"""

from __future__ import annotations

from loguru import logger
import queue
import threading
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from src.market.data.cache import CacheManager
    from src.market.types import DailyTicker



class TickAggregator:
    """将 xtquant 全推 Tick 聚合成 1m OHLCV Bar + 实时日线 + 板块 1m/日线。

    内部使用 queue + worker 线程模式，on_tick() 仅做 O(1) 入队操作，
    重计算（日线合成、板块更新）在后台线程中批量执行。
    """

    def __init__(self, cache: CacheManager) -> None:
        self._cache = cache
        self._lock = threading.Lock()
        self._bar_buffers: dict[str, list[dict]] = {}
        self._tick_buffers: dict[str, list[dict]] = {}
        self._current_minute: dict[str, int] = {}
        self._current_time: dict[str, str] = {}

        # 异步队列 + worker 线程
        self._tick_queue: queue.Queue[dict] = queue.Queue(maxsize=50000)
        self._worker_thread = threading.Thread(target=self._worker, daemon=True, name="tick-aggregator-worker")
        self._worker_thread.start()

    def on_tick(self, datas: dict) -> None:
        """处理全推 Tick 数据（在 xtquant 回调线程中调用）。

        仅做轻量入队，不执行重计算。datas: {code: tick_dict}。
        """
        # 快速过滤无效数据，避免填满队列
        valid = {}
        for code, tick in datas.items():
            if not isinstance(tick, dict):
                continue
            tick_time = tick.get("time", 0)
            if not tick_time:
                continue
            valid[code] = tick

        if not valid:
            return

        try:
            # 非阻塞入队，队列满时丢弃旧数据保留最新
            self._tick_queue.put_nowait(valid)
        except queue.Full:
            # 队列已满，尝试 drain 一半再入队
            try:
                self._drain_queue(20000)
                self._tick_queue.put_nowait(valid)
            except queue.Full:
                logger.warning("tick queue still full after drain, dropping batch")

    def _drain_queue(self, max_items: int = 10000) -> None:
        """从队列中批量取出 tick 并处理。"""
        batch: dict[str, list[dict]] = {}
        count = 0
        while count < max_items:
            try:
                item = self._tick_queue.get_nowait()
                for code, tick in item.items():
                    batch.setdefault(code, []).append(tick)
                count += 1
            except queue.Empty:
                break

        if not batch:
            return

        with self._lock:
            for code, ticks in batch.items():
                prev_minute = self._current_minute.get(code)
                for tick in ticks:
                    minute_key, time_str = self._minute_from_tick(tick.get("time", 0))
                    if minute_key is None:
                        continue
                    if prev_minute is not None and prev_minute != minute_key:
                        self._flush_code_locked(code)
                    self._current_minute[code] = minute_key
                    self._current_time[code] = time_str
                    prev_minute = minute_key
                    self._tick_buffers.setdefault(code, []).append(tick)
                    self._write_live_bar_locked(code)

    def _worker(self) -> None:
        """后台 worker：定时消费队列并执行重计算。"""
        while True:
            try:
                self._drain_and_process()
            except Exception:
                logger.exception("tick aggregator worker error")
            threading.Event().wait(0.5)  # 500ms 消费周期

    def _drain_and_process(self) -> None:
        """消费队列中的所有 tick，更新日线和板块。"""
        self._drain_queue()
        # flush 所有有 pending tick 的 code，触发日线和板块更新
        with self._lock:
            codes = list(self._tick_buffers.keys())
        for code in codes:
            with self._lock:
                self._flush_code_locked(code)

    def flush(self) -> None:
        """强制刷新所有待处理的 tick。"""
        with self._lock:
            codes = list(self._tick_buffers.keys())
            for code in codes:
                self._flush_code_locked(code)

    def get_bars(self, code: str) -> pd.DataFrame | None:
        """获取某只股票已聚合的 1m Bar（线程安全）。"""
        with self._lock:
            self._flush_code_locked(code)
            bars = self._bar_buffers.get(code, [])
            if not bars:
                return None
            return pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

    def clear(self) -> None:
        """清空所有 buffer（refresh 时调用）。"""
        with self._lock:
            self._bar_buffers.clear()
            self._tick_buffers.clear()
            self._current_minute.clear()
            self._current_time.clear()

    # ── 内部 ─

    def _flush_code_locked(self, code: str) -> None:
        """聚合 buffered ticks 为一根已完成的 1m bar 并写入缓存。"""
        ticks = self._tick_buffers.pop(code, [])
        if not ticks:
            return

        bar = self._aggregate_ticks(ticks)
        if bar is None:
            return

        # 写入已完成 bar + 更新日线
        self._write_1m_bar(code, bar)
        self._update_daily_from_1m(code)

    def _aggregate_ticks(self, ticks: list[dict]) -> dict | None:
        """从一组 tick 聚合为一根 1m Bar。"""
        prices = [t.get("lastPrice", 0) for t in ticks if t.get("lastPrice", 0) > 0]
        if not prices:
            return None

        # 成交量/额用累计值取差值（不过滤，取全部 tick 的头尾）
        all_vols = [t.get("pvolume", 0) for t in ticks]
        all_amts = [t.get("amount", 0) for t in ticks]
        vol = max(0.0, all_vols[-1] - all_vols[0]) if len(all_vols) >= 2 else 0.0
        amt = max(0.0, all_amts[-1] - all_amts[0]) if len(all_amts) >= 2 else 0.0

        return {
            "timestamp": pd.Timestamp(ticks[0].get("time", 0), unit="s"),
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": vol / 10000.0,   # 股→万股
            "amount": amt / 10000.0,   # 元→万元
        }

    def _write_1m_bar(self, code: str, bar: dict) -> None:
        """将一根已完成的 1m bar 写入 _bar_buffers 并更新 session。"""
        self._bar_buffers.setdefault(code, []).append(bar)
        self._sync_1m_to_session(code)

    def _write_live_bar_locked(self, code: str) -> None:
        """实时更新当前分钟的 live bar 到 session（需持有锁）。"""
        ticks = self._tick_buffers.get(code, [])
        if not ticks:
            return
        bar = self._aggregate_ticks(ticks)
        if bar is not None:
            self._sync_1m_to_session(code, live_bar=bar)

    def _sync_1m_to_session(self, code: str, live_bar: dict | None = None) -> None:
        """将 _bar_buffers[code] + 可选的 live_bar 同步到 session.today_1m_ticker。"""
        bars = self._bar_buffers.get(code, [])
        if live_bar is not None:
            bars = bars + [live_bar]
        if bars:
            self._cache.session.today_1m_ticker[code] = pd.DataFrame(bars)
        else:
            self._cache.session.today_1m_ticker.pop(code, None)

    def _update_daily_from_1m(self, code: str) -> None:
        """从 today_1m_ticker 合成当日日线，写入 today_daily_ticker。
        同时更新该股票所属板块的 1m 和日线。"""
        session = self._cache.session
        df_1m = session.today_1m_ticker.get(code)
        if df_1m is None or df_1m.empty:
            return

        last_dt = session.last_daily_ticker.get(code)
        pre_close = last_dt.close if last_dt else float(df_1m["open"].iloc[0])

        dt = DailyTicker(
            ts_code=code,
            timestamp=int(df_1m["timestamp"].iloc[0].timestamp() * 1000),
            open=float(df_1m["open"].iloc[0]),
            high=float(df_1m["high"].max()),
            low=float(df_1m["low"].min()),
            close=float(df_1m["close"].iloc[-1]),
            pre_close=pre_close,
            volume=float(df_1m["volume"].sum()),
            amount=float(df_1m["amount"].sum()),
            volume_ratio=0.0,
            turnover_rate=last_dt.turnover_rate if last_dt else 0.0,
            turnover_rate_f=last_dt.turnover_rate_f if last_dt else 0.0,
            pe=last_dt.pe if last_dt else 0.0,
            pe_ttm=last_dt.pe_ttm if last_dt else 0.0,
            pb=last_dt.pb if last_dt else 0.0,
            ps=last_dt.ps if last_dt else 0.0,
            ps_ttm=last_dt.ps_ttm if last_dt else 0.0,
            dv_ratio=last_dt.dv_ratio if last_dt else 0.0,
            dv_ttm=last_dt.dv_ttm if last_dt else 0.0,
            total_share=last_dt.total_share if last_dt else 0.0,
            float_share=last_dt.float_share if last_dt else 0.0,
            free_share=last_dt.free_share if last_dt else 0.0,
            total_mv=last_dt.total_mv if last_dt else 0.0,
            circ_mv=last_dt.circ_mv if last_dt else 0.0,
        )
        session.today_daily_ticker[code] = dt

        # 更新该股票所属的板块
        self._update_sector_for_stock(code)

    # ── 板块 1m / 日线 ──

    def _update_sector_for_stock(self, stock_code: str) -> None:
        """根据单只股票的更新，重新计算其所属板块的 1m 和日线。"""
        session = self._cache.session
        if session.all_members is None or session.all_members.empty:
            return

        # 找出该股票所属的所有概念
        members = session.all_members
        if "ts_code" not in members.columns:
            return
        concepts = members[members["ts_code"] == stock_code]
        if concepts.empty:
            return

        # 概念昨日收盘价
        concept_yesterday_close: dict[str, float] = session.adhoc.get(
            "_concept_yesterday_close", {},
        )

        for _, row in concepts.iterrows():
            con_code = str(row["con_code"])
            sector_key = f"{con_code}.TI" if not con_code.endswith(".TI") else con_code
            base_close = concept_yesterday_close.get(con_code)
            if base_close is None or base_close <= 0:
                continue

            self._compute_sector_1m_and_daily(sector_key, con_code, base_close)

    def _compute_sector_1m_and_daily(
        self, sector_key: str, con_code: str, base_close: float,
    ) -> None:
        """用成员 1m 数据合成板块 1m bar 和当日日线，写入 session 统一缓存。"""
        session = self._cache.session
        members = session.all_members
        if members is None or members.empty:
            return

        member_codes = members[members["con_code"] == con_code]["ts_code"].tolist()
        if not member_codes:
            return

        # 收集所有成员的 today_1m_ticker
        parts: list[pd.DataFrame] = []
        for mcode in member_codes:
            df = session.today_1m_ticker.get(mcode)
            if df is not None and not df.empty:
                p = df.copy()
                p["ticker"] = mcode
                parts.append(p)

        if not parts:
            return

        df_all = pd.concat(parts, ignore_index=True)
        if df_all.empty or "close" not in df_all.columns:
            return

        # 获取成员权重和昨日收盘
        last_daily = session.last_daily_ticker
        df_all["weight"] = df_all["ticker"].map(
            {k: v.circ_mv for k, v in last_daily.items()}
        )
        df_all["prev_close"] = df_all["ticker"].map(
            {k: v.close for k, v in last_daily.items()}
        )
        df_all = df_all[df_all["weight"].notna() & (df_all["weight"] > 0) & df_all["prev_close"].notna()]
        if df_all.empty:
            return

        df_all["pct_chg"] = df_all["close"] / df_all["prev_close"] - 1.0

        # 按 timestamp 分组
        grouped = df_all.groupby("timestamp")

        timestamps: list = []
        closes: list[float] = []
        volumes: list[float] = []
        amounts: list[float] = []

        for ts, grp in grouped:
            w = grp["weight"].to_numpy(dtype=float)
            pct = grp["pct_chg"].to_numpy(dtype=float)
            vol = grp["volume"].to_numpy(dtype=float)
            amt = grp["amount"].to_numpy(dtype=float)

            valid = ~np.isnan(pct) & (w > 0)
            if not valid.any():
                continue

            weighted_pct = float(np.sum(pct[valid] * w[valid]) / np.sum(w[valid]))
            weighted_close = base_close * (1.0 + weighted_pct)

            timestamps.append(ts)
            closes.append(weighted_close)
            volumes.append(float(np.sum(vol[valid])))
            amounts.append(float(np.sum(amt[valid])))

        if not timestamps:
            return

        df_sector = pd.DataFrame({
            "timestamp": timestamps,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volumes,
            "amount": amounts,
        })
        df_sector = df_sector.dropna(subset=["close"])
        if not df_sector.empty:
            session.today_1m_ticker[sector_key] = df_sector

        # 板块日线
        if not df_sector.empty:
            last_sec_dt = session.last_daily_ticker.get(sector_key)
            sec_pre_close = last_sec_dt.close if last_sec_dt else float(df_sector["open"].iloc[0])

            sec_dt = DailyTicker(
                ts_code=sector_key,
                timestamp=int(df_sector["timestamp"].iloc[0].timestamp() * 1000),
                open=float(df_sector["open"].iloc[0]),
                high=float(df_sector["high"].max()),
                low=float(df_sector["low"].min()),
                close=float(df_sector["close"].iloc[-1]),
                pre_close=sec_pre_close,
                volume=float(df_sector["volume"].sum()),
                amount=float(df_sector["amount"].sum()),
                volume_ratio=0.0,
                turnover_rate=last_sec_dt.turnover_rate if last_sec_dt else 0.0,
                turnover_rate_f=last_sec_dt.turnover_rate_f if last_sec_dt else 0.0,
                pe=last_sec_dt.pe if last_sec_dt else 0.0,
                pe_ttm=last_sec_dt.pe_ttm if last_sec_dt else 0.0,
                pb=last_sec_dt.pb if last_sec_dt else 0.0,
                ps=last_sec_dt.ps if last_sec_dt else 0.0,
                ps_ttm=last_sec_dt.ps_ttm if last_sec_dt else 0.0,
                dv_ratio=last_sec_dt.dv_ratio if last_sec_dt else 0.0,
                dv_ttm=last_sec_dt.dv_ttm if last_sec_dt else 0.0,
                total_share=last_sec_dt.total_share if last_sec_dt else 0.0,
                float_share=last_sec_dt.float_share if last_sec_dt else 0.0,
                free_share=last_sec_dt.free_share if last_sec_dt else 0.0,
                total_mv=last_sec_dt.total_mv if last_sec_dt else 0.0,
                circ_mv=last_sec_dt.circ_mv if last_sec_dt else 0.0,
            )
            session.today_daily_ticker[sector_key] = sec_dt

    @staticmethod
    def _minute_from_tick(tick_time: object) -> tuple[int | None, str]:
        """从 tick 时间字段提取分钟 key 和格式化的时间字符串。"""
        import datetime

        try:
            if isinstance(tick_time, (int, float)):
                ts = tick_time / 1000.0 if tick_time > 1e10 else tick_time
                dt = datetime.datetime.fromtimestamp(ts)
            elif isinstance(tick_time, str):
                if tick_time.isdigit():
                    ts_val = int(tick_time)
                    ts = ts_val / 1000.0 if ts_val > 1e10 else ts_val
                    dt = datetime.datetime.fromtimestamp(ts)
                else:
                    dt = datetime.datetime.fromisoformat(tick_time.replace("Z", "+00:00"))
            else:
                return None, ""

            minute_key = dt.hour * 60 + dt.minute
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            return minute_key, time_str
        except (ValueError, OSError, TypeError):
            return None, ""
