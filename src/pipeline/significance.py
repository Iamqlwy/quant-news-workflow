"""行业/个股重要性判断 —— 关键词预过滤 + 三阶段 LLM pipeline (hard_gate → classify → per-type scoring)"""

import re
from datetime import timedelta
from time import perf_counter
from typing import Any

from kbquant.client import QuantClient
from kbquant.schemas.search import FetchByIdsRequest, SearchRequest
from loguru import logger

from src.core.clock import Clock
from src.workflow_logging import log_progress




KNOWN_CONTEXT = """
## 已知市场主线/宏观背景 (known_context)

以下是当前市场已经熟知、反复讨论的宏观背景和持续事件，不应视为"新催化"：

### 1. 中东地缘冲突与霍尔木兹海峡
- 2026年2月底，美国联合以色列刺杀伊朗最高领袖哈梅内伊，双方随后开战。
- 自3月起，霍尔木兹海峡处于封锁状态，全球原油价格因此持续处于80美元/桶以上。
- 美军在中东部署超过1万名军事人员、十余艘军舰执行封锁任务。
- 该冲突已持续数月，市场已充分消化"油价高位+地缘风险"的逻辑。
- 相关日常进展（封锁执行、军舰调动、例行军事行动）不应视为新催化。
- 仅以下情况可视为边际变化：
  - 封锁实际解除或实质性松动（如美伊达成停火协议且开始执行）；
  - 冲突扩展至新的国家或地区（如伊朗封锁扩展至阿曼湾以外）；
  - 霍尔木兹海峡周边发生新的大规模战斗且影响产能或运输。

### 重要提示
如果当前资讯只是上述 known_context 中某条主线的重复或日常进展，没有出现：
- 新的数量级（数据大幅超预期/不及预期）；
- 新的落地事件（从计划变为执行）；
- 新的转折（方向性变化）；
- 新的定量变量（价格、库存、订单、产能等具体数字）；

则信息增量（novelty）不应超过10分。但如果资讯包含了新的定量数据（具体的产量数字、具体的价格水平、具体的制裁名单、具体的时间表变化），novelty 可按正常标准评分（5-15），依据数据的边际增量评分。
"""

# ---- 关键词预过滤：命中任一模式则直接丢弃，不进入 LLM 评估 ----

_NUMERIC_MARKET_PATTERN = re.compile(
    r"(最新价|现价|报价|收报|收于|报)\s*\d+(?:\.\d+)?\s*(?:元|美元|欧元|英镑|日元)"
    r"|(\d+(?:\.\d+)?\s*(?:元|美元)\s*(?:上涨|下跌|涨|跌)\s*\d+(?:\.\d+)?%)"
    r"|((?:上涨|下跌|涨幅|跌幅|涨停|跌停|大涨|大跌|收跌|收涨|高开|低开|涨逾|走低|走强|走高|领涨|探底回升|拉升|跌超|日内涨|日内跌|周内涨|周内跌|月内涨|月内跌)\s*\d+(?:\.\d+)?%)"
    r"|((?:成交额|成交量|换手率)\s*[:：]?\s*\d+(?:\.\d+)?(?:亿|万|%)?)"
    r"|((?:震荡上行|震荡下行|震荡拉升|震荡回调)\s*\d+(?:\.\d+)?%)"
    r"|((?:失守|跌破|突破|站上|跌穿)\s*\d+(?:\.\d+)?)"
    r"|(.{2,6}(?:涨|跌)\s*\d+(?:\.\d+)?%)"
)

_FOREX_MARKET_PATTERN = re.compile(
    r"([A-Z]{3}/[A-Z]{3})"
    r"|((?:美元兑|欧元兑|英镑兑|日元兑|离岸人民币|在岸人民币).{0,12}?(?:涨|跌|涨超|跌超)\s*\d+(?:\.\d+)?%)"
    r"|((?:现报|报|最新报)\s*\d+(?:\.\d+)?)"
    r"|((?:汇率|中间价)\s*(?:为|报|在)?\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_WEATHER_DISASTER_PATTERN = re.compile(
    r"(天气预报|气象台|中央气象台|"
    r"暴雨|大暴雨|特大暴雨|雷暴|雷电|冰雹|寒潮|高温预警|"
    r"台风|飓风|热带风暴|风暴潮|龙卷风|"
    r"地震|震中|震级|余震|海啸|"
    r"泥石流|山体滑坡|洪水|内涝|干旱|"
    r"红色预警|橙色预警|黄色预警|蓝色预警)"
)

# 天气/灾害中包含经济影响关键词时不应丢弃
_WEATHER_ECONOMIC_EXCEPTION = re.compile(
    r"(停产|产能|港口|供应链|物流|运输|煤矿|钢厂|化工厂|炼油|"
    r"电力|电网|停电|限电|水库|农作物|粮食|生猪|水产|"
    r"基础设施|公路|铁路|机场|航班)"
)

_OTHER_DISCARD_PATTERN = re.compile(r"(企查查APP|枪击案|异常波动公告)")

# 只有明确非商业的火灾才丢弃（居民楼/民宅/森林火灾）
_RESIDENTIAL_FIRE_PATTERN = re.compile(r"(居民楼火灾|民宅火灾|住宅火灾|森林大火|山火)")

