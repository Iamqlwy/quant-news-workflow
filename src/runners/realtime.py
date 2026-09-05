"""实时模式运行器 - 处理实时数据流和循环任务"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from src.workflow_logging import log_progress

if TYPE_CHECKING:
    from src.runners.components import WorkflowComponents


class RealtimeRunner:
    """实时模式运行器

    职责：
    - 管理三个独立的异步循环：consumer、pipeline、trigger
    - 处理实时数据流
    - 协调调度器
    """

    def __init__(self, components: WorkflowComponents) -> None:
        if components.simulation_mode:
            raise ValueError("RealtimeRunner requires simulation_mode=False")

        self.components = components
        self.clock = components.clock
        self.market = components.market
        self.consumer = components.consumer
        self.orchestrator = components.orchestrator
        self.trigger_engine = components.trigger_engine
        self.scheduler = components.scheduler

    async def run(self) -> None:
        """运行实时循环"""
        log_progress(
            "Workflow",
            "所有组件已初始化",
            mode="realtime",
            loops=["consumer_loop", "pipeline_loop", "trigger_loop", "scheduler"],
            clock=self.clock.now,
        )

        loop_tasks = [
            asyncio.create_task(self._consumer_loop(), name="consumer_loop"),
            asyncio.create_task(self._pipeline_loop(), name="pipeline_loop"),
            asyncio.create_task(self._trigger_loop(), name="trigger_loop"),
            asyncio.create_task(self.scheduler.run(), name="scheduler"),
        ]

        try:
            await asyncio.gather(*loop_tasks)
        except asyncio.CancelledError:
            logger.info("实时循环被取消")
            log_progress("Realtime", "停止", reason="cancelled", final_clock=str(self.clock.now))
            for task in loop_tasks:
                if not task.done():
                    task.cancel()
            raise

    async def _consumer_loop(self) -> None:
        """消费者循环：轮询 KB 队列"""
        log_progress("ConsumerLoop", "启动", clock=self.clock.now)
        await self.consumer.run_forever()

    async def _pipeline_loop(self) -> None:
        """流水线循环：处理任务状态转换"""
        log_progress("PipelineLoop", "启动", clock=self.clock.now)

        def _on_done(task: asyncio.Task) -> None:
            if exc := task.exception():
                logger.opt(exception=True).error("pipeline tick exception")
 
        while True:
            task = asyncio.create_task(self.orchestrator.tick())
            task.add_done_callback(_on_done)
            self.market.refresh()
            self.clock.advance()

            # 交易日和非交易日使用不同的轮询间隔
            if self.market.is_trading_day:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(60)

    async def _trigger_loop(self) -> None:
        """触发器循环：评估触发条件"""
        log_progress("TriggerLoop", "启动", clock=self.clock.now)
        await self.trigger_engine.run_forever()
