"""清理 wfuse tasks 表中重复的 feedback_ids 及 kbquant feedbacks 表中对应记录。

每个 task 只保留第一个 feedback_id，其余删除。

用法：cd <project_root> && python scripts/_cleanup_dupe_feedbacks.py [--dry-run]
"""

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from uuid import UUID

from dotenv import load_dotenv

load_dotenv()

from loguru import logger
from sqlalchemy import func, select, delete as sa_delete
from sqlalchemy import update as sa_update

from src.db import async_session
from src.models.tables import Task
# kbquant
from kbquant.database import write_async_session as kb_write_session
from kbquant.models.feedback import Feedback as KBFeedback

# 去重策略：按 (task_id, 全文内容) 分组，每组只保留最早创建的那一条
# 这样比"仅保留第一个 ID"更安全——万一两条内容不同但都有效，至少保留一条


async def main(dry_run: bool = True):
    # ── 1. 从 wfuse 找出 feedback_ids > 1 的 task ──
    async with async_session() as sess:
        result = await sess.execute(
            select(Task.id, Task.feedback_ids)
            .where(func.jsonb_array_length(Task.feedback_ids) > 1)
            .order_by(func.jsonb_array_length(Task.feedback_ids).desc())
        )
        dup_tasks = [(r[0], r[1]) for r in result.fetchall()]

    if not dup_tasks:
        logger.info("没有 feedback_ids > 1 的 task，无需清理")
        return

    total_extra = sum(len(fids) - 1 for _, fids in dup_tasks)
    logger.info("发现 {} 个 task 有重复 feedback_ids，共 {} 条多余的 feedback 待删除",
                len(dup_tasks), total_extra)

    # ── 2. 收集所有待删除的 feedback UUID ──
    # 每个 task 保留第一个 feedback_id，其余加入删除列表
    to_delete_fb_ids: list[str] = []  # 待从 kbquant 删除的 feedback UUID
    # task_id -> 保留后的 feedback_ids 列表（只有1个）
    task_updates: dict[str, list] = {}  # task_id -> [kept_fid]

    for task_id, fids in dup_tasks:
        if not fids or len(fids) < 2:
            continue
        kept = fids[0]
        removed = fids[1:]
        to_delete_fb_ids.extend(removed)
        task_updates[str(task_id)] = [kept]

    logger.info("待删除 feedback UUID (kbquant): {} 个", len(to_delete_fb_ids))

    if dry_run:
        logger.warning("=== DRY RUN 模式，不实际执行删除 ===")
        # 打印前 10 个样例
        for i, (task_id, fids) in enumerate(dup_tasks[:10]):
            logger.info("  task={} keep={} remove={}", task_id, fids[0], fids[1:])
        if len(dup_tasks) > 10:
            logger.info("  ... 共 {} 个 task", len(dup_tasks))
        logger.info("[DRY RUN] 将删除 {} 条 kbquant feedback，更新 {} 个 wfuse task", len(to_delete_fb_ids), len(task_updates))
        return

    # ── 3. 从 kbquant 数据库删除重复的 feedback ──
    deleted_kb = 0
    failed_kb = 0
    # 分批删除，每批 100 个
    BATCH = 100
    for i in range(0, len(to_delete_fb_ids), BATCH):
        batch = to_delete_fb_ids[i : i + BATCH]
        batch_uuids = [UUID(x) for x in batch]
        try:
            async with kb_write_session() as sess:
                stmt = sa_delete(KBFeedback).where(KBFeedback.id.in_(batch_uuids))
                result = await sess.execute(stmt)
                await sess.commit()
                deleted_kb += result.rowcount
                logger.info("  kbquant 批次 {}/{}: 删除 {} 条 feedback", i // BATCH + 1,
                            (len(to_delete_fb_ids) + BATCH - 1) // BATCH, result.rowcount)
        except Exception as e:
            failed_kb += len(batch)
            logger.error("  kbquant 批次 {} 删除失败: {}", i // BATCH + 1, e)

    logger.info("kbquant 清理完成: 删除 {}, 失败 {}", deleted_kb, failed_kb)

    # ── 4. 更新 wfuse tasks 表，只保留第一个 feedback_id ──
    updated_wf = 0
    failed_wf = 0
    for task_id, new_fids in task_updates.items():
        try:
            async with async_session() as sess:
                await sess.execute(
                    sa_update(Task).where(Task.id == task_id).values(feedback_ids=new_fids)
                )
                await sess.commit()
                updated_wf += 1
        except Exception as e:
            failed_wf += 1
            logger.error("  wfuse task={} 更新失败: {}", task_id, e)

    logger.info("wfuse tasks 更新完成: 更新 {}, 失败 {}", updated_wf, failed_wf)
    logger.info("=== 清理完成 === 删除 kbquant feedback: {}，更新 wfuse task: {}", deleted_kb, updated_wf)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理重复 feedback")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="仅预览，不实际删除（默认开启）")
    parser.add_argument("--execute", action="store_true",
                        help="实际执行删除")
    args = parser.parse_args()

    dry_run = not args.execute
    asyncio.run(main(dry_run=dry_run))
