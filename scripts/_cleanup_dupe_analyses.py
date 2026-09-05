"""去重 task 中高度相似的 analysis_ids，同步清理 quant_kb.analyses 和 wfuse.entities。

用法：cd <project_root> && python scripts/_cleanup_dupe_analyses.py --scan      # 仅扫描
                                     python scripts/_cleanup_dupe_analyses.py --execute   # 执行清理
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db import async_session as wfuse_session

QUANT_KB_URL = "postgresql+asyncpg://postgres:postgres@localhost:15432/quant_kb"
_quant_engine = create_async_engine(QUANT_KB_URL, echo=False, pool_pre_ping=True)
_quant_session = async_sessionmaker(_quant_engine, class_=AsyncSession, expire_on_commit=False)

SIMILARITY_THRESHOLD = 0.80


def _analysis_text(a: dict) -> str:
    parts = [a.get("title") or "", a.get("content") or "", a.get("analysis_type") or ""]
    return " ".join(p for p in parts if p)


def _find_duplicate_groups(analyses: list[dict]) -> list[list[str]]:
    """找出需要去重的 analysis ID 组（每组保留第一个，其余标记为待删除）。

    返回: [[keep_id, del_id1, del_id2, ...], ...]
    """
    texts = [_analysis_text(a) for a in analyses]
    n = len(texts)
    # Union-Find 聚类
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if SequenceMatcher(None, texts[i], texts[j]).ratio() >= SIMILARITY_THRESHOLD:
                union(i, j)

    # 按组收集
    groups_map: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups_map[find(i)].append(i)

    result = []
    for indices in groups_map.values():
        if len(indices) >= 2:
            # 保留第一个（index 最小），其余删除
            sorted_indices = sorted(indices)
            result.append([analyses[i]["id"] for i in sorted_indices])

    return result


async def main(scan_only: bool = True):
    # 1. 从 wfuse 查出 analysis_ids >= 3 的 task
    async with wfuse_session() as sess:
        result = await sess.execute(text("""
            SELECT id, raw_info_id, state, analysis_ids
            FROM tasks
            WHERE analysis_ids IS NOT NULL AND jsonb_array_length(analysis_ids) >= 3
            ORDER BY jsonb_array_length(analysis_ids) DESC
        """))
        task_rows = [(r[0], r[1], r[2], list(r[3]) if r[3] else []) for r in result.fetchall()]

    if not task_rows:
        print("没有 analysis_ids >= 3 的 task")
        return

    # 2. 收集所有 analysis ID，从 quant_kb 拉取内容
    all_aids = set()
    for _, _, _, aids in task_rows:
        all_aids.update(aids)

    aid_to_row: dict[str, dict] = {}
    async with _quant_session() as sess:
        aid_list = list(all_aids)
        for i in range(0, len(aid_list), 200):
            batch = aid_list[i : i + 200]
            placeholders = ",".join(f"'{a}'::uuid" for a in batch)
            result = await sess.execute(text(f"""
                SELECT id, title, content, analysis_type, created_at
                FROM analyses WHERE id IN ({placeholders})
            """))
            for r in result.fetchall():
                aid_to_row[str(r[0])] = {
                    "id": str(r[0]),
                    "title": r[1] or "",
                    "content": r[2] or "",
                    "analysis_type": r[3] or "",
                    "created_at": str(r[4]) if r[4] else "",
                }

    # 3. 逐 task 聚类去重
    to_delete_analysis_ids: set[str] = set()
    task_updates: dict[str, list[str]] = {}  # task_id -> 删除后的 analysis_ids

    for task_id, raw_info_id, state, aids in task_rows:
        analyses = [aid_to_row.get(aid) for aid in aids if aid in aid_to_row]
        if len(analyses) < 2:
            continue

        groups = _find_duplicate_groups(analyses)
        if not groups:
            continue

        del_ids = set()
        for group in groups:
            # group[0] keep, group[1:] delete
            del_ids.update(group[1:])

        if del_ids:
            to_delete_analysis_ids.update(del_ids)
            new_aids = [aid for aid in aids if aid not in del_ids]
            task_updates[str(task_id)] = new_aids

            print(f"task={task_id} raw_info={raw_info_id} state={state}")
            print(f"  原始 analysis_ids ({len(aids)}): {aids}")
            for group in groups:
                print(f"  重复组: keep={group[0]}  del={group[1:]}")
            print(f"  去重后 ({len(new_aids)}): {new_aids}")
            print()

    print(f"{'='*60}")
    print(f"汇总:")
    print(f"  涉及 task: {len(task_updates)} 个")
    print(f"  待删除 quant_kb.analyses: {len(to_delete_analysis_ids)} 条")
    if to_delete_analysis_ids:
        print(f"  待删除 ID: {list(to_delete_analysis_ids)[:10]}...")

    if scan_only:
        print("\n扫描完成（--scan 模式，不修改数据）")
        return

    # === 执行清理 ===

    # 4. 删除 quant_kb.analyses 中的重复记录
    if to_delete_analysis_ids:
        del_list = list(to_delete_analysis_ids)
        print(f"\n删除 quant_kb.analyses 中 {len(del_list)} 条重复记录...")
        async with _quant_session() as sess:
            for i in range(0, len(del_list), 200):
                batch = del_list[i : i + 200]
                placeholders = ",".join(f"'{a}'::uuid" for a in batch)
                result = await sess.execute(
                    text(f"DELETE FROM analyses WHERE id IN ({placeholders})")
                )
                await sess.commit()
                print(f"  已删 {result.rowcount} 条")

    # 5. 更新 wfuse.tasks.analysis_ids
    print(f"\n更新 wfuse.tasks 中 {len(task_updates)} 个 task 的 analysis_ids...")
    async with wfuse_session() as sess:
        for task_id, new_aids in task_updates.items():
            # 构建 JSONB 数组字面量
            if new_aids:
                elements = ", ".join(f"'{a}'" for a in new_aids)
                jsonb_expr = f"jsonb_build_array({elements})"
            else:
                jsonb_expr = "'[]'::jsonb"
            await sess.execute(
                text(f"UPDATE tasks SET analysis_ids = {jsonb_expr} WHERE id = '{task_id}'::uuid")
            )
        await sess.commit()
        print("  已更新")

    # 6. 清理 wfuse.entities 中引用已删除 analysis 的孤儿记录
    if to_delete_analysis_ids:
        print(f"\n清理 wfuse.entities 中引用已删除 analysis 的记录...")
        async with wfuse_session() as sess:
            for i in range(0, len(del_list), 200):
                batch = del_list[i : i + 200]
                placeholders = ",".join(f"'{a}'::uuid" for a in batch)
                result = await sess.execute(
                    text(f"DELETE FROM entities WHERE entity_type = 'A' AND entity_uuid IN ({placeholders})")
                )
                await sess.commit()
                print(f"  已删 {result.rowcount} 条 entity 记录")

    print("\n=== 清理完成 ===")
    await _quant_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="去重 analysis_ids 并清理 KB 和 entity")
    parser.add_argument("--scan", action="store_true", default=True, help="仅扫描（默认）")
    parser.add_argument("--execute", action="store_true", help="执行清理")
    args = parser.parse_args()
    asyncio.run(main(scan_only=not args.execute))
