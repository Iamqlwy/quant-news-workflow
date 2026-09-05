"""批量评分脚本：读取 news.csv 中 2026-04-16 的资讯，用 SignificanceJudge 评分，按总分降序输出 CSV。"""
import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.llm.provider import LLMProvider
from src.pipeline.significance import SignificanceJudge, _pre_filter


def extract_title(content: str) -> str:
    import re
    m = re.search(r"【(.+?)】", content)
    if m:
        return m.group(1)
    if content.startswith("市场资讯："):
        rest = content[5:].strip()
        first_sentence = rest.split("。")[0][:80]
        return first_sentence or "市场资讯"
    return content[:50]


def extract_source(content: str) -> str:
    if content.startswith("市场资讯："):
        return "市场资讯"
    return "csv_import"


async def score_one(judge: SignificanceJudge, sem: asyncio.Semaphore, idx: int, row: dict) -> dict:
    title = extract_title(row["content"])
    body = row["content"]
    source = extract_source(row["content"])
    published_at = row["datetime"]

    async with sem:
        started = datetime.now()
        result = await judge.evaluate(title, body, source, published_at)
        elapsed = (datetime.now() - started).total_seconds()

    scores = result.get("scores", {})
    return {
        "idx": idx,
        "title": title,
        "source": source,
        "published_at": published_at,
        "body_preview": body[:200],
        "elapsed_s": round(elapsed, 2),
        "info_type": result.get("info_type", ""),
        "is_significant": result.get("is_significant", False),
        "is_urgent": result.get("is_urgent", False),
        "total_score": result.get("total_score", 0),
        "novelty": scores.get("novelty", 0),
        "direction": scores.get("direction", 0),
        "urgency": scores.get("urgency", 0),
        "reliability": scores.get("reliability", 0),
        "market_relevance": scores.get("market_relevance", 0),
        "direction_hint": result.get("direction_hint", ""),
        "time_horizon": result.get("time_horizon", ""),
        "rationale": result.get("rationale", ""),
        "affected_tickers": ",".join(result.get("affected_tickers", [])),
        "affected_sectors": ",".join(result.get("affected_sectors", [])),
        "key_entities": ",".join(result.get("key_entities", [])),
    }


async def main():
    csv_path = Path("news.csv")
    print(f"读取 {csv_path} ...")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = [row for row in reader]

    # 筛选 2026-04-16
    target_date = "2026-04-16"
    rows_0416 = [r for r in all_rows if r["datetime"].startswith(target_date)]
    print(f"共 {len(all_rows)} 条，{target_date} 有 {len(rows_0416)} 条")

    if not rows_0416:
        print("没有找到 2026-04-16 的数据，退出。")
        return

    # 先跑一遍关键词预过滤，统计过滤数
    pre_filtered = 0
    unfiltered_rows = []
    for r in rows_0416:
        title = extract_title(r["content"])
        body = r["content"]
        should_discard, _ = _pre_filter(title, body)
        if should_discard:
            pre_filtered += 1
        else:
            unfiltered_rows.append(r)
    print(f"关键词预过滤: {pre_filtered} 条被丢弃, {len(unfiltered_rows)} 条进入 LLM 评分")

    # 初始化 LLM（与生产环境 judge 配置一致）
    print(f"初始化 LLM: deepseek/{settings.deepseek_model} ...")
    llm = LLMProvider(
        "deepseek",
        settings.deepseek_model,
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    # 初始化 KB 客户端（用于搜相似资讯，降低已知事件增量报道的分数）
    from kbquant.client import QuantClient
    from src.utils.http_resilience import create_resilient_httpx_client

    base_url = settings.kb_api_base_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[:-len("/api/v1")]
    client_config = create_resilient_httpx_client(
        max_connections=200,
        max_keepalive_connections=20,
        keepalive_expiry=10.0,
        enable_http2=False,
    )
    quant = QuantClient(
        base_url=base_url,
        api_key=settings.kb_api_key,
        limits=client_config["limits"],
        timeout=client_config["timeout"],
    )
    judge = SignificanceJudge(llm, quant=quant, clock=None)

    # 并发评分
    concurrency = 500
    sem = asyncio.Semaphore(concurrency)
    print(f"开始并发评分（并发度={concurrency}）...")
    started = datetime.now()

    coros = [score_one(judge, sem, i, row) for i, row in enumerate(unfiltered_rows)]
    results = await asyncio.gather(*coros)

    elapsed_total = (datetime.now() - started).total_seconds()
    avg_time = elapsed_total / len(results) if results else 0
    print(f"评分完成！总耗时 {elapsed_total:.1f}s，平均 {avg_time:.1f}s/条")

    # 按总分降序排序
    results.sort(key=lambda r: -r["total_score"])

    # 统计
    significant = [r for r in results if r["is_significant"]]
    macro = [r for r in results if r["info_type"] == "macro"]
    urgent = [r for r in results if r["is_urgent"]]

    print(f"\n=== 统计 ===")
    print(f"总计 (预过滤后):     {len(results)}")
    print(f"显著 (进入深度分析): {len(significant)}")
    print(f"跳过:                {len(results) - len(significant)}")
    print(f"宏观分流:            {len(macro)}")
    print(f"宏观紧急:            {len(urgent)}")
    non_macro_scores = [r["total_score"] for r in results if r["info_type"] != "macro"]
    if non_macro_scores:
        print(f"非宏观平均分:        {sum(non_macro_scores)/len(non_macro_scores):.1f}")

    if significant:
        print(f"\n=== 高分 TOP 20 ===")
        for r in significant[:20]:
            print(f"  [{r['total_score']:3d}] {r['info_type']:10s} {r['title'][:60]}")

    # 保存 CSV
    out_path = Path(f"data/significance_{target_date}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "idx", "title", "source", "published_at", "body_preview", "elapsed_s",
        "info_type", "is_significant", "is_urgent", "total_score",
        "novelty", "direction", "urgency", "reliability", "market_relevance",
        "direction_hint", "time_horizon", "rationale",
        "affected_tickers", "affected_sectors", "key_entities",
    ]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"\n结果已保存: {out_path} ({len(results)} 条，按总分降序)")

    # 同时保存 JSON
    json_path = Path(f"data/significance_{target_date}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "test_time": datetime.now().isoformat(),
            "target_date": target_date,
            "config": {
                "total_threshold": 65,
                "novelty_threshold": 10,
                "direction_threshold": 10,
                "concurrency": concurrency,
            },
            "stats": {
                "total_in_csv": len(all_rows),
                "target_date_rows": len(rows_0416),
                "pre_filtered": pre_filtered,
                "llm_scored": len(results),
                "significant": len(significant),
                "skipped": len(results) - len(significant),
                "macro": len(macro),
                "urgent": len(urgent),
                "avg_score_non_macro": round(sum(non_macro_scores)/len(non_macro_scores), 1) if non_macro_scores else None,
            },
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"JSON 已保存: {json_path}")


if __name__ == "__main__":
    asyncio.run(main())
