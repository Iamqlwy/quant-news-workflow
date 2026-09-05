"""清理 raw_info_id 重复 >3 的 tasks，每组保留最早3条。

删除顺序：
  1. wfuse.entities（按 task_id 关联）
  2. wfuse.tasks
  3. quant_kb.analyses / trading_operations / feedbacks（未被保留 task 共享的）

用法：
  python scripts/_cleanup_dupes.py --dry-run    # 默认，仅预览
  python scripts/_cleanup_dupes.py --apply       # 执行删除
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import psycopg2
from sqlalchemy import text

from src.db import async_session

KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    # ── Step 1: 收集要删除的 task_id 和其关联的 KB ID ──
    async with async_session() as db:
        result = await db.execute(text("""
            WITH ranked AS (
                SELECT id, raw_info_id,
                    ROW_NUMBER() OVER (PARTITION BY raw_info_id ORDER BY created_at ASC, id ASC) AS rn,
                    analysis_ids, trade_ids, feedback_ids
                FROM tasks
                WHERE raw_info_id IN (
                    SELECT raw_info_id FROM tasks
                    WHERE raw_info_id IS NOT NULL
                    GROUP BY raw_info_id HAVING COUNT(*) > 3
                )
            )
            SELECT id, raw_info_id, rn, analysis_ids, trade_ids, feedback_ids
            FROM ranked
            ORDER BY raw_info_id, rn
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    # Keep rn <= 3, delete rn > 3
    delete_task_ids = [str(r["id"]) for r in rows if r["rn"] > 2]

    # Collect KB refs from deleted tasks
    delete_analysis_ids: set[str] = set()
    delete_trade_ids: set[str] = set()
    delete_feedback_ids: set[str] = set()
    for r in rows:
        if r["rn"] > 2:
            for aid in (r["analysis_ids"] or []):
                delete_analysis_ids.add(str(aid))
            for tid in (r["trade_ids"] or []):
                delete_trade_ids.add(str(tid))
            for fid in (r["feedback_ids"] or []):
                delete_feedback_ids.add(str(fid))

    # Dedupe: remove IDs also referenced by kept tasks
    for r in rows:
        if r["rn"] <= 2:
            for aid in (r["analysis_ids"] or []):
                delete_analysis_ids.discard(str(aid))
            for tid in (r["trade_ids"] or []):
                delete_trade_ids.discard(str(tid))
            for fid in (r["feedback_ids"] or []):
                delete_feedback_ids.discard(str(fid))

    # Get entities for deleted tasks
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT id, entity_uuid, entity_type FROM entities
            WHERE task_id = ANY(:task_ids)
        """), {"task_ids": delete_task_ids})
        entity_rows = [dict(r._mapping) for r in result.fetchall()]

    delete_entity_ids = [str(r["id"]) for r in entity_rows]

    print(f"待删除 tasks:        {len(delete_task_ids)}")
    print(f"待删除 entities:      {len(delete_entity_ids)}")
    print(f"待删除 KB analyses:   {len(delete_analysis_ids)}")
    print(f"待删除 KB trades:     {len(delete_trade_ids)}")
    print(f"待删除 KB feedbacks:   {len(delete_feedback_ids)}")

    if not args.apply:
        print("\n[DRY RUN] 未执行实际删除。加上 --apply 以执行。")
        return

    # ── Step 2: 执行删除 ──
    print("\n开始删除...")

    # 2a. wfuse entities
    async with async_session() as db:
        result = await db.execute(
            text("DELETE FROM entities WHERE id = ANY(:ids)"),
            {"ids": delete_entity_ids},
        )
        print(f"  wfuse.entities: 删除 {result.rowcount} 行")

        # 2b. 也按 task_id 清理残留
        result = await db.execute(
            text("DELETE FROM entities WHERE task_id = ANY(:ids)"),
            {"ids": delete_task_ids},
        )
        residue = result.rowcount
        if residue > 0:
            print(f"  wfuse.entities (by task_id 残留): 删除 {residue} 行")

        # 2c. wfuse tasks
        result = await db.execute(
            text("DELETE FROM tasks WHERE id = ANY(:ids)"),
            {"ids": delete_task_ids},
        )
        print(f"  wfuse.tasks: 删除 {result.rowcount} 行")
        await db.commit()

    # 2d. quant_kb
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            if delete_analysis_ids:
                cur.execute(
                    "DELETE FROM analyses WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(delete_analysis_ids)},
                )
                print(f"  quant_kb.analyses: 删除 {cur.rowcount} 行")
            if delete_trade_ids:
                cur.execute(
                    "DELETE FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(delete_trade_ids)},
                )
                print(f"  quant_kb.trading_operations: 删除 {cur.rowcount} 行")
            if delete_feedback_ids:
                cur.execute(
                    "DELETE FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(delete_feedback_ids)},
                )
                print(f"  quant_kb.feedbacks: 删除 {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()

    print("\n清理完成。")


if __name__ == "__main__":
    asyncio.run(main())
