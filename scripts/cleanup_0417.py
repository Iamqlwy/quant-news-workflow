"""删除 2026-04-17 触发的 trigger 关联的 tasks/trade/analysis

涉及两个数据库：
  - wfuse: 删除 tasks + entities
  - quant_kb: 删除 analysis 和 trading_operations

用法：确认无误后，将 DRY_RUN=True 改为 False 再执行。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from sqlalchemy import text

from src.db import async_session

DRY_RUN = False  # ← 确认后改为 False

# quant_kb 连接信息（与 scripts/analyze_trading_performance.py 一致）
KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}


async def gather_ids():
    """从 wfuse 查出所有要删的 task_id、analysis_id、trade_id"""
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT ta.id AS task_id,
                   ta.analysis_ids,
                   ta.trade_ids
            FROM tasks ta
            JOIN triggers tr ON ta.trigger_id = tr.id
            WHERE tr.triggered_at::date = '2026-04-17'
        """))
        rows = result.fetchall()

    task_ids = set()
    analysis_ids: set[str] = set()
    trade_ids: set[str] = set()

    for r in rows:
        tid, aids, trids = r._mapping["task_id"], r._mapping["analysis_ids"], r._mapping["trade_ids"]
        task_ids.add(str(tid))
        for aid in (aids or []):
            analysis_ids.add(str(aid))
        for trid in (trids or []):
            trade_ids.add(str(trid))

    return task_ids, analysis_ids, trade_ids


def delete_from_kb(cursor, analysis_ids: set[str], trade_ids: set[str]):
    """删除 quant_kb 中的 analysis 和 trading_operations"""
    if analysis_ids:
        cursor.execute(
            "DELETE FROM analyses WHERE id = ANY(%(ids)s)",
            {"ids": list(analysis_ids)},
        )
        print(f"  quant_kb.analyses: 删除 {cursor.rowcount} 行")

    if trade_ids:
        cursor.execute(
            "DELETE FROM trading_operations WHERE id = ANY(%(ids)s)",
            {"ids": list(trade_ids)},
        )
        print(f"  quant_kb.trading_operations: 删除 {cursor.rowcount} 行")


async def main():
    task_ids, analysis_ids, trade_ids = await gather_ids()
    print(f"待删除:")
    print(f"  wfuse.tasks:     {len(task_ids)} 个")
    print(f"  quant_kb.analysis: {len(analysis_ids)} 个")
    print(f"  quant_kb.trading_operations: {len(trade_ids)} 个")

    if DRY_RUN:
        print("\n[DRY RUN] 未执行实际删除。将 DRY_RUN=False 后重试。")
        return

    # 1. wfuse: 先删 entities（有 FK 到 tasks），再删 tasks
    async with async_session() as db:
        await db.execute(text("DELETE FROM entities WHERE task_id = ANY(:ids)"), {"ids": list(task_ids)})
        result = await db.execute(text("DELETE FROM tasks WHERE id = ANY(:ids)"), {"ids": list(task_ids)})
        print(f"\nwfuse.tasks: 删除 {result.rowcount} 行")
        await db.commit()

    # 2. quant_kb
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            delete_from_kb(cur, analysis_ids, trade_ids)
        conn.commit()
    finally:
        conn.close()

    print("\n删除完成。")


if __name__ == "__main__":
    asyncio.run(main())
