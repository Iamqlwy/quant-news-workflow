"""直连 quant_kb 数据库删除重复的 feedback 记录。

策略：wfuse tasks 表中每个 task 只保留了一个 feedback_id，
此脚本删除 quant_kb.feedbacks 中所有不在 task.feedback_ids 里的 feedback。

用法：cd <project_root> && python scripts/_cleanup_dupe_feedbacks_kb.py [--dry-run]
"""

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db import async_session
from src.models.tables import Task

# 直连 quant_kb 数据库（与 wfuse 同主机不同库名）
QUANT_KB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15432/quant_kb"
_quant_kb_engine = create_async_engine(QUANT_KB_URL, echo=False, pool_pre_ping=True)
_quant_kb_session = async_sessionmaker(_quant_kb_engine, class_=AsyncSession, expire_on_commit=False)


async def main(dry_run: bool = True):
    # 1. 从 wfuse 收集所有应该保留的 feedback_id
    async with async_session() as sess:
        result = await sess.execute(
            select(Task.feedback_ids).where(func.jsonb_array_length(Task.feedback_ids) > 0)
        )
        kept_ids: set[str] = set()
        for (fids,) in result.fetchall():
            if fids:
                for fid in fids:
                    kept_ids.add(str(fid))

    logger.info("wfuse 中应保留的 feedback 总数: {}", len(kept_ids))

    # 2. 从 quant_kb 查出所有 feedback
    async with _quant_kb_session() as sess:
        result = await sess.execute(text("SELECT id FROM feedbacks"))
        all_kb_ids = [str(r[0]) for r in result.fetchall()]

    to_delete = [fid for fid in all_kb_ids if fid not in kept_ids]
    logger.info("quant_kb feedback 总数: {}, 待删除: {}", len(all_kb_ids), len(to_delete))

    if dry_run:
        logger.warning("=== DRY RUN 模式 ===")
        if to_delete:
            logger.info("待删除前10条: {}", to_delete[:10])
        return

    # 3. 分批删除
    deleted = 0
    failed = 0
    BATCH = 500
    for i in range(0, len(to_delete), BATCH):
        batch = to_delete[i : i + BATCH]
        placeholders = ",".join(f"'{fid}'::uuid" for fid in batch)
        try:
            async with _quant_kb_session() as sess:
                sql = f"DELETE FROM feedbacks WHERE id IN ({placeholders})"
                result = await sess.execute(text(sql))
                await sess.commit()
                deleted += result.rowcount
                logger.info("  批次 {}/{}: 删除 {} 条", i // BATCH + 1, (len(to_delete) + BATCH - 1) // BATCH, result.rowcount)
        except Exception as e:
            failed += len(batch)
            logger.error("  批次 {} 失败: {}", i // BATCH + 1, e)

    logger.info("=== 清理完成 === 删除 {} 条, 失败 {} 条", deleted, failed)
    await _quant_kb_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="直连 quant_kb 清理重复 feedback")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=not args.execute))
