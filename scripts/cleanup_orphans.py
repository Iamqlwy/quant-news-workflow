"""数据修复脚本：删除失效引用、修复 created_at > updated_at

三件事：
1. 删除 source_task_id 指向不存在 Task 的 trigger
2. created_at > updated_at 的 Task：set created_at = updated_at
3. 删除 entities 中引用 KB 中不存在的 analysis/feedback/world_node 的记录

用法：
  python scripts/cleanup_orphans.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from kbquant.client import QuantClient

from src.config import settings

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def is_valid_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


async def _parse_db_url() -> str:
    return settings.database_url.replace("+asyncpg", "")


class KBClient:
    def __init__(self):
        base_url = settings.kb_api_base_url.rstrip("/")
        if base_url.endswith("/api/v1"):
            base_url = base_url[: -len("/api/v1")]
        self._quant = QuantClient(base_url=base_url, api_key=settings.kb_api_key)

    async def close(self):
        await self._quant.close()

    async def _batch_get(self, resource: str, ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        valid_ids = [i for i in ids if is_valid_uuid(i)]
        client = getattr(self._quant, resource)
        for i in range(0, len(valid_ids), 100):
            chunk = valid_ids[i:i + 100]
            try:
                items = await client.get_many(chunk)
                for item in items:
                    result[str(item.id)] = item
            except Exception:
                for rid in chunk:
                    try:
                        item = await client.get(rid)
                        result[str(item.id)] = item
                    except Exception:
                        pass
        return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不实际删除/修改")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    db_url = await _parse_db_url()
    conn = await asyncpg.connect(db_url)

    if args.dry_run:
        print("=== DRY RUN MODE === (不会实际修改数据)\n")

    # ── 1. 删除失效 trigger ──
    print("=" * 60)
    print("[1/3] 清理 source_task_id 指向不存在 Task 的 trigger...")
    task_ids = {r["id"] for r in await conn.fetch("SELECT id FROM tasks")}
    triggers = await conn.fetch("SELECT * FROM triggers")
    bad_triggers = [
        t for t in triggers
        if t["source_task_id"] and t["source_task_id"] not in task_ids
    ]
    print(f"  失效触发器: {len(bad_triggers)} 条")
    for t in bad_triggers:
        print(f"    Trigger {t['id']}: name={t['name']}, source_task_id={t['source_task_id']}")

    if not args.dry_run and bad_triggers:
        trigger_ids = [t["id"] for t in bad_triggers]
        await conn.execute("DELETE FROM triggers WHERE id = ANY($1)", trigger_ids)
        print(f"  已删除 {len(bad_triggers)} 条触发器")

    # ── 2. 修复 created_at > updated_at ──
    print("\n[2/3] 修复 created_at > updated_at 的 Task...")
    bad_tasks = await conn.fetch(
        "SELECT id, created_at, updated_at FROM tasks WHERE created_at > updated_at"
    )
    print(f"  时间倒挂: {len(bad_tasks)} 条")
    if args.verbose:
        for t in bad_tasks[:10]:
            print(f"    Task {t['id']}: {t['created_at']} > {t['updated_at']}")
        if len(bad_tasks) > 10:
            print(f"    ... 还有 {len(bad_tasks) - 10} 条")

    if not args.dry_run and bad_tasks:
        task_ids_fix = [t["id"] for t in bad_tasks]
        await conn.execute(
            "UPDATE tasks SET created_at = updated_at WHERE id = ANY($1)",
            task_ids_fix,
        )
        print(f"  已修复 {len(bad_tasks)} 条 Task (created_at := updated_at)")

    # ── 3. 清理失效的 entity 引用 ──
    print("\n[3/3] 清理 KB 中不存在的 entity 引用...")
    kb = KBClient()
    try:
        entities = await conn.fetch("SELECT * FROM entities")
        entity_by_type: dict[str, list] = {"A": [], "T": [], "F": [], "N": []}
        for e in entities:
            etype = e["entity_type"]
            if etype in entity_by_type and is_valid_uuid(str(e["entity_uuid"])):
                entity_by_type[etype].append(e)

        to_delete: list[UUID] = []

        # type=A -> KB analyses
        if entity_by_type["A"]:
            a_ids = list({str(e["entity_uuid"]) for e in entity_by_type["A"]})
            print(f"\n  检查 A 类 entities ({len(entity_by_type['A'])} 条, {len(a_ids)} 个唯一 ID)...")
            kb_analyses = await kb._batch_get("analysis", a_ids)
            found_a = set(kb_analyses.keys())
            for e in entity_by_type["A"]:
                if str(e["entity_uuid"]) not in found_a:
                    to_delete.append(e["id"])
                    if args.verbose or len(to_delete) <= 20:
                        print(f"    [DEL] Entity {e['id']}: analysis {e['entity_uuid']} 不存在 (ref={e['ref']})")

        # type=T -> KB trades
        if entity_by_type["T"]:
            t_ids = list({str(e["entity_uuid"]) for e in entity_by_type["T"]})
            print(f"\n  检查 T 类 entities ({len(entity_by_type['T'])} 条, {len(t_ids)} 个唯一 ID)...")
            kb_trades = await kb._batch_get("trading", t_ids)
            found_t = set(kb_trades.keys())
            for e in entity_by_type["T"]:
                if str(e["entity_uuid"]) not in found_t:
                    to_delete.append(e["id"])
                    if args.verbose or len(to_delete) <= 20:
                        print(f"    [DEL] Entity {e['id']}: trade {e['entity_uuid']} 不存在 (ref={e['ref']})")

        # type=F -> KB feedbacks
        if entity_by_type["F"]:
            f_ids = list({str(e["entity_uuid"]) for e in entity_by_type["F"]})
            print(f"\n  检查 F 类 entities ({len(entity_by_type['F'])} 条, {len(f_ids)} 个唯一 ID)...")
            kb_feedbacks = await kb._batch_get("feedback", f_ids)
            found_f = set(kb_feedbacks.keys())
            for e in entity_by_type["F"]:
                if str(e["entity_uuid"]) not in found_f:
                    to_delete.append(e["id"])
                    if args.verbose or len(to_delete) <= 20:
                        print(f"    [DEL] Entity {e['id']}: feedback {e['entity_uuid']} 不存在 (ref={e['ref']})")

        # type=N -> KB world_nodes
        if entity_by_type["N"]:
            n_ids = list({str(e["entity_uuid"]) for e in entity_by_type["N"]})
            print(f"\n  检查 N 类 entities ({len(entity_by_type['N'])} 条, {len(n_ids)} 个唯一 ID)...")
            kb_nodes = await kb._batch_get("nodes", n_ids)
            found_n = set(kb_nodes.keys())
            for e in entity_by_type["N"]:
                if str(e["entity_uuid"]) not in found_n:
                    to_delete.append(e["id"])
                    if args.verbose or len(to_delete) <= 20:
                        print(f"    [DEL] Entity {e['id']}: world_node {e['entity_uuid']} 不存在 (ref={e['ref']})")

        print(f"\n  待删除 entities: {len(to_delete)} 条")
        if to_delete and not args.dry_run:
            await conn.execute("DELETE FROM entities WHERE id = ANY($1)", to_delete)
            print(f"  已删除 {len(to_delete)} 条 entity")
    finally:
        await kb.close()

    await conn.close()

    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY RUN 完成，未做任何修改。去掉 --dry-run 执行实际修复。")
    else:
        print("清理完成。")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
