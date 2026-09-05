"""扫描并清理 wfuse entities 表中的孤儿引用（指向已不存在实体的记录）。

entity_type 映射：
  A → quant_kb.analyses
  T → quant_kb.trading_operations
  F → quant_kb.feedbacks
  N → quant_kb.world_nodes
  R → quant_kb.raw_information
  G → wfuse.triggers (本地)

用法：
  python scripts/_cleanup_orphan_entities.py --scan    # 仅扫描统计
  python scripts/_cleanup_orphan_entities.py --execute  # 执行删除
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db import async_session as wfuse_session
from src.models.tables import Entity

QUANT_KB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15432/quant_kb"
_quant_kb_engine = create_async_engine(QUANT_KB_URL, echo=False, pool_pre_ping=True)
_quant_kb_session = async_sessionmaker(_quant_kb_engine, class_=AsyncSession, expire_on_commit=False)

# entity_type → (quant_kb table name, database: 'kb' | 'wfuse')
ENTITY_MAP = {
    "A": ("analyses", "kb"),
    "T": ("trading_operations", "kb"),
    "F": ("feedbacks", "kb"),
    "N": ("world_nodes", "kb"),
    "R": ("raw_information", "kb"),
    "G": ("triggers", "wfuse"),
}


async def find_orphans(entity_type: str) -> list[str]:
    """返回指定 entity_type 的孤儿 entity ID (主键UUID字符串) 列表。"""
    table_name, db = ENTITY_MAP[entity_type]

    # 收集该类型所有的 (主键ID, entity_uuid)
    async with wfuse_session() as sess:
        result = await sess.execute(
            select(Entity.id, Entity.entity_uuid).where(Entity.entity_type == entity_type)
        )
        rows = [(str(r[0]), str(r[1])) for r in result.fetchall()]

    if not rows:
        return []

    uuids = list(set(entity_uuid for _, entity_uuid in rows))

    # 分批检查（避免 IN 子句过大），每次 2000 个
    BATCH = 2000
    missing_uuids: set[str] = set()

    if db == "kb":
        for i in range(0, len(uuids), BATCH):
            batch = uuids[i : i + BATCH]
            placeholders = ",".join(f"'{u}'::uuid" for u in batch)
            async with _quant_kb_session() as sess:
                result = await sess.execute(
                    text(f"SELECT id FROM {table_name} WHERE id IN ({placeholders})")
                )
                found = {str(r[0]) for r in result.fetchall()}
                missing_uuids.update(set(batch) - found)
    else:  # wfuse
        for i in range(0, len(uuids), BATCH):
            batch = uuids[i : i + BATCH]
            placeholders = ",".join(f"'{u}'::uuid" for u in batch)
            async with wfuse_session() as sess:
                result = await sess.execute(
                    text(f"SELECT id FROM {table_name} WHERE id IN ({placeholders})")
                )
                found = {str(r[0]) for r in result.fetchall()}
                missing_uuids.update(set(batch) - found)

    # 找出孤儿 entity 行的主键ID
    orphan_ids: list[str] = []
    for entity_pk, entity_uuid in rows:
        if entity_uuid in missing_uuids:
            orphan_ids.append(entity_pk)

    return orphan_ids


async def delete_orphans(entity_type: str, orphan_ids: list[str]) -> int:
    """删除指定 entity UUID 的孤儿行。"""
    if not orphan_ids:
        return 0
    BATCH = 500
    deleted = 0
    for i in range(0, len(orphan_ids), BATCH):
        batch = orphan_ids[i : i + BATCH]
        ids_str = ",".join(f"'{eid}'::uuid" for eid in batch)
        async with wfuse_session() as sess:
            result = await sess.execute(
                text(f"DELETE FROM entities WHERE id IN ({ids_str})")
            )
            await sess.commit()
            deleted += result.rowcount
    return deleted


async def main(scan_only: bool = True):
    total_orphans = 0
    type_stats: dict[str, int] = {}

    # 先统计各类型总数
    async with wfuse_session() as sess:
        result = await sess.execute(
            select(Entity.entity_type, text("count(*)")).group_by(Entity.entity_type)
        )
        total_by_type = {r[0]: r[1] for r in result.fetchall()}

    logger.info("wfuse entities 表概况: {}", dict(total_by_type))

    for entity_type in sorted(ENTITY_MAP.keys()):
        total = total_by_type.get(entity_type, 0)
        logger.info("检查 entity_type={} ({}条)...", entity_type, total)
        orphans = await find_orphans(entity_type)
        type_stats[entity_type] = len(orphans)
        total_orphans += len(orphans)
        if orphans:
            pct = len(orphans) / total * 100 if total else 0
            logger.info("  entity_type={}: {} / {} ({:.1f}%) 孤儿", entity_type, len(orphans), total, pct)
        else:
            logger.info("  entity_type={}: 无孤儿", entity_type)

    logger.info("=" * 50)
    logger.info("总计孤儿 entity 行: {}", total_orphans)
    for et, n in sorted(type_stats.items()):
        logger.info("  {}: {}", et, n)

    if scan_only:
        logger.info("扫描完成（--scan 模式，不删除）")
        return

    if total_orphans == 0:
        logger.info("无孤儿，无需删除")
        return

    # 执行删除
    for entity_type in sorted(ENTITY_MAP.keys()):
        orphans = await find_orphans(entity_type)
        if orphans:
            n = await delete_orphans(entity_type, orphans)
            logger.info("删除 entity_type={}: {} 条", entity_type, n)

    logger.info("=== 清理完成 ===")
    await _quant_kb_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理 entities 表孤儿引用")
    parser.add_argument("--scan", action="store_true", default=True, help="仅扫描（默认）")
    parser.add_argument("--execute", action="store_true", help="执行删除")
    args = parser.parse_args()
    asyncio.run(main(scan_only=not args.execute))
