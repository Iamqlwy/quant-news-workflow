"""数据一致性检查：检测 wfuse → quant_kb 的幽灵引用（指向不存在记录的引用）。

检查方向：
  1. wfuse → quant_kb：wfuse 中的引用字段指向 quant_kb 中不存在的记录（幽灵引用）
  2. quant_kb → wfuse：quant_kb 中有记录但 wfuse 中无引用（潜在孤儿，可选检查）

用法：
  python scripts/check_data_consistency.py           # 仅检查方向1（幽灵引用）
  python scripts/check_data_consistency.py --all     # 同时检查方向1和方向2
  python scripts/check_data_consistency.py --fix     # 检查并清理幽灵引用（将无效ID从wfuse中移除）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from sqlalchemy import text

from src.db import async_session

# quant_kb 连接信息
KB_DB = {
    "dbname": "quant_kb",
    "host": "localhost",
    "port": 15432,
    "user": "postgres",
    "password": "postgres",
}

# entity_type → (kb_table, kb_id_column)
ENTITY_TYPE_MAP = {
    "A": ("analyses", "id"),
    "T": ("trading_operations", "id"),
    "F": ("feedbacks", "id"),
    "N": ("nodes", "id"),
    "R": ("raw_information", "id"),
    "G": (None, None),  # trigger，在 wfuse 本地
}


def _kb_table_for_type(entity_type: str) -> str | None:
    info = ENTITY_TYPE_MAP.get(entity_type)
    return info[0] if info else None


async def check_wfuse_to_kb(dry_run: bool = True, fix: bool = False):
    """检查 wfuse → quant_kb 方向：wfuse 引用是否指向不存在的 KB 记录。"""

    # ── 1. 收集 quant_kb 中所有现存 ID ──
    conn = psycopg2.connect(**KB_DB)
    kb_ids: dict[str, set[str]] = {}  # table_name → set of id strings
    try:
        with conn.cursor() as cur:
            for table in ["raw_information", "analyses", "trading_operations", "feedbacks", "nodes"]:
                try:
                    cur.execute(f'SELECT id FROM "{table}"' if table == "raw_information" else f"SELECT id FROM {table}")
                    kb_ids[table] = {str(r[0]) for r in cur.fetchall()}
                except Exception as e:
                    print(f"  [WARN] 无法查询 quant_kb.{table}: {e}")
                    kb_ids[table] = set()
    finally:
        conn.close()

    for table, ids in kb_ids.items():
        print(f"quant_kb.{table}: {len(ids)} 行")

    # ── 2. 收集 wfuse 中的所有引用 ──
    issues: list[dict] = []  # {source_table, source_field, source_id, ghost_id, ghost_type}

    async with async_session() as db:
        # 2a. tasks.raw_info_id → raw_information
        result = await db.execute(text("SELECT id, raw_info_id FROM tasks WHERE raw_info_id IS NOT NULL"))
        for r in result.fetchall():
            rid = str(r.raw_info_id)
            if rid not in kb_ids.get("raw_information", set()):
                issues.append({
                    "source_table": "tasks",
                    "source_field": "raw_info_id",
                    "source_id": str(r.id),
                    "ghost_id": rid,
                    "ghost_type": "raw_information",
                })

        # 2b. tasks.analysis_ids → analyses
        result = await db.execute(text("SELECT id, analysis_ids FROM tasks WHERE analysis_ids IS NOT NULL AND jsonb_array_length(analysis_ids) > 0"))
        for r in result.fetchall():
            for aid in (r.analysis_ids or []):
                aid_str = str(aid)
                if aid_str not in kb_ids.get("analyses", set()):
                    issues.append({
                        "source_table": "tasks",
                        "source_field": "analysis_ids",
                        "source_id": str(r.id),
                        "ghost_id": aid_str,
                        "ghost_type": "analyses",
                    })

        # 2c. tasks.trade_ids → trading_operations
        result = await db.execute(text("SELECT id, trade_ids FROM tasks WHERE trade_ids IS NOT NULL AND jsonb_array_length(trade_ids) > 0"))
        for r in result.fetchall():
            for tid in (r.trade_ids or []):
                tid_str = str(tid)
                if tid_str not in kb_ids.get("trading_operations", set()):
                    issues.append({
                        "source_table": "tasks",
                        "source_field": "trade_ids",
                        "source_id": str(r.id),
                        "ghost_id": tid_str,
                        "ghost_type": "trading_operations",
                    })

        # 2d. tasks.feedback_ids → feedbacks
        result = await db.execute(text("SELECT id, feedback_ids FROM tasks WHERE feedback_ids IS NOT NULL AND jsonb_array_length(feedback_ids) > 0"))
        for r in result.fetchall():
            for fid in (r.feedback_ids or []):
                fid_str = str(fid)
                if fid_str not in kb_ids.get("feedbacks", set()):
                    issues.append({
                        "source_table": "tasks",
                        "source_field": "feedback_ids",
                        "source_id": str(r.id),
                        "ghost_id": fid_str,
                        "ghost_type": "feedbacks",
                    })

        # 2e. entities.entity_uuid → 对应 KB 表
        result = await db.execute(text("SELECT id, entity_type, entity_uuid, task_id FROM entities"))
        for r in result.fetchall():
            etype = str(r.entity_type)
            euuid = str(r.entity_uuid)
            kb_table = _kb_table_for_type(etype)
            if kb_table and euuid not in kb_ids.get(kb_table, set()):
                issues.append({
                    "source_table": "entities",
                    "source_field": "entity_uuid",
                    "source_id": str(r.id),
                    "ghost_id": euuid,
                    "ghost_type": f"{kb_table} (type={etype})",
                })

        # 2f. triggers.source_analysis_id → analyses
        result = await db.execute(text("SELECT id, source_analysis_id FROM triggers WHERE source_analysis_id IS NOT NULL"))
        for r in result.fetchall():
            said = str(r.source_analysis_id)
            if said not in kb_ids.get("analyses", set()):
                issues.append({
                    "source_table": "triggers",
                    "source_field": "source_analysis_id",
                    "source_id": str(r.id),
                    "ghost_id": said,
                    "ghost_type": "analyses",
                })

        # 2g. triggers.trade_id → trading_operations
        result = await db.execute(text("SELECT id, trade_id FROM triggers WHERE trade_id IS NOT NULL"))
        for r in result.fetchall():
            tid = str(r.trade_id)
            if tid not in kb_ids.get("trading_operations", set()):
                issues.append({
                    "source_table": "triggers",
                    "source_field": "trade_id",
                    "source_id": str(r.id),
                    "ghost_id": tid,
                    "ghost_type": "trading_operations",
                })

        # 2h. price_monitors.trade_id → trading_operations
        result = await db.execute(text("SELECT id, trade_id FROM price_monitors WHERE trade_id IS NOT NULL"))
        for r in result.fetchall():
            tid = str(r.trade_id)
            if tid not in kb_ids.get("trading_operations", set()):
                issues.append({
                    "source_table": "price_monitors",
                    "source_field": "trade_id",
                    "source_id": str(r.id),
                    "ghost_id": tid,
                    "ghost_type": "trading_operations",
                })

        # ── 3. 汇总报告 ──
        print(f"\n{'='*60}")
        print(f"幽灵引用检查结果：共 {len(issues)} 个幽灵引用")
        print(f"{'='*60}")

        if not issues:
            print("未发现幽灵引用，数据一致。")
            return

        # 按来源表分组统计
        by_table = defaultdict(list)
        for iss in issues:
            by_table[iss["source_table"]].append(iss)

        for table, items in sorted(by_table.items()):
            print(f"\n--- {table} ({len(items)} 个幽灵引用) ---")
            # 按字段再分组
            by_field = defaultdict(list)
            for item in items:
                by_field[item["source_field"]].append(item)
            for field, fitems in sorted(by_field.items()):
                print(f"  {field}: {len(fitems)} 个")
                for item in fitems[:10]:  # 最多显示前10条
                    print(f"    wfuse_id={item['source_id']} → ghost {item['ghost_type']}={item['ghost_id']}")
                if len(fitems) > 10:
                    print(f"    ... 还有 {len(fitems) - 10} 条")

        # ── 4. 修复（可选）──
        if fix and not dry_run:
            print(f"\n{'='*60}")
            print("开始修复幽灵引用...")
            print(f"{'='*60}")

            # 按 (source_table, source_field) 分组修复
            fixed_count = 0

            for table in sorted(by_table):
                items = by_table[table]
                by_field = defaultdict(list)
                for item in items:
                    by_field[item["source_field"]].append(item)

                if table == "tasks":
                    for field, fitems in by_field.items():
                        if field == "raw_info_id":
                            # 将 raw_info_id 置为 NULL（或跳过，因为这是核心字段）
                            print(f"\n  [WARN] tasks.raw_info_id 有 {len(fitems)} 个幽灵引用，跳过自动修复（需人工判断）")
                            for item in fitems:
                                print(f"    task_id={item['source_id']} → ghost raw_information={item['ghost_id']}")
                            continue

                        # analysis_ids / trade_ids / feedback_ids: 从 JSONB 数组中移除幽灵 ID
                        ghost_ids_for_task: dict[str, set[str]] = defaultdict(set)
                        for item in fitems:
                            ghost_ids_for_task[item["source_id"]].add(item["ghost_id"])

                        for task_id, ghost_set in ghost_ids_for_task.items():
                            col = field
                            # 使用 jsonb - 操作符移除元素
                            for gid in ghost_set:
                                await db.execute(
                                    text(f"UPDATE tasks SET {col} = {col} - :gid WHERE id = :tid"),
                                    {"gid": gid, "tid": task_id},
                                )
                            fixed_count += 1
                            print(f"  tasks.{field}: task_id={task_id} 移除 {len(ghost_set)} 个幽灵ID")

                elif table == "entities":
                    ghost_entity_ids = {item["source_id"] for item in items}
                    result = await db.execute(
                        text("DELETE FROM entities WHERE id = ANY(:ids)"),
                        {"ids": list(ghost_entity_ids)},
                    )
                    print(f"\n  entities: 删除 {result.rowcount} 条幽灵实体记录")
                    fixed_count += result.rowcount

                elif table == "triggers":
                    for field, fitems in by_field.items():
                        for item in fitems:
                            col = "source_analysis_id" if field == "source_analysis_id" else "trade_id"
                            await db.execute(
                                text(f"UPDATE triggers SET {col} = NULL WHERE id = :tid"),
                                {"tid": item["source_id"]},
                            )
                            fixed_count += 1
                            print(f"  triggers.{field}: trigger_id={item['source_id']} 置为 NULL")

                elif table == "price_monitors":
                    for item in items:
                        await db.execute(
                            text("UPDATE price_monitors SET trade_id = NULL WHERE id = :pid"),
                            {"pid": item["source_id"]},
                        )
                        fixed_count += 1
                        print(f"  price_monitors.trade_id: monitor_id={item['source_id']} 置为 NULL")

            await db.commit()
            print(f"\n修复完成，共处理 {fixed_count} 处。")
        elif fix and dry_run:
            print("\n[DRY RUN 模式] 未执行修复。去掉 --dry-run 以实际修复。")


async def check_kb_to_wfuse():
    """检查 quant_kb → wfuse 方向：KB 中有记录但 wfuse 中无引用（孤儿记录）。"""
    conn = psycopg2.connect(**KB_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM analyses")
            kb_analyses = {str(r[0]) for r in cur.fetchall()}
            cur.execute("SELECT id FROM trading_operations")
            kb_trades = {str(r[0]) for r in cur.fetchall()}
            cur.execute("SELECT id FROM feedbacks")
            kb_feedbacks = {str(r[0]) for r in cur.fetchall()}
            try:
                cur.execute("SELECT id FROM nodes")
                kb_nodes = {str(r[0]) for r in cur.fetchall()}
            except Exception:
                kb_nodes = set()
            try:
                cur.execute('SELECT id FROM "raw_information"')
                kb_raw = {str(r[0]) for r in cur.fetchall()}
            except Exception:
                kb_raw = set()
    finally:
        conn.close()

    async with async_session() as db:
        # 收集 wfuse 中所有引用的 KB ID
        result = await db.execute(text("SELECT entity_uuid, entity_type FROM entities"))
        wfuse_refs: dict[str, set[str]] = defaultdict(set)  # entity_type → set of uuids
        for r in result.fetchall():
            wfuse_refs[str(r.entity_type)].add(str(r.entity_uuid))

        # tasks 中的 JSONB 数组引用
        result = await db.execute(text("SELECT analysis_ids, trade_ids, feedback_ids FROM tasks"))
        for r in result.fetchall():
            for aid in (r.analysis_ids or []):
                wfuse_refs["A"].add(str(aid))
            for tid in (r.trade_ids or []):
                wfuse_refs["T"].add(str(tid))
            for fid in (r.feedback_ids or []):
                wfuse_refs["F"].add(str(fid))

        # tasks.raw_info_id
        result = await db.execute(text("SELECT raw_info_id FROM tasks"))
        for r in result.fetchall():
            if r.raw_info_id:
                wfuse_refs["R"].add(str(r.raw_info_id))

        # triggers 中的引用
        result = await db.execute(text("SELECT source_analysis_id, trade_id FROM triggers"))
        for r in result.fetchall():
            if r.source_analysis_id:
                wfuse_refs["A"].add(str(r.source_analysis_id))
            if r.trade_id:
                wfuse_refs["T"].add(str(r.trade_id))

        # price_monitors 中的引用
        result = await db.execute(text("SELECT trade_id FROM price_monitors"))
        for r in result.fetchall():
            if r.trade_id:
                wfuse_refs["T"].add(str(r.trade_id))

    # 对比
    print(f"\n{'='*60}")
    print(f"KB 孤儿记录检查（quant_kb 中有但 wfuse 无引用）")
    print(f"{'='*60}")

    orphan_analyses = kb_analyses - wfuse_refs.get("A", set())
    orphan_trades = kb_trades - wfuse_refs.get("T", set())
    orphan_feedbacks = kb_feedbacks - wfuse_refs.get("F", set())
    orphan_nodes = kb_nodes - wfuse_refs.get("N", set())
    orphan_raw = kb_raw - wfuse_refs.get("R", set())

    total_orphans = len(orphan_analyses) + len(orphan_trades) + len(orphan_feedbacks) + len(orphan_nodes) + len(orphan_raw)

    print(f"analyses 孤儿:          {len(orphan_analyses)} / {len(kb_analyses)}")
    print(f"trading_operations 孤儿: {len(orphan_trades)} / {len(kb_trades)}")
    print(f"feedbacks 孤儿:         {len(orphan_feedbacks)} / {len(kb_feedbacks)}")
    print(f"nodes 孤儿:             {len(orphan_nodes)} / {len(kb_nodes)}")
    print(f"raw_information 孤儿:   {len(orphan_raw)} / {len(kb_raw)}")
    print(f"\n总计: {total_orphans} 个 KB 孤儿记录")

    if total_orphans > 0:
        print("\n提示：使用 scripts/cleanup_orphan_kb.py 清理这些孤儿记录。")


async def main():
    parser = argparse.ArgumentParser(description="数据一致性检查：wfuse ↔ quant_kb")
    parser.add_argument("--all", action="store_true", help="同时检查双向（默认仅检查 wfuse→KB）")
    parser.add_argument("--fix", action="store_true", help="修复幽灵引用（从 wfuse 中移除无效引用）")
    parser.add_argument("--dry-run", action="store_true", default=True, help="仅报告不修复（默认开启）")
    args = parser.parse_args()

    print("=" * 60)
    print("数据一致性检查：wfuse ↔ quant_kb")
    print("=" * 60)

    # 方向1: wfuse → quant_kb（幽灵引用）
    print("\n>>> 方向1: wfuse → quant_kb（幽灵引用检查）")
    await check_wfuse_to_kb(dry_run=args.dry_run, fix=args.fix)

    # 方向2: quant_kb → wfuse（KB 孤儿）
    if args.all:
        print("\n>>> 方向2: quant_kb → wfuse（KB 孤儿检查）")
        await check_kb_to_wfuse()

    print("\n检查完成。")


if __name__ == "__main__":
    asyncio.run(main())
