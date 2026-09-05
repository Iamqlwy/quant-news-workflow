"""统一时钟 —— 实盘和模拟共用同一套时间推进机制"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Self

import pandas as pd
from loguru import logger

from src.core.timezone import BEIJING_TZ


@dataclass
class TimeConfig:
    start_time: datetime
    tick_duration: timedelta
    realtime: bool = True  # True=实盘(真实等待), False=模拟(不等待)
    end_time: datetime | None = None  # 时钟到此时间后自动停止推进，None=不限


class Clock:
    def __init__(self, config: TimeConfig) -> None:
        self._cfg = config
        current = config.start_time
        if pd.isna(current):
            raise ValueError(f"无效的 start_time: {current!r}")
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TZ)
        self._current = current
        # 规范化 end_time
        self._end_time: datetime | None = config.end_time
        if self._end_time is not None and self._end_time.tzinfo is None:
            self._end_time = self._end_time.replace(tzinfo=BEIJING_TZ)

    # ── 核心推进方法 ──────────────────────────

    def _clip_to_end(self, dt: datetime) -> datetime:
        """约束 dt 不超过 end_time。"""
        if self._end_time is not None and dt > self._end_time:
            return self._end_time
        return dt

    @property
    def now(self) -> datetime:
        if self._cfg.realtime:
            return datetime.now(BEIJING_TZ)
        return self._current

    @property
    def is_expired(self) -> bool:
        """时钟是否已到达或超过 end_time。"""
        if self._end_time is None:
            return False
        return self._current >= self._end_time

    @property
    def today(self) -> date:
        if self._cfg.realtime:
            return datetime.now(BEIJING_TZ).date()
        return self._current.date()

    @property
    def today_str(self) -> str:
        """返回 YYYYMMDD 格式的日期字符串。"""
        return self.today.strftime("%Y%m%d")

    @property
    def epoch(self) -> float:
        """缓存 TTL 用 —— 每次 tick 变化后自然过期"""
        if self._cfg.realtime:
            return datetime.now(BEIJING_TZ).timestamp()
        return self._current.timestamp()

    @property
    def is_realtime(self) -> bool:
        """是否为实盘时钟。"""
        return self._cfg.realtime

    @property
    def minutes_since_midnight(self) -> int:
        """返回从午夜到现在的分钟数。"""
        n = self.now
        return n.hour * 60 + n.minute

    @property
    def is_trading_session(self) -> bool:
        """在 A 股交易时段（9:30–11:30, 13:00–15:00）返回 True。"""
        m = self.minutes_since_midnight
        morning = 570 <= m <= 690    # 9:30 - 11:30
        afternoon = 780 <= m <= 901  # 13:00 - 15:01（含1分钟缓冲）
        return morning or afternoon

    @property
    def is_pre_market(self) -> bool:
        """在 0:00–9:30 之间返回 True。"""
        m = self.minutes_since_midnight
        return 0 <= m < 570

    @property
    def is_post_market(self) -> bool:
        """在 15:00–24:00 之间返回 True。"""
        m = self.minutes_since_midnight
        return m > 901

    @property
    def phase(self) -> str:
        """返回当前时段。"""
        if self.is_pre_market:
            return "pre_market"
        if self.is_trading_session:
            return "trading"
        return "post_market"

    def advance(self) -> None:
        if self._cfg.realtime:
            logger.debug("实盘模式 advance() 为 no-op（now 始终为挂钟时间）")
        else:
            self._current = self._clip_to_end(self._current + self._cfg.tick_duration)

    def advance_by(self, delta: timedelta) -> None:
        if self._cfg.realtime:
            logger.debug("实盘模式 advance_by() 为 no-op（now 始终为挂钟时间）")
            return
        if self._current.tzinfo is None:
            self._current = self._current.replace(tzinfo=BEIJING_TZ)
        self._current = self._clip_to_end(self._current + delta)

    def reset_to(self, target: datetime) -> None:
        if self._cfg.realtime:
            logger.debug("实盘模式 reset_to() 为 no-op（now 始终为挂钟时间）")
            return
        if target.tzinfo is None:
            target = target.replace(tzinfo=BEIJING_TZ)
        self._current = self._clip_to_end(target)

    async def wait(self) -> None:
        if self._cfg.realtime:
            await asyncio.sleep(self._cfg.tick_duration.total_seconds())

    # ── checkpoint ──

    @classmethod
    def from_checkpoint(cls, config: TimeConfig, checkpoint_path: str) -> Self:
        """若 checkpoint 存在则从断点续跑，否则从 config.start_time 开始。"""
        path = Path(checkpoint_path)
        start_time = config.start_time
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                resumed = datetime.fromisoformat(data["last_time"])
                if resumed.tzinfo is None:
                    resumed = resumed.replace(tzinfo=BEIJING_TZ)
                start_time = resumed
                logger.info("从断点续跑: {} (checkpoint={})", resumed, checkpoint_path)
            except Exception as e:
                logger.warning("读取断点文件失败，使用原始 start_time: {}", e)
        return cls(TimeConfig(
            start_time=start_time,
            tick_duration=config.tick_duration,
            realtime=config.realtime,
            end_time=config.end_time,
        ))

    def save_checkpoint(self, checkpoint_path: str) -> None:
        """保存当前时间到断点文件。"""
        path = Path(checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"last_time": self._current.isoformat()}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.debug("保存模拟时钟断点: {}", self._current.isoformat())
