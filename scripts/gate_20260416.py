"""门控脚本：用 GATE_PROMPT 对 2026-04-16 的资讯做 pass/maybe/reject 三分类。

复用 significance.py 的关键词预过滤和相似资讯检索，LLM 层用更简洁的门控 prompt。
"""
import asyncio
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from src.config import settings
from src.llm.provider import LLMProvider
from src.pipeline.significance import _pre_filter, _build_context_block

# ---------- 标题/来源提取（与 csv_loader 一致）----------

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


# ---------- 门控 Prompt ----------

GATE_PROMPT = """你是事件驱动交易系统的高精度资讯门控器。

你的目标是：只放行真正值得进入后续系统分析的资讯；宁可漏掉一些弱机会，也不要放行大量低价值噪音。

你不是新闻摘要器，不是宏观评论员，也不是交易建议生成器。
你的任务只有一个：判断这条资讯是否具备事件驱动分析价值。

核心放行标准：
只有同时满足以下四个条件，才允许 pass：
1. 硬事实：资讯包含已经发生、正式披露或可验证的事实，而不是观点、预测、表态、计划或传闻。
2. 边际变化：资讯包含新增、落地、升级、转折、超预期的信息，而不是例行披露、重复转载或已知趋势延续。
3. 影响机制明确：能清楚影响收入、利润、成本、供需、估值、风险、控制权、流动性中的至少一个。
4. 影响强度足够：影响不是象征性、小金额、普通运营或泛泛利好，而是有可能改变市场对相关资产的定价。

硬排除规则：
以下情况直接 reject：
- 只有股价涨跌、板块拉升、异动描述，没有新增原因；
- 观点、预测、研报看好、专家称、机构认为、公司表示未来布局；
- 重复已知事件，无新增数据、转折、升级或落地；
- 例行公告：股东大会、董事会决议、普通财报预告、普通担保、普通理财、章程修改；
- 普通运营新闻：参展、小奖项、非核心产品发布、非核心人事变动；
- 小金额回购/增持、小合同、小订单，对收入/市值影响很小；
- 泛泛政策表态、会议提法、研究推动、征求意见，缺少正式落地细则；
- 概念叙事、行业空间、长期趋势，没有订单、价格、销量、政策、审批等硬证据；
- 纯海外事件，且不直接涉及中国资产、中国供应链、对华政策或关键商品；
- 无法映射到 A 股公司、行业、商品链条或国内政策变量。

允许 pass 的事件类型仅限以下几类：
1. 公司重大硬事件：
   业绩明显超预期/暴雷、重大订单/合同、重大并购重组、控制权变化、监管调查/处罚、重大诉讼、核心产品获批/失败、重大事故、核心客户变化、大比例回购/增持、重大资产出售。
2. 行业供需/价格重大变化：
   关键商品价格大幅变化、库存异常变化、产能关停、事故停产、限产落地、进口/出口限制、招标明显放量、需求数据明显超预期。
3. 政策/监管正式落地：
   正式文件、执行细则、补贴细则、试点名单、准入规则、限制规则、处罚规则、审批结果、标准落地。
4. 明确影响中国资产的海外事件：
   对华制裁、关税、出口管制、关键原材料断供、海外竞争对手停产、全球大宗商品剧烈变化、海外大厂资本开支变化且明确影响中国供应链。
5. 概念主题硬证据：
   订单落地、客户确认、销量爆发、价格变化、补贴落地、审批通过、商业化数据确认。

强度判断：
- 合同/订单金额占公司营收 <1%，通常 reject；>5% 可考虑 pass；>10% 更容易 pass。
- 回购/增持金额占市值 <0.1%，通常 reject；>0.5% 可考虑 pass。
- 产品发布没有订单、客户、审批、收入数据，通常 reject。
- 政策没有执行细则、补贴金额、约束规则或明确对象，通常 reject。
- 价格变化没有幅度、持续性或供需原因，通常 reject。
- 宏观/海外事件不能明确传导到 A 股资产，通常 reject。

输出 JSON：
{
  "gate": "pass/reject",
  "event_type": "company/industry/policy/concept/macro/market_noise",
  "signal_type": "hard_fact/soft_signal/repeat/noise",
  "asset_relevance": "direct/indirect/weak/none",
  "impact_mechanism": "revenue/profit/cost/supply_demand/valuation/risk/control/liquidity/none",
  "strength": "strong/medium/weak/none",
  "reason": "一句话说明为什么放行或拒绝"
}"""