# C-suite 辞职不应丢弃，只过滤非核心人员辞职
# 匹配两类模式：1) 职位在前，辞职在后（"董事长张三辞职"）2) 辞职在前，职位在后（"张三辞任财务总监"）
_KEY_EXEC_RESIGN_PATTERN = re.compile(
    r"(?:CEO|CFO|CTO|COO|董事长|总经理|总裁|首席(?:技术官|财务官|运营官|战略官|经济学家|科学家)?|财务总监|技术总监|运营总监)"
    r"[^，。]{0,10}(?:辞职|离职|辞任)"
    r"|"
    r"(?:辞职|离职|辞任)[^，。]{0,10}?(?:CEO|CFO|CTO|COO|董事长|总经理|总裁|首席|财务总监|技术总监|运营总监)"
)



def _pre_filter(title: str, body: str) -> tuple[bool, str]:
    """关键词预过滤。返回 (是否应丢弃, 命中原因)。

    过滤明确的噪音（天气/灾难/企查查/异常波动公告/汇率变化等）。
    纯数值行情变化（涨跌幅/成交量等）只过滤短标题（<=30 字），
    避免丢弃标题包含实体信息、正文恰好提及行情数据的实质性资讯。
    """
    text = f"{title}\n{body}"

    # ---- 天气/灾难：如包含经济影响关键词则不丢弃 ----
    if _WEATHER_DISASTER_PATTERN.search(text):
        if not _WEATHER_ECONOMIC_EXCEPTION.search(text):
            return True, "weather_disaster"
        # 有经济影响关键词，继续后续判断

    # ---- 其他明确噪音 ----
    if _OTHER_DISCARD_PATTERN.search(text):
        return True, "other_discard"

    # ---- 火灾：只有非商业火灾才丢弃 ----
    if "火灾" in text:
        if _RESIDENTIAL_FIRE_PATTERN.search(text):
            return True, "other_discard"
        # 商业/工业场所火灾不丢弃，继续判断
        if not re.search(r"(厂区|工厂|仓库|商场|油库|化工厂|电厂|矿井)", text):
            return True, "other_discard"

    # ---- 辞职：C-suite 保留，其他丢弃 ----
    if re.search(r"(辞职|离职|辞任)", text):
        if not _KEY_EXEC_RESIGN_PATTERN.search(text):
            return True, "other_discard"

    if _FOREX_MARKET_PATTERN.search(title):
        return True, "forex_market"

    title_stripped = title.strip()
    title_len = len(title_stripped)
    if title_len <= 30 and _NUMERIC_MARKET_PATTERN.search(title_stripped):
        return True, "numeric_market"

    return False, ""


# ---- 三阶段 prompt：hard_gate → classify → per-type scoring ----

HARD_GATE_PROMPT = r"""
你是A股资讯粗筛器。你的任务不是精判，而是过滤掉明显不可能影响A股的资讯。只有证据确凿的无关联才拒绝，存疑的放行到评分阶段。

通过（以下任一情况即可通过，gate_score >= 35）：
- 能说出它可能影响哪个A股公司或A股行业，以及大致从哪个方面（收入、利润、订单、成本、产能、政策、供需、价格、库存等）。
- 涉及对华政策、关税、制裁、出口管制，可能影响中国资产或产业链。
- 涉及国际大宗商品、关键原材料、汇率、利率的明显变化，可能传导到A股。
- 涉及海外科技巨头的资本开支、出货量、供应链变化，且A股有对应供应链公司。

不通过（仅限以下明显无关的情况）：
- 纯港股/海外公司日常经营，没有任何A股供应链或合作伙伴映射。
- 纯行情播报（涨跌幅/成交量等），无任何事件或原因解释。
- 纯天气/自然灾害/社会新闻，与资本市场无关。

gate_score 指南：
0-30：明显无关，不可能影响A股。
31-34：有微弱关联但非常间接。
35-100：可能影响A股，进入下一阶段详细评估。分数高低不影响后续判断。

pass_gate = gate_score >= 35。

**重要原则："存疑则放行" —— gate 只是粗筛，如果无法确定是否影响A股，让评分阶段做最终判断。宁可多放行一些到后续详细评估，也不要扼杀潜在信号。**

示例（应该 PASS 的边界案例）：
- "越南对华钢材加征反倾销税" —— 涉及对华关税/出口，映射钢铁板块，应给 40-50
- "某美资AI公司获10亿美元大订单，合作方涉及台积电、三星" —— 虽非直接A股但国内AI算力供应链有映射，应给 35-45
- "某省国资委推进国企整合指导意见" —— 方向性政策虽无执行细节但改变了重组预期，应给 40-50
- "美联储官员暗示可能提前结束缩表" —— 全球流动性预期变化，影响人民币汇率和北向资金，应给 35-45

只输出JSON：
{
  "pass_gate": true/false,
  "gate_score": 0,
  "reason": "一句话"
}
"""

