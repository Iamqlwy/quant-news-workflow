"""CSV 资讯加载器 —— 按时间窗口分批推入 KB"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import NotRequired, TextIO, TypedDict, cast

from kbquant.client import QuantClient
from kbquant.schemas.information import RawInformationCreate
from loguru import logger

from src.core.clock import Clock
from src.core.timezone import BEIJING_TZ

_TITLE_RE = re.compile(r"【(.+?)】")


class CSVRow(TypedDict):
    datetime: str
    content: str
    _dt: NotRequired[datetime]


def _extract_title(content: str) -> str:
    m = _TITLE_RE.search(content)
    if m:
        return m.group(1)
    if content.startswith("市场资讯："):
        rest = content[5:].strip()
        first_sentence = rest.split("。")[0][:80]
        return first_sentence or "市场资讯"
    return content[:50]


def _extract_source(content: str) -> str:
    if content.startswith("市场资讯："):
        return "市场资讯"
    return "csv_import"


class CSVNewsLoader:
    def __init__(self, csv_path: str, quant: QuantClient, clock: Clock, tick_minutes: int, retention_rate: float = 1.0, ingest_to_kb: bool = True) -> None:
        self._path = Path(csv_path)
        self._quant = quant
        self._clock = clock
        self._tick_delta = timedelta(minutes=tick_minutes)
        self._retention = retention_rate
        self._ingest_to_kb = ingest_to_kb
        self._file: TextIO | None = None
        self._reader: csv.DictReader[str] | None = None
        self._buffered: list[CSVRow] | None = None
        self._eof = False

    def close(self) -> None:
        """释放文件句柄。"""
        if self._file is not None:
            with contextlib.suppress(Exception):
                self._file.close()
            self._file = None
            self._reader = None
            self._eof = True

    def __del__(self) -> None:
        self.close()

    @property
    def tick_minutes(self) -> int:
        return int(self._tick_delta.total_seconds() // 60)

    @property
    def is_exhausted(self) -> bool:
        """CSV file exhausted, no buffered rows."""
        return self._eof and self._buffered is None

    async def load_batch(self) -> int:
        """读取落入当前时间窗口的资讯，推入 KB。返回成功数量。"""
        if self._eof and self._buffered is None:
            return 0

        if self._reader is None:
            await self._open()

        window_end = self._clock.now
        batch: list[CSVRow] = []

        # 处理上一批缓存的行
        if self._buffered is not None:
            cached = self._buffered
            self._buffered = None
            for row in cached:
                dt = row.get("_dt")
                if dt is None:
                    continue
                if dt < window_end:
                    batch.append(row)
                else:
                    self._buffered = [row]
                    break

        # 继续读文件
        if not self._eof:
            try:
                assert self._reader is not None
                while True:
                    row = cast(CSVRow, next(self._reader))
                    dt = _parse_dt(row["datetime"])
                    row["_dt"] = dt
                    if dt < window_end:
                        batch.append(row)
                    else:
                        self._buffered = [row]
                        break
            except StopIteration:
                self._eof = True
                logger.info("CSV 读取完毕: {}", self._path)
                self.close()
            except Exception:
                self.close()
                raise

        # 保留率过滤
        before_filter = len(batch)
        if self._retention < 1.0 and before_filter > 0:
            batch = [row for row in batch if random.random() < self._retention]
            dropped = before_filter - len(batch)
            if dropped > 0:
                logger.debug("保留率={:.0%}, 丢弃 {} 条, 保留 {} 条", self._retention, dropped, len(batch))

        # 推入 KB
        count = 0
        if not self._ingest_to_kb:
            if batch:
                logger.info("load_batch 跳过 KB 推送 (ingest_to_kb=False), 本窗口 {} 条, window_end={}", len(batch), window_end)
            return len(batch)

        for row in batch:
            try:
                title = _extract_title(row["content"])
                source = _extract_source(row["content"])
                data = RawInformationCreate(
                    title=title,
                    body=row["content"],
                    source=source,
                    published_at=_parse_dt(row["datetime"]),
                    info_type="news",
                )
                for attempt in range(5):
                    try:
                        await self._quant.information.ingest(data)
                        break
                    except Exception as e:
                        if attempt >= 4:
                            raise
                        delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
                        logger.warning("KB ingest 失败 ({})，第 {}/5 次重试，{:.1f}s 后重试: {}", type(e).__name__, attempt + 1, delay, e)
                        await asyncio.sleep(delay)
                count += 1
            except Exception as e:
                logger.error("KB ingest 失败: {}", e)

        if count > 0:
            logger.info("load_batch 推送 {} 条资讯入 KB, window_end={}", count, window_end)
        return count

    async def _open(self) -> None:
        self._file = open(self._path, newline="", encoding="utf-8")  # noqa: SIM115
        self._reader = csv.DictReader(self._file)
        logger.info("CSV 已打开: {}, 当前时钟: {}", self._path, self._clock.now)
        # 快进到 clock.now
        while True:
            try:
                assert self._reader is not None
                row = cast(CSVRow, next(self._reader))
            except StopIteration:
                self._eof = True
                logger.info("CSV 读取完毕: {}", self._path)
                self.close()
                return
            except Exception:
                self.close()
                raise
            dt = _parse_dt(row["datetime"])
            row["_dt"] = dt
            if dt >= self._clock.now:
                self._buffered = [row]
                return


def _parse_dt(s: str) -> datetime:
    # CSV 中的历史时间按北京时间解释，和全局 Clock 保持一致
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
