"""轻量级定时任务调度器 —— 实盘和模拟共用，通过 Clock 统一时间"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from loguru import logger

from src.core.clock import Clock

Callback = Callable[[], Awaitable[Any]]


@dataclass
class _Task:
    name: str
    fn: Callback
    next_run: datetime | None = None
    interval: timedelta | None = None
    at_time: time | None = None


class Scheduler:
    """统一调度器 —— 实盘和模拟共用同一套 API。

    - 实盘：``await scheduler.run()`` 作为后台协程，每秒检查一次。
    - 模拟：在 Phase 1 tick 中调用 ``await scheduler.tick()``。
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._tasks: list[_Task] = []

    # ── 注册 API ──────────────────────────────────────

    def daily(self, name: str, at: time, fn: Callback) -> None:
        """每日定点执行（北京时间）。"""
        now = self.clock.now
        self._tasks.append(
            _Task(
                name=name,
                fn=fn,
                next_run=self._next_daily_time(now, at),
                at_time=at,
            )
        )

    def every(self, name: str, interval: timedelta, fn: Callback) -> None:
        """每隔固定时间执行一次。"""
        now = self.clock.now
        self._tasks.append(
            _Task(
                name=name,
                fn=fn,
                next_run=now + interval,
                interval=interval,
            )
        )

    # ── 驱动入口 ──────────────────────────────────────

    async def tick(self) -> None:
        """检查并触发所有到期任务。调用方应每步都调用。"""
        now = self.clock.now
        for task in self._tasks:
            if task.next_run is not None and now >= task.next_run:
                await self._fire(task, now)

    async def run(self) -> None:
        """实盘模式：后台自循环，每秒检查一次。"""
        while True:
            await self.tick()
            await asyncio.sleep(1)

    # ── 内部 ──────────────────────────────────────────

    async def _fire(self, task: _Task, now: datetime) -> None:
        logger.debug("Scheduler 触发: {} at {}", task.name, now)
        if task.interval is not None:
            assert task.next_run is not None
            catchup = 0
            while task.next_run <= now:
                if catchup > 100:
                    logger.warning(
                        "Scheduler 追赶过多: {} ({} 次)，跳过至当前时间",
                        task.name,
                        catchup,
                    )
                    task.next_run = now + task.interval
                    break
                try:
                    await task.fn()
                except KeyboardInterrupt:
                    logger.info("Scheduler task '{}' interrupted by user", task.name)
                    raise
                except asyncio.CancelledError:
                    logger.info("Scheduler task '{}' cancelled", task.name)
                    raise
                except Exception as e:
                    logger.opt(exception=True).error("Scheduler task '{}' failed: {}", task.name, e)
                task.next_run += task.interval
                catchup += 1
        else:
            try:
                await task.fn()
            except KeyboardInterrupt:
                logger.info("Scheduler task '{}' interrupted by user", task.name)
                raise
            except asyncio.CancelledError:
                logger.info("Scheduler task '{}' cancelled", task.name)
                raise
            except Exception as e:
                logger.opt(exception=True).error("Scheduler task '{}' failed: {}", task.name, e)
            if task.at_time is not None:
                task.next_run = self._next_daily_time(now, task.at_time)

    def _next_daily_time(self, after: datetime, at: time) -> datetime:
        """计算下一次 at 时间，从 after 之后开始。"""
        candidate = after.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
