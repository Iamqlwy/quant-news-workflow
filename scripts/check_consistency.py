"""wfuse <-> quant_kb 数据一致性检查脚本

检查项：
  tasks.analysis_ids / trade_ids / feedback_ids  ->  KB 中存在
  wfuse entities (A/T/F/N/G/R)                   ->  对应资源存在
  triggers.source_task_id / source_analysis_id    ->  对应资源存在
  KB 孤儿检测：analysis/trade/feedback 未被任何 wfuse task 引用
  时间字段：created_at <= updated_at
  JSONB 列表去重 & 垃圾数据检测

asyncpg 对 JSONB 列返回 JSON 字符串（如 '["uuid1","uuid2"]'），
本脚本自动 json.loads 解析后再处理。

用法：
  python scripts/check_consistency.py [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from kbquant.client import QuantClient

from src.config import settings

# ── UUID 校验 ──────────────────────────────────────────

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def is_valid_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


# ── JSONB 列归一化 ─────────────────────────────────────


def _jsonb_to_list(raw: list | str | None) -> list:
    """asyncpg 对 JSONB 列可能返回 JSON 字符串或已解析 list，统一归一到 list。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = _json.loads(raw)
        except (TypeError, _json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(raw, list):
        return [item for item in raw if item is not None]
    return []


def _parse_uuid_list(raw: list | str | None) -> list[str]:
    lst = _jsonb_to_list(raw)
    return [str(item).strip() for item in lst if is_valid_uuid(str(item).strip())]


def filter_valid_uuids(raw: list | str | None) -> tuple[list[str], list[str]]:
    """分离有效 UUID 和垃圾值。"""
    lst = _jsonb_to_list(raw)
    valid: list[str] = []
    garbage: list[str] = []
    for item in lst:
        s = str(item).strip()
        if is_valid_uuid(s):
            valid.append(s)
        else:
            garbage.append(repr(item)[:60])
    return valid, garbage


def _batch_chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── wfuse 数据读取 ────────────────────────────────────


async def _parse_db_url() -> str:
    url = settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL 未设置")
    return url.replace("+asyncpg", "")


async def fetch_wfuse_data(db_url: str):
    conn = await asyncpg.connect(db_url)
    try:
        tasks = await conn.fetch("SELECT * FROM tasks ORDER BY created_at")
        entities = await conn.fetch("SELECT * FROM entities ORDER BY created_at")
        triggers = await conn.fetch("SELECT * FROM triggers ORDER BY created_at")
        return tasks, entities, triggers
    finally:
        await conn.close()


# ── KB 数据读取 ───────────────────────────────────────


class KBClient:
    def __init__(self):
        base_url = settings.kb_api_base_url.rstrip("/")
        if base_url.endswith("/api/v1"):
            base_url = base_url[: -len("/api/v1")]
        self._quant = QuantClient(base_url=base_url, api_key=settings.kb_api_key)

    async def close(self):
        await self._quant.close()

    async def _batch_get(self, resource: str, ids: list[str]) -> dict[str, dict]:
        """通用批量查询：analysis/trading/feedback/nodes。"""
        result: dict[str, dict] = {}
        valid_ids = [i for i in ids if is_valid_uuid(i)]
        client = getattr(self._quant, resource)
        for chunk in _batch_chunks(valid_ids, 100):
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

    async def get_analyses(self, ids: list[str]) -> dict[str, dict]:
        return await self._batch_get("analysis", ids)

    async def get_trades(self, ids: list[str]) -> dict[str, dict]:
        return await self._batch_get("trading", ids)

    async def get_feedbacks(self, ids: list[str]) -> dict[str, dict]:
        return await self._batch_get("feedback", ids)

    async def get_nodes(self, ids: list[str]) -> dict[str, dict]:
        return await self._batch_get("nodes", ids)

    async def count_all(self, resource: str) -> int | str:
        count = 0
        try:
            client = getattr(self._quant, resource)
            async for _ in client.list_iter(page_size=100):
                count += 1
        except Exception as e:
            return f"ERROR({e})"
        return count


# ── 检查逻辑 ──────────────────────────────────────────


def check_task_integrity(tasks) -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    seen_raw_info: dict[str, int] = defaultdict(int)
    garbage_stats: dict[str, int] = defaultdict(int)

    for t in tasks:
        tid = t["id"]
        if not t["raw_info_id"]:
            issues.append(f"Task {tid}: raw_info_id 为空")
        else:
            seen_raw_info[str(t["raw_info_id"])] += 1
        if not t["state"]:
            issues.append(f"Task {tid}: state 为空")

        if t["created_at"] and t["updated_at"]:
            if t["created_at"] > t["updated_at"]:
                issues.append(
                    f"Task {tid}: created_at > updated_at "
                    f"({t['created_at']} > {t['updated_at']})"
                )

        for col in ("analysis_ids", "trade_ids", "feedback_ids"):
            lst = _jsonb_to_list(t[col])
            if not lst:
                continue
            str_items = [str(x) for x in lst]
            # 去重
            if len(str_items) != len(set(str_items)):
                seen = defaultdict(int)
                for x in str_items:
                    seen[x] += 1
                dups = {k: v for k, v in seen.items() if v > 1}
                issues.append(f"Task {tid}: {col} 含重复项 {dups}")
            # 垃圾数据
            _, garbage = filter_valid_uuids(t[col])
            for g in garbage:
                key = f"{col}: {g}"
                garbage_stats[key] += 1

    for rid, count in seen_raw_info.items():
        if count > 1 and rid:
            issues.append(f"raw_info_id={rid} 在 {count} 个 Task 中重复")
    return issues, dict(garbage_stats)


def check_trigger_integrity(triggers, task_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for t in triggers:
        tid = t["id"]
        if not t["name"]:
            issues.append(f"Trigger {tid}: name 为空")
        if t["source_task_id"] and str(t["source_task_id"]) not in task_ids:
            issues.append(f"Trigger {tid}: source_task_id={t['source_task_id']} 对应的 Task 不存在")
    return issues


def check_entity_integrity(entities, task_ids: set[str]) -> list[str]:
    issues: list[str] = []
    for e in entities:
        eid = e["id"]
        if e["entity_type"] not in ("A", "T", "F", "N", "G", "R"):
            issues.append(f"Entity {eid}: 未知 entity_type={e['entity_type']}")
        if not e["ref"]:
            issues.append(f"Entity {eid}: ref 为空")
        if e["task_id"] and str(e["task_id"]) not in task_ids:
            issues.append(f"Entity {eid}: task_id={e['task_id']} 对应的 Task 不存在")
    return issues


# ── 主流程 ────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(description="wfuse <-> quant_kb 数据一致性检查")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # ── 1. 读取 wfuse ──
    print("=" * 60)
    print("[1/4] 读取 wfuse 数据库...")
    db_url = await _parse_db_url()
    tasks, entities, triggers = await fetch_wfuse_data(db_url)

    task_count = len(tasks)
    entity_count = len(entities)
    trigger_count = len(triggers)
    print(f"  tasks:   {task_count}")
    print(f"  entities:{entity_count}")
    print(f"  triggers:{trigger_count}")

    all_issues: list[str] = []

    # ── 2. 内部一致性 ──
    print("\n[2/4] 内部一致性检查...")
    task_ids = {str(t["id"]) for t in tasks}

    ti, garbage = check_task_integrity(tasks)
    tri = check_trigger_integrity(triggers, task_ids)
    ei = check_entity_integrity(entities, task_ids)
    all_issues.extend(ti)
    all_issues.extend(tri)
    all_issues.extend(ei)

    if garbage:
        print(f"\n  JSONB 垃圾数据 ({len(garbage)} 种):")
        for k, v in sorted(garbage.items(), key=lambda x: -x[1])[:20]:
            print(f"    {k}  ({v} 次)")

    print(f"\n  Task 问题:    {len(ti)}")
    print(f"  Trigger 问题: {len(tri)}")
    print(f"  Entity 问题:  {len(ei)}")

    # ── 3. 交叉验证 ──
    print("\n[3/4] 交叉验证 (wfuse <-> KB)...")
    kb = KBClient()
    try:
        # 收集 IDs（从 JSONB 列正确解析）
        analysis_ids: set[str] = set()
        trade_ids: set[str] = set()
        feedback_ids: set[str] = set()

        for t in tasks:
            for aid in _parse_uuid_list(t["analysis_ids"]):
                analysis_ids.add(aid)
            for tid_ in _parse_uuid_list(t["trade_ids"]):
                trade_ids.add(tid_)
            for fid in _parse_uuid_list(t["feedback_ids"]):
                feedback_ids.add(fid)

        entity_a_ids: set[str] = set()
        entity_t_ids: set[str] = set()
        entity_f_ids: set[str] = set()
        entity_n_ids: set[str] = set()
        entity_g_ids: set[str] = set()
        entity_r_ids: set[str] = set()

        for e in entities:
            etype = e["entity_type"]
            euuid = str(e["entity_uuid"])
            if not is_valid_uuid(euuid):
                continue
            if etype == "A":
                entity_a_ids.add(euuid)
            elif etype == "T":
                entity_t_ids.add(euuid)
            elif etype == "F":
                entity_f_ids.add(euuid)
            elif etype == "N":
                entity_n_ids.add(euuid)
            elif etype == "G":
                entity_g_ids.add(euuid)
            elif etype == "R":
                entity_r_ids.add(euuid)

        trigger_analysis_ids: set[str] = set()
        trigger_trade_ids: set[str] = set()
        for t in triggers:
            said = str(t["source_analysis_id"]) if t["source_analysis_id"] else ""
            tid_ = str(t["trade_id"]) if t["trade_id"] else ""
            if said and is_valid_uuid(said):
                trigger_analysis_ids.add(said)
            if tid_ and is_valid_uuid(tid_):
                trigger_trade_ids.add(tid_)

        all_analysis = analysis_ids | entity_a_ids | trigger_analysis_ids
        all_trade = trade_ids | entity_t_ids | trigger_trade_ids
        all_feedback = feedback_ids | entity_f_ids

        print(f"  analysis 引用: {len(all_analysis)}  (tasks={len(analysis_ids)}, entities={len(entity_a_ids)}, triggers={len(trigger_analysis_ids)})")
        print(f"  trade 引用:    {len(all_trade)}  (tasks={len(trade_ids)}, entities={len(entity_t_ids)}, triggers={len(trigger_trade_ids)})")
        print(f"  feedback 引用:  {len(all_feedback)}  (tasks={len(feedback_ids)}, entities={len(entity_f_ids)})")
        print(f"  node 引用 (N):  {len(entity_n_ids)}")
        print(f"  trigger (G):    {len(entity_g_ids)}")
        print(f"  type R:         {len(entity_r_ids)}")

        # ── 批量验证 ──
        found_a: set[str] = set()
        found_t: set[str] = set()
        found_f: set[str] = set()

        if all_analysis:
            print(f"\n  --- 验证 KB analyses ({len(all_analysis)} 条) ---")
            kb_analyses = await kb.get_analyses(sorted(all_analysis))
            found_a = set(kb_analyses.keys())
            missing_a = all_analysis - found_a
            for aid in sorted(missing_a):
                sources = []
                if aid in analysis_ids:
                    sources.append("tasks.analysis_ids")
                if aid in entity_a_ids:
                    sources.append("entities(type=A)")
                if aid in trigger_analysis_ids:
                    sources.append("triggers.source_analysis_id")
                issue = f"KB analysis {aid} 不存在 (引用自: {', '.join(sources)})"
                all_issues.append(issue)
                if args.verbose or len(missing_a) <= 20:
                    print(f"    [MISS] {issue}")
            if len(missing_a) > 20:
                print(f"    [MISS] ... 共 {len(missing_a)} 条缺失（用 --verbose 查看全部）")
            print(f"    缺失: {len(missing_a)}, 存在: {len(found_a)}")

        if all_trade:
            print(f"\n  --- 验证 KB trades ({len(all_trade)} 条) ---")
            kb_trades = await kb.get_trades(sorted(all_trade))
            found_t = set(kb_trades.keys())
            missing_t = all_trade - found_t
            for tid in sorted(missing_t):
                sources = []
                if tid in trade_ids:
                    sources.append("tasks.trade_ids")
                if tid in entity_t_ids:
                    sources.append("entities(type=T)")
                if tid in trigger_trade_ids:
                    sources.append("triggers.trade_id")
                issue = f"KB trade {tid} 不存在 (引用自: {', '.join(sources)})"
                all_issues.append(issue)
                if args.verbose or len(missing_t) <= 20:
                    print(f"    [MISS] {issue}")
            if len(missing_t) > 20:
                print(f"    [MISS] ... 共 {len(missing_t)} 条缺失（用 --verbose 查看全部）")
            print(f"    缺失: {len(missing_t)}, 存在: {len(found_t)}")

        if all_feedback:
            print(f"\n  --- 验证 KB feedbacks ({len(all_feedback)} 条) ---")
            kb_feedbacks = await kb.get_feedbacks(sorted(all_feedback))
            found_f = set(kb_feedbacks.keys())
            missing_f = all_feedback - found_f
            for fid in sorted(missing_f):
                sources = []
                if fid in feedback_ids:
                    sources.append("tasks.feedback_ids")
                if fid in entity_f_ids:
                    sources.append("entities(type=F)")
                issue = f"KB feedback {fid} 不存在 (引用自: {', '.join(sources)})"
                all_issues.append(issue)
                if args.verbose or len(missing_f) <= 20:
                    print(f"    [MISS] {issue}")
            if len(missing_f) > 20:
                print(f"    [MISS] ... 共 {len(missing_f)} 条缺失（用 --verbose 查看全部）")
            print(f"    缺失: {len(missing_f)}, 存在: {len(found_f)}")

        # ── 验证 node (N) ──
        if entity_n_ids:
            print(f"\n  --- 验证 KB world_nodes ({len(entity_n_ids)} 条) ---")
            kb_nodes = await kb.get_nodes(sorted(entity_n_ids))
            found_n = set(kb_nodes.keys())
            missing_n = entity_n_ids - found_n
            for nid in sorted(missing_n):
                issue = f"KB world_node {nid} 不存在 (引用自: entities type=N)"
                all_issues.append(issue)
                if args.verbose or len(missing_n) <= 20:
                    print(f"    [MISS] {issue}")
            if len(missing_n) > 20:
                print(f"    [MISS] ... 共 {len(missing_n)} 条缺失（用 --verbose 查看全部）")
            print(f"    缺失: {len(missing_n)}, 存在: {len(found_n)}")

        # ── 验证 trigger (G) ──
        if entity_g_ids:
            trigger_uuid_set = {str(t["id"]) for t in triggers}
            missing_g = entity_g_ids - trigger_uuid_set
            for gid in sorted(missing_g):
                issue = f"wfuse entity type=G 引用 trigger {gid} 但该 trigger 不存在"
                all_issues.append(issue)
                print(f"    [MISS] {issue}")
            if missing_g:
                print(f"    缺失: {len(missing_g)}")

        if entity_r_ids:
            print(f"\n  [INFO] entity type=R 共 {len(entity_r_ids)} 条（未经 KB 验证）")

        # ── 孤儿 KB 检测 ──
        print("\n  --- 孤儿 KB 检测（统计总数） ---")
        kb_a_count = await kb.count_all("analysis")
        print(f"    KB analyses:  {kb_a_count}, 被 wfuse 引用: {len(found_a)}")
        kb_t_count = await kb.count_all("trading")
        print(f"    KB trades:    {kb_t_count}, 被 wfuse 引用: {len(found_t)}")
        kb_f_count = await kb.count_all("feedback")
        print(f"    KB feedbacks: {kb_f_count}, 被 wfuse 引用: {len(found_f)}")

    finally:
        await kb.close()

    # ── 4. 汇总 ──
    print("\n" + "=" * 60)
    print("[4/4] 汇总")
    if all_issues:
        print(f"  发现问题 {len(all_issues)} 条：")
        for issue in all_issues[:50]:
            print(f"    - {issue}")
        if len(all_issues) > 50:
            print(f"    ... 还有 {len(all_issues) - 50} 条")
    else:
        print("  全部通过，未发现问题。")
    print(f"\n  统计: tasks={task_count}, entities={entity_count}, triggers={trigger_count}")
    return 0 if not all_issues else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