CLASSIFY_PROMPT = r"""
你是A股资讯分类器。

只给出分类，不评分。如果不确定是否归为 market_noise，宁可归入其他类型，让评分阶段做最终判断。

分类：
company：A股公司基本面事件。
policy：国内政策、监管、补贴、准入、处罚、标准。
industry：行业价格、库存、订单、排产、开工率、交期、产能、停产、限产、涨价函、招标、进出口。
concept：AI、机器人、低空经济、固态电池、算力、半导体等主题机会。
macro：宏观、流动性、利率、汇率、财政、经济数据、地缘、大宗商品。
market_noise：确实纯粹是行情播报、天气灾害、社会新闻等，完全没有资产映射。

优先级：
company > policy > industry > concept > macro > market_noise。

只输出JSON：
{
  "info_type": "company/policy/industry/concept/macro/market_noise"
}
"""

SCORE_OUTPUT_SCHEMA = r"""
只输出JSON：
{
  "is_significant": true/false,
  "is_urgent": true/false,
  "route": "drop/archive/daily/deep_analysis/urgent_analysis",
  "total_score": 0,
  "scores": {
    "novelty": 0,
    "direction": 0,
    "urgency": 0,
    "reliability": 0,
    "market_relevance": 0
  },
  "signal_type": "hard_fact/industry_hard_signal/soft_signal/repeat/heat_noise/noise",
  "direction_hint": "long/short/neutral",
  "time_horizon": "short_term/medium_term/long_term",
  "affected_tickers": [],
  "affected_sectors": [],
  "key_entities": [],
  "rationale": "三句话以内"
}
"""

COMPANY_SCORE_PROMPT = r"""
你是A股公司事件评分器。

只评估 company 类资讯。

评分核心：这条资讯是否改变了这家公司的基本面预期？

判断信息增量时，不要只看有没有数字。关注：
- 这件事是否改变了市场对该公司未来收入/利润/风险的判断？
- 如果是例行公告（股东大会、章程修改、普通担保等），即使有具体数字，信息增量也接近零。
- 如果是重大订单/合同/处罚/诉讼/事故/审批结果，即使没有精确金额，信息增量也可能很高。
- "异常波动公告"本身信息增量为零——它只是说"股价波动但公司不知道原因"。

novelty 指南（0-25）：
- 12-25：出现了之前未知的事件或变化，可能影响公司预期
- 6-11：对已知信息的实质性补充，有新的细节
- 0-5：例行披露、重复信息

direction 指南（0-25）：
- 10-25：能判断这件事对公司是利好还是利空
- 5-9：有方向但需要多步推导
- 0-4：方向模糊

urgency 指南（0-20）：
- 12-20：突发事件，窗口以小时/天计
- 6-11：重要但可在数日内消化
- 0-5：无时间敏感度

reliability 指南（0-15）：
- 10-15：官方公告/交易所披露/监管文件
- 6-9：权威媒体/券商研究
- 0-5：非官方渠道/有待证实

market_relevance 指南（0-15）：
- 10-15：直接影响A股公司主营业务/利润
- 6-9：间接影响
- 0-5：影响微弱

约束：
- 纯观点/表态/未来规划，若没有任何已发生的事实支撑，novelty 不超过10。
- 例行流程公告（章程修改、换届、内部架构调整、普通担保）通常 total 在35-50之间。如果例行的形式包裹着实质的战略信息（如章程修改为重大重组铺路、换届伴随战略转型），按正常标准评分，不适用本条。
- 例行公告中如果包含市场未曾知晓的定量数据（如担保规模异常大、架构调整涉及核心业务拆分），novelty 可以超过10。
- 软表态（"公司看好""未来将布局"）不应单独成为 significant 的理由。
""" + SCORE_OUTPUT_SCHEMA

INDUSTRY_SCORE_PROMPT = r"""
你是A股行业信号评分器。

只评估 industry 类资讯。

评分核心：这条资讯是否改变了我们对某个行业的供需、价格、利润或竞争格局的预期？

"信息"不等于"数字"。以下都是有效信息：
- 价格变化、库存变化、开工率变化、产能变化 —— 即使没有精确数字
- 涨价函、限产通知、停产公告、招标结果 —— 事件本身就是信息
- 产业链调研中的具体状态描述（"库存不足十天""订单排到三季度""产能打满"）—— 状态变化就是信息

以下通常不是有效信息：
- 景气延续、需求旺盛、前景广阔 —— 没有新的边际事实
- 行业长期趋势的泛泛讨论 —— 市场已经定价的共识

不要因为资讯来自"业内人士""产业链调研""券商调研"就直接低分。

novelty 指南（0-25）：
- 12-25：出现了之前未被市场认知的供需变化、价格趋势、产能调整或政策冲击
- 6-11：对已知行业趋势的增量验证，有新的细节
- 0-5：泛泛的行业讨论、重复的景气描述

direction 指南（0-25）：
- 8-25：能判断该变化利好/利空哪些环节
- 4-7：有行业影响但受益/受损环节不清晰
- 0-3：无法推出方向

urgency 指南（0-20）：
- 12-20：供需/价格的突发重大变化，窗口以小时/天计
- 6-11：重要行业变化但可在数日内消化
- 0-5：无时间敏感度

reliability 指南（0-15）：
- 10-15：官方公告/交易所披露/政府文件/正式数据
- 6-9：权威媒体/产业链调研且信息具体
- 0-5：非官方渠道/有待证实

market_relevance 指南（0-15）：
- 10-15：能直接映射到A股行业利润
- 6-9：间接影响A股行业
- 0-5：影响微弱

约束：
- 仅有宏观叙事或行业展望，缺少具体供需/价格/产能变化事实，novelty 不超过10。
- 能形成供需、价格、利润传导的行业变化，即使源自产业链调研/业内人士，也应正常评分。
""" + SCORE_OUTPUT_SCHEMA

