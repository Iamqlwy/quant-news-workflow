"""从 tasks 表查：哪些 task 的 trigger_id 指向 triggered_at=2026-04-17 的 trigger"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db import async_session


async def main():
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT ta.id AS task_id, ta.raw_info_id, ta.state, ta.info_type,
                   ta.trigger_id, ta.analysis_ids, ta.trade_ids,
                   tr.name AS trigger_name, tr.triggered_at, tr.action_type
            FROM tasks ta
            JOIN triggers tr ON ta.trigger_id = tr.id
            WHERE tr.triggered_at::date = '2026-04-17'
            ORDER BY ta.id
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]
        print(f"=== 共 {len(rows)} 个 task 的 trigger 在 2026-04-17 触发 ===\n")

        # 汇总
        all_trade_ids: set[str] = set()
        all_analysis_ids: set[str] = set()

        for r in rows:
            print(f"task_id={r['task_id']}")
            print(f"  trigger_name={r['trigger_name']}")
            print(f"  trigger_triggered_at={r['triggered_at']}")
            print(f"  action_type={r['action_type']}")
            print(f"  state={r['state']} info_type={r['info_type']}")
            print(f"  analysis_ids={r['analysis_ids']}")
            print(f"  trade_ids={r['trade_ids']}")
            print()
            for aid in (r["analysis_ids"] or []):
                all_analysis_ids.add(str(aid))
            for tid in (r["trade_ids"] or []):
                all_trade_ids.add(str(tid))

        print(f"=== trade IDs ({len(all_trade_ids)}) ===")
        for tid in sorted(all_trade_ids):
            print(f"  {tid}")

        print(f"\n=== analysis IDs ({len(all_analysis_ids)}) ===")
        for aid in sorted(all_analysis_ids):
            print(f"  {aid}")


if __name__ == "__main__":
    asyncio.run(main())
