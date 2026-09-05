"""详情脚本：展开 triggers 的 27 个幽灵引用 + KB 的 10 个孤儿记录"""
import asyncio, sys
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

    # ====================================================================
    # Part 1: triggers 幽灵引用详情
    # ====================================================================
    async with async_session() as db:
        # 1a. triggers.source_analysis_id 幽灵
        result = await db.execute(text(
            "SELECT id, name, status, source_analysis_id, trade_id, action_type, triggered_at "
            "FROM triggers WHERE source_analysis_id IS NOT NULL"
        ))
        rows = [dict(r._mapping) for r in result.fetchall()]
        ghost_sa = [r for r in rows if str(r["source_analysis_id"]) not in kb_a]
        print("=" * 70)
        print(f"triggers.source_analysis_id 幽灵: {len(ghost_sa)} 个")
        print("=" * 70)
        for r in ghost_sa:
            print(f"  trigger_id   = {r['id']}")
            print(f"  name         = {r['name']}")
            print(f"  status       = {r['status']}")
            print(f"  action_type  = {r['action_type']}")
            print(f"  triggered_at = {r['triggered_at']}")
            print(f"  ghost        = source_analysis_id={r['source_analysis_id']}")
            print()

        # 1b. triggers.trade_id 幽灵
        result = await db.execute(text(
            "SELECT id, name, status, source_analysis_id, trade_id, action_type, triggered_at "
            "FROM triggers WHERE trade_id IS NOT NULL"
        ))
        rows = [dict(r._mapping) for r in result.fetchall()]
        ghost_t = [r for r in rows if str(r["trade_id"]) not in kb_t]
        print("=" * 70)
        print(f"triggers.trade_id 幽灵: {len(ghost_t)} 个")
        print("=" * 70)
        for r in ghost_t:
            print(f"  trigger_id   = {r['id']}")
            print(f"  name         = {r['name']}")
            print(f"  status       = {r['status']}")
            print(f"  action_type  = {r['action_type']}")
            print(f"  triggered_at = {r['triggered_at']}")
            print(f"  ghost        = trade_id={r['trade_id']}")
            print()

    # ====================================================================
    # Part 2: KB 孤儿详情
    # ====================================================================
    # 收集 wfuse 中所有引用到的 KB ID
    async with async_session() as db:
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

    conn = psycopg2.connect(**KB_DB)
    with conn.cursor() as cur:
        if orphan_a:
            cur.execute(
                "SELECT id, created_at FROM analyses WHERE id::text = ANY(%(ids)s)",
                {"ids": list(orphan_a)},
            )
            print("=" * 70)
            print(f"quant_kb.analyses 孤儿（wfuse 无引用）: {len(orphan_a)} 个")
            print("=" * 70)
            for r in cur:
                print(f"  id={r[0]}  created_at={r[1]}")

        if orphan_t:
            cur.execute(
                "SELECT id, ticker, direction, status, created_at "
                "FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                {"ids": list(orphan_t)},
            )
            print("=" * 70)
            print(f"quant_kb.trading_operations 孤儿（wfuse 无引用）: {len(orphan_t)} 个")
            print("=" * 70)
            for r in cur:
                print(f"  id={r[0]}  ticker={r[1]}  direction={r[2]}  status={r[3]}  created_at={r[4]}")

        if orphan_f:
            cur.execute(
                "SELECT id, created_at FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                {"ids": list(orphan_f)},
            )
            print("=" * 70)
            print(f"quant_kb.feedbacks 孤儿（wfuse 无引用）: {len(orphan_f)} 个")
            print("=" * 70)
            for r in cur:
                print(f"  id={r[0]}  created_at={r[1]}")
    conn.close()
    print()


asyncio.run(main())