POLICY_SCORE_PROMPT = r"""
你是A股政策事件评分器。

只评估 policy 类资讯。

评分核心：这条政策信息是否改变了相关行业的经营环境预期？

政策信息的信息增量不在于文件形式，而在于是否出现了之前未被预期的政策方向或力度变化。

以下情形信息增量较高：
- 从"研究"到"执行"、从"征求意见"到"正式发布"、从"原则性"到"具体细则" —— 政策进程的推进本身
- 补贴/准入/处罚/标准的力度明显超出或不及市场预期
- 政策覆盖范围、时间表、执行主体的首次明确
- 审批结果的通过/否决本身改变了行业格局判断

以下情形信息增量较低：
- 方向性描述与市场已有预期一致，没有新的细节
- 重复解读已有政策，没有新增信息
- 无法判断该政策对行业收入/成本/竞争格局的实质影响

novelty 指南（0-25）：
- 15-25：出现了之前未知的政策方向变化、力度调整或关键审批结果
- 8-14：对已知政策方向的具体化补充，有新的细节但不是方向性转折
- 0-7：例行表态、已有政策的重复解读、模糊的方向描述

direction 指南（0-25）：
- 12-25：能清晰判断政策利好/利空哪些具体行业环节
- 6-11：有政策信号但传导链较长或受益方不清晰
- 0-5：方向模糊或影响极小

urgency 指南（0-20）：
- 15-20：突发政策/制裁/限制，窗口以小时/天计
- 8-14：重要政策变化但可在数日内消化
- 0-7：无时间敏感度

reliability 指南（0-15）：
- 12-15：政府正式文件/官方公告/交易所通知
- 8-11：权威媒体/官方渠道确认
- 0-7：市场传闻/有待证实

market_relevance 指南（0-15）：
- 12-15：直接影响A股行业收入/成本/准入/竞争格局
- 8-11：间接影响
- 0-7：影响微弱

约束：
- 仅有表态/方向性描述，无执行层面信息（范围、力度、时间表），novelty 不超过15。来自权威机构（国务院、发改委、央行、财政部）的方向性表态，即使缺少执行细则，如果改变了政策的方向预期或力度预期，可按正常标准评分，不超过20。
- 能清晰推导出对具体行业的收入/成本/准入影响，即使政策尚未正式发布，也可正常评分。
- 无法说清对哪个A股行业产生什么影响，market_relevance 不超过12。如果政策影响范围可缩小到少数几个行业，即使具体的传导机制需要推导，market_relevance 可到8-10。
""" + SCORE_OUTPUT_SCHEMA

CONCEPT_SCORE_PROMPT = r"""
你是A股主题机会评分器。

只评估 concept 类资讯。

评分核心：这条资讯是否提供了主题逻辑链上之前未被确认或未被定价的关键环节？

概念/主题类资讯的难点在于：大部分是热度噪音，但有些确实传递了重要的产业信号。区分的关键不是"有没有数字"，而是"这条信息是否改变了主题逻辑链上某个环节的概率或时间线"。

以下情形信息增量较高：
- 技术路线被关键验证或关键失败 —— 改变了整个方向的概率
- 产业链核心环节出现此前未知的供给瓶颈或产能突破
- 重要客户/重要合作伙伴关系的确认或丢失
- 审批/认证/标准的关键节点通过或受阻
- 头部公司战略转向（进入或退出）改变了行业竞争格局
- 商业化进程出现阶段性标志事件（首次交付、首次装机、首次量产）

以下情形信息增量较低：
- 新品发布/预售/大定/下载量/用户热度 —— 消费端热闹不等于利润增量
- 公司声称"未来布局""看好方向""持续投入" —— 没有新的边际事实
- 只有主题标签关联，但没有具体业务进展

关键判断：读完这条资讯后，你是否对这个主题相关A股公司的收入/利润/竞争格局预期发生了变化？

novelty 指南（0-25）：
- 12-25：出现了改变主题逻辑链概率或时间线的新事实
- 6-11：对已有趋势的增量确认，增强了确定性
- 0-5：热度描述、概念关联、未来展望

direction 指南（0-25）：
- 8-25：能判断该事件利好/利空哪些A股供应链环节
- 4-7：有方向但传导链较长或受益环节不明确
- 0-3：方向模糊或无法映射到A股

urgency 指南（0-20）：
- 12-20：突发重大验证/失败/审批/制裁，窗口以小时/天计
- 6-11：重要变化但可在数日内消化
- 0-5：无时间敏感度

reliability 指南（0-15）：
- 10-15：官方公告/上市公司披露/政府审批文件
- 6-9：权威媒体/产业链调研且信息具体
- 0-5：非官方渠道/有待证实

market_relevance 指南（0-15）：
- 10-15：能明确映射到A股供应链/合作伙伴的收入或利润变化
- 6-9：间接关联A股供应链
- 0-7：无法映射到A股

约束：
- 仅有发布/预约/热度/下载量/品牌声量，没有可映射到A股利润或供应链的事实，novelty 5-15。
- 能指向A股供应链/合作伙伴的收入或利润变化，即使没有精确数字，也属于有效信息。
- 主题叙事不能单独成为 significant 的理由 —— 必须有具体事件或变化。
""" + SCORE_OUTPUT_SCHEMA

