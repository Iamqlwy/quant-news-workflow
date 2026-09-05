"""流水线引擎 —— 时间窗口驱动的两模式编排器

夜间 (15:00 → 次日 8:00)：仅跑 significance 重要性判断。
日间 (8:00 → 15:00)：每个 task 一次性走完整条链：
  ingested → significance → deep_analysis → risk_control → reflection_pending。

日间首次进入时触发 morning_init：先为 6:00-8:00 间隙的 INGESTED 补 significance，
再执行 macro_daily，最后处理所有积压任务。

_chain_task 采用瀑布式保存：每完成一个阶段立即写 DB，后续阶段失败不会丢失前序成果。
"""

import asyncio
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
from kbquant.client import QuantClient
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src.agents.deep_analysis import create_deep_analysis_agent
from src.agents.macro_daily import create_macro_agent
from src.agents.reflection import create_reflection_agent
from src.agents.risk_control import create_risk_agent
from src.config import settings
from src.core.clock import Clock
from src.core.timezone import BEIJING_TZ
from src.db import async_session
from src.llm.provider import LLMProvider
from src.market import MarketDataProvider
from src.models.tables import Entity, Task
from src.pipeline.significance import SignificanceJudge
from src.pipeline.states import ACTIONABLE_STATES, TaskState
from src.tools import get_registry_items, init_ctx
from src.triggers.compiler import TriggerCompiler
from src.workflow_logging import gather_with_progress, log_progress


@dataclass(frozen=True)
class WindowConfig:
    """时间窗口配置 —— 修改 tick 节奏或早晨处理时间只需改这里。"""
    morning_start_minutes: int = 8 * 60    # 8:00，日间模式开始
    evening_start_minutes: int = 15 * 60   # 15:00，夜间模式开始


def _sem(name: str, limit: int) -> asyncio.Semaphore:
    logger.debug("Agent 并发限制 {}={}", name, limit)
    return asyncio.Semaphore(limit)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转换为 float，LLM 返回 null/字符串时回退到默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# asyncpg connection-drop 异常，触发自动重试
# SQLAlchemy 可能将 asyncpg 异常包装为 DBAPIError，异步模式下也可能直接抛出裸异常
try:
    from sqlalchemy.exc import DBAPIError
    _DB_RETRYABLE = (
        asyncpg.exceptions.ConnectionDoesNotExistError,
        asyncpg.exceptions.ConnectionFailureError,
        asyncpg.exceptions.InterfaceError,
        DBAPIError,
    )
except ImportError:
    _DB_RETRYABLE = (
        asyncpg.exceptions.ConnectionDoesNotExistError,
        asyncpg.exceptions.ConnectionFailureError,
        asyncpg.exceptions.InterfaceError,
    )

