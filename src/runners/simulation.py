"""模拟模式运行器 - 处理 CSV 数据回放和触发器评估"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select

from src.config import settings
from src.db import async_session
from src.models.tables import Task
from src.pipeline.states import ACTIONABLE_STATES, TaskState
from src.workflow_logging import log_progress, should_log_progress

if TYPE_CHECKING:
    from src.runners.components import WorkflowComponents


class SimulationRunner:
    """模拟模式运行器

    职责：
    - 管理模拟时钟的推进
    - 批量加载 CSV 数据
    - 回放交易时段的触发器评估
    - 清理残存任务
    """

    def __init__(self, components: WorkflowComponents) -> None:
        if not components.simulation_mode:
            raise ValueError("SimulationRunner requires simulation_mode=True")
        if components.csv_loader is None:
            raise ValueError("SimulationRunner requires csv_loader")

        self.components = components
        self.csv_loader = components.csv_loader
        self.clock = components.clock
        self.market = components.market
        self.consumer = components.consumer
        self.orchestrator = components.orchestrator
        self.trigger_engine = components.trigger_engine
        self.scheduler = components.scheduler

        # 配置
        self.max_ticks = 10000
        self.max_cleanup_ticks = 500
        self.no_activity_threshold = 3

    async def run(self) -> None:
        """运行模拟循环"""
        log_progress(
            "Simulation",
            "开始",
            start=self.clock.now,
            tick_minutes=self.csv_loader.tick_minutes if self.csv_loader else 0,
            ingest_to_kb=settings.simulation_ingest_to_kb,
        )
        if not settings.simulation_ingest_to_kb:
            logger.warning("模拟模式: ingest_to_kb=False，资讯不会推入 KB，仅推进时钟和处理已有任务")
        if settings.simulation_skip_orchestrator_tick:
            logger.warning("模拟模式: skip_orchestrator_tick=True，跳过 Pipeline 任务调度，仅回放触发器")

        # 重置所有触发器为 waiting（处理上次运行残留）
        await self._reset_triggers()

        # 预打开 CSV，在 clock 推进之前定位起始位置，避免首批数据丢失
        if self.csv_loader is not None:
            await self.csv_loader._open()

        eval_count = 0
        tick_index = 0
        no_activity_count = 0

        # ═══════════════════════════════════════════════════
        # 主循环：先推进时钟 → 触发器回放 → 批量加载
        # 回放基于上一 tick 已入库的资讯，加载为下一 tick 做准备
        # ═══════════════════════════════════════════════════
        while tick_index < self.max_ticks:
            tick_start = self.clock.now

            # 检查是否已到达模拟结束时间（上轮 advance 可能被 _clip_to_end 钳制到 end_time）
            if self.clock.is_expired:
                logger.info("模拟时钟到达 end_time，结束主循环，clock={}", tick_start)
                log_progress("Simulation", "定时结束", final_clock=str(tick_start))
                break

            tick_index += 1
            log_progress("SimulationTick", "开始", tick_index=tick_index, tick_at=tick_start)

            # Phase 0: 先推进时钟，得到 tick_end
            self.clock.advance()
            tick_end = self.clock.now

            # Phase 1: 回放 [tick_start, tick_end] 内的交易时段触发器
            # 先基于上一 tick 已入库的资讯评估触发条件
            replay_eval_count = await self._replay_triggers_in_window(tick_start, tick_end)
            eval_count += replay_eval_count

            ## 这里可能会创建触发任务，由下方一起做了 

            # Phase 2: 批量处理（CSV 加载 + consumer 轮询 + pipeline tick）
            # 此时 clock 已推进，load_batch 加载的是 [tick_start, tick_end) 的资讯
            count, processed_tasks = await self._process_batch()

            # 检查定时任务
            await self.scheduler.tick()

            logger.info(
                "Phase1(trigger回放) -> Phase2(批量处理), tick={}, pushed={}, pipeline_tasks={}",
                tick_index, count, processed_tasks
            )

            # 检查是否结束
            pending = None
            if self.csv_loader.is_exhausted:
                await self.trigger_engine.flush_pending()
                pending = await self._pending_count()

                # 连续无活动检测
                if pending == 0 and replay_eval_count == 0:
                    no_activity_count += 1
                    if no_activity_count >= self.no_activity_threshold:
                        logger.info(
                            "CSV 读完，所有任务处理完毕，连续 {} 个 tick 无活动，end={}",
                            no_activity_count, tick_end
                        )
                        log_progress(
                            "SimulationTick", "完成",
                            tick_index=tick_index, pushed=count,
                            pipeline_tasks=processed_tasks, replay_evals=replay_eval_count, pending=pending,
                        )
                        break
                else:
                    no_activity_count = 0
            else:
                no_activity_count = 0

            log_progress(
                "SimulationTick", "完成",
                tick_index=tick_index, pushed=count,
                pipeline_tasks=processed_tasks, replay_evals=replay_eval_count, pending=pending,
            )

            # 每 tick 保存断点，支持崩溃后续跑
            self.clock.save_checkpoint(settings.simulation_checkpoint_path)

        # 超过最大 tick 数
        if tick_index >= self.max_ticks:
            logger.error("达到最大 tick 数 {}，强制退出（可能存在无限循环）", self.max_ticks)
            log_progress("Simulation", "异常退出", reason="达到最大tick数", max_ticks=self.max_ticks)

        # ═══════════════════════════════════════════════════
        # 清理阶段：处理 trigger 产生的剩余任务
        # ═══════════════════════════════════════════════════
        await self._cleanup_remaining_tasks()

        logger.info("Simulation 全部完成, total_ticks={}, trigger_evals={}, final_clock={}", tick_index, eval_count, self.clock.now)
        log_progress("Simulation", "结束", total_ticks=tick_index, replay_evals=eval_count, final_clock=str(self.clock.now))

        self.clock.save_checkpoint(settings.simulation_checkpoint_path)

    async def _reset_triggers(self) -> None:
        """重置非终态触发器为 waiting，保留终态（completed/skipped/expired/cancelled）"""
        from sqlalchemy import update as sa_update

        from src.models.tables import TriggerRecord

        _TERMINAL_STATES = {"completed", "skipped", "expired", "cancelled"}
        async with async_session() as db:
            await db.execute(
                sa_update(TriggerRecord)
                .where(TriggerRecord.status.notin_(_TERMINAL_STATES))
                .values(status="waiting")
            )
            await db.commit()

    async def _process_batch(self) -> tuple[int, int]:
        """处理一个 tick 的批量数据

        Returns:
            (pushed_count, processed_tasks): CSV 推送数和 pipeline 处理任务数
        """
        self.market.refresh()

        # 加载 CSV 批次
        count = 0
        try:
            count = await self.csv_loader.load_batch()
            if count > 0:
                logger.info("tick={} push {} 条资讯入 KB", self.clock.now, count)
        except Exception:
            logger.opt(exception=True).error("load_batch 失败")

        # Consumer 轮询
        try:
            await self.consumer._poll()
        except Exception:
            logger.opt(exception=True).error("consumer poll 失败")

        # 等待 trigger 回调完成（回调中创建的新任务需要先入 DB）
        try:
            await self.trigger_engine.flush_pending()
        except Exception:
            logger.opt(exception=True).error("trigger flush_pending 失败")

        # Pipeline tick
        processed_tasks = 0
        if not settings.simulation_skip_orchestrator_tick:
            try:
                processed_tasks = await self.orchestrator.tick()
            except Exception:
                logger.opt(exception=True).error("pipeline tick 失败")

        return count, processed_tasks

    async def _replay_triggers_in_window(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """回放 [window_start, window_end] 与交易时段的交集（1 分钟粒度）

        Returns:
            eval_count: 评估次数
        """
        trading_days = set(self.market.trading_days)

        # 非交易日 → 跳过
        today_str = window_start.strftime("%Y%m%d")
        if today_str not in trading_days:
            if self.clock.now < window_end:
                self.clock.reset_to(window_end)
            return 0

        # 计算交易时段与窗口的交集
        sub_windows = self._calculate_trading_windows(window_start, window_end)

        if not sub_windows:
            if self.clock.now < window_end:
                self.clock.reset_to(window_end)
            return 0

        total_sec = int(sum((b - a).total_seconds() for a, b in sub_windows))
        total_minutes = max(1, total_sec // 60)
        log_progress(
            "TriggerReplay", "开始",
            window_start=window_start, window_end=window_end,
            sub_windows=[(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in sub_windows],
            approx_minutes=total_minutes,
        )

        eval_count = 0
        for sub_start, sub_end in sub_windows:
            self.clock.reset_to(sub_start)
            while self.clock.now < sub_end:
                self.market.refresh()
                try:
                    await self.trigger_engine._evaluate_all()
                    await self.trigger_engine.flush_pending()
                    eval_count += 1
                    if should_log_progress(eval_count, total_minutes, step=30):
                        log_progress("TriggerReplay", "进行中", eval_count=eval_count, now=self.clock.now)
                except Exception:
                    logger.opt(exception=True).error("tick={} trigger evaluation exception", self.clock.now)
                self.clock.advance_by(timedelta(minutes=1))

        # 恢复到 window_end
        if self.clock.now < window_end:
            self.clock.reset_to(window_end)
        log_progress("TriggerReplay", "完成", window_end=window_end, eval_count=eval_count)
        return eval_count

    def _calculate_trading_windows(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """计算当天交易时段（9:30-11:30, 13:00-15:00）与窗口的交集"""
        morning_start = max(window_start, window_start.replace(hour=9, minute=30, second=0, microsecond=0))
        morning_end = min(window_end, window_start.replace(hour=11, minute=30, second=0, microsecond=0))
        afternoon_start = max(window_start, window_start.replace(hour=13, minute=0, second=0, microsecond=0))
        afternoon_end = min(window_end, window_start.replace(hour=15, minute=0, second=0, microsecond=0))

        sub_windows: list[tuple[datetime, datetime]] = []
        if morning_start < morning_end:
            sub_windows.append((morning_start, morning_end))
        if afternoon_start < afternoon_end:
            sub_windows.append((afternoon_start, afternoon_end))

        return sub_windows

    async def _cleanup_remaining_tasks(self) -> None:
        """清理残存任务"""
        await self.trigger_engine.flush_pending()

        # 如果用户明确跳过了 orchestrator.tick()，或者时钟已到达 end_time 无法再推进，
        # 则跳过清理循环，直接将残存任务标记为 ERROR。
        if settings.simulation_skip_orchestrator_tick or self.clock.is_expired:
            remaining = await self._pending_count()
            logger.info(
                "SimulationCleanup 跳过: skip_orchestrator_tick={}, is_expired={}, pending={}",
                settings.simulation_skip_orchestrator_tick, self.clock.is_expired, remaining
            )
            return

        log_progress("SimulationCleanup", "开始")

        for cleanup_idx in range(1, self.max_cleanup_ticks + 1):
            self.market.refresh()
            try:
                processed_tasks = await self.orchestrator.tick(force_full_pipeline=True)
            except Exception as e:
                logger.opt(exception=True).error("cleanup tick failed: {}", e)
                processed_tasks = 0

            pending = await self._pending_count()
            if should_log_progress(cleanup_idx, self.max_cleanup_ticks, step=10) or pending == 0:
                log_progress(
                    "SimulationCleanup", "进行中",
                    current=cleanup_idx, total=self.max_cleanup_ticks,
                    pending=pending, pipeline_tasks=processed_tasks,
                )
            if pending == 0:
                break
            self.clock.advance_by(timedelta(minutes=30))
        else:
            logger.warning("清理循环达到上限 {} 次，强制退出", self.max_cleanup_ticks)

        # 标记残存任务为 ERROR
        remaining = await self._pending_count()
        if remaining > 0:
            async with async_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(Task)
                    .where(Task.state.in_(ACTIONABLE_STATES))
                    .values(state=TaskState.ERROR, updated_at=self.clock.now)
                )
                await db.commit()
            logger.warning("已标记 {} 个残存任务为 ERROR", remaining)

    async def _pending_count(self) -> int:
        """获取待处理任务数"""
        async with async_session() as db:
            result = await db.execute(select(Task).where(Task.state.in_(ACTIONABLE_STATES)))
            return len(result.scalars().all())
