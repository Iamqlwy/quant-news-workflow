"""重要性评判测试 —— 随机 100 条资讯，并发调用 LLM"""
import asyncio
from loguru import logger
import csv
import json
import random
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.llm.provider import LLMProvider
from src.pipeline.significance import SignificanceJudge
from src.ingestion.csv_loader import _extract_title, _extract_source


async def run_one(judge: SignificanceJudge, idx: int, row: dict, sem: asyncio.Semaphore) -> dict:
    """评判单条资讯"""
    title = _extract_title(row["content"])
    source = _extract_source(row["content"])
    published_at = row["datetime"]
    body = row["content"]

    async with sem:
        started = datetime.now()
        result = await judge.evaluate(title, body, source, published_at)
        elapsed = (datetime.now() - started).total_seconds()

    return {
        "idx": idx,
        "title": title,
        "source": source,
        "published_at": published_at,
        "body_preview": body[:200],
        "elapsed_s": round(elapsed, 2),
        "result": result,
    }


async def main():
    # 1. 读取 news.csv，随机抽 100 条
    logger.info("读取 news.csv ...")
    csv_path = Path("news.csv")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = [row for row in reader]
    logger.info(f"共 {len(all_rows)} 条，随机抽取 100 条...")
    sample = random.sample(all_rows, min(100, len(all_rows)))

    # 2. 初始化 LLM 和评判器
    logger.info("初始化 LLM 客户端...")
    llm = LLMProvider("qwen", "qwen3.5-flash", settings.qwen_api_key, settings.qwen_base_url)
    judge = SignificanceJudge(llm)

    # 3. 并发 100 条
    sem = asyncio.Semaphore(100)
    logger.info("开始并发评判（并发度=100）...")
    started = datetime.now()

    coros = [run_one(judge, i, row, sem) for i, row in enumerate(sample)]
    results = await asyncio.gather(*coros)

    elapsed_total = (datetime.now() - started).total_seconds()
    logger.info(f"完成！总耗时 {elapsed_total:.1f}s，平均 {(elapsed_total/100):.1f}s/条")

    # 4. 统计
    significant = [r for r in results if r["result"].get("is_significant")]
    skipped = [r for r in results if not r["result"].get("is_significant")]
    macro = [r for r in results if r["result"].get("info_type") == "macro"]
    scores = [r["result"].get("total_score", 0) for r in results if r["result"].get("info_type") != "macro"]

    logger.info(f"\n=== 统计 ===")
    logger.info(f"显著 (进入深度分析): {len(significant)}")
    logger.info(f"跳过:                 {len(skipped)}")
    logger.info(f"宏观 (分流到宏观流程): {len(macro)}")
    logger.info(f"平均分 (非宏观):       {sum(scores)/len(scores):.1f}" if scores else "平均分: N/A")

    if significant:
        logger.info(f"\n=== 高分组 (score >= 60) ===")
        for r in sorted(significant, key=lambda x: -x["result"].get("total_score", 0))[:10]:
            res = r["result"]
            logger.info(f"  [{res.get('total_score',0):3d}] {res.get('info_type','?'):10s} {r['title'][:60]}")

    if macro:
        logger.info(f"\n=== 宏观分流 ===")
        for r in macro[:5]:
            logger.info(f"  {r['title'][:60]}")

    # 5. 保存结果
    out_path = Path("data/significance_test.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "test_time": datetime.now().isoformat(),
        "total_items": len(sample),
        "elapsed_s": round(elapsed_total, 1),
        "stats": {
            "significant": len(significant),
            "skipped": len(skipped),
            "macro": len(macro),
            "avg_score_non_macro": round(sum(scores)/len(scores), 1) if scores else None,
        },
        "results": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
