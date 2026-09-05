"""Workflow 进度日志辅助工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from loguru import logger


def _normalize_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in value.items()) + "}"
    return str(value)


def format_progress_fields(**fields: Any) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={_normalize_value(value)}")
    return ", ".join(parts)


def log_progress(label: str, status: str, *, level: str = "debug", **fields: Any) -> None:
    message = f"{label} {status}"
    suffix = format_progress_fields(**fields)
    if suffix:
        message = f"{message} | {suffix}"
    getattr(logger, level.lower())(message)


def should_log_progress(current: int, total: int, *, step: int) -> bool:
    if total <= 0:
        return False
    if current <= 1 or current >= total:
        return True
    return current % step == 0


@contextmanager
def progress_span(
    label: str,
    *,
    start_level: str = "info",
    success_level: str = "info",
    failure_level: str = "error",
    **fields: Any,
) -> Iterator[None]:
    started = perf_counter()
    log_progress(label, "开始", level=start_level, **fields)
    try:
        yield
    except Exception:
        log_progress(
            label,
            "失败",
            level=failure_level,
            elapsed_s=perf_counter() - started,
            **fields,
        )
        raise
    log_progress(
        label,
        "完成",
        level=success_level,
        elapsed_s=perf_counter() - started,
        **fields,
    )

async def gather_with_progress(
    label: str,
    coros: list,
    *,
    report_every: int = 10,
    return_exceptions: bool = True,
) -> list:
    """批量执行协程，每完成 report_every 个时输出进度日志。

    使用 asyncio.as_completed 实现，保持并发语义且返回值顺序与输入一致。
    """
    if not coros:
        return []

    total = len(coros)
    # 包装协程，返回 (index, result)，避免 asyncio.as_completed 返回的对象
    # 与 asyncio.ensure_future 创建的 Task 不是同一对象的 KeyError。

    async def _wrap(idx: int, c: Awaitable[Any]) -> tuple[int, Any]:
        try:
            return idx, (await c)
        except Exception as e:
            return idx, e

    wrap_tasks = [asyncio.ensure_future(_wrap(i, c)) for i, c in enumerate(coros)]
    results: list = [None] * total
    completed = 0

    for fut in asyncio.as_completed(wrap_tasks):
        idx, result = await fut
        if isinstance(result, Exception) and not return_exceptions:
            raise result
        results[idx] = result
        completed += 1  # noqa: SIM113 (as_completed cant use enumerate)
        if completed % report_every == 0 or completed == total:
            log_progress(label, f"进度 {completed}/{total}", completed=completed, total=total,level="info")

    return results