MACRO_SCORE_PROMPT = r"""
你是A股宏观事件评分器。

只评估 macro 类资讯。

宏观重要不等于A股可交易。macro 默认进入 daily，不默认 deep_analysis。

可高分：
国内流动性、财政、地产、消费、产业政策明显超预期；经济数据明显超预期或不及预期；人民币、利率、关键商品重大变化；对华制裁、关税、出口管制、进口限制；海外事件明确影响中国资产或A股产业链。

注意：
- 海外央行表态不应一律判低分 —— 美联储利率决议、点阵图调整、SEP 变化、缩表节奏变化等直接影响全球流动性和人民币汇率，对A股有明确传导链，按正常标准评分。
- "海外事件"不等于 market_relevance 低。关键判据是能否推出对A股产业链的传导逻辑。例如海外科技公司重大资本开支/Capex 变化、AI 基础设施投资规模调整，如果A股有对应供应链，market_relevance 按正常标准评分。
- 仅以下情况 novelty 偏低：已充分预期的例行讲话、无新增信息的重复表述、不能推出A股产业或流动性方向的风险偏好叙事。

novelty 指南（0-25）：
- 12-25：出现了之前未被预期的宏观变化或政策信号
- 6-11：对已知宏观趋势的增量确认
- 0-5：例行讲话/事件/数据，与市场预期一致

direction 指南（0-25）：
- 8-25：能推出较清晰的A股行业或流动性方向
- 4-7：有大方向但传导链长或不确定
- 0-3：方向模糊

urgency 指南（0-20）：
- 15-20：突发宏观事件/制裁/政策，窗口以小时/天计
- 8-14：重要但可在数日内消化
- 0-7：例行发布/无时间敏感度

reliability 指南（0-15）：
- 10-15：政府正式文件/央行公告/交易所披露
- 6-9：权威媒体/官方渠道
- 0-5：非官方渠道/有待证实

market_relevance 指南（0-15）：
- 8-15：明确影响A股行业/资产/人民币/关键商品链条
- 4-7：间接影响A股，传导链存在但较长
- 0-3：纯海外事件，基本不影响A股

约束：
- 纯海外宏观且不涉及中国资产或A股产业链，market_relevance 5-10。如果存在明确的对中国资产的传导机制（如美联储利率决议→人民币汇率→北向资金），market_relevance 可到10-12。
- direction 对宏观事件通常低于公司事件，但根据实际传导链清晰度评分：能推导到具体A股行业的流动性或成本逻辑的，direction 8-20；仅有方向感觉但传导链不清晰的，direction 4-10。
- market_relevance 15分制：能明确影响A股行业/资产（人民币、关键商品、利率走廊）的 10-15；间接影响的 6-9；纯海外事件 0-5。
- novelty>=12、direction>=8、market_relevance>=8、total>=50 通常可进入 deep_analysis。
- urgent 必须 novelty>=20、urgency>=15、market_relevance>=10、total>=80。
""" + SCORE_OUTPUT_SCHEMA

SCORE_PROMPTS = {
    "company": COMPANY_SCORE_PROMPT,
    "industry": INDUSTRY_SCORE_PROMPT,
    "policy": POLICY_SCORE_PROMPT,
    "concept": CONCEPT_SCORE_PROMPT,
    "macro": MACRO_SCORE_PROMPT,
}

_TYPE_THRESHOLDS = {
    "company": {"novelty": 10, "direction": 8, "market_relevance": 8, "total": 45},
    "industry": {"novelty": 8, "direction": 6, "market_relevance": 8, "total": 45},
    "policy": {"novelty": 10, "direction": 8, "market_relevance": 8, "total": 50},
    "concept": {"novelty": 10, "direction": 8, "market_relevance": 8, "total": 45},
}

_BAD_SIGNAL_TYPES = {"noise"}
_TYPE_THRESHOLDS["macro"] = {"novelty": 12, "direction": 8, "market_relevance": 8, "total": 50}


def _build_news_block(title: str, body: str, source: str, published_at: str, max_body: int = 3000) -> str:
    """构建统一的资讯文本块。"""
    return f"## news\n标题：{title}\n来源：{source}\n时间：{published_at}\n\n正文：\n{body[:max_body]}"


def _build_score_input(
    title: str,
    body: str,
    source: str,
    published_at: str,
    info_type: str,
    context_block: str,
) -> str:
    """构建评分阶段的 user 输入：仅 news + known_context + similar_news，不注入 gate/classification 输出，保证评分独立。"""
    user_content = _build_news_block(title, body, source, published_at, max_body=3000)
    if info_type == "macro":
        user_content += "\n\n" + KNOWN_CONTEXT
    if context_block:
        user_content += "\n\n## similar_news\n" + context_block
    return user_content


