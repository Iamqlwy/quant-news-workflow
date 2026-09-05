"""触发器回调处理器 - 处理触发器激活后的动作分发"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from kbquant.client._base import QuantClientNotFoundError
from kbquant.schemas.trading import TradingOperationCreate, TradingOperationUpdate
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import async_session
from src.models.tables import Task

if TYPE_CHECKING:
    from datetime import datetime

    from kbquant.client import QuantClient

    from src.models.tables import TriggerRecord


class TriggerCallbackHandler:
    """触发器回调处理器

    职责：
    - 处理 deep_analysis 触发：创建新的深度分析任务
    - 处理 buy 触发：创建买入交易操作
    - 处理 sell/trade 触发：更新交易状态为平仓
    """

    def __init__(self, quant: QuantClient, get_now: callable) -> None:
        """
        Args:
            quant: KB 量化客户端
            get_now: 获取当前时间的函数（支持模拟时钟）
        """
        self.quant = quant
        self._get_now = get_now
        # 正在处理中的 raw_info_id 集合，防止同一批次内并发回调重复创建 Task
        self._inflight_raw_ids: set[str] = set()
        self._inflight_lock = asyncio.Lock()

    async def handle(self, trigger: TriggerRecord) -> None:
        """处理触发器激活

        Args:
            trigger: 触发的 TriggerRecord

        Raises:
            Exception: 处理失败时抛出异常（由 engine 捕获并恢复状态）
        """
        now = self._get_now()

        async with async_session() as db:
            if trigger.action_type == "deep_analysis":
                await self._handle_deep_analysis(trigger, now, db)
            elif trigger.action_type == "buy":
                await self._handle_buy(trigger, now, db)
            elif trigger.action_type in ("trade", "sell"):
                await self._handle_sell(trigger, now, db)
            else:
                logger.warning("未知的 action_type: {}, trigger={}", trigger.action_type, trigger.name)

            # 标记触发器为已完成（如果 _handle_deep_analysis 因 raw_info 不存在而跳过，直接保持 skipped 状态）
            if trigger.status != "skipped":
                trigger.status = "completed"
            trigger.triggered_at = now
            await db.merge(trigger)
            await db.commit()

    async def _handle_deep_analysis(self, trigger: TriggerRecord, now: datetime, db: AsyncSession) -> None:
        """处理深度分析触发"""
        # 通过 source_task_id 查出原始 Task，只复制 raw_info_id（同一资讯重新分析）
        # analysis_id / trade_id / feedback_id 不复制——新 Task 由 deep_analysis 重新产出
        src_raw_info_id = None
        if trigger.source_task_id:
            src_task = await db.get(Task, trigger.source_task_id)
            if src_task:
                src_raw_info_id = src_task.raw_info_id

        final_raw_info_id = src_raw_info_id or str(trigger.id)

        # 并发去重：先获取该 raw_info_id 的排他"创建权"，防止同一批次内多个回调并发创建 Task
        async with self._inflight_lock:
            if final_raw_info_id in self._inflight_raw_ids:
                logger.warning(
                    "{} 跳过触发：raw_info {} 已有并发回调正在处理中",
                    trigger.name, final_raw_info_id,
                )
                trigger.status = "skipped"
                return
            self._inflight_raw_ids.add(final_raw_info_id)

        try:
            # 校验 raw_info 在 kbquant 中仍然存在，防止为已过期/删除的资讯反复创建 Task
            try:
                from src.utils.http_resilience import retry_api_call
                await retry_api_call(
                    lambda: self.quant.information.get(final_raw_info_id),
                    name="校验raw_info存在",
                    task_id=final_raw_info_id,
                )
            except QuantClientNotFoundError:
                logger.warning(
                    "{} 跳过触发：raw_info {} 在 kbquant 中已不存在（可能已过期或被删除）",
                    trigger.name, final_raw_info_id,
                )
                trigger.status = "skipped"
                return

            # 检查 raw_info_id 已生成的 task 数，超过阈值则跳过，防止同一资讯反复分析
            from sqlalchemy import func, select as sa_select
            existing_count = await db.scalar(
                sa_select(func.count()).where(Task.raw_info_id == final_raw_info_id)
            )
            if existing_count is not None and existing_count >= 3:
                logger.warning(
                    "{} 跳过触发：raw_info {} 已创建 {} 条 Task，不再重复创建",
                    trigger.name, final_raw_info_id, existing_count,
                )
                trigger.status = "skipped"
                return

            task = Task(
                id=uuid4(),
                raw_info_id=final_raw_info_id,
                state="deep_analyzing",
                created_at=now,
                updated_at=now,
                trigger_id=trigger.id,
            )
            db.add(task)
            logger.info("{} 触发深度分析，创建 Task {}", trigger.name, task.id)
        finally:
            async with self._inflight_lock:
                self._inflight_raw_ids.discard(final_raw_info_id)

    async def _handle_buy(self, trigger: TriggerRecord, now: datetime, db: AsyncSession) -> None:
        """处理买入触发"""
        params = trigger.action_params or {}
        ticker = params.get("ticker") or params.get("symbol")

        if not ticker:
            logger.warning("{} action_type=buy 但缺少 ticker，跳过交易创建", trigger.name)
            return

        create_data = TradingOperationCreate(
            operation_type="buy",
            symbol=ticker,
            quantity=params.get("quantity"),
            price=params.get("price"),
            rationale=params.get("rationale", "触发器条件满足后自动买入"),
            expected_impact=params.get("expected_impact"),
            risk_level=params.get("risk_level", "medium"),
            custom_time=now,
        )

        try:
            from src.utils.http_resilience import retry_api_call
            result = await retry_api_call(
                lambda: self.quant.trading.create(create_data),
                name="触发买入创建trade",
                task_id=str(trigger.id),
            )
            trade_id = str(result.id)
            logger.info("{} 触发买入，ticker={}, trade_id={}", trigger.name, ticker, trade_id)
        except Exception as e:
            logger.error("{} 触发买入失败，ticker={}, error={}", trigger.name, ticker, e)
            raise

    async def _handle_sell(self, trigger: TriggerRecord, now: datetime, db: AsyncSession) -> None:
        """处理卖出/平仓触发"""
        if not trigger.trade_id:
            logger.warning("{} action_type=sell 但无 trade_id", trigger.name)
            return

        try:
            update = TradingOperationUpdate(status="triggered_close", custom_time=now)
            from src.utils.http_resilience import retry_api_call
            await retry_api_call(
                lambda: self.quant.trading.update(trigger.trade_id, update),
                name="触发卖出更新trade",
                task_id=str(trigger.id),
            )
            logger.info("{} 触发卖出平仓，trade_id={}", trigger.name, trigger.trade_id)
        except Exception as e:
            logger.error("{} 触发卖出失败，trade_id={}, error={}", trigger.name, trigger.trade_id, e)
            raise
