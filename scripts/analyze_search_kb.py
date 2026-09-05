import json
from collections import Counter, defaultdict
from pathlib import Path

with open("search_kb_queries_stats.json", "r", encoding="utf-8") as f:
    stats = json.load(f)

frequency = stats["frequency"]
total = stats["total_records"]
unique = stats["unique_queries"]

# Top 100 queries
top100 = frequency[:100]

# Analyze by frequency buckets
buckets = defaultdict(list)
for q, cnt in frequency:
    if cnt >= 10:
        buckets["10次及以上"].append((q, cnt))
    elif cnt >= 5:
        buckets["5-9次"].append((q, cnt))
    elif cnt >= 3:
        buckets["3-4次"].append((q, cnt))
    elif cnt >= 2:
        buckets["2次"].append((q, cnt))
    else:
        buckets["1次"].append((q, cnt))

print("=== 频率分布 ===")
for bucket in ["10次及以上", "5-9次", "3-4次", "2次", "1次"]:
    queries = buckets.get(bucket, [])
    print(f"{bucket}: {len(queries)} 条不同查询, 共 {sum(c for _, c in queries)} 次调用")

# Keyword analysis - extract common tokens
all_keywords = Counter()
stop_words = {"的", "了", "在", "和", "是", "与", "及", "或", "等", "对", "从", "到", "为", "以", "：", ":", "、", "，", ",", "。", ".", " ", "  "}
for q, cnt in frequency:
    tokens = q.replace("，", " ").replace("、", " ").replace("：", " ").split()
    for t in tokens:
        t = t.strip()
        if t and len(t) >= 2 and t not in stop_words:
            all_keywords[t] += cnt

print("\n=== 高频关键词 (出现次数 >= 50) ===")
for kw, cnt in all_keywords.most_common(100):
    if cnt < 50:
        break
    print(f"  {kw}: {cnt}")

# Analyze topics by entity
entity_counter = Counter()
for q, cnt in frequency:
    for entity in ["中芯国际", "宁德时代", "兴发集团", "华虹", "兆易创新", "北京君正", "美光科技",
                    "隆基绿能", "通威股份", "阳光电源", "比亚迪", "贵州茅台", "五粮液",
                    "韦尔股份", "北方华创", "中微公司", "寒武纪", "海光信息",
                    "药明康德", "恒瑞医药", "迈瑞医疗", "片仔癀"]:
        if entity in q:
            entity_counter[entity] += cnt

print("\n=== 标的出现频次 ===")
for entity, cnt in entity_counter.most_common(20):
    print(f"  {entity}: {cnt}")

# Analyze query intent
intent_counter = Counter()
intent_patterns = {
    "复盘/反馈": ["复盘", "反馈", "教训", "判断错误", "证伪"],
    "基本面/业绩": ["业绩", "基本面", "年报", "营收", "净利润", "毛利率", "产能", "供需"],
    "技术面": ["技术面", "突破", "趋势", "反转", "均线", "K线", "MACD", "RSI"],
    "制裁/政策": ["制裁", "出口管制", "政策", "补贴", "关税", "法规"],
    "机构/评级": ["机构", "评级", "下调", "减持", "卖出", "看空", "利好", "利空"],
    "节点/状态": ["节点", "WorldNode", "状态"],
    "行业/板块": ["行业", "板块", "半导体", "芯片", "医药", "新能源", "光伏", "消费"],
    "价格/供需": ["价格", "涨价", "下跌", "降", "成本", "竞争", "产能过剩"],
    "风险": ["风险"],
    "投资逻辑": ["投资逻辑"],
    "分析报告": ["分析报告", "分析"],
    "偏好": ["偏好"],
}
for q, cnt in frequency:
    matched = False
    for intent, keywords in intent_patterns.items():
        if any(kw in q for kw in keywords):
            intent_counter[intent] += cnt
            matched = True
    if not matched:
        intent_counter["其他"] += cnt

print("\n=== 查询意图分析 ===")
for intent, cnt in intent_counter.most_common():
    pct = cnt / total * 100
    print(f"  {intent}: {cnt} 次 ({pct:.1f}%)")