def _enforce_gate(gate: dict[str, Any]) -> dict[str, Any]:
    """确保 gate 输出字段存在且类型正确。"""
    gate.setdefault("pass_gate", False)
    gate.setdefault("gate_score", 0)
    gate.setdefault("reason", "")
    if not isinstance(gate["pass_gate"], bool):
        gate["pass_gate"] = bool(gate["pass_gate"])
    if not isinstance(gate["gate_score"], (int, float)):
        try:
            gate["gate_score"] = int(gate["gate_score"])
        except (ValueError, TypeError):
            gate["gate_score"] = 0
    return gate


def _enforce_classification(cls: dict[str, Any]) -> dict[str, Any]:
    """确保分类输出合法。"""
    valid_types = {"company", "policy", "industry", "concept", "macro", "market_noise"}
    info_type = cls.get("info_type", "market_noise")
    if info_type not in valid_types:
        info_type = "market_noise"
    cls["info_type"] = info_type
    return cls


def _empty_result(rationale: str) -> dict[str, Any]:
    """构建空结果。"""
    return {
        "info_type": "market_noise",
        "is_significant": False,
        "is_urgent": False,
        "route": "drop",
        "total_score": 0,
        "scores": {},
        "signal_type": "noise",
        "rationale": rationale,
        "affected_tickers": [],
        "affected_sectors": [],
        "key_entities": [],
        "direction_hint": "neutral",
        "time_horizon": "short_term",
        "gate": {"pass_gate": False, "gate_score": 0, "reason": rationale},
        "classification": {"info_type": "market_noise"},
    }


def _keyword_drop_result(reason: str) -> dict[str, Any]:
    """构建关键词过滤丢弃结果。"""
    return {
        "info_type": "market_noise",
        "is_significant": False,
        "is_urgent": False,
        "route": "drop",
        "total_score": 0,
        "scores": {},
        "signal_type": "noise",
        "rationale": f"关键词预过滤命中: {reason}",
        "affected_tickers": [],
        "affected_sectors": [],
        "key_entities": [],
        "direction_hint": "neutral",
        "time_horizon": "short_term",
        "gate": {"pass_gate": False, "gate_score": 0, "reason": f"关键词预过滤: {reason}"},
        "classification": {"info_type": "market_noise"},
    }


def gate_to_result(gate: dict[str, Any]) -> dict[str, Any]:
    """gate 不通过时直接返回 market_noise 结果。"""
    return {
        "info_type": "market_noise",
        "is_significant": False,
        "is_urgent": False,
        "route": "drop",
        "total_score": gate.get("gate_score", 0),
        "scores": {},
        "signal_type": "noise",
        "rationale": gate.get("reason", "硬闸门拦截"),
        "affected_tickers": [],
        "affected_sectors": [],
        "key_entities": [],
        "direction_hint": "neutral",
        "time_horizon": "short_term",
        "gate": gate,
        "classification": {"info_type": "market_noise"},
    }


_VALID_SIGNAL_TYPES = {"hard_fact", "industry_hard_signal", "soft_signal", "repeat", "heat_noise", "noise"}
_VALID_DIRECTIONS = {"long", "short", "neutral"}
_VALID_HORIZONS = {"short_term", "medium_term", "long_term"}


