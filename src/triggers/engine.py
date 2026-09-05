"""触发器后台轮询引擎 —— 预取 + 去重 + 并行评估"""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from loguru import logger
from sqlalchemy import select, update

from src.config import settings
from src.core.timezone import BEIJING_TZ
from src.db import async_session
from src.market import MarketDataProvider
from src.models.tables import TriggerRecord
from src.triggers.atoms import (
    ATOM_SCHEMA,
    build_member_ticker_keys,
    build_sector_data_keys,
    build_ticker_data_keys,
    evaluate_condition_tree,
)
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import evaluate_atom
from src.workflow_logging import gather_with_progress, log_progress

# 从 Schema 自动派生数据需求表（真相源在 ATOM_SCHEMA）
_ATOM_TICKER_KEYS = build_ticker_data_keys()
_ATOM_SECTOR_KEYS = build_sector_data_keys()
_MEMBER_NEED_KEYS = build_member_ticker_keys()


class TriggerEngine:
    def __init__(self, market: MarketDataProvider, on_trigger: Callable[..., Any] | None = None) -> None:
        self.market = market
        self._on_trigger = on_trigger
        self._running = False
        self._idle_cycles = 0
        self._pending_callbacks: list[asyncio.Task] = []

    async def run_forever(self) -> None:
        self._running = True
        interval = settings.trigger_eval_interval_seconds

        while self._running:
            # 非交易日或非交易时段：跳过评估，休眠后继续等待
            if not self.market.is_trading_day or not self.market.clock.is_trading_session:
                self._idle_cycles += 1
                if self._idle_cycles % 600 == 0:
                    log_progress(
                        "TriggerEngine",
                        "非交易时段，等待中",
                        idle_cycles=self._idle_cycles,
                        is_trading_day=self.market.is_trading_day,
                        phase=self.market.clock.phase,
                    )
                await asyncio.sleep(1)
                continue

            self._idle_cycles = 0
            try:
                processed = await self._evaluate_all()
                await self.flush_pending()
                if processed == 0:
                    self._idle_cycles += 1
                    if self._idle_cycles % 60 == 0:
                        log_progress(
                            "TriggerEngine",
                            "仍在轮询",
                            interval_s=interval,
                            idle_cycles=self._idle_cycles,
                        )
                else:
                    self._idle_cycles = 0
            except Exception as e:
                logger.opt(exception=True).error("trigger evaluation exception")
                # 即使评估抛异常，也要 flush 已派发的回调，避免任务积压
                try:
                    await self.flush_pending()
                except Exception as fe:
                    logger.opt(exception=True).error("flush_pending failed (exception recovery)")

            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False

    # ═══════════════════════════════════════════════
    # Phase 1: 加载 + 时间窗口过滤
    # ═══════════════════════════════════════════════

    async def _load_active_triggers(self) -> list[TriggerRecord]:
        async with async_session() as db:
            result = await db.execute(select(TriggerRecord).where(TriggerRecord.status == "waiting"))
            triggers = list(result.scalars().all())

            if not triggers:
                return []

            now = self._now()
            active_triggers: list[TriggerRecord] = []
            expired_ids: list = []
            error_ids: list = []
            for t in triggers:
                if t.not_before and now < t.not_before:
                    continue
                if t.not_after and now > t.not_after:
                    expired_ids.append(t.id)
                    continue
                # 编译失败的触发器标记为 error，避免每轮重复加载
                if not t.condition or t.condition == {}:
                    error_ids.append(t.id)
                    continue
                if isinstance(t.condition, dict) and "error" in t.condition:
                    error_ids.append(t.id)
                    continue
                active_triggers.append(t)

            # 批量更新过期/错误触发器（同一事务内）
            if expired_ids:
                await db.execute(
                    update(TriggerRecord).where(TriggerRecord.id.in_(expired_ids)).values(status="expired")
                )
            if error_ids:
                await db.execute(update(TriggerRecord).where(TriggerRecord.id.in_(error_ids)).values(status="error"))
            await db.commit()

        return active_triggers

    # ═══════════════════════════════════════════════
    # Phase 2: 收集唯一实体
    # ═══════════════════════════════════════════════

    def _collect_all_entities(self, triggers: list[TriggerRecord]) -> tuple[set[str], set[str]]:
        """遍历所有 trigger 条件树，收集唯一 ticker 和 sector。"""
        all_tickers: set[str] = set()
        all_sectors: set[str] = set()

        for t in triggers:
            conditions = t.condition
            if not conditions or conditions == {}:
                continue
            if isinstance(conditions, dict) and "error" in conditions:
                continue

            atoms_in_tree = self._collect_atoms(conditions)
            for _, (_, params) in atoms_in_tree.items():
                if "ticker" in params:
                    all_tickers.add(params["ticker"])
                if "sector" in params:
                    all_sectors.add(params["sector"])
                if "sector_a" in params:
                    all_sectors.add(params["sector_a"])
                if "sector_b" in params:
                    all_sectors.add(params["sector_b"])

        return all_tickers, all_sectors

    # ═══════════════════════════════════════════════
    # Phase 2.5: 分析数据需求
    # ═══════════════════════════════════════════════

    def _analyze_atom_requirements(
        self, triggers: list[TriggerRecord]
    ) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
        """分析活跃 trigger 需要哪些数据，返回按需预取的指引。

        Returns:
            ticker_needs: {ticker: {tech, snapshot, turnover, ...}}
            sector_needs: {sector: {overview, members, leader, ...}}
            member_needs: {sector: {zdt_record, snapshot, ...}}
        """
        ticker_needs: dict[str, set[str]] = {}
        sector_needs: dict[str, set[str]] = {}
        member_needs: dict[str, set[str]] = {}

        for t in triggers:
            atoms_in_tree = self._collect_atoms(t.condition) if t.condition else {}
            for _, (atom_name, params) in atoms_in_tree.items():
                # ticker 数据需求
                ticker = params.get("ticker")
                if ticker:
                    if ticker not in ticker_needs:
                        ticker_needs[ticker] = set()
                    ticker_needs[ticker] |= _ATOM_TICKER_KEYS.get(atom_name, set())

                    # price_move lookback_days≠默认值 额外需要 history
                    if atom_name == "price_move":
                        default = ATOM_SCHEMA["price_move"]["optional_params"]["lookback_days"][0]
                        if params.get("lookback_days", default) != default:
                            ticker_needs[ticker].add("history")

                    # volume_ratio n_days≠默认值 额外需要 history
                    if atom_name == "volume_ratio":
                        default = ATOM_SCHEMA["volume_ratio"]["optional_params"]["n_days"][0]
                        if params.get("n_days", default) != default:
                            ticker_needs[ticker].add("history")

                # sector 数据需求
                for sector_key in ("sector", "sector_a", "sector_b"):
                    sector = params.get(sector_key)
                    if sector:
                        if sector not in sector_needs:
                            sector_needs[sector] = set()
                        sector_needs[sector] |= _ATOM_SECTOR_KEYS.get(atom_name, set())

                        # sector_move 指定 velocity_minutes 时需要 intraday
                        if atom_name == "sector_move" and params.get("velocity_minutes") is not None:
                            sector_needs[sector].add("intraday")

                        # 收集板块成员需要的 ticker 数据 key
                        member_keys = _MEMBER_NEED_KEYS.get(atom_name)
                        if member_keys:
                            if sector not in member_needs:
                                member_needs[sector] = set()
                            member_needs[sector] |= member_keys

        return ticker_needs, sector_needs, member_needs

    # ═══════════════════════════════════════════════
    # Phase 3: 构建 EvalContext
    # ═══════════════════════════════════════════════

    async def _build_eval_context(
        self,
        tickers: set[str],
        sectors: set[str],
        ticker_needs: dict[str, set[str]],
        sector_needs: dict[str, set[str]],
        member_needs: dict[str, set[str]],
    ) -> EvalContext:
        import time as _time
        now = self._now()

        ticker_data: dict[str, dict] = {}
        prices: dict[str, dict] = {}
        if tickers:
            ticker_list = list(tickers)
            t_price_0 = _time.monotonic()
            prices = await self.market.get_realtime_prices(ticker_list)
            t_price_1 = _time.monotonic()
            ticker_results = await gather_with_progress(
                "TriggerData:ticker数据预取",
                [self._prefetch_ticker(t, ticker_needs.get(t, set())) for t in ticker_list],
                report_every=max(1, len(ticker_list) // 4),
            )
            t_ticker_1 = _time.monotonic()
            logger.debug("[timing] ticker prices: {:.0f}ms, ticker prefetch ({} items): {:.0f}ms",
                        (t_price_1 - t_price_0)*1000, len(ticker_list), (t_ticker_1 - t_price_1)*1000)
            for t, result in zip(ticker_list, ticker_results, strict=False):
                if isinstance(result, Exception):
                    logger.warning("prefetch ticker {} failed: {}", t, result)
                    ticker_data[t] = {"price": prices.get(t, {}), "snapshot": {"error": str(result)}}
                else:
                    rd: dict = cast(dict, result)
                    rd["price"] = prices.get(t, {})
                    ticker_data[t] = rd

        sector_data: dict[str, dict] = {}
        t_sec_0 = t_sec_1 = _time.monotonic()
        if sectors:
            sector_list = list(sectors)
            t_sec_0 = _time.monotonic()
            sector_results = await gather_with_progress(
                "TriggerData:sector数据预取",
                [self._prefetch_sector(s, sector_needs.get(s, set())) for s in sector_list],
                report_every=max(1, len(sector_list) // 4),
            )
            t_sec_1 = _time.monotonic()
            for s, result in zip(sector_list, sector_results, strict=False):
                if isinstance(result, Exception):
                    logger.warning("prefetch sector {} failed: {}", s, result)
                    sector_data[s] = {
                        "overview": {"error": str(result)},
                        "members": [],
                        "leader": {},
                        "intraday": {},
                        "volume_ratio": {},
                    }
                else:
                    sector_data[s] = cast(dict, result)

        t_member_0 = _time.monotonic()
        await self._prefetch_member_tickers(sector_data, member_needs, prices, ticker_data)
        t_member_1 = _time.monotonic()

        market_summary = self._build_market_summary()
        t_summary = _time.monotonic()

        logger.debug("[timing] sector prefetch ({} items): {:.0f}ms, member prefetch: {:.0f}ms, market summary: {:.0f}ms",
                    len(sectors), (t_sec_1 - t_sec_0)*1000, (t_member_1 - t_member_0)*1000, (t_summary - t_member_1)*1000)

        return EvalContext(
            now=now,
            ticker_data=ticker_data,
            sector_data=sector_data,
            market_summary=market_summary,
        )

    async def _prefetch_ticker(self, ticker: str, needed_keys: set[str]) -> dict:
        """按需预取单个 ticker 的数据（并行在线程池中执行）。"""
        import time as _time
        market = self.market
        result: dict = {}
        timings: dict[str, float] = {}

        async def _timed(key: str, fn: Callable[..., Any]) -> tuple[float, Any]:
            t0 = _time.monotonic()
            r = await fn()
            timings[key] = (_time.monotonic() - t0) * 1000
            return r

        async def _fetch_snapshot() -> dict | None:
            df = await asyncio.to_thread(market.get_bars, ticker, "1m")
            if df is None or df.empty:
                return ("snapshot", {"error": f"无1分钟数据: {ticker}", "ticker": ticker})
            open_price = float(df["open"].iloc[0])
            latest_close = float(df["close"].iloc[-1])
            high = float(df["high"].max())
            low = float(df["low"].min())
            latest_pct = round((latest_close - open_price) / open_price * 100, 2) if open_price else 0.0
            high_pct = round((high - open_price) / open_price * 100, 2) if open_price else 0.0
            low_pct = round((low - open_price) / open_price * 100, 2) if open_price else 0.0
            # 向量化构建 bars 列表
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
            return ("snapshot", {
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
            })

        async def _fetch_turnover() -> list[dict]:
            return ("turnover", await asyncio.to_thread(market.get_turnover_rate, ticker))

        async def _fetch_zdt_record() -> list[dict]:
            rec = await asyncio.to_thread(market.get_zdt_record, ticker)
            return ("zdt_record", rec if isinstance(rec, dict) else {})

        async def _fetch_history() -> dict[str, list[dict]]:
            history = await asyncio.to_thread(market.get_price_history, ticker, None, None)
            return ("history", history if isinstance(history, dict) else {})

        fetchers = {
            "snapshot": _fetch_snapshot,
            "turnover": _fetch_turnover,
            "zdt_record": _fetch_zdt_record,
            "history": _fetch_history,
        }

        tasks = [_timed(k, fetchers[k]) for k in fetchers if k in needed_keys]
        if tasks:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for item in gathered:
                if isinstance(item, BaseException):
                    logger.warning("prefetch ticker {} data failed: {}", ticker, item)
                elif item is not None:
                    key, val = item
                    result[key] = val
            logger.debug("[timing] {} data timings: {}", ticker, timings)

        return result

    async def _prefetch_sector(self, sector: str, needed_keys: set[str]) -> dict:
        """按需预取单个 sector 的数据。"""
        market = self.market

        overview = {}
        if "overview" in needed_keys:
            overview = await market.get_sector_overview(sector)

        concept_code = overview.get("code", "") or overview.get("concept_code", "")
        members: list[str] = []
        intraday: dict = {}

        if "members" in needed_keys and concept_code:
            members = await asyncio.to_thread(market.get_concept_members, concept_code)
        if "intraday" in needed_keys and concept_code:
            intraday = await asyncio.to_thread(market.get_sector_intraday, concept_code, True)

        return {
            "overview": overview,
            "members": members if isinstance(members, list) else [],
            "intraday": intraday if isinstance(intraday, dict) else {},
        }

    async def _prefetch_member_tickers(
        self,
        sector_data: dict[str, dict],
        member_needs: dict[str, set[str]],
        all_prices: dict[str, dict],
        ticker_data: dict[str, dict],
    ) -> None:
        """补充预取板块成员的 ticker 数据（并行化）。"""
        all_member_tickers: set[str] = set()
        for sector, _needed in member_needs.items():
            sec = sector_data.get(sector, {})
            for ticker in sec.get("members", []):
                if ticker not in ticker_data:
                    all_member_tickers.add(ticker)

        if not all_member_tickers:
            return

        member_ticker_needs: dict[str, set[str]] = {}
        for sector, needed in member_needs.items():
            sec = sector_data.get(sector, {})
            for ticker in sec.get("members", []):
                if ticker not in member_ticker_needs:
                    member_ticker_needs[ticker] = set()
                member_ticker_needs[ticker] |= needed

        # 构建本地价格字典，避免入参 all_prices 的隐藏副作用
        member_prices: dict[str, dict] = {}
        missing_for_price = [t for t in all_member_tickers if t not in all_prices]
        if missing_for_price:
            try:
                extra_prices = await self.market.get_realtime_prices(missing_for_price)
                member_prices.update(extra_prices)
            except Exception as e:
                logger.warning("预取成员 ticker 价格失败: {}", e)
        for t in all_member_tickers:
            if t in all_prices:
                member_prices[t] = all_prices[t]

        # 并行预取所有成员 ticker 数据
        async def _fetch_member_data(ticker: str) -> tuple[str, dict]:
            needed = member_ticker_needs.get(ticker, set())
            result: dict = {}

            if "zdt_record" in needed:
                rec = await asyncio.to_thread(self.market.get_zdt_record, ticker)
                result["zdt_record"] = rec if isinstance(rec, dict) else {}

            if "snapshot" in needed:
                df = await asyncio.to_thread(self.market.get_bars, ticker, "1m")
                if df is not None and not df.empty:
                    result["snapshot"] = {
                        "ticker": ticker,
                        "price": float(df["close"].iloc[-1]),
                        "available": True,
                    }

            result["price"] = member_prices.get(ticker, {})
            return ticker, result

        results = await gather_with_progress(
            "TriggerData:板块成员数据预取",
            [_fetch_member_data(t) for t in all_member_tickers],
            report_every=max(1, len(all_member_tickers) // 10),
        )

        for item in results:
            if isinstance(item, Exception):
                logger.warning("预取成员 ticker 数据失败: {}", item)
            elif item is not None:
                ticker, data = item
                ticker_data[ticker] = data

    def _build_market_summary(self) -> dict:
        """构建扁平格式的市场概况（含市场情绪评分所需的所有字段）。"""
        market = self.market
        breadth = market.get_market_breadth()
        if "error" in breadth:
            return {
                "up_down_ratio": 1.0,
                "avg_pct_chg": 0.0,
                "total_amount_yi": 0.0,
                "index_overview": {},
                "amount_ratio": 1.0,
                "zdt_follow_pct": 0.0,
                "zdt_follow_rate": 0.0,
                "zdt_consecutive_rate": 0.0,
                "error": breadth["error"],
            }

        # ── 指数概览 ──
        index_overview = market.get_index_overview()

        # ── 量比计算（当日成交额 / 近5日均成交额） ──
        amount_ratio = 1.0
        total_amount_yi = breadth.get("total_amount_yi", 0.0)
        if total_amount_yi > 0:
            try:
                daily_window = market.trading_days
                if daily_window:
                    today_str = market.clock.today_str
                    prev_days = [d for d in daily_window if d < today_str][-5:]
                    amts = []
                    for d in prev_days:
                        snap = market.get_market_snapshot(d)
                        amt = snap.get("total_amount", 0)
                        if amt > 0:
                            amts.append(amt)
                    if amts:
                        avg_amt_5d = sum(amts) / len(amts)
                        amount_ratio = round(total_amount_yi / avg_amt_5d, 2) if avg_amt_5d > 0 else 1.0
            except Exception:
                pass

        # ── 昨日涨停今日表现 ──
        zdt_follow = market.get_zdt_follow_through()

        return {
            "up_down_ratio": breadth.get("up_down_ratio", 1.0),
            "avg_pct_chg": breadth.get("avg_pct_chg", 0.0),
            "total_amount_yi": total_amount_yi,
            "index_overview": index_overview,
            "amount_ratio": amount_ratio,
            "zdt_follow_pct": zdt_follow.get("zdt_follow_pct", 0.0),
            "zdt_follow_rate": zdt_follow.get("zdt_follow_rate", 0.0),
            "zdt_consecutive_rate": zdt_follow.get("zdt_consecutive_rate", 0.0),
        }

    # ═══════════════════════════════════════════════
    # Phase 4 + 5: 评估 + 处理
    # ═══════════════════════════════════════════════

    async def _evaluate_all(self) -> int:
        import time as _time
        t0 = _time.monotonic()

        # Phase 1: 加载 + 过滤
        active_triggers = await self._load_active_triggers()
        if not active_triggers:
            return 0

        # Phase 2: 收集唯一实体
        all_tickers, all_sectors = self._collect_all_entities(active_triggers)

        # Phase 2.5: 分析数据需求
        ticker_needs, sector_needs, member_needs = self._analyze_atom_requirements(active_triggers)

        t1 = _time.monotonic()
        logger.debug("[timing] Phase 1-2.5 (load+analyze): {:.0f}ms, active={}, tickers={}, sectors={}, needs={}",
                    (t1 - t0)*1000, len(active_triggers), len(all_tickers), len(all_sectors), dict(ticker_needs))

        # Phase 3: 构建 EvalContext（按需预取）
        ctx = await self._build_eval_context(all_tickers, all_sectors, ticker_needs, sector_needs, member_needs)

        t2 = _time.monotonic()
        logger.debug("[timing] Phase 3 (build EvalContext): {:.0f}ms", (t2 - t1)*1000)

        # Phase 4: 去重评估所有唯一 atom → atom_cache
        # atom key: (atom_name, dedup_key) → bool
        # 其中 dedup_key 是 params 的 JSON 序列化（支持不可 hash 值）
        unique_atoms: dict[str, bool] = {}
        # trigger_id → {path → dedup_key}
        trigger_atom_paths: dict[str, dict[str, str]] = {}

        for t in active_triggers:
            conditions = t.condition
            if not conditions or conditions == {}:
                continue
            if isinstance(conditions, dict) and "error" in conditions:
                continue

            atoms_in_tree = self._collect_atoms(conditions)
            trigger_atom_paths[str(t.id)] = {}
            for path, (atom_name, params) in atoms_in_tree.items():
                dedup_key = _dedup_key(atom_name, params)
                trigger_atom_paths[str(t.id)][path] = dedup_key
                if dedup_key not in unique_atoms:
                    unique_atoms[dedup_key] = False

        # 批量评估所有唯一 atom（同步，纯 CPU）
        for dedup_key in list(unique_atoms):
            atom_name, params = _parse_dedup_key(dedup_key)
            result: dict = evaluate_atom(atom_name, params, ctx)
            unique_atoms[dedup_key] = bool(result.get("triggered", False))

        t3 = _time.monotonic()
        logger.debug("[timing] Phase 4 (evaluate {} unique atoms): {:.0f}ms", len(unique_atoms), (t3 - t2)*1000)

        # 并行评估所有 trigger 的条件树（使用路径匹配）
        async def evaluate_trigger(t: TriggerRecord) -> TriggerRecord | None:
            tid = str(t.id)
            if tid not in trigger_atom_paths:
                return None
            atom_results: dict[str, bool] = {}
            for path, dedup_key in trigger_atom_paths[tid].items():
                atom_results[path] = unique_atoms.get(dedup_key, False)
            if evaluate_condition_tree(t.condition, atom_results):
                return t
            return None

        results = await gather_with_progress(
            "TriggerBatch:触发条件评估",
            [evaluate_trigger(t) for t in active_triggers],
            report_every=max(1, len(active_triggers) // 4),
        )

        # Phase 5: 批量处理触发的 trigger
        triggered_triggers: list[TriggerRecord] = []
        for r in results:
            if isinstance(r, Exception):
                logger.opt(exception=True).error("trigger evaluation exception: {}", r)
            elif r is not None:
                triggered_triggers.append(cast(TriggerRecord, r))

        if triggered_triggers:
            log_progress(
                "Trigger",
                "批量命中",
                count=len(triggered_triggers),
            )
            for t in triggered_triggers:
                logger.info("Trigger hit: name={}, id={}, action_type={}", t.name, t.id, t.action_type)
                _now = self._now()
                # 先标记为 triggered，再执行回调，回调成功后改为 completed
                async def _execute_callback(trigger: TriggerRecord) -> None:
                    try:
                        # 先标记为 triggered，防止重复评估
                        async with async_session() as db:
                            await db.execute(
                                update(TriggerRecord).where(TriggerRecord.id == trigger.id).values(
                                    status="triggered", triggered_at=_now
                                )
                            )
                            await db.commit()

                        # 执行回调
                        await self._on_trigger(trigger)

                        # 回调成功，标记为 completed（由 _on_trigger 内部完成）
                    except Exception as e:
                        logger.error("Trigger {} callback failed: {} ({})", trigger.id, e, type(e).__name__)
                        # 回调失败，回退到 waiting 以便下一轮重试
                        try:
                            async with async_session() as db:
                                await db.execute(
                                    update(TriggerRecord).where(TriggerRecord.id == trigger.id).values(
                                        status="waiting"
                                    )
                                )
                                await db.commit()
                        except Exception as re:
                            logger.error("Trigger {} 回退 waiting 失败: {}", trigger.id, re)

                self._pending_callbacks.append(asyncio.create_task(_execute_callback(t)))

        log_progress(
            "TriggerBatch",
            "评估完成",
            triggers=len(active_triggers),
            triggered=len(triggered_triggers),
        )
        return len(active_triggers)

    async def flush_pending(self) -> None:
        """等待所有尚未完成的 trigger 回调执行完毕。"""
        if not self._pending_callbacks:
            return
        tasks = self._pending_callbacks
        self._pending_callbacks = []
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30.0,
            )
            ok_count = sum(1 for r in results if not isinstance(r, Exception))
            err_count = sum(1 for r in results if isinstance(r, Exception))
            if ok_count > 0 or err_count > 0:
                logger.info("Trigger callbacks completed: ok={}, err={}", ok_count, err_count)
            for r in results:
                if isinstance(r, Exception):
                    logger.opt(exception=True).error("trigger callback exception: {}", r)
        except TimeoutError:
            logger.error(
                "trigger callbacks timed out (30s), {}/{} not completed, cancelled",
                sum(1 for t in tasks if not t.done()),
                len(tasks),
            )
            for t in tasks:
                if not t.done():
                    t.cancel()

    # ═══════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════

    def _collect_atoms(self, tree: dict) -> dict[str, tuple[str, dict]]:
        """遍历条件树，用路径作为 key，不修改原树。

        Returns: {path: (atom_name, params)}
        路径如 "0", "1", "0.1", "1.2.0" 表示 children 索引路径。
        """
        atoms: dict[str, tuple[str, dict]] = {}

        def _walk(node: dict, prefix: str) -> None:
            if "atom" in node:
                atoms[prefix] = (node["atom"], node.get("params", {}))
            for i, child in enumerate(node.get("children", [])):
                child_prefix = f"{prefix}.{i}" if prefix else str(i)
                _walk(child, child_prefix)

        # 如果根节点没有 children 且不是原子叶，尝试从根开始
        if "logic" in tree and "children" in tree:
            children = tree.get("children", [])
            if not isinstance(children, list) or len(children) == 0:
                logger.warning("_collect_atoms: 逻辑节点缺少 children 列表，跳过")
                return atoms
            for i, child in enumerate(children):
                _walk(child, str(i))
        elif "atom" in tree:
            _walk(tree, "0")

        return atoms

    def _now(self) -> datetime:
        if self.market.clock is not None:
            return self.market.clock.now
        return datetime.now(BEIJING_TZ)


# ═══════════════════════════════════════════════
# 去重 key 工具（模块级，供测试使用）
# ═══════════════════════════════════════════════


def _dedup_key(atom_name: str, params: dict) -> str:
    """生成去重 key：JSON 序列化，支持任意可 JSON 化的参数值。"""
    try:
        params_json = json.dumps(params, sort_keys=True, ensure_ascii=False)
    except TypeError as e:
        logger.warning("_dedup_key JSON 序列化失败，回退到 str 转换: atom={}, error={}", atom_name, e)
        params_json = json.dumps({k: str(v) for k, v in params.items()}, sort_keys=True, ensure_ascii=False)
    return f"{atom_name}::{params_json}"


def _parse_dedup_key(key: str) -> tuple[str, dict]:
    """从去重 key 反序列化出 (atom_name, params)。"""
    atom_name, params_json = key.split("::", 1)
    return atom_name, json.loads(params_json)