# ---------- 相似资讯检索（复用 significance.py 的检索逻辑）----------

async def search_similar(quant, title: str, body: str, clock=None) -> list[dict]:
    """搜近期相似资讯，帮 LLM 判断是否重复/已知事件进展。"""
    if quant is None:
        return []
    try:
        from kbquant.schemas.search import SearchRequest, FetchByIdsRequest

        query = f"{title} {body}" if body else title
        if not query.strip():
            return []
        date_range = None
        if clock is not None:
            two_months_ago = clock.now - timedelta(days=60)
            date_range = {"start": two_months_ago.strftime("%Y-%m-%d")}
        req = SearchRequest(
            query_text=query,
            mode="bm25",
            only_tables=["raw_information"],
            date_range=date_range,
            limit=10,
        )
        resp = await quant.search.search(req)
        raw_ids = [str(r.id) for r in resp.items]
        if not raw_ids:
            return []
        fetch_req = FetchByIdsRequest(table_ids={"raw_information": raw_ids[:5]})
        fetch_resp = await quant.search.fetch_by_ids(fetch_req)
        raw_data = fetch_resp.data.get("raw_information", [])
        return [
            {
                "result_type": "raw_information",
                "title": d.get("title", ""),
                "body": (d.get("body") or d.get("content") or "")[:500],
            }
            for d in raw_data
            if d.get("title")
        ]
    except Exception:
        return []


# ---------- 单条评分 ----------

