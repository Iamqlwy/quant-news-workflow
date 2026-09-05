"""清理幽灵引用：triggers 的 27 个幽灵引用 + quant_kb 的 10 个孤儿记录"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from sqlalchemy import text
from src.db import async_session

KB_DB = {"dbname": "quant_kb", "host": "localhost", "port": 15432, "user": "postgres", "password": "postgres"}


async def main():
    # ── 收集 KB 现存 ID ──
    conn = psycopg2.connect(**KB_DB)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM analyses")
        kb_a = {str(r[0]) for r in cur.fetchall()}
        cur.execute("SELECT id FROM trading_operations")
        kb_t = {str(r[0]) for r in cur.fetchall()}
        cur.execute("SELECT id FROM feedbacks")
        kb_f = {str(r[0]) for r in cur.fetchall()}
    conn.close()

    async with async_session() as db:
        # ── 1. 查出所有 ghost trigger 引用 ──
        result = await db.execute(text("SELECT id, source_analysis_id FROM triggers WHERE source_analysis_id IS NOT NULL"))
        ghost_sa_ids = [str(r.id) for r in result.fetchall() if str(r.source_analysis_id) not in kb_a]

        result = await db.execute(text("SELECT id, trade_id FROM triggers WHERE trade_id IS NOT NULL"))
        ghost_t_ids = [str(r.id) for r in result.fetchall() if str(r.trade_id) not in kb_t]

        print(f"triggers.source_analysis_id 幽灵: {len(ghost_sa_ids)} 个")
        print(f"triggers.trade_id 幽灵: {len(ghost_t_ids)} 个")
        total_trigger_fixes = len(ghost_sa_ids) + len(ghost_t_ids)

        # ── 2. 修复 wfuse.triggers ──
        if ghost_sa_ids:
            result = await db.execute(
                text("UPDATE triggers SET source_analysis_id = NULL WHERE id = ANY(:ids)"),
                {"ids": ghost_sa_ids},
            )
            print(f"  → triggers.source_analysis_id: {len(ghost_sa_ids)} 行置为 NULL")

        if ghost_t_ids:
            result = await db.execute(
                text("UPDATE triggers SET trade_id = NULL WHERE id = ANY(:ids)"),
                {"ids": ghost_t_ids},
            )
            print(f"  → triggers.trade_id: {len(ghost_t_ids)} 行置为 NULL")

        # ── 3. 查出 KB 孤儿 ──
        used_a: set[str] = set()
        used_t: set[str] = set()
        used_f: set[str] = set()

        r = await db.execute(text("SELECT analysis_ids, trade_ids, feedback_ids FROM tasks"))
        for row in r:
            for aid in (row.analysis_ids or []):
                used_a.add(str(aid))
            for tid in (row.trade_ids or []):
                used_t.add(str(tid))
            for fid in (row.feedback_ids or []):
                used_f.add(str(fid))

        r = await db.execute(text("SELECT entity_uuid, entity_type FROM entities"))
        for row in r:
            if row.entity_type == "A":
                used_a.add(str(row.entity_uuid))
            elif row.entity_type == "T":
                used_t.add(str(row.entity_uuid))
            elif row.entity_type == "F":
                used_f.add(str(row.entity_uuid))

        r = await db.execute(text("SELECT source_analysis_id, trade_id FROM triggers"))
        for row in r:
            if row.source_analysis_id:
                used_a.add(str(row.source_analysis_id))
            if row.trade_id:
                used_t.add(str(row.trade_id))

        r = await db.execute(text("SELECT trade_id FROM price_monitors"))
        for row in r:
            if row.trade_id:
                used_t.add(str(row.trade_id))

        orphan_a = kb_a - used_a
        orphan_t = kb_t - used_t
        orphan_f = kb_f - used_f

        print(f"\nquant_kb.analyses 孤儿: {len(orphan_a)} 个")
        print(f"quant_kb.trading_operations 孤儿: {len(orphan_t)} 个")
        print(f"quant_kb.feedbacks 孤儿: {len(orphan_f)} 个")

        await db.commit()

    # ── 4. 删除 KB 孤儿 ──
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            if orphan_a:
                cur.execute(
                    "DELETE FROM analyses WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_a)},
                )
                print(f"  → quant_kb.analyses: 删除 {cur.rowcount} 行")
            if orphan_t:
                cur.execute(
                    "DELETE FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_t)},
                )
                print(f"  → quant_kb.trading_operations: 删除 {cur.rowcount} 行")
            if orphan_f:
                cur.execute(
                    "DELETE FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_f)},
                )
                print(f"  → quant_kb.feedbacks: 删除 {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()

    total = total_trigger_fixes + len(orphan_a) + len(orphan_t) + len(orphan_f)
    print(f"\n清理完成，共处理 {total} 处。")


asyncio.run(main())
