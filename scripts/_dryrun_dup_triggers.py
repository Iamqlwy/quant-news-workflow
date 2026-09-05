import asyncio, sys
sys.path.insert(0, ".")
from sqlalchemy import text
from src.db import async_session

async def main():
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT trigger_id, COUNT(*) AS cnt,
                   MIN(created_at) AS earliest, MAX(created_at) AS latest
            FROM tasks
            WHERE trigger_id IS NOT NULL
            GROUP BY trigger_id
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
        """))
        dup_groups = [dict(r._mapping) for r in result.fetchall()]

    total_dup_triggers = len(dup_groups)
    total_all = sum(g["cnt"] for g in dup_groups)
    total_to_delete = total_all - total_dup_triggers  # 每组保留1条

    print(f"重复 trigger_id 组数: {total_dup_triggers}")
    print(f"涉及 task 总数: {total_all}，待删除: {total_to_delete}")
    print()
    for g in dup_groups:
        print(f"  trigger_id={g['trigger_id']}  重复{g['cnt']}次  最早={g['earliest']}  最晚={g['latest']}")

asyncio.run(main())
