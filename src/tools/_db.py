"""工具层数据库操作 —— 触发器记录的创建、查询、取消。

将数据库访问从 writer.py 抽取到此模块，保持工具层的抽象边界。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update

from src.db import async_session
from src.models.tables import TriggerRecord


async def create_trigger_record(
    name: str,
    condition: dict,
    action_type: str,
    action_params: dict | None,
    trade_id: str | None,
    source_task_id: str | None,
    source_analysis_id: str | None,
    not_before: datetime | None,
    not_after: datetime | None,
    focus_on: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    async with async_session() as db:
        try:
            trigger = TriggerRecord(
                name=name,
                status="waiting",
                condition=condition,
                action_type=action_type,
                action_params=action_params,
                trade_id=trade_id,
                source_task_id=source_task_id,
                source_analysis_id=source_analysis_id,
                not_before=not_before,
                not_after=not_after,
                focus_on=focus_on,
                created_at=created_at,
            )
            db.add(trigger)
            await db.commit()
            await db.refresh(trigger)
            return trigger.id
        except Exception:
            await db.rollback()
            raise


async def list_trigger_records(stock_name: str, task_id: str | None, analysis_ids: list[str] | None) -> list[TriggerRecord]:
    async with async_session() as db:
        try:
            q = select(TriggerRecord).where(TriggerRecord.name.contains(stock_name))
            filters = []
            if task_id:
                filters.append(TriggerRecord.source_task_id == task_id)
            for aid in analysis_ids:
                if aid:
                    filters.append(TriggerRecord.source_analysis_id == aid)
            if filters:
                q = q.where(or_(*filters))
            q = q.order_by(TriggerRecord.created_at.desc()).limit(20)
            result = await db.execute(q)
            return list(result.scalars().all())
        except Exception:
            await db.rollback()
            raise


async def get_trigger_by_id(trigger_id: UUID) -> TriggerRecord | None:
    async with async_session() as db:
        try:
            result = await db.execute(select(TriggerRecord).where(TriggerRecord.id == trigger_id))
            return result.scalar_one_or_none()
        except Exception:
            await db.rollback()
            raise


async def cancel_trigger_in_db(trigger_id: UUID) -> None:
    async with async_session() as db:
        try:
            await db.execute(update(TriggerRecord).where(TriggerRecord.id == trigger_id).values(status="cancelled"))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
