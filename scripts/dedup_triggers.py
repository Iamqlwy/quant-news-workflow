"""清理 tasks 表中重复 trigger_id 的行，每组只保留最早的一条。

涉及三个数据源：
  - wfuse: 删除 tasks + entities（重复行）
  - quant_kb: 删除 analyses / trading_operations / feedbacks

用法：确认无误后，将 DRY_RUN = True 改为 False 再执行。
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

# quant_kb 连接信息
KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}


async def main():
    async with async_session() as db:
        # ── Step 1: 按 trigger_id 分组，找出需要删除的 task 及其关联 ID ──
        result = await db.execute(text("""
            WITH ranked AS (
                SELECT
                    id,
                    trigger_id,
                    analysis_ids,
                    trade_ids,
                    feedback_ids,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY trigger_id
                        ORDER BY created_at ASC, id ASC
                    ) AS rn
                FROM tasks
                WHERE trigger_id IS NOT NULL
            )
            SELECT id, trigger_id, analysis_ids, trade_ids, feedback_ids, created_at, rn
            FROM ranked
            ORDER BY trigger_id, rn
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    # ── Step 2: 分类（保留 vs 删除） ──
    keep_ids: set[str] = set()
    delete_task_ids: set[str] = set()
    analysis_ids: set[str] = set()
    trade_ids: set[str] = set()
    feedback_ids: set[str] = set()

    trigger_groups: dict[str, list[dict]] = {}
    for r in rows:
        tid = str(r["trigger_id"])
        trigger_groups.setdefault(tid, []).append(r)

    for tid, group in trigger_groups.items():
        for i, r in enumerate(group):
            task_id = str(r["id"])
            if i == 0:
                # 第一条（最早）保留
                keep_ids.add(task_id)
            else:
                # 后续重复 → 删除
                delete_task_ids.add(task_id)
                for aid in (r["analysis_ids"] or []):
                    analysis_ids.add(str(aid))
                for tid_ in (r["trade_ids"] or []):
                    trade_ids.add(str(tid_))
                for fid in (r["feedback_ids"] or []):
                    feedback_ids.add(str(fid))

    # ── Step 3: 从被删除 task 的 analysis_ids/trade_ids 中，
    # 剔除那些也被保留 task 引用的 ID（避免误删共享实体） ──
    shared_analysis_ids: set[str] = set()
    shared_trade_ids: set[str] = set()
    shared_feedback_ids: set[str] = set()
    for r in rows:
        if str(r["id"]) in keep_ids:
            for aid in (r["analysis_ids"] or []):
                shared_analysis_ids.add(str(aid))
            for tid_ in (r["trade_ids"] or []):
                shared_trade_ids.add(str(tid_))
            for fid in (r["feedback_ids"] or []):
                shared_feedback_ids.add(str(fid))

    analysis_ids -= shared_analysis_ids
    trade_ids -= shared_trade_ids
    feedback_ids -= shared_feedback_ids

    # ── 统计 ──
    dup_trigger_count = sum(1 for g in trigger_groups.values() if len(g) > 1)
    print(f"=== 重复 trigger 统计 ===")
    print(f"总 trigger 组数: {len(trigger_groups)}")
    print(f"有重复的 trigger 组: {dup_trigger_count}")
    print(f"保留的 task: {len(keep_ids)} 个")
    print(f"待删除的 task: {len(delete_task_ids)} 个")
    print(f"待删除的 analysis: {len(analysis_ids)} 个")
    print(f"待删除的 trade: {len(trade_ids)} 个")
    print(f"待删除的 feedback: {len(feedback_ids)} 个")
    print()

    # ── 列出待删除的 task 详情 ──
    if delete_task_ids:
        print("=== 待删除 task 详情 ===")
        for tid, group in trigger_groups.items():
            if len(group) <= 1:
                continue
            print(f"\ntrigger_id={tid}:")
            for r in group:
                marker = " [保留]" if str(r["id"]) in keep_ids else " [删除]"
                print(f"  task_id={r['id']} created_at={r['created_at']} rn={r['rn']}{marker}")

    if DRY_RUN:
        print("\n[DRY RUN] 未执行实际删除。将 DRY_RUN = False 后重试。")
        return

    # ── Step 4: wfuse 删除（先 entities 后 tasks） ──
    async with async_session() as db:
        # 4a. 按 entity_uuid + entity_type 删除（A/T/F 对应的 entity 行）
        if analysis_ids:
            result = await db.execute(
                text("DELETE FROM entities WHERE entity_uuid = ANY(:ids) AND entity_type = 'A'"),
                {"ids": list(analysis_ids)},
            )
            print(f"\nwfuse.entities (A/analysis): 删除 {result.rowcount} 行")
        if trade_ids:
            result = await db.execute(
                text("DELETE FROM entities WHERE entity_uuid = ANY(:ids) AND entity_type = 'T'"),
                {"ids": list(trade_ids)},
            )
            print(f"wfuse.entities (T/trade): 删除 {result.rowcount} 行")
        if feedback_ids:
            result = await db.execute(
                text("DELETE FROM entities WHERE entity_uuid = ANY(:ids) AND entity_type = 'F'"),
                {"ids": list(feedback_ids)},
            )
            print(f"wfuse.entities (F/feedback): 删除 {result.rowcount} 行")

        # 4b. 按 task_id 删除（清除被删 task 关联的全部 entity）
        result = await db.execute(
            text("DELETE FROM entities WHERE task_id = ANY(:ids)"),
            {"ids": list(delete_task_ids)},
        )
        print(f"wfuse.entities (by task_id): 删除 {result.rowcount} 行")

        # 4c. 删除 task 本身
        result = await db.execute(
            text("DELETE FROM tasks WHERE id = ANY(:ids)"),
            {"ids": list(delete_task_ids)},
        )
        print(f"wfuse.tasks: 删除 {result.rowcount} 行")
        await db.commit()

    # ── Step 5: quant_kb 删除 ──
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            if analysis_ids:
                cur.execute(
                    "DELETE FROM analyses WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(analysis_ids)},
                )
                print(f"quant_kb.analyses: 删除 {cur.rowcount} 行")

            if trade_ids:
                cur.execute(
                    "DELETE FROM trading_operations WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(trade_ids)},
                )
                print(f"quant_kb.trading_operations: 删除 {cur.rowcount} 行")

            if feedback_ids:
                cur.execute(
                    "DELETE FROM feedbacks WHERE id::text = ANY(%(ids)s)",
                    {"ids": list(feedback_ids)},
                )
                print(f"quant_kb.feedbacks: 删除 {cur.rowcount} 行")
        conn.commit()
    finally:
        conn.close()

    print("\n清理完成。")


if __name__ == "__main__":
    asyncio.run(main())
