"""消费者：轮询 KB processing_queue，拉取新资讯，创建本地 Task"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kbquant.client import QuantClient
from kbquant.schemas.pipeline import PipelineStatusUpdate
from loguru import logger
from sqlalchemy import select

from src.db import async_session
from src.models.tables import Task

if TYPE_CHECKING:
    from src.core.clock import Clock


class ConsumerPoller:
    def __init__(self, quant: QuantClient, clock: Clock | None = None) -> None:
        self.quant = quant
        self._running = False
        self._clock = clock

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._poll()
            except Exception:
                logger.opt(exception=True).error("consumer poll failed, clock={}", self._clock.now if self._clock else "N/A")
            await self._wait()

    async def _wait(self) -> None:
        await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """拉取 KB 中状态为 ingested 的资讯，翻页循环直到队列清空"""
        now = self._clock.now if self._clock else None
        total_pushed = 0
        page = 1
        while True:
            try:
                from src.utils.http_resilience import retry_api_call
                result = await retry_api_call(
                    lambda: self.quant.pipeline.list_queue(status="ingested", page=page, page_size=300),
                    name="KB轮询队列",
                    task_id="poller",
                )
            except Exception:
                logger.debug("KB 轮询失败（将重试）", exc_info=True)
                break

            items = result.get("items", [])
            if not items:
                break

            # 批量查询已存在的 raw_info_id，避免 N+1 查询
            raw_info_ids = [item.get("raw_info_id", "") for item in items if item.get("raw_info_id")]
            async with async_session() as db:
                existing_result = await db.execute(
                    select(Task.raw_info_id).where(Task.raw_info_id.in_(raw_info_ids))
                )
                existing_ids = {row[0] for row in existing_result.fetchall()}

            new_tasks: list[Task] = []
            page_pushed = 0
            for item in items:
                raw_info_id = item.get("raw_info_id", "")
                if not raw_info_id:
                    continue

                # 检查本地是否已有此 task
                if str(raw_info_id) in existing_ids:
                    continue

                task = Task(
                    raw_info_id=str(raw_info_id),
                    state="ingested",
                    created_at=now,
                    updated_at=now,
                )
                new_tasks.append(task)
                page_pushed += 1

            # 批量入库 + KB 状态更新
            if new_tasks:
                async with async_session() as db:
                    db.add_all(new_tasks)
                    await db.commit()
                    logger.debug("Consumer 批量创建 {} 条 Task", len(new_tasks))

            for task in new_tasks:
                try:
                    from src.utils.http_resilience import retry_api_call
                    await retry_api_call(
                        lambda tid=task.raw_info_id: self.quant.pipeline.update_status(
                            tid, PipelineStatusUpdate(status="consumed_by_workflow"),
                        ),
                        name="KB管线状态更新",
                        task_id="poller",
                    )
                except Exception:
                    logger.debug("KB 管线状态更新失败", exc_info=True)

            total_pushed += page_pushed
            page += 1

            # 如果当前页没有新创建的 task（全都是已存在的），说明后面的也是重复数据，提前退出
            if page_pushed == 0:
                break

        if total_pushed > 0:
            logger.info("Consumer 轮询完成，共创建 {} 条 Task", total_pushed)
