"""清理 quant_kb 中未被 wfuse.tasks 引用的孤儿记录。

检查范围：
  - analyses:       不在任何 task.analysis_ids 中的记录
  - trading_operations: 不在任何 task.trade_ids 中的记录
  - feedbacks:      不在任何 task.feedback_ids 中的记录

用法：
  python scripts/_cleanup_kb_orphans.py              # dry-run，仅预览
  python scripts/_cleanup_kb_orphans.py --apply      # 执行删除
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _unpack_jsonb_array(col) -> list[str]:
    """将 SQLAlchemy 返回的 JSONB 列转为字符串列表，处理字符串和 list 两种格式。"""
    if col is None:
        return []
    if isinstance(col, str):
        try:
            parsed = json.loads(col)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(col, list):
        return [str(x) for x in col]
    return []


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    # --- 1. 从 wfuse.tasks 收集所有被引用的 KB ID ---
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT analysis_ids, trade_ids, feedback_ids
            FROM tasks
            WHERE analysis_ids IS NOT NULL
               OR trade_ids IS NOT NULL
               OR feedback_ids IS NOT NULL
        """))
        rows = result.fetchall()

    ref_analyses: set[str] = set()
    ref_trades: set[str] = set()
    ref_feedbacks: set[str] = set()
    for r in rows:
        for aid in _unpack_jsonb_array(r[0]):
            ref_analyses.add(aid)
        for tid in _unpack_jsonb_array(r[1]):
            ref_trades.add(tid)
        for fid in _unpack_jsonb_array(r[2]):
            ref_feedbacks.add(fid)

    print(f"wfuse tasks 引用: analyses={len(ref_analyses)}, trades={len(ref_trades)}, feedbacks={len(ref_feedbacks)}")

    # --- 2. 从 quant_kb 查询全部 ID ---
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM analyses")
            all_analyses = {r[0] for r in cur.fetchall()}

            cur.execute("SELECT id::text FROM trading_operations")
            all_trades = {r[0] for r in cur.fetchall()}

            cur.execute("SELECT id::text FROM feedbacks")
            all_feedbacks = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    # --- 3. 计算差集 ---
    orphan_analyses = all_analyses - ref_analyses
    orphan_trades = all_trades - ref_trades
    orphan_feedbacks = all_feedbacks - ref_feedbacks

    print(f"quant_kb 总量:  analyses={len(all_analyses)}, trades={len(all_trades)}, feedbacks={len(all_feedbacks)}")
    print(f"孤儿 (KB有但wfuse未引用): analyses={len(orphan_analyses)}, trades={len(orphan_trades)}, feedbacks={len(orphan_feedbacks)}")

    total = len(orphan_analyses) + len(orphan_trades) + len(orphan_feedbacks)
    if total == 0:
        print("\n无孤儿记录，quant_kb 与 wfuse 完全一致。")
        return

    if not args.apply:
        print(f"\n[DRY RUN] 共 {total} 条孤儿记录，未执行删除。加上 --apply 以执行。")
        return

    # --- 4. 执行删除 ---
    print(f"\n开始删除 {total} 条孤儿记录...")
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            if orphan_analyses:
                cur.execute(
                    "DELETE FROM analyses WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_analyses)},
                )
                print(f"  analyses:           删除 {cur.rowcount} 行")
            if orphan_trades:
                cur.execute(
                    "DELETE FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_trades)},
                )
                print(f"  trading_operations: 删除 {cur.rowcount} 行")
            if orphan_feedbacks:
                cur.execute(
                    "DELETE FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(orphan_feedbacks)},
                )
                print(f"  feedbacks:          删除 {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()

    print("\n清理完成。")


if __name__ == "__main__":
    asyncio.run(main())
