"""补救脚本：清理 quant_kb 中失去 wfuse.entities 引用的孤儿记录。

前置条件：dedup_triggers.py 已删除了 wfuse 中的 entity 和 task 行，
但 quant_kb 的 analyses / trading_operations / feedbacks 未被清理。

逻辑：从 quant_kb 查询所有 analysis/trade/feedback ID，
与 wfuse.entities 中现存的 entity_uuid 对比，缺失的即为孤儿。
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

KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}


async def main():
    # ── 收集 wfuse.entities 中现存的 entity_uuid ──
    async with async_session() as db:
        result = await db.execute(text("SELECT entity_uuid, entity_type FROM entities"))
        wfuse_entities = {(str(r.entity_uuid), r.entity_type) for r in result.fetchall()}

    print(f"wfuse.entities 现存: {len(wfuse_entities)} 行")

    # ── 连接 quant_kb，查所有 ID ──
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM analyses")
            kb_analyses = {str(r[0]) for r in cur.fetchall()}
            cur.execute("SELECT id FROM trading_operations")
            kb_trades = {str(r[0]) for r in cur.fetchall()}
            cur.execute("SELECT id FROM feedbacks")
            kb_feedbacks = {str(r[0]) for r in cur.fetchall()}

        print(f"quant_kb.analyses: {len(kb_analyses)} 行")
        print(f"quant_kb.trading_operations: {len(kb_trades)} 行")
        print(f"quant_kb.feedbacks: {len(kb_feedbacks)} 行")

        # ── 找出孤儿（quant_kb 有但 wfuse.entities 无） ──
        orphan_analyses = {aid for aid in kb_analyses if (aid, "A") not in wfuse_entities}
        orphan_trades = {tid for tid in kb_trades if (tid, "T") not in wfuse_entities}
        orphan_feedbacks = {fid for fid in kb_feedbacks if (fid, "F") not in wfuse_entities}

        print(f"\n待删除 analysis (孤儿): {len(orphan_analyses)} 个")
        print(f"待删除 trade (孤儿): {len(orphan_trades)} 个")
        print(f"待删除 feedback (孤儿): {len(orphan_feedbacks)} 个")

        if DRY_RUN:
            print("\n[DRY RUN] 未执行实际删除。将 DRY_RUN = False 后重试。")
            return

        # ── 执行删除 ──
        with conn.cursor() as cur:
            if orphan_analyses:
                cur.execute(
                    "DELETE FROM analyses WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_analyses)},
                )
                print(f"quant_kb.analyses: 删除 {cur.rowcount} 行")

            if orphan_trades:
                cur.execute(
                    "DELETE FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_trades)},
                )
                print(f"quant_kb.trading_operations: 删除 {cur.rowcount} 行")

            if orphan_feedbacks:
                cur.execute(
                    "DELETE FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_feedbacks)},
                )
                print(f"quant_kb.feedbacks: 删除 {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()

    print("\n清理完成。")


if __name__ == "__main__":
    asyncio.run(main())