async def _db_retry(fn: Callable[[], Awaitable[Any]], label: str, max_retries: int = 3, base_delay: float = 0.5) -> Any:
    """对 PostgreSQL 连接断开等瞬态错误进行重试。非重试异常直接抛出。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except _DB_RETRYABLE as exc:
            last_exc = exc
            if attempt >= max_retries - 1:
                break
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            logger.warning(
                "DB 连接异常 ({}): {}，第 {}/{} 次重试，{:.1f}s 后重试",
                label, exc, attempt + 1, max_retries, delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# 已完成的终态（无需再处理）
_COMPLETED_STATES: frozenset[TaskState] = frozenset({
    TaskState.REFLECTION_COMPLETE,
    TaskState.SKIPPED,
    TaskState.NO_VALUE,
    TaskState.MACRO_REPORT_UPDATED,
})


class PipelineOrchestrator:
    def __init__(
        self,
        quant: QuantClient,
        market: MarketDataProvider,
        windows: WindowConfig | None = None,
        *,
        judge_provider: LLMProvider,
        deep_analysis_provider: LLMProvider,
        risk_control_provider: LLMProvider,
        macro_provider: LLMProvider,
        reflection_provider: LLMProvider,
        clock: Clock | None = None,
    ) -> None:
        self.quant = quant
        self.market = market
        self.clock = clock
        self._windows = windows or WindowConfig()
        self.sig_judge = SignificanceJudge(judge_provider, quant=quant, clock=clock)
        self._deep_analysis_provider = deep_analysis_provider
        self._risk_control_provider = risk_control_provider
        self._macro_provider = macro_provider
        self._reflection_provider = reflection_provider
        self._compiler: TriggerCompiler | None = None

        # Agent 并发控制
        cfg = settings
        self._global_sem = _sem("global", cfg.agent_global_concurrency)
        reflection_sem = _sem("reflection", cfg.agent_reflection_concurrency)
        macro_sem = _sem("macro", cfg.agent_macro_concurrency)
        self._sems: dict[TaskState, asyncio.Semaphore] = {
            TaskState.INGESTED: _sem("significance", cfg.agent_global_concurrency),
            TaskState.DEEP_ANALYZING: _sem("deep_analysis", cfg.agent_deep_analysis_concurrency),
            TaskState.RISK_CHECKING: _sem("risk_control", cfg.agent_risk_control_concurrency),
            TaskState.REFLECTION_PENDING: reflection_sem,
            TaskState.REFLECTION_EXECUTING: reflection_sem,
            TaskState.MACRO_URGENT: macro_sem,
        }
        init_ctx(quant=quant, market=market, compiler=None, clock=clock)
        self._day_done_date: str | None = None  # 已完成早晨初始化的日期 (YYYYMMDD)，用于跨天重置
        self._last_logged_mode: str | None = None  # 记录上一次日志模式，用于检测模式切换
        self._morning_init_lock = asyncio.Lock()
        self._macro_run_lock = asyncio.Lock()

    def _ensure_compiler(self) -> TriggerCompiler:
        if self._compiler is None:
            self._compiler = TriggerCompiler(market=self.market)
        init_ctx(quant=self.quant, market=self.market, compiler=self._compiler, clock=self.clock)
        return self._compiler

    def _semaphore_for_task(self, task: Task) -> asyncio.Semaphore | None:
        """按任务当前阶段选择 Agent 并发限制。"""
        state = TaskState(task.state)
        if state == TaskState.DEEP_ANALYZED:
            return self._sems.get(TaskState.DEEP_ANALYZING)
        if state == TaskState.RISK_VERIFIED:
            return self._sems.get(TaskState.RISK_CHECKING)
        return self._sems.get(state)

    # ── 模式判断 ──────────────────────────────

    @property
    def _mode(self) -> str:
        """根据当前时钟返回 "night" 或 "day"。非交易日始终为 night。"""
        if not self.market.is_trading_day:
            return "night"
        now = self._now()
        m = now.hour * 60 + now.minute
        if m >= self._windows.evening_start_minutes or m < self._windows.morning_start_minutes:
            return "night"
        return "day"

    async def tick(self, *, force_full_pipeline: bool = False) -> int:
        """时间感知的 pipeline tick —— 夜间仅 triage，日间完整链式处理。

        force_full_pipeline=True 时跳过模式检测，处理所有 actionable 状态（模拟 cleanup 用）。
        """
        current_mode = "force_full" if force_full_pipeline else self._mode
        if not force_full_pipeline and current_mode != self._last_logged_mode:
            logger.info("Pipeline 模式切换: {} -> {}", self._last_logged_mode or "初始化", current_mode)

        if force_full_pipeline:
            self._last_logged_mode = current_mode
            return await self._tick_day(skip_morning_init=True)
        if self._mode == "night":
            # 从日间切到夜间的第一个 tick，先跑一次完整的日间 tick 清空积压
            if self._last_logged_mode == "day":
                logger.info("Pipeline 日→夜过渡：先清空积压的非 INGESTED 任务")
                self._last_logged_mode = "night"
                return await self._tick_day(skip_morning_init=True)
            self._last_logged_mode = current_mode
            return await self._tick_night()
        self._last_logged_mode = current_mode
        return await self._tick_day()

    # ── 夜间 tick ──────────────────────────────

    async def _tick_night(self) -> int:
        """夜间模式：仅对 INGESTED 任务做 significance 判断。"""
        tasks = await self._fetch_and_lock([TaskState.INGESTED])
        if not tasks:
            return 0

        sem = self._sems.get(TaskState.INGESTED)

        async def run_one(task: Task) -> None:
            async with self._global_sem:
                if sem is not None:
                    async with sem:
                        await self._run_significance_and_save(task)
                else:
                    await self._run_significance_and_save(task)

        log_progress("PipelineTick", "夜间Triage", fetched=len(tasks))
        results = await gather_with_progress(
            "PipelineTick:夜间Triage",
            [run_one(t) for t in tasks],
            report_every=max(1, len(tasks) // 10),
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.opt(exception=True).error("Task {} (id={}) 执行异常: {}", i, getattr(tasks[i], "id", "?"), r)
        log_progress("PipelineTick", "夜间Triage完成", processed=len(tasks))
        return len(tasks)

    async def _run_significance_and_save(self, task: Task) -> None:
        """跑 significance 并写回 DB。夜间模式专用——不做后续 chain。"""
        try:
            await self._run_significance(task)
        except Exception:
            task.state = TaskState.ERROR
            task.updated_at = self._now()
            raise
        finally:
            try:
                async def _release() -> None:
                    async with async_session() as db:
                        t = await db.merge(task)
                        t.locked_at = None
                        await db.commit()
                await _db_retry(_release, f"release_lock task={task.id}")
            except Exception:
                logger.opt(exception=True).error("Task {} 锁释放失败，需等待超时回收", task.id)

    # ── 日间 tick ──────────────────────────────

    async def _tick_day(self, *, skip_morning_init: bool = False) -> int:
        """日间模式：早晨初始化（仅一次）+ 所有 actionable 任务走完整链。"""
        today_str = self._now().strftime("%Y%m%d")
        if self._day_done_date != today_str and not skip_morning_init:
            async with self._morning_init_lock:
                if self._day_done_date != today_str:
                    await self._morning_init()
                    self._day_done_date = today_str

        tasks = await self._fetch_and_lock(ACTIONABLE_STATES)
        if not tasks:
            return 0

        async def run_chain(task: Task) -> None:
            try:
                async with self._global_sem:
                    sem = self._semaphore_for_task(task)
                    if sem is not None:
                        async with sem:
                            await self._chain_task(task)
                    else:
                        await self._chain_task(task)
            except Exception as e:
                logger.error("Task {} 链式处理异常: {}", task.id, repr(e))
                # _chain_task 内部已逐阶段保存；这里只确保锁释放，不覆盖已持久化的成果
                async def _release() -> None:
                    async with async_session() as db:
                        t = await db.merge(task)
                        if TaskState(task.state) not in _COMPLETED_STATES and task.state != TaskState.ERROR:
                            t.state = TaskState.ERROR
                            t.updated_at = self._now()
                        t.locked_at = None
                        await db.commit()
                try:
                    await _db_retry(_release, f"release_on_error task={task.id}")
                except Exception:
                    logger.opt(exception=True).error("Task {} 异常后锁释放失败，需等待超时回收", task.id)

        log_progress(
            "PipelineTick",
            "日间处理",
            fetched=len(tasks),
            states=self._summarize_states(tasks),
        )
        results = await gather_with_progress(
            "PipelineTick:日间处理",
            [run_chain(t) for t in tasks],
            report_every=max(1, len(tasks) // 10),
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.opt(exception=True).error("Task {} (id={}) 执行异常: {}", i, getattr(tasks[i], "id", "?"), r)
        log_progress("PipelineTick", "日间处理完成", processed=len(tasks))
        return len(tasks)

    async def _morning_init(self) -> None:
        """早晨初始化：(a) 为早于 8:00 创建但未做 significance 的 INGESTED 补做；
        (b) 创建并执行 macro_daily，等待完成。"""
        logger.info("MorningInit 开始, clock={}", self._now())
        # (a) 补做 6:00-8:00 间隙遗漏的 significance
        morning = self._windows.morning_start_minutes
        pre_8am = await self._fetch_ingested_before(morning)
        if pre_8am:
            sem = self._sems.get(TaskState.INGESTED)

            async def run_one(task: Task) -> None:
                async with self._global_sem:
                    if sem is not None:
                        async with sem:
                            await self._run_significance_and_save(task)
                    else:
                        await self._run_significance_and_save(task)

            log_progress("MorningInit", "补做盘前Significance", count=len(pre_8am))
            results = await gather_with_progress(
                "MorningInit:补做Significance",
                [run_one(t) for t in pre_8am],
                report_every=max(1, len(pre_8am) // 10),
            )
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    logger.warning("MorningInit 补做 significance 失败: task={}, {} ({})", getattr(pre_8am[i], "id", "?"), r, type(r).__name__)

        # (b) 消费所有积压的宏观资讯
        async with async_session() as db:
            await self._run_macro_batch(db)

        log_progress("MorningInit", "完成")

    async def _fetch_ingested_before(self, minutes_since_midnight: int) -> list[Task]:
        """拉取 INGESTED 状态且 created_at 时间早于指定分钟数的任务。"""

        async def _do() -> list[Task]:
            async with async_session() as db:
                result = await db.execute(
                    select(Task).where(Task.state == TaskState.INGESTED)
                )
                tasks = list(result.scalars().all())
            return [
                t for t in tasks
                if t.created_at is not None
                and t.created_at.hour * 60 + t.created_at.minute < minutes_since_midnight
            ]

        return await _db_retry(_do, "fetch_ingested_before")

    # ── 任务链：瀑布式完整生命周期 ──────────────

    async def _chain_task(self, task: Task) -> None:
        """瀑布式链式处理：每完成一个阶段立即写 DB，再继续下一个阶段。
        每个阶段完成后不依赖局部变量，直接从 task.state 判断下一步。
        """
        start_state = TaskState(task.state)
        log_progress("Task", "链式处理开始", task_id=task.id, state=start_state)

        # ── Stage 1: INGESTED → significance → 接着走 Stage 2 ──
        if TaskState(task.state) == TaskState.INGESTED:
            await self._run_significance(task)
            await self._save_progress(task, release_lock=False)  # 中间保存，不释放锁
            log_progress("Task", "Significance完成", task_id=task.id, next_state=TaskState(task.state))

        # ── Stage 2: DEEP_ANALYZING → deep_analysis → route → 状态自然流入后续 if ──
        if TaskState(task.state) == TaskState.DEEP_ANALYZING:
            await self._run_deep_analysis(task)
            # _run_deep_analysis 检测到"无深度分析价值"会直接设为 REFLECTION_COMPLETE 并返回
            if TaskState(task.state) == TaskState.NO_VALUE:
                await self._save_progress(task)
                log_progress("Task", "深度分析早退(无分析价值)", task_id=task.id)
                return
            await self._route_post_analysis(task)
            await self._save_progress(task, release_lock=False)  # 中间保存，不释放锁
            log_progress("Task", "深度分析完成", task_id=task.id, next_state=TaskState(task.state))
            # 不 return — route 已将状态设为 RISK_CHECKING(buy) 或 REFLECTION_PENDING(non-buy)，
            # 让下面的 if 块自然命中

        # ── Stage 2 recovery: DEEP_ANALYZED → route ──
        if TaskState(task.state) == TaskState.DEEP_ANALYZED:
            await self._route_post_analysis(task)
            await self._save_progress(task, release_lock=False)  # 中间保存，不释放锁
            log_progress("Task", "深度分析分流恢复完成", task_id=task.id, next_state=TaskState(task.state))

        # ── Stage 2b: RISK_CHECKING（独立进入，如从夜间积压） ──
        if TaskState(task.state) == TaskState.RISK_CHECKING:
            await self._run_risk_control(task)
            task.state = TaskState.REFLECTION_PENDING
            if not task.reflection_at:
                task.reflection_at = self._now() + timedelta(days=settings.default_reflection_delay_days)
            task.updated_at = self._now()
            await self._save_progress(task)
            return

        # ── Stage 3: REFLECTION ──
        if TaskState(task.state) == TaskState.REFLECTION_PENDING:
            if task.reflection_at and self._now() < task.reflection_at:
                await self._save_progress(task)  # 还没到复盘时间，释放锁等待下次轮询
                return
            task.state = TaskState.REFLECTION_EXECUTING
            task.updated_at = self._now()
            await self._save_progress(task, release_lock=False)  # 中间保存，不释放锁
            await self._run_reflection(task)
            await self._save_progress(task)  # 最终保存，释放锁
            return

        if TaskState(task.state) == TaskState.REFLECTION_EXECUTING:
            await self._run_reflection(task)
            await self._save_progress(task)
            return

        # ── 宏观 ──
        if TaskState(task.state) == TaskState.MACRO_URGENT:
            async with async_session() as db:
                await self._run_macro_batch(db, task)
            return

        # 最终释放锁
        await self._save_progress(task)

        logger.info("Task 链式处理完成, id={}, from={}, to={}", task.id, start_state, task.state)
        log_progress(
            "Task", "链式处理完成",
            task_id=task.id, from_state=start_state, final_state=task.state,
        )

    async def _save_progress(self, task: Task, *, release_lock: bool = True) -> None:
        """将 task 的当前状态持久化到 DB。

        使用 UPDATE 语句而非 merge() 避免 ORM session 混用问题。
        同时将 _collect_entities 缓冲的 Entity 行在同一事务中写入。

        Args:
            task: 要保存的任务对象
            release_lock: 是否释放锁（locked_at=None）。链式处理中间的保存应传 False，
                         防止其他 tick 在链未完成时捡到同一任务导致重复处理。
        """

        async def _do_save() -> None:
            async with async_session() as db:
                updates = {
                    "state": task.state,
                    "updated_at": task.updated_at or self._now(),
                }

                if release_lock:
                    updates["locked_at"] = None

                if task.info_type is not None:
                    updates["info_type"] = task.info_type
                if task.significance_score is not None:
                    updates["significance_score"] = task.significance_score
                if task.significance_rationale is not None:
                    updates["significance_rationale"] = task.significance_rationale
                if task.reflection_at is not None:
                    updates["reflection_at"] = task.reflection_at

                if task.analysis_ids is not None:
                    updates["analysis_ids"] = task.analysis_ids
                if task.trade_ids is not None:
                    updates["trade_ids"] = task.trade_ids
                if task.feedback_ids is not None:
                    updates["feedback_ids"] = task.feedback_ids

                # 将缓冲的 Entity 行在同一事务中写入，避免孤立记录
                # 先取走引用再写入，避免 commit 失败重试时重复添加
                pending: list[Entity] | None = getattr(task, "_pending_entities", None)
                if pending:
                    task._pending_entities = None  # type: ignore[attr-defined]
                    db.add_all(pending)

                await db.execute(
                    sa_update(Task).where(Task.id == task.id).values(**updates)
                )
                await db.commit()

        await _db_retry(_do_save, f"save_progress task={task.id}")


    @staticmethod
    async def _retry_async(fn: Callable[[], Awaitable[Any]], label: str, max_retries: int = 5, base_delay: float = 1.0) -> Any:
        """带指数退避的异步重试。

        失败时在 logger 中记录 WARNING，全部重试耗尽后抛出最后一个异常。
        """
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                return await fn()
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries - 1:
                    break
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1.0)
                logger.warning(
                    f"{label} 失败 (第 {attempt + 1}/{max_retries - 1} 次重试)，{delay:.1f}s 后重试: {exc}"
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    # ── 各阶段执行方法 ─────────────────────────

    async def _run_significance(self, task: Task) -> None:
        """执行 significance 判断，直接修改 task 的 state。"""
        log_progress("Task", "开始重要性评估", task_id=task.id, raw_info_id=task.raw_info_id)
        try:
            info = await self._retry_async(
                lambda: self.quant.information.get(task.raw_info_id),
                f"获取 raw_info_id={task.raw_info_id}",
            )
        except Exception as e:
            logger.error("fetch raw_info_id={} failed (retries exhausted): {}", task.raw_info_id, e)
            task.state = TaskState.ERROR
            task.updated_at = self._now()
            return

        result = await self.sig_judge.evaluate(
            info.title,
            info.body or "",
            info.source,
            str(info.published_at),
            raw_info_id=task.raw_info_id,
        )

        info_type = result.get("info_type", "company")
        if info_type not in ("company", "macro", "industry", "concept", "policy", "market_noise"):
            logger.warning("未知的 info_type={}，按 market_noise 处理。raw_info_id={}", info_type, task.raw_info_id)
            info_type = "market_noise"

        if info_type == "macro":
            task.info_type = "macro"
            total = _safe_float(result.get("total_score", 0))
            task.significance_score = total
            task.significance_rationale = result.get("rationale", "")
            route = result.get("route", "daily")
            if route == "drop" or total < 50:
                task.state = TaskState.SKIPPED
            elif result.get("is_urgent"):
                task.state = TaskState.MACRO_URGENT
            else:
                task.state = TaskState.MACRO_QUEUED
        elif info_type == "market_noise":
            task.info_type = "market_noise"
            task.state = TaskState.SKIPPED
            task.significance_score = _safe_float(result.get("total_score", 0))
            task.significance_rationale = result.get("rationale", "")
        else:
            task.info_type = info_type
            if result.get("is_significant"):
                task.state = TaskState.DEEP_ANALYZING
                task.significance_score = _safe_float(result.get("total_score", 0))
                task.significance_rationale = result.get("rationale", "")
            else:
                task.state = TaskState.SKIPPED
                task.significance_score = _safe_float(result.get("total_score", 0))
                task.significance_rationale = result.get("rationale", "")
        task.updated_at = self._now()
        log_progress(
            "Task",
            "重要性评估完成",
            task_id=task.id,
            info_type=info_type,
            is_significant=result.get("is_significant"),
            is_urgent=result.get("is_urgent"),
            next_state=task.state,
            score=result.get("total_score", 0),
        )

        # 将重要性评分回写到 kbquant 的 raw_information 记录
        try:
            from uuid import UUID as _UUID

            from kbquant.schemas.information import BatchUpdateImportanceRequest

            raw_score = _safe_float(result.get("total_score", 0))
            normalized_score = max(min(raw_score / 100.0, 1.0), 0.0)
            await self._retry_async(
                lambda: self.quant.information.batch_update_importance(
                    BatchUpdateImportanceRequest(scores={_UUID(task.raw_info_id): normalized_score})
                ),
                f"回写 importance_score (raw_info_id={task.raw_info_id})",
            )
        except Exception as e:
            logger.warning("回写 importance_score 到 kbquant 失败 (重试耗尽): {} (raw_info_id={})", e, task.raw_info_id)

    async def _run_deep_analysis(self, task: Task) -> None:
        self._ensure_compiler()

        agent = create_deep_analysis_agent(
            self._deep_analysis_provider.create_chat_model, task=task, clock=self.clock,
        )

        log_progress("Task:深度分析", "开始", task_id=task.id, raw_info_id=task.raw_info_id)
        result = await agent.run({"task_id": task.id})
        log_progress(
            "Task:深度分析",
            "完成",
            task_id=task.id,
            content_len=len(result.get("content", "")),
            entity_count=len(result.get("entities", {})),
        )
        all_outputs = result.get("content", [])
        if not isinstance(all_outputs, list):
            all_outputs = [all_outputs]
        await self._collect_entities(task, agent_name="deep_analysis")

        early_exit = result.get("early_exit", False)
        llm_content_risk = result.get("llm_content_risk", False)
        if early_exit:
            # 早退：无分析价值，无需复盘，直接完结
            task.state = TaskState.NO_VALUE
            task.updated_at = self._now()
            return
        if llm_content_risk:
            # 内容安全审查拒绝，直接退出当前 Agent
            task.state = TaskState.NO_VALUE
            task.updated_at = self._now()
            return


        task.state = TaskState.DEEP_ANALYZED
        days = _extract_reflection_days(all_outputs, self._now())
        task.reflection_at = self._now() + timedelta(days=days)
        task.updated_at = self._now()

    async def _route_post_analysis(self, task: Task) -> None:
        """分析完成后分流：有买入建议走风控审核，非买入交易或空直接进入复盘等待。"""
        has_buy = False
        lookup_failed: list[str] = []
        if task.trade_ids:
            for tid in task.trade_ids:
                try:
                    trade = await self._retry_async(
                        lambda tid=tid: self.quant.trading.get(tid),
                        f"查询 trade={tid}",
                    )
                    if getattr(trade, "operation_type", "") == "buy":
                        has_buy = True
                        break
                except Exception as exc:
                    lookup_failed.append(str(tid))
                    msg = str(exc) or exc.__class__.__name__
                    logger.warning("查询 trade={} 失败；未确认 buy，按非 buy 分流: {}", tid, msg)

        if has_buy:
            task.state = TaskState.RISK_CHECKING
        else:
            task.state = TaskState.REFLECTION_PENDING
        task.updated_at = self._now()
        log_progress(
            "Task",
            "分析后分流完成",
            task_id=task.id,
            trade_ids=task.trade_ids,
            has_buy=has_buy,
            trade_lookup_failed=lookup_failed,
            next_state=task.state,
        )

    async def _run_risk_control(self, task: Task) -> None:
        self._ensure_compiler()
        agent = create_risk_agent(
            self._risk_control_provider.create_chat_model, task=task, clock=self.clock,
        )
        latest_trade = task.trade_ids[-1] if task.trade_ids else None
        log_progress("Task:风控检查", "开始", task_id=task.id, trade_id=latest_trade)
        result = await agent.run({"task_id": str(task.id)})
        log_progress("Task:风控检查", "完成", task_id=task.id, content_len=len(result.get("content", "")))
        await self._collect_entities(task, agent_name="risk_control")

        task.state = TaskState.RISK_VERIFIED
        if not task.reflection_at:
            task.reflection_at = self._now() + timedelta(days=settings.default_reflection_delay_days)
        task.updated_at = self._now()

    async def _run_reflection(self, task: Task) -> None:
        self._ensure_compiler()
        logger.info("开始复盘: {}", task.id)
        agent = create_reflection_agent(
            self._reflection_provider.create_chat_model, task=task, clock=self.clock,
        )
        latest_analysis = task.analysis_ids[-1] if task.analysis_ids else None
        log_progress("Task:复盘", "开始", task_id=task.id, analysis_id=latest_analysis)
        result = await agent.run({"task_id": task.id})
        log_progress("Task:复盘", "完成", task_id=task.id, content_len=len(result.get("content", "")))
        await self._collect_entities(task, agent_name="reflection")

        task.state = TaskState.REFLECTION_COMPLETE
        task.updated_at = self._now()

    # ── 数据库工具方法 ─────────────────────────

    async def _fetch_and_lock(self, states: set[TaskState] | list[TaskState]) -> list[Task]:
        """拉取指定状态的任务并锁定（设置 locked_at）。

        使用 SELECT ... FOR UPDATE SKIP LOCKED 防止并发 tick 拿到同一批 task。
        """

        async def _do_fetch() -> list[Task]:
            async with async_session() as db:
                now = self._now()
                real_now = datetime.now(BEIJING_TZ)
                timeout = real_now - timedelta(minutes=30)
                result = await db.execute(
                    select(Task)
                    .where(
                        Task.state.in_(states),
                        or_(Task.locked_at.is_(None), Task.locked_at < timeout),
                    )
                    .order_by(Task.created_at)
                    .with_for_update(skip_locked=True)
                )
                tasks = list(result.scalars().all())
                for t in tasks:
                    t.locked_at = real_now
                    t.updated_at = now
                await db.commit()
                if tasks:
                    state_summary = ", ".join(
                        f"{s}={c}" for s, c in Counter(str(task.state) for task in tasks).items()
                    )
                    logger.debug("_fetch_and_lock: 锁定 {} 个 task | {}", len(tasks), state_summary)
                return tasks

        return await _db_retry(_do_fetch, "fetch_and_lock")

    # ── 宏观 ────────────────────────────────────

    async def _collect_macro_items(
        self,
        states: tuple[TaskState, ...],
        db: AsyncSession,
    ) -> tuple[str, list[Task]]:
        """领取宏观 task 并拉取资讯正文，返回 (格式化文本, task列表)。"""
        now = self._now()
        real_now = datetime.now(BEIJING_TZ)
        stmt = (
            select(Task)
            .where(
                Task.state.in_(states),
                or_(Task.locked_at.is_(None), Task.locked_at < real_now - timedelta(minutes=30)),
            )
            .order_by(Task.created_at)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        tasks = list(result.scalars().all())

        if not tasks:
            log_progress("MacroItems", "无待消费资讯", states=[state.value for state in states])
            return "今日无未消费的宏观资讯", []

        for t in tasks:
            t.locked_at = real_now
            t.updated_at = now
        await db.commit()

        info_tasks = [t for t in tasks if t.raw_info_id is not None]
        if not info_tasks:
            log_progress(
                "MacroItems",
                "领取完成",
                states=[state.value for state in states],
                claimed=len(tasks),
                collected=0,
            )
            return "今日无未消费的宏观资讯", tasks

        ids = [t.raw_info_id for t in info_tasks]
        try:
            infos = await self._retry_async(
                lambda: self.quant.information.get_many(ids),
                f"批量拉取 {len(ids)} 条资讯",
            )
        except Exception as e:
            task_ids = [str(t.id) for t in info_tasks[:5]]
            logger.warning("批量拉取 {} 条资讯失败 (重试耗尽, {}: {}), tasks={}, 本轮跳过，等待下次重试", len(ids), type(e).__name__, e, task_ids)
            return "拉取资讯失败，请稍后重试", []

        summaries: list[str] = []
        for info in sorted(infos, key=lambda i: i.published_at):
            title = info.title
            source = info.source
            body = (info.body or "")[:300]
            summaries.append(f"* [{source}] {title}\n  {body}")

        log_progress(
            "MacroItems",
            "领取完成",
            states=[state.value for state in states],
            claimed=len(tasks),
            collected=len(summaries),
        )
        return f"未消费宏观资讯（共 {len(info_tasks)} 条）：\n" + "\n".join(summaries), tasks

    def _consume_macro_items(self, tasks: list[Task]) -> None:
        """将 macro items 标记为已消费。在 agent 成功运行后调用。"""
        now = self._now()
        for t in tasks:
            t.state = TaskState.MACRO_REPORT_UPDATED
            t.locked_at = None
            t.updated_at = now
        if tasks:
            log_progress("MacroItems", "标记已消费", consumed=len(tasks))

    async def _run_macro_batch(self, db: AsyncSession, trigger_task: Task | None = None) -> None:
        """宏观批量处理：收集所有待消费的 MACRO_URGENT 和 MACRO_QUEUED 条目，
        按时间排序统一处理，调用 agent 一次性生成报告。

        通过 _macro_run_lock 确保同一时刻只有一条 agent 在跑。
        trigger_task 为 None 时表示早晨定时触发。
        """
        async with self._macro_run_lock:
            items_text, macro_tasks = await self._collect_macro_items(
                (TaskState.MACRO_URGENT, TaskState.MACRO_QUEUED),
                db=db,
            )

            if not macro_tasks:
                log_progress("Task:宏观批量分析", "无待消费条目，跳过")
                if trigger_task is not None:
                    trigger_task.state = TaskState.MACRO_REPORT_UPDATED
                    trigger_task.locked_at = None
                    trigger_task.updated_at = self._now()
                    await db.commit()
                return

            agent = create_macro_agent(
                self._macro_provider.create_chat_model, task=trigger_task, clock=self.clock,
            )
            task_id_log = trigger_task.id if trigger_task is not None else f"morning_{self.clock.today_str}"
            log_progress("Task:宏观批量分析", "开始", task_id=task_id_log, item_count=len(macro_tasks))
            result = await agent.run({
                "task_id": task_id_log,
                "macro_type": "urgent",
                "today_macro_items": items_text,
            })
            log_progress(
                "Task:宏观批量分析",
                "完成",
                task_id=task_id_log,
                content_len=len(result.get("content", "")),
            )
            self._consume_macro_items(macro_tasks)
            if trigger_task is not None:
                trigger_task.state = TaskState.MACRO_REPORT_UPDATED
                trigger_task.locked_at = None
                trigger_task.updated_at = self._now()
            await db.commit()

    async def _collect_entities(self, task: Task, agent_name: str = "") -> None:
        """收集 session registry 中的所有实体引用，缓冲到 task._pending_entities。
        实际的 DB 写入由 _save_progress 在同一事务中完成。"""
        registry_items = get_registry_items()

        logger.debug("_collect_entities: task={}, registry_size={}", task.id, len(registry_items))

        pending: list[Entity] = []
        for ref, info in registry_items.items():
            prefix = ref[0]
            if prefix not in ("A", "T", "F", "N", "G", "R"):
                continue
            pending.append(
                Entity(
                    entity_type=prefix,
                    entity_uuid=UUID(info["uuid"]),
                    ref=ref,
                    source=info["source"],
                    task_id=task.id,
                    agent_name=agent_name,
                    created_at=self._now(),
                )
            )

        # 将 source="create" 的 ID 追加到 task 的 JSONB 列（保持原有行为）
        for src_key, task_attr in [("A", "analysis_ids"), ("T", "trade_ids"), ("F", "feedback_ids")]:
            created_ids = [info["uuid"] for ref, info in registry_items.items()
                           if ref.startswith(src_key) and info["source"] == "create"]
            if not created_ids:
                continue
            current = getattr(task, task_attr, None)
            if current is None:
                current = []
                setattr(task, task_attr, current)
            existing = set(current)
            for uuid_str in created_ids:
                if uuid_str not in existing:
                    current.append(uuid_str)
                    existing.add(uuid_str)
            flag_modified(task, task_attr)

        if pending:
            # 缓冲到 task 上，等待 _save_progress 在同一事务中写入
            existing = getattr(task, "_pending_entities", None)
            if existing is None:
                task._pending_entities = pending  # type: ignore[attr-defined]
            else:
                existing.extend(pending)

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now
        return datetime.now(BEIJING_TZ)

    def _summarize_states(self, tasks: list[Task]) -> str:
        state_counts = Counter(str(task.state) for task in tasks)
        return ", ".join(f"{state}={count}" for state, count in sorted(state_counts.items()))


def _extract_reflection_days(contents: str | list[str], reference_dt: datetime | None = None) -> int:
    """从 agent 全阶段输出中提取复盘天数。reference_dt 用于模拟模式下传入时钟日期。"""
    import re
    from datetime import date as _date

    texts = contents if isinstance(contents, list) else [str(contents)]
    combined = "\n".join(texts)

    section_match = re.search(r'9\.?\s*[*_]*复盘建议[*_]*\s*\n(.*?)(?=\n---|\n##\s|\n\d{1,2}\.?\s+|\Z)', combined, re.DOTALL)
    section = section_match.group(1) if section_match else combined

    today = reference_dt.date() if reference_dt else _date.today()

    date_match = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', section)
    if date_match:
        try:
            y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            target = _date(y, m, d)
            days = (target - today).days
            if days >= 1:
                return days
        except ValueError:
            pass

    rel_match = re.search(r'建议\s*(\d+)\s*(天|周|个?月)\s*后', section)
    if rel_match:
        n = int(rel_match.group(1))
        unit = rel_match.group(2)
        if '天' in unit:
            return n
        elif '周' in unit:
            return n * 7
        elif '月' in unit:
            return n * 30

    rel_global = re.search(r'建议\s*(\d+)\s*(天|周|个?月)\s*后', combined)
    if rel_global:
        n = int(rel_global.group(1))
        unit = rel_global.group(2)
        if '天' in unit:
            return n
        elif '周' in unit:
            return n * 7
        elif '月' in unit:
            return n * 30

    return settings.default_reflection_delay_days







