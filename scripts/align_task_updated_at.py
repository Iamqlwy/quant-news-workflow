"""将 tasks 表中 created_at 在 2026 年 6 月的记录的 created_at 对齐到下一个 3 的倍数小时。

优先级：
  1. 有 trigger_id 且 trigger 已触发（triggered_at 非空）→ 使用 triggered_at
  2. 否则 → 使用 raw_info.published_at

取时间的小时，对齐到下一个被 3 整除的小时（0→3, 1→3, 2→3, 3→3, 4→6, ...）。

用法：
    python scripts/align_task_updated_at.py           # dry_run=True 预览变更
    python scripts/align_task_updated_at.py --apply   # 实际写入
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import asyncio
from datetime import datetime, timedelta, timezone

import psycopg2
from sqlalchemy import update

from src.core.timezone import BEIJING_TZ
from src.db import async_session
from src.models.tables import Task

DRY_RUN = "--apply" not in sys.argv

KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}


def _to_beijing(dt: datetime) -> str:
    """将 DB 返回的 UTC naive datetime 显示为北京时间字符串。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(dt.astimezone(BEIJING_TZ))


def align_hour(dt: datetime) -> datetime:
    """在当前时区上将小时对齐到下一个被 3 整除的值，然后转为 UTC。
    0→3, 1→3, 2→3, 3→6, 4→6, 5→6, ..., 21→0(次日), 22→0(次日), 23→0(次日)
    """
    hour = dt.hour
    target = ((hour // 3) + 1) * 3
    if target >= 24:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        dt = dt.replace(hour=target, minute=0, second=0, microsecond=0)
    return dt.astimezone(timezone.utc)


def fetch_published_at(raw_info_ids: list[str]) -> dict[str, datetime]:
    """从 quant_kb.raw_information 批量查询 published_at。"""
    if not raw_info_ids:
        return {}
    with psycopg2.connect(**KB_DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, published_at FROM raw_information WHERE id::text = ANY(%s)",
                (raw_info_ids,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}


async def fetch_trigger_times(trigger_ids: list[str]) -> dict[str, datetime]:
    """从 wfuse.triggers 批量查询 triggered_at。"""
    if not trigger_ids:
        return {}
    from sqlalchemy import text

    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT id::text, triggered_at FROM triggers WHERE id::text = ANY(:ids)"
            ),
            {"ids": trigger_ids},
        )
        out: dict[str, datetime] = {}
        for row in result.all():
            if row[1] is not None:
                dt = row[1]
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                out[row[0]] = dt.astimezone(BEIJING_TZ)
        return out


async def main():
    from sqlalchemy import text

    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT id, created_at, raw_info_id, trigger_id FROM tasks "
                "WHERE created_at >= '2026-06-01' AND created_at < '2026-07-01' "
                "ORDER BY created_at"
            )
        )
        rows = [(r[0], r[1], r[2], r[3]) for r in result.all()]

    print(f"共找到 {len(rows)} 条 2026-06 月记录")

    # 分别收集 trigger_id 和 raw_info_id
    trigger_ids = list({str(r[3]) for r in rows if r[3]})
    raw_ids = list({r[2] for r in rows if r[2]})
    print(f"去重后 trigger_id: {len(trigger_ids)} 个, raw_info_id: {len(raw_ids)} 个")

    trigger_map = await fetch_trigger_times(trigger_ids)
    print(f"有 triggered_at 的 trigger: {len(trigger_map)} 个")

    published_map = fetch_published_at(raw_ids)
    print(f"KB 中有 published_at 的 raw_info: {len(published_map)} 个")

    # 汇总来源统计
    trigger_sourced = 0
    raw_sourced = 0
    no_source = 0

    print(f"\n模式: {'DRY RUN (预览)' if DRY_RUN else 'APPLY (实际写入)'}")
    print()
    print(f"{'task_id':<38} {'source':<10} {'ref_time':<22} {'old_created':<22} {'new_created':<22}")
    print("-" * 130)

    updates: list[tuple[str, datetime]] = []

    for task_id, created_at, raw_info_id, trigger_id in rows:
        ref_time = None

        # 优先使用 trigger 的 triggered_at（直接使用，不做任何变换）
        if trigger_id:
            ref_time = trigger_map.get(str(trigger_id))
            if ref_time is not None:
                new_updated = ref_time
                if new_updated != created_at:
                    updates.append((str(task_id), new_updated))
                    print(
                        f"{str(task_id):<38} {'trigger':<10} {_to_beijing(ref_time):<22} "
                        f"{_to_beijing(created_at):<22} {_to_beijing(new_updated):<22}"
                    )
                trigger_sourced += 1
                continue

        # fallback 到 raw_info.published_at（需要 align_hour 对齐）
        if raw_info_id:
            ref_time = published_map.get(raw_info_id)
            if ref_time is not None:
                raw_sourced += 1

        if ref_time is None:
            no_source += 1
            continue

        new_updated = align_hour(ref_time)
        if new_updated != created_at:
            updates.append((str(task_id), new_updated))
            print(
                f"{str(task_id):<38} {'raw_info':<10} {_to_beijing(ref_time):<22} "
                f"{_to_beijing(created_at):<22} {_to_beijing(new_updated):<22}"
            )

    print("-" * 130)
    print(
        f"需变更: {len(updates)} / {len(rows)} "
        f"(trigger来源: {trigger_sourced}, raw_info来源: {raw_sourced}, 无来源: {no_source})"
    )

    if DRY_RUN:
        print("\n[DryRun] 未实际写入。添加 --apply 参数执行写入。")
    else:
        if not updates:
            print("无需变更。")
            return

        async with async_session() as db:
            for task_id, new_updated in updates:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(created_at=new_updated)
                )
            await db.commit()

        print(f"\n已更新 {len(updates)} 条记录。")


if __name__ == "__main__":
    asyncio.run(main())
