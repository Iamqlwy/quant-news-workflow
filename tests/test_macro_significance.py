"""宏观紧急度评判测试 —— 对 45 条宏观资讯并发调用 LLM"""
import asyncio
from loguru import logger
import json
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.llm.provider import LLMProvider
from src.pipeline.macro_significance import MacroSignificanceJudge


async def run_one(judge: MacroSignificanceJudge, sem: asyncio.Semaphore, item: dict) -> dict:
    """评判单条宏观资讯"""
    async with sem:
        started = datetime.now()
        result = await judge.evaluate(
            item["title"], item["body_preview"], item["source"], item["published_at"]
        )
        elapsed = (datetime.now() - started).total_seconds()

    return {
        "title": item["title"],
        "source": item["source"],
        "published_at": item["published_at"],
        "body_preview": item["body_preview"],
        "elapsed_s": round(elapsed, 2),
        "result": result,
    }


async def main():
    # 1. 读取上一步的宏观资讯
    with open("data/macro_items.json", encoding="utf-8") as f:
        macro_items = json.load(f)
    logger.info(f"读取 {len(macro_items)} 条宏观资讯")

    # 2. 初始化
    llm = LLMProvider("qwen", "qwen3.5-flash", settings.qwen_api_key, settings.qwen_base_url)
    judge = MacroSignificanceJudge(llm)
    sem = asyncio.Semaphore(45)

    # 3. 并发
    logger.info("开始并发评判（并发度=45）...")
    started = datetime.now()
    coros = [run_one(judge, sem, item) for item in macro_items]
    results = await asyncio.gather(*coros)
    elapsed_total = (datetime.now() - started).total_seconds()

    # 4. 统计
    urgent = [r for r in results if r["result"].get("is_urgent")]
    daily = [r for r in results if not r["result"].get("is_urgent")]
    scores = [r["result"].get("total_score", 0) for r in results]

    logger.info(f"完成！{elapsed_total:.1f}s")
    logger.info(f"\n=== 统计 ===")
    logger.info(f"紧急 (立即处理): {len(urgent)}")
    logger.info(f"日更 (进入日更队列): {len(daily)}")
    logger.info(f"平均分: {sum(scores)/len(scores):.1f}")

    if urgent:
        logger.info(f"\n=== 紧急 (score >= 80) ===")
        for r in sorted(urgent, key=lambda x: -x["result"].get("total_score", 0)):
            res = r["result"]
            assets = res.get("affected_assets", [])
            themes = res.get("key_themes", [])
            logger.info(f"  [{res.get('total_score',0):3d}] {r['title'][:70]}")
            logger.info(f"       资产: {assets}  主题: {themes}")
            logger.info(f"       理由: {res.get('rationale','')[:120]}")

    if daily:
        logger.info(f"\n=== 日更队列 top 10 (按分数) ===")
        for r in sorted(daily, key=lambda x: -x["result"].get("total_score", 0))[:10]:
            res = r["result"]
            logger.info(f"  [{res.get('total_score',0):3d}] {r['title'][:70]}")
            logger.info(f"       理由: {res.get('rationale','')[:120]}")

    # 5. 保存
    out_path = Path("data/macro_significance_test.json")
    output = {
        "test_time": datetime.now().isoformat(),
        "total_items": len(macro_items),
        "elapsed_s": round(elapsed_total, 1),
        "stats": {
            "urgent": len(urgent),
            "daily": len(daily),
            "avg_score": round(sum(scores)/len(scores), 1),
        },
        "results": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
