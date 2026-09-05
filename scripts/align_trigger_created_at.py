"""将 triggers 表的 created_at 修正为对应 source_task_id 的 tasks.created_at。

用法：
    python scripts/align_trigger_created_at.py           # dry_run=True 预览变更
    python scripts/align_trigger_created_at.py --apply   # 实际写入
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text, update

from src.core.timezone import BEIJING_TZ
from src.db import async_session
from src.models.tables import TriggerRecord

DRY_RUN = "--apply" not in sys.argv


def _to_beijing(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(dt.astimezone(BEIJING_TZ))


async def main():
    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT t.id, t.created_at, t.source_task_id, tk.created_at "
                "FROM triggers t "
                "JOIN tasks tk ON tk.id = t.source_task_id "
                "WHERE t.created_at != tk.created_at "
                "ORDER BY t.created_at"
            )
        )
        rows = [(r[0], r[1], r[2], r[3]) for r in result.all()]

    print(f"共找到 {len(rows)} 条 triggers.created_at != tasks.created_at 的记录")

    print(f"\n模式: {'DRY RUN (预览)' if DRY_RUN else 'APPLY (实际写入)'}")
    print()
    print(f"{'trigger_id':<38} {'old_created':<22} {'task_created':<22}")
    print("-" * 100)

    updates: list[tuple[str, datetime]] = []

    for trigger_id, old_created, _, task_created in rows:
        updates.append((str(trigger_id), task_created))
        print(
            f"{str(trigger_id):<38} {_to_beijing(old_created):<22} {_to_beijing(task_created):<22}"
        )

    print("-" * 100)
    print(f"需变更: {len(updates)} / {len(rows)}")

    if DRY_RUN:
        print("\n[DryRun] 未实际写入。添加 --apply 参数执行写入。")
    else:
        if not updates:
            print("无需变更。")
            return

        async with async_session() as db:
            for trigger_id, new_created in updates:
                await db.execute(
                    update(TriggerRecord)
                    .where(TriggerRecord.id == trigger_id)
                    .values(created_at=new_created)
                )
            await db.commit()

        print(f"\n已更新 {len(updates)} 条记录。")


if __name__ == "__main__":
    asyncio.run(main())
