"""对 wfuse 中 feedback_ids > 1 的 task，拉取对应 kbquant feedback 并比较相似性。

用法：cd <project_root> && python scripts/_analyze_duplicate_feedbacks.py [--limit N] [--min-count 2]
"""

import argparse
import asyncio
import sys
from difflib import SequenceMatcher
from pathlib import Path
from textwrap import shorten

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from sqlalchemy import func, select

load_dotenv()

from src.config import settings
from src.db import async_session
from src.models.tables import Task
from src.utils.http_resilience import create_resilient_httpx_client
from kbquant.client import QuantClient

# 对齐 _create_quant_client 的连接池配置
_HTTPX_CONFIG = create_resilient_httpx_client(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=30.0,
    enable_http2=False,
)

_QUANT = QuantClient(
    base_url=settings.kb_api_base_url.rstrip("/api/v1").rstrip("/"),
    api_key=settings.kb_api_key,
    limits=_HTTPX_CONFIG["limits"],
    timeout=_HTTPX_CONFIG["timeout"],
)


def _text_similarity(a: str, b: str) -> float:
    """0-1 的文本相似度（基于 SequenceMatcher）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _feedback_text(fb: dict) -> str:
    """拼接 feedback 的全文用于相似度计算。"""
    parts = [
        fb.get("title") or "",
        fb.get("lessons_learned") or "",
        fb.get("error_reason") or "",
        fb.get("expected_outcome") or "",
        fb.get("actual_outcome") or "",
        fb.get("missed_factors") or "",
        fb.get("adjustment_suggestions") or "",
    ]
    return " ".join(p for p in parts if p)


async def fetch_feedbacks(quant: QuantClient, fids: list[str]) -> list[dict]:
    """批量拉取 feedback 数据。"""
    raw = await quant.feedback.get_many(fids)
    return [r.model_dump() if hasattr(r, "model_dump") else r for r in raw]


async def main(limit: int, min_count: int):
    # 1. 从 wfuse 查出 feedback_ids 数量 >= min_count 的 task
    async with async_session() as sess:
        stmt = (
            select(Task.id, Task.raw_info_id, Task.feedback_ids, Task.state)
            .where(func.jsonb_array_length(Task.feedback_ids) >= min_count)
            .order_by(func.jsonb_array_length(Task.feedback_ids).desc())
            .limit(limit)
        )
        result = await sess.execute(stmt)
        rows = result.fetchall()

    if not rows:
        print(f"没有 feedback_ids >= {min_count} 的 task")
        return

    print(f"共 {len(rows)} 个 task（feedback 数 >= {min_count}）\n")

    # 2. 用预创建的客户端连接 kbquant
    quant = _QUANT

    # 3. 逐个 task 拉取 feedback 并比较
    total_duplicated = 0
    similarity_threshold = 0.85

    for row in rows:
        task_id, raw_info_id, fids, state = row
        if not fids or len(fids) < 2:
            continue

        try:
            feedbacks = await fetch_feedbacks(quant, fids)
        except Exception as e:
            print(f"  [SKIP] task={task_id} 拉取 feedback 失败: {e}")
            continue

        if len(feedbacks) < 2:
            continue

        texts = [_feedback_text(fb) for fb in feedbacks]

        # 两两比较
        pair_scores: list[tuple[int, int, float]] = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                score = _text_similarity(texts[i], texts[j])
                pair_scores.append((i, j, score))

        max_sim = max(s for _, _, s in pair_scores) if pair_scores else 0
        avg_sim = sum(s for _, _, s in pair_scores) / len(pair_scores) if pair_scores else 0

        is_dup = max_sim >= similarity_threshold
        if is_dup:
            total_duplicated += 1

        marker = " [DUP]" if is_dup else ""
        print(f"--- task={task_id} raw_info={raw_info_id} state={state} n_feedbacks={len(fids)}{marker}")
        print(f"    最大相似度={max_sim:.3f}  平均相似度={avg_sim:.3f}")
        for k, fb in enumerate(feedbacks):
            fid = fb.get("id", "?")
            title = fb.get("title", "?")
            created = fb.get("created_at", "?")
            lessons = shorten(fb.get("lessons_learned") or "", 120, placeholder="...")
            print(f"    [{k}] id={fid} created={created}")
            print(f"        title={title}")
            print(f"        lessons_learned={lessons}")

        if is_dup and len(pair_scores) >= 2:
            for i, j, score in pair_scores:
                if score >= similarity_threshold:
                    print(f"    >> 高相似对: [{i}] vs [{j}] similarity={score:.3f}")

        print()

    # 4. 汇总统计
    if rows:
        print(f"{'='*60}")
        print(f"汇总: {len(rows)} 个 task 中，{total_duplicated} 个存在高相似度(>={similarity_threshold})的重复 feedback")
        print(f"高相似度占比: {total_duplicated}/{len(rows)} = {total_duplicated/len(rows)*100:.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="分析重复 feedback")
    parser.add_argument("--limit", type=int, default=50, help="最多处理多少个 task")
    parser.add_argument("--min-count", type=int, default=2, help="feedback_ids 最少多少个")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, min_count=args.min_count))