async def gate_one(llm, quant, sem: asyncio.Semaphore, idx: int, row: dict) -> dict:
    title = extract_title(row["content"])
    body = row["content"]
    source = extract_source(row["content"])
    published_at = row["datetime"]

    async with sem:
        # 1. 搜相似资讯
        similar = await search_similar(quant, title, body)

        # 2. 构建消息
        user_content = f"标题：{title}\n来源：{source}\n时间：{published_at}\n\n正文：\n{body[:3000]}"
        context_block = _build_context_block(similar)
        if context_block:
            user_content += "\n" + context_block

        messages = [
            {"role": "system", "content": GATE_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # 3. 调用 LLM
        started = datetime.now()
        try:
            result = await llm.chat_json(messages)
        except Exception:
            result = {"gate": "reject", "event_type": "unknown", "signal_type": "noise",
                       "asset_relevance": "none", "impact_mechanism": "none",
                       "strength": "none", "reason": "LLM 调用失败"}
        elapsed = (datetime.now() - started).total_seconds()

    return {
        "idx": idx,
        "title": title,
        "source": source,
        "published_at": published_at,
        "body_preview": body[:200],
        "elapsed_s": round(elapsed, 2),
        "gate": result.get("gate", "reject"),
        "event_type": result.get("event_type", ""),
        "signal_type": result.get("signal_type", ""),
        "asset_relevance": result.get("asset_relevance", ""),
        "impact_mechanism": result.get("impact_mechanism", ""),
        "strength": result.get("strength", ""),
        "reason": result.get("reason", ""),
    }


# ---------- 主流程 ----------

async def main():
    csv_path = Path("news.csv")
    print(f"读取 {csv_path} ...")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = [row for row in reader]

    target_date = "2026-04-16"
    rows_0416 = [r for r in all_rows if r["datetime"].startswith(target_date)]
    print(f"共 {len(all_rows)} 条，{target_date} 有 {len(rows_0416)} 条")

    if not rows_0416:
        print("没有找到 2026-04-16 的数据，退出。")
        return

    # 关键词预过滤
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
    print(f"关键词预过滤: {pre_filtered} 条被丢弃, {len(unfiltered_rows)} 条进入 LLM 门控")

    # 初始化 LLM
    print(f"初始化 LLM: deepseek/{settings.deepseek_model} ...")
    llm = LLMProvider(
        "deepseek",
        settings.deepseek_model,
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )

    # 初始化 KB 客户端
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

    # 并发门控
    concurrency = 500
    sem = asyncio.Semaphore(concurrency)
    print(f"开始并发门控（并发度={concurrency}）...")
    started = datetime.now()

    coros = [gate_one(llm, quant, sem, i, row) for i, row in enumerate(unfiltered_rows)]
    results = await asyncio.gather(*coros)

    elapsed_total = (datetime.now() - started).total_seconds()
    avg_time = elapsed_total / len(results) if results else 0
    print(f"门控完成！总耗时 {elapsed_total:.1f}s，平均 {avg_time:.1f}s/条")

    # 排序：pass > reject
    gate_order = {"pass": 0, "reject": 1}
    results.sort(key=lambda r: gate_order.get(r["gate"], 2))

    # 统计
    passed = [r for r in results if r["gate"] == "pass"]
    rejected = [r for r in results if r["gate"] == "reject"]
    hard_facts = [r for r in results if r["signal_type"] == "hard_fact"]
    strong = [r for r in results if r["strength"] == "strong"]
    medium = [r for r in results if r["strength"] == "medium"]
    by_event = {}
    by_mechanism = {}
    for r in results:
        t = r["event_type"]
        by_event[t] = by_event.get(t, 0) + 1
        m = r["impact_mechanism"]
        if m and m != "none":
            by_mechanism[m] = by_mechanism.get(m, 0) + 1

    print(f"\n=== 统计 ===")
    print(f"总计 (预过滤后):     {len(results)}")
    print(f"pass:                {len(passed)} ({len(passed)/len(results)*100:.1f}%)")
    print(f"reject:              {len(rejected)} ({len(rejected)/len(results)*100:.1f}%)")
    print(f"hard_fact:           {len(hard_facts)}")
    print(f"strong: {len(strong)}, medium: {len(medium)}")
    print(f"按 event_type:       {by_event}")
    print(f"按 impact_mechanism: {by_mechanism}")

    if passed:
        print(f"\n=== PASS TOP 40 ===")
        for r in passed[:40]:
            print(f"  [{r['event_type']:10s}] {r['title'][:70]}")
            print(f"         signal={r['signal_type']}, relevance={r['asset_relevance']}, "
                  f"mechanism={r['impact_mechanism']}, strength={r['strength']}")
            print(f"         reason={r['reason']}")

    if rejected:
        print(f"\n=== REJECT 抽样 10 ===")
        for r in rejected[:10]:
            print(f"  [{r['event_type']:10s}] {r['title'][:70]}")
            print(f"         signal={r['signal_type']}, relevance={r['asset_relevance']}, "
                  f"strength={r['strength']}, reason={r['reason']}")

    # 保存 CSV
    out_path = Path(f"data/gate_{target_date}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "idx", "title", "source", "published_at", "body_preview", "elapsed_s",
        "gate", "event_type", "signal_type", "asset_relevance",
        "impact_mechanism", "strength", "reason",
    ]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"\n结果已保存: {out_path} ({len(results)} 条)")

    # 同时保存 JSON
    json_path = Path(f"data/gate_{target_date}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "test_time": datetime.now().isoformat(),
            "target_date": target_date,
            "config": {"concurrency": concurrency},
            "stats": {
                "total_in_csv": len(all_rows),
                "target_date_rows": len(rows_0416),
                "pre_filtered": pre_filtered,
                "llm_gated": len(results),
                "pass": len(passed),
                "reject": len(rejected),
                "pass_rate": round(len(passed)/len(results)*100, 1) if results else 0,
                "hard_fact": len(hard_facts),
                "strong": len(strong),
                "medium": len(medium),
                "by_event_type": by_event,
                "by_mechanism": by_mechanism,
            },
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"JSON 已保存: {json_path}")


if __name__ == "__main__":
    asyncio.run(main())