def _enforce_score(score: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化 scoring 阶段的 LLM JSON 输出。"""
    # --- is_significant / is_urgent ---
    for key in ("is_significant", "is_urgent"):
        if not isinstance(score.get(key), bool):
            score[key] = bool(score.get(key, False))

    # --- total_score ---
    if not isinstance(score.get("total_score"), (int, float)):
        try:
            score["total_score"] = int(score["total_score"])
        except (ValueError, TypeError):
            score["total_score"] = 0

    # --- scores 子维度 ---
    scores = score.get("scores")
    if not isinstance(scores, dict):
        scores = {}
        score["scores"] = scores
    for dim in ("novelty", "direction", "urgency", "reliability", "market_relevance"):
        if not isinstance(scores.get(dim), (int, float)):
            try:
                scores[dim] = int(scores.get(dim, 0))
            except (ValueError, TypeError):
                scores[dim] = 0

    # --- signal_type ---
    st = score.get("signal_type", "")
    if st not in _VALID_SIGNAL_TYPES:
        score["signal_type"] = "noise"

    # --- 枚举字段 ---
    if score.get("direction_hint") not in _VALID_DIRECTIONS:
        score["direction_hint"] = "neutral"
    if score.get("time_horizon") not in _VALID_HORIZONS:
        score["time_horizon"] = "short_term"

    # --- 列表字段 ---
    for key in ("affected_tickers", "affected_sectors", "key_entities"):
        if not isinstance(score.get(key), list):
            score[key] = []

    # --- rationale ---
    if not isinstance(score.get("rationale"), str):
        score["rationale"] = str(score.get("rationale", ""))

    return score


def _merge_gate_cls_score(gate: dict[str, Any], cls: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    """合并 gate、classification、score 为最终结果，保留 gate 和 classification 便于调试。"""
    score["gate"] = gate
    score["classification"] = cls
    # 确保 info_type 以 classify 为准
    score["info_type"] = cls.get("info_type", score.get("info_type", "market_noise"))
    return score


def _normalize_route(result: dict[str, Any]) -> dict[str, Any]:
    """根据 is_significant / is_urgent 规范 route 字段。"""
    if result.get("info_type") == "market_noise":
        if result.get("route") not in ("drop", "archive"):
            result["route"] = "drop"
        return result
    if not result.get("is_significant"):
        if result.get("route") not in ("drop", "archive", "daily"):
            result["route"] = "archive"
        return result
    # is_significant=true
    if result.get("is_urgent"):
        result["route"] = "urgent_analysis"
    else:
        if result.get("route") not in ("deep_analysis", "urgent_analysis", "daily"):
            result["route"] = "deep_analysis"
    return result

def _enforce_macro_constraints(result: dict[str, Any]) -> dict[str, Any]:
    """对 macro 类结果做硬约束兜底。

    1. 非 macro 类不处理
    2. urgent 需要 is_significant=True + novelty>=20、urgency>=15、market_relevance>=10、total>=80
    3. is_significant=False 时强制 is_urgent=False
    """
    if result.get("info_type") != "macro":
        return result
    scores = result.get("scores", {})
    if not result.get("is_significant"):
        result["is_urgent"] = False
    elif not (
        scores.get("novelty", 0) >= 20
        and scores.get("urgency", 0) >= 15
        and scores.get("market_relevance", 0) >= 10
        and result.get("total_score", 0) >= 80
    ):
        result["is_urgent"] = False

    # macro 不再设人工硬天花板，信赖 LLM 的评分和 urgent 门槛的筛选
    # direction 和 market_relevance 仅保留维度理论最大值约束，防止异常值
    scores["direction"] = max(0, min(scores.get("direction", 0), 25))
    scores["market_relevance"] = max(0, min(scores.get("market_relevance", 0), 15))
    result["total_score"] = sum(scores.values())
    result["scores"] = scores
    return result


def _enforce_non_macro_constraints(result: dict[str, Any]) -> dict[str, Any]:
    """对非 macro 类结果做按类型阈值兜底。"""
    info_type = result.get("info_type", "")
    # market_noise 永远不能 significant/urgent
    if info_type == "market_noise":
        result["is_significant"] = False
        result["is_urgent"] = False
        return result
    # 禁止型 signal_type
    signal_type = result.get("signal_type", "")
    if signal_type in _BAD_SIGNAL_TYPES:
        result["is_significant"] = False
    # 按类型阈值兜底
    scores = result.get("scores", {})
    thresholds = _TYPE_THRESHOLDS.get(info_type)
    if thresholds:
        if not (
            scores.get("novelty", 0) >= thresholds["novelty"]
            and scores.get("direction", 0) >= thresholds["direction"]
            and scores.get("market_relevance", 0) >= thresholds["market_relevance"]
            and result.get("total_score", 0) >= thresholds["total"]
        ):
            result["is_significant"] = False
    # is_significant=False 时强制 is_urgent=False（放在阈值兜底之后）
    if not result.get("is_significant"):
        result["is_urgent"] = False
    # macro 的 urgent 已由 _enforce_macro_constraints 处理
    if info_type == "macro":
        return result
    # 非 macro 类型：允许 urgent，但需要更高的门槛
    # 要求 total>=80 且 urgency>=15（与 macro 的 urgent 标准一致）
    if not (
        result.get("total_score", 0) >= 80
        and scores.get("urgency", 0) >= 15
    ):
        result["is_urgent"] = False
    return result


class SignificanceJudge:
    """行业/个股资讯重要性评估"""

    def __init__(self, llm: Any, quant: QuantClient | None = None, clock: Clock | None = None) -> None:
        self._llm = llm
        self._quant = quant
        self._clock = clock

    async def evaluate(self, title: str, body: str, source: str, published_at: str,
                       raw_info_id: str | None = None) -> dict:
        # ---- 空内容兜底 ----
        if not title.strip() and not (body or "").strip():
            return _empty_result("资讯内容为空，默认跳过")

        # ---- 关键词预过滤 ----
        should_discard, reason = _pre_filter(title, body)
        if should_discard:
            log_progress(
                "SignificanceJudge",
                "关键词过滤跳过",
                title=title[:30],
                reason=reason,
            )
            return _keyword_drop_result(reason)

        news_block = _build_news_block(title, body, source, published_at, max_body=3000)

        # ---- Stage 1: hard gate ----
        gate = await self._llm.chat_json([
            {"role": "system", "content": HARD_GATE_PROMPT},
            {"role": "user", "content": news_block},
        ])
        gate = _enforce_gate(gate)

        log_progress(
            "SignificanceJudge",
            "gate完成",
            title=title[:30],
            pass_gate=gate.get("pass_gate"),
            gate_score=gate.get("gate_score"),
        )

        if not gate.get("pass_gate"):
            return gate_to_result(gate)

        # ---- Stage 2: classify ----
        cls = await self._llm.chat_json([
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": news_block},
        ])
        cls = _enforce_classification(cls)

        log_progress(
            "SignificanceJudge",
            "classify完成",
            title=title[:30],
            info_type=cls.get("info_type"),
        )

        # market_noise 直接舍弃，不进入 scoring
        if cls.get("info_type") == "market_noise":
            return {
                "info_type": "market_noise",
                "is_significant": False,
                "is_urgent": False,
                "route": "drop",
                "total_score": 0,
                "scores": {},
                "signal_type": "noise",
                "rationale": "classify 判定为 market_noise，直接舍弃",
                "affected_tickers": [],
                "affected_sectors": [],
                "key_entities": [],
                "direction_hint": "neutral",
                "time_horizon": "short_term",
                "gate": gate,
                "classification": cls,
            }

        # ---- search similar only after gate passes ----
        similar_items = await self._search_similar(title, body, raw_info_id=raw_info_id)
        context_block = _build_context_block(similar_items)

        # ---- Stage 3: per-type scoring ----
        info_type = cls.get("info_type", "market_noise")
        score_prompt = SCORE_PROMPTS.get(info_type)

        score_input = _build_score_input(
            title=title,
            body=body,
            source=source,
            published_at=published_at,
            info_type=info_type,
            context_block=context_block,
        )

        started = perf_counter()
        try:
            log_progress(
                "SignificanceJudge",
                "scoring开始",
                title=title[:30],
                source=source,
                published_at=published_at,
            )
            score = await self._llm.chat_json([
                {"role": "system", "content": score_prompt},
                {"role": "user", "content": score_input},
            ])
            score = _enforce_score(score)

            result = _merge_gate_cls_score(gate, cls, score)
            result = _enforce_macro_constraints(result)
            result = _enforce_non_macro_constraints(result)
            result = _normalize_route(result)

            log_progress(
                "SignificanceJudge",
                "scoring完成",
                title=title[:30],
                info_type=result.get("info_type"),
                is_significant=result.get("is_significant"),
                is_urgent=result.get("is_urgent"),
                score=result.get("total_score", 0),
                elapsed_s=perf_counter() - started,
            )
            return result
        except Exception as e:
            logger.opt(exception=True).error("significance evaluation failed")
            log_progress(
                "SignificanceJudge",
                "回退默认结果",
                level="warning",
                title=title[:30],
                elapsed_s=perf_counter() - started,
            )
            return {
                "info_type": "market_noise",
                "is_significant": False,
                "is_urgent": False,
                "route": "drop",
                "total_score": 0,
                "scores": {},
                "signal_type": "noise",
                "rationale": "评估失败，默认跳过",
                "affected_tickers": [],
                "affected_sectors": [],
                "key_entities": [],
                "direction_hint": "neutral",
                "time_horizon": "short_term",
                "gate": gate,
                "classification": cls,
            }

    def _build_search_query(self, title: str, body: str = "") -> str:
        """构建 BM25 检索词：直接用标题 + 正文全文，ES 服务端 IK Analyzer 负责中文分词。"""
        title = title.strip()
        if body:
            return f"{title} {body}"
        return title

    async def _search_similar(self, title: str, body: str = "",
                              raw_info_id: str | None = None) -> list[dict]:
        if self._quant is None:
            return []
        try:
            query = self._build_search_query(title, body)
            if not query:
                return []
            # 用项目时钟往前推 2 个月作为搜索起点
            date_range = None
            if self._clock is not None:
                two_months_ago = self._clock.now - timedelta(days=60)
                date_range = {"start": two_months_ago.strftime("%Y-%m-%d")}
            req = SearchRequest(
                query_text=query,
                mode="bm25",
                only_tables=["raw_information"],
                date_range=date_range,
                limit=10,
            )
            resp = await self._quant.search.search(req)
            raw_ids = [str(r.id) for r in resp.items]
            if not raw_ids:
                return []
            fetch_req = FetchByIdsRequest(table_ids={"raw_information": raw_ids[:5]})
            fetch_resp = await self._quant.search.fetch_by_ids(fetch_req)
            raw_data = fetch_resp.data.get("raw_information", [])
            # 过滤自身记录：排除与当前 raw_info_id 相同的条目
            return [
                {
                    "result_type": "raw_information",
                    "title": d.get("title", ""),
                    "body": (d.get("body") or d.get("content") or "")[:500],
                }
                for d in raw_data
                if d.get("title") and (raw_info_id is None or str(d.get("id")) != str(raw_info_id))
            ]
        except Exception:
            logger.debug("相似资讯检索失败，跳过上下文注入")
            return []


def _build_context_block(similar_items: list[dict]) -> str:
    if not similar_items:
        return ""
    lines = ["## 近期已处理的相似资讯 (similar_news)"]
    for item in similar_items:
        rt = item.get("result_type", "unknown")
        body = item.get("body", "")
        line = f"- [{rt}] {item['title']}"
        if body:
            line += f"\n  {body[:200]}"
        lines.append(line)
    lines.append("\n**重要**：如果当前资讯与上述任一资讯描述的是同一事件或同一信号，说明该信息已经被处理过。请在评分时对 novelty 和 urgency 各降低3-8分。如果该事件出现了实质性转折、升级、落地或新的定量数据，则不适用本条，按正常标准评分。不要因相似资讯的存在而对其他维度（direction、reliability、market_relevance）降分。")
    return "\n".join(lines)
