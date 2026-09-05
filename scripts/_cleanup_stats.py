"""统计 raw_info_id 重复 >2 的 tasks 数量，输出待删除的 task_id 列表（dry-run 默认）。"""
import asyncio, sys, json
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import text
from src.db import async_session


async def main():
    async with async_session() as db:
        # 找出 raw_info_id 出现 >2 次的组，每个组保留最早2条，其余要删除
        result = await db.execute(text("""
            WITH ranked AS (
                SELECT
                    id, raw_info_id,
                    ROW_NUMBER() OVER (PARTITION BY raw_info_id ORDER BY created_at ASC, id ASC) AS rn,
                    created_at, state, trigger_id,
                    analysis_ids, trade_ids, feedback_ids
                FROM tasks
                WHERE raw_info_id IN (
                    SELECT raw_info_id FROM tasks
                    WHERE raw_info_id IS NOT NULL
                    GROUP BY raw_info_id
                    HAVING COUNT(*) > 2
                )
            )
            SELECT id, raw_info_id, rn, state, created_at, trigger_id,
                   analysis_ids, trade_ids, feedback_ids
            FROM ranked
            ORDER BY raw_info_id, rn
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    dup_raw_info_counts = {}
    for r in rows:
        rid = str(r["raw_info_id"])
        dup_raw_info_counts.setdefault(rid, {"total": 0, "to_delete": 0})
        dup_raw_info_counts[rid]["total"] += 1
        if r["rn"] > 2:
            dup_raw_info_counts[rid]["to_delete"] += 1

    total_groups = len(dup_raw_info_counts)
    total_tasks_in_groups = sum(d["total"] for d in dup_raw_info_counts.values())
    total_to_delete = sum(d["to_delete"] for d in dup_raw_info_counts.values())

    print(f"raw_info_id 重复 >2 的组数: {total_groups}")
    print(f"这些组中的 tasks 总数: {total_tasks_in_groups}")
    print(f"待删除 task 数（每组保留最早2条）: {total_to_delete}")
    print()

    # Show some examples
    print("=== 前10组示例 ===")
    for i, (rid, d) in enumerate(sorted(dup_raw_info_counts.items(), key=lambda x: -x[1]["total"])):
        if i >= 10:
            break
        print(f"  raw_info_id={rid}: {d['total']} tasks, 删除 {d['to_delete']} 条")

    # Collect to_delete task IDs
    to_delete_ids = [
        str(r["id"]) for r in rows
        if r["rn"] > 2
    ]

    # Collect all KB refs from deleted tasks
    all_analysis_ids = set()
    all_trade_ids = set()
    all_feedback_ids = set()
    keep_task_ids = set()
    for r in rows:
        tid = str(r["id"])
        if r["rn"] <= 2:
            keep_task_ids.add(tid)
        else:
            for aid in (r["analysis_ids"] or []):
                all_analysis_ids.add(str(aid))
            for t_id in (r["trade_ids"] or []):
                all_trade_ids.add(str(t_id))
            for fid in (r["feedback_ids"] or []):
                all_feedback_ids.add(str(fid))

    # Remove IDs that are also referenced by kept tasks
    for r in rows:
        if r["rn"] <= 2:
            for aid in (r["analysis_ids"] or []):
                all_analysis_ids.discard(str(aid))
            for t_id in (r["trade_ids"] or []):
                all_trade_ids.discard(str(t_id))
            for fid in (r["feedback_ids"] or []):
                all_feedback_ids.discard(str(fid))

    # Also get entity refs from deleted tasks
    entity_result = await db.execute(text("""
        SELECT id, entity_uuid, entity_type FROM entities
        WHERE task_id = ANY(:task_ids)
    """), {"task_ids": to_delete_ids})
    entities_to_delete = [dict(r._mapping) for r in entity_result.fetchall()]

    print()
    print(f"待删除 tasks: {len(to_delete_ids)}")
    print(f"待删除 entities: {len(entities_to_delete)}")
    print(f"待删除 KB analyses (去重后): {len(all_analysis_ids)}")
    print(f"待删除 KB trades (去重后): {len(all_trade_ids)}")
    print(f"待删除 KB feedbacks (去重后): {len(all_feedback_ids)}")

    # Save for the actual fix script
    payload = {
        "delete_task_ids": sorted(to_delete_ids)[:1000],  # cap for file size
        "delete_entity_ids": [str(e["id"]) for e in entities_to_delete][:5000],
        "delete_analysis_ids": sorted(all_analysis_ids)[:500],
        "delete_trade_ids": sorted(all_trade_ids)[:500],
        "delete_feedback_ids": sorted(all_feedback_ids)[:500],
    }
    with open(Path(__file__).parent / "_cleanup_payload.json", "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\npayload saved to scripts/_cleanup_payload.json")

asyncio.run(main())
