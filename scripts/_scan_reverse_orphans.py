"""扫描 quant_kb 中未被 wfuse.tasks 引用的孤儿记录（反向孤儿）。

检查范围：
- analyses: task.analysis_ids (JSONB array) 中出现的 ID
- trading_operations: task.trade_ids (JSONB array) 中出现的 ID
- feedbacks: task.feedback_ids (JSONB array) 中出现的 ID
- raw_information: task.raw_info_id 中出现的 ID

找出 quant_kb 中有但 wfuse.tasks 中没有任何引用的记录。

用法：
  python scripts/_scan_reverse_orphans.py --scan     # 仅扫描
  python scripts/_scan_reverse_orphans.py --execute   # 执行删除
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db import async_session as wfuse_session

QUANT_KB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15432/quant_kb"
_quant_kb_engine = create_async_engine(QUANT_KB_URL, echo=False, pool_pre_ping=True)
_quant_kb_session = async_sessionmaker(_quant_kb_engine, class_=AsyncSession, expire_on_commit=False)


async def _collect_task_refs() -> dict[str, set[str]]:
    """收集 wfuse.tasks 中引用的所有 KB ID，按类型分组。

    返回: {"analysis": set_of_ids, "trade": set_of_ids, "feedback": set_of_ids, "raw_info": set_of_ids}
    """
    refs: dict[str, set[str]] = defaultdict(set)

    async with wfuse_session() as sess:
        # raw_info_id
        result = await sess.execute(text("SELECT raw_info_id FROM tasks WHERE raw_info_id IS NOT NULL"))
        for r in result.fetchall():
            refs["raw_info"].add(str(r[0]))

        # analysis_ids: JSONB array
        result = await sess.execute(
            text("SELECT analysis_ids FROM tasks WHERE analysis_ids IS NOT NULL AND jsonb_array_length(analysis_ids) > 0")
        )
        for r in result.fetchall():
            for aid in (r[0] or []):
                refs["analysis"].add(str(aid))

        # trade_ids
        result = await sess.execute(
            text("SELECT trade_ids FROM tasks WHERE trade_ids IS NOT NULL AND jsonb_array_length(trade_ids) > 0")
        )
        for r in result.fetchall():
            for tid in (r[0] or []):
                refs["trade"].add(str(tid))

        # feedback_ids
        result = await sess.execute(
            text("SELECT feedback_ids FROM tasks WHERE feedback_ids IS NOT NULL AND jsonb_array_length(feedback_ids) > 0")
        )
        for r in result.fetchall():
            for fid in (r[0] or []):
                refs["feedback"].add(str(fid))

    return dict(refs)


async def _collect_kb_ids() -> dict[str, set[str]]:
    """收集 quant_kb 中各表的所有 ID。"""
    kb_ids: dict[str, set[str]] = {
        "analysis": set(),
        "trade": set(),
        "feedback": set(),
        "raw_info": set(),
    }

    table_map = {
        "analysis": "analyses",
        "trade": "trading_operations",
        "feedback": "feedbacks",
        "raw_info": "raw_information",
    }

    async with _quant_kb_session() as sess:
        for key, table in table_map.items():
            result = await sess.execute(text(f"SELECT id FROM {table}"))
            kb_ids[key] = {str(r[0]) for r in result.fetchall()}

    return kb_ids


async def delete_from_kb(table: str, ids: list[str]) -> int:
    """从 quant_kb 删除指定 ID 列表的记录。"""
    if not ids:
        return 0
    BATCH = 500
    deleted = 0
    for i in range(0, len(ids), BATCH):
        batch = ids[i : i + BATCH]
        placeholders = ",".join(f"'{eid}'::uuid" for eid in batch)
        async with _quant_kb_session() as sess:
            result = await sess.execute(
                text(f"DELETE FROM {table} WHERE id IN ({placeholders})")
            )
            await sess.commit()
            deleted += result.rowcount
    return deleted


async def main(scan_only: bool = True):
    logger.info("收集 wfuse.tasks 中的引用...")
    task_refs = await _collect_task_refs()
    for k, v in task_refs.items():
        logger.info("  tasks 引用 {}: {} 个唯一 ID", k, len(v))

    logger.info("收集 quant_kb 中各表 ID...")
    kb_ids = await _collect_kb_ids()
    for k, v in kb_ids.items():
        logger.info("  quant_kb.{}: {} 条", k, len(v))

    table_map = {
        "analysis": "analyses",
        "trade": "trading_operations",
        "feedback": "feedbacks",
        "raw_info": "raw_information",
    }

    logger.info("=" * 60)
    total_orphans = 0
    orphan_counts: dict[str, int] = {}

    for key, table in table_map.items():
        refs = task_refs.get(key, set())
        kbs = kb_ids.get(key, set())

        # 孤儿 = KB 中有，但 tasks 引用中无
        orphans = kbs - refs

        # 同时检查哪些 ID 在 task refs 中但不在 KB 中（正向孤儿，之前已处理过）
        missing_from_kb = refs - kbs

        pct = len(orphans) / len(kbs) * 100 if kbs else 0
        logger.info("  {}: {} 条在 KB, {} 条被引用, {} 条孤儿 ({:.1f}%), {} 条正向缺失",
                      key, len(kbs), len(refs), len(orphans), pct, len(missing_from_kb))
        orphan_counts[key] = len(orphans)
        total_orphans += len(orphans)

    logger.info("=" * 60)
    logger.info("总计反向孤儿 (KB 有但 tasks 无引用): {}", total_orphans)
    for k, n in sorted(orphan_counts.items()):
        logger.info("  {}: {}", k, n)

    if scan_only:
        logger.info("扫描完成（--scan 模式，不删除）")
        return

    if total_orphans == 0:
        logger.info("无孤儿，无需删除")
        return

    # 执行删除
    for key, table in table_map.items():
        refs = task_refs.get(key, set())
        kbs = kb_ids.get(key, set())
        orphans = list(kbs - refs)
        if orphans:
            n = await delete_from_kb(table, orphans)
            logger.info("删除 quant_kb.{}: {} 条", table, n)

    logger.info("=== 清理完成 ===")
    await _quant_kb_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="扫描 quant_kb 中被 wfuse.tasks 引用不到的反向孤儿")
    parser.add_argument("--scan", action="store_true", default=True, help="仅扫描（默认）")
    parser.add_argument("--execute", action="store_true", help="执行删除")
    args = parser.parse_args()
    asyncio.run(main(scan_only=not args.execute))
