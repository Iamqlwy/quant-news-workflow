"""宏观分析 Agent —— 3 阶段 ReAct (LangChain)

市场图表（四大指数 120 天日线同图）和宏观报告历史由 _prepare_context 自动预取注入上下文，
Agent 无需调用图表工具或报告查询工具，直接阅读上下文即可。
统一处理紧急宏观资讯（urgent）和日终宏观汇总（daily），区别仅在于输入内容。

子类扩展点：
- _prepare_context(): 在分析前预处理上下文（如注入额外宏观数据）
- _build_overall_prompt(): 动态修改 overall prompt
"""

import json
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from src.agents.base import StageAgent, _execute_tool_calls, _set_task_ctx
from src.tools.registry import get
from src.workflow_logging import log_progress

SYS_OVERALL = """你是一份宏观状态文档的维护者。你的任务不是写日报，而是维护一份持续更新的宏观全景记录——让后续阅读者一眼就能看到：当前宏观处于什么状态、哪些力量正在驱动市场、哪些在减弱、本期有什么新变化。

3个阶段（市场数据已预注入上下文）：
1. **背景检索** → 先看图识别市场状态，再阅读上一期宏观报告和历史判断，识别什么力量在持续、什么已经过期
2. **综合研判** → 结合图表证据判断本期信息对宏观定位的冲击，评估新力量的持续性和旧力量的存续状态
3. **更新报告** → 增量修改宏观文档：激活新力量、标记失效力量、更新定位——不改动无关内容

关键原则：
- 宏观力量有生命周期——从"出现"到"发酵"到"落地/证伪"到"失效"。报告必须追踪这个周期。
- 如果上期报告中某个判断本期没有新信息，但影响仍在，它就仍然有效，必须保留。
- 力量失效包括：政策落地（事件发生）、数据反转（趋势改变）、市场充分定价（price in）、时间窗口过期。
- 只分析宏观维度（货币/信用/增长/外部），不涉及个股和行业。"""

SYS_RESEARCH = """【当前阶段：背景检索】
【当前阶段可用工具】search_kb、read

检索宏观背景信息。系统已在阶段开始时注入了：
- 完整的上一期宏观报告全文
- 近期宏观报告历史全文
- 四大指数（上证/深证/创业板/科创50）120天日线同图，每个子图含 K 线（归一化涨跌幅）、MA5/MA10/MA20 均线和每日子图标题含日涨跌幅

按以下顺序严格执行，不要跳过图表阅读：

### 第一步（必做）：阅读并描述图表
先仔细查看四大指数同图，在输出中以"【图表观察】"开头，逐指数描述以下内容：
- 各指数近期的整体趋势方向（上升/下降/震荡）及趋势启动时间
- 最近 1-5 个交易日的走势（涨/跌/横盘，各自的日涨跌幅）
- MA5、MA10、MA20 三条均线的相对位置和方向（多头排列/空头排列/粘合/交叉），是否出现金叉或死叉
- 各指数之间的相对强弱——哪个指数领涨、哪个最弱、是否有明显分化

### 第二步：阅读宏观报告
通读上一期宏观报告全文，对照历史摘要，识别：
  - 哪些宏观力量仍在生效（上期报告 Section 2 中的条目）
  - 哪些已经过期/落地/被市场定价
  - 哪些需要更新强度（增强/减弱/方向变化）

### 第三步：交叉验证
将图表中的市场表现与宏观报告中的判断做比对：
- 市场走势是否印证了某些宏观力量的判断？哪些力量在图表中找到支撑信号？
- 哪些力量与当前市场表现矛盾——报告判断偏乐观但指数疲软，或报告偏谨慎但指数强势？
- 各指数之间的分化（如小盘 vs 大盘、成长 vs 价值）是否符合宏观逻辑？
- 如有本期资讯，搜索相关历史宏观判断和类似案例

收敛规则：
- 理想迭代：2-4次。6次是安全上限。图表已经在上下文中，不需要用工具重新获取。
- 输出以"研究总结："开头，按 (1)仍在生效的力量 (2)可能过期/需要更新的力量 (3)本期新发现 三点归纳。"""

SYS_ANALYZE = """【当前阶段：综合研判】
【当前阶段可用工具】无。这是纯推理阶段，不能调用任何工具。

结合上下文中的指数图表和研究总结，按以下顺序综合研判：

1. **存量力量审计** —— 逐条审查上一期报告的 Section 2（仍在生效的宏观力量）：
   - 每条力量当前状态：继续有效 / 正在减弱 / 已经失效？
   - 失效原因：政策落地？数据反转？市场已定价？时间窗口过期？
   - **图表证据**：对照四大指数图表，为每条力量判断提供行情层面的证据——行情趋势是否与力量逻辑一致？市场是否已在定价该力量？必须在每条力量的分析中引用具体的指数走势信息（涨跌方向、均线状态、近期变化）。

2. **宏观定位冲击** —— 本期信息对货币/信用/增长/外部四个维度的冲击：
   - 哪个维度被改变？程度多大？
   - 是一个新趋势的起点，还是已有趋势的延续？

3. **传导路径** —— 从宏观到资产端的传导：
   - 通过什么渠道影响？（利率、汇率、信用利差、风险偏好）
   - 传导链的确定性和时滞？

4. **本期力量分级** —— 对本期发现的新力量/变化，判断其持续性：
   - 结构性（持续数月以上）：政策转向、经济周期拐点、制度变革
   - 周期性（数周到数月）：数据趋势、政策节奏、外部冲击
   - 事件性（数天到数周）：一次性政策公布、数据发布、地缘事件

5. **待观察清单更新** —— 什么信号出现时会触发下次宏观定位更新？"""

SYS_WRITE = """【当前阶段：更新总结】
【当前阶段可用工具】update_macro_report、create_node、update_node_state、append_market_preference

将研判结论落地到系统。

## 宏观报告模板

update_macro_report 的 content 必须是完整的五段式 Markdown。以下为固定格式，不得增删章节：

```
## 1. 当前宏观定位

货币维度：一句话（如"中性偏松，降准预期仍在但尚未落地"）
信用维度：一句话（如"社融增速企稳，但结构偏弱"）
增长维度：一句话（如"弱复苏，制造业 PMI 连续两月在荣枯线以上"）
外部维度：一句话（如"美联储加息预期见顶，人民币贬值压力缓解"）

## 2. 仍在生效的宏观力量

（每一条格式：`[状态] 力量名称（来源版本）：当前进展和影响。`）
状态标记：[活跃] / [减弱] / [即将落地] / [已落地待消化]
来源版本：该力量首次出现时的版本号（如 v3）。如果来自本期新增，则标注 v{本期}
示例：
  [活跃] 降准预期（v3）：央行暗示降准，市场计入 25-50bp。尚未落地，仍在发酵。
  [减弱] 出口下滑（v7）：12 月出口数据疲软。最新 PMI 新出口订单回升，此力量正在减弱。
  [已落地待消化] LPR 下调 10bp（v5）：已落地。市场正在消化对银行息差的影响，预计一周内转为常规状态。

继承规则：
- 上一期 Section 2 中的力量，如果研判认为"继续有效"或"减弱"，必须保留并更新状态和描述。
- 上一期力量如果"已落地且充分消化"或"数据反转推翻了原判断"，可移除，不保留。
- 本期新增力量 → 来源版本标注为 v{本期}

## 3. 本期新变化

本期新增的宏观信号或判断修正（每条 1-2 句）。
如果本期没有任何新变化，写"本期无新增宏观信号"。

## 4. 资产观点

大类资产方向判断：
- 权益：一句话，含方向（看好/中性/谨慎）和关键理由
- 债券：一句话
- 汇率：一句话
- 商品：一句话（如无判断可省略）

## 5. 待观察清单

未来 1-4 周需要密切跟踪的信号（3-5 条，每条包含触发条件和预期行动）。
```
"""


class MacroAgent(StageAgent):
    """宏观分析 Agent —— 3 阶段 ReAct

    urgent 和 daily 共用同一个 Agent 类，区别仅在于输入上下文（macro_type 字段）。
    """

    def __init__(self, make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> None:
        super().__init__(
            make_chat_model=make_chat_model,
            overall_system_prompt=SYS_OVERALL,
            stages=[
                {
                    "name": "背景检索",
                    "system_prompt": SYS_RESEARCH,
                    "tools": get("search_kb", "read"),
                    "max_iterations": 6,
                },
                {
                    "name": "综合研判",
                    "system_prompt": SYS_ANALYZE,
                    "tools": [],
                    "max_iterations": 2,
                },
                {
                    "name": "更新总结",
                    "system_prompt": SYS_WRITE,
                    "tools": get(
                        "create_node",
                        "update_node_state",
                        "update_macro_report",
                        "append_market_preference",
                    ),
                    "max_iterations": 5,
                },
            ],
            **kwargs,
        )
        self.max_context_tokens = 1_000_000



    async def _init_session(self, context: dict[str, Any]) -> tuple[list, str, str]:
        """跳过实体加载（macro 上下文由调用方直接注入），只设置 task context 供工具使用"""
        from src.tools import reset_session_registry

        if self._reset_registry:
            reset_session_registry()

        _set_task_ctx(context)

        text_content = json.dumps(context, ensure_ascii=False, indent=2, default=str)
        messages: list = []
        if self.overall_system_prompt:
            messages.append(SystemMessage(content=self.overall_system_prompt))
        messages.append(HumanMessage(content=text_content))

        agent_label = self.__class__.__name__
        task_id = context.get("task_id") or "-"
        log_progress(agent_label, "开始", task_id=task_id, stage_count=len(self.stages))
        self.messages = messages
        self.tool_use = set()
        return messages, str(task_id), agent_label

    async def _on_stage_start(self, stage_name: str, stage_index: int, messages: list) -> None:
        """在第一个阶段开始时注入宏观报告全文、历史摘要和四大指数图表"""
        if stage_index != 0:
            return

        from collections import defaultdict

        from src.tools.context import get_ctx

        ctx = get_ctx()
        quant = ctx.quant
        market = ctx.market
        task_id = str(self._task.id) if self._task else "?"

        try:
            from src.utils.http_resilience import retry_api_call
            history = await retry_api_call(
                lambda: quant.macro_report.get_history(limit=20),
                name="预取宏观报告历史",
                task_id=task_id,
            )
            # 按天分组，每天取最后一个版本
            by_day = defaultdict(list)
            for item in history.items:
                if item.updated_at:
                    day = item.updated_at.date()
                    by_day[day].append(item)
            # 每天取最后一个版本（version最大），然后取最近5天
            last_per_day = []
            for day in sorted(by_day.keys(), reverse=True):
                latest = max(by_day[day], key=lambda x: x.version)
                last_per_day.append(latest)
                if len(last_per_day) >= 5:
                    break

            history_lines = []
            for item in last_per_day:
                updated = item.updated_at.isoformat() if item.updated_at else ""
                history_lines.append(f"--- v{item.version}（更新于 {updated}）---\n{item.content}")
            history_text = (
                "【近期宏观报告历史】\n\n" + "\n\n".join(history_lines)
                if history_lines
                else "【近期宏观报告历史】暂无"
            )
        except Exception as e:
            logger.warning("预取宏观报告历史失败: {} ({}) [task={}]", e, type(e).__name__, task_id)
            history_text = "【近期宏观报告历史】暂无"

        messages.append(SystemMessage(content=history_text))

        # 预取四大指数图表
        try:
            import time

            from src.market.charts import generate_multi_index_chart
            from src.utils.oss_uploader import upload_bytes

            png_bytes = generate_multi_index_chart(market)
            url = upload_bytes(png_bytes, f"charts/multi_index/macro_{int(time.time() * 1000)}.png")
            messages.append(HumanMessage(content=[
                {"type": "text", "text": "【四大指数 120 天日线同图】"},
                {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
            ]))
        except Exception as e:
            logger.warning("预取市场图表失败: {} ({}) [task={}]", e, type(e).__name__, task_id)

    async def end(self) -> None:
        """如果模型在阶段中未调用 update_macro_report 或 append_market_preference，则补调"""
        missing: list[str] = []
        if "update_macro_report" not in self.tool_use:
            missing.append("update_macro_report")
        if "append_market_preference" not in self.tool_use:
            missing.append("append_market_preference")
        if not missing:
            return

        missing_str = " 和 ".join(missing)
        llm = self._make_chat_model()
        stage_tools = self._get_stage_tools("更新总结", self.stages[2].get("tools", []))
        llm_with_tools = llm.bind_tools(stage_tools, parallel_tool_calls=True)
        self.messages.append(
            HumanMessage(
                content=f"（你尚未调用 {missing_str}。请现在调用它们，将分析结果写入系统。只需调用工具即可。）"
            )
        )
        try:
            response = await llm_with_tools.ainvoke(self.messages)
            self.messages.append(response)
            if response.tool_calls:
                for tc in response.tool_calls:
                    self.tool_use.add(tc["name"])
                await _execute_tool_calls(
                    response.tool_calls,
                    stage_tools,
                    "更新总结",
                    f"{self.__class__.__name__}:更新总结",
                    str(self._task.id) if self._task else "?",
                    self.messages,
                )
            else:
                logger.warning(
                    "end() 补调 LLM 未返回工具调用，{} 可能未写入 [task={}]",
                    missing_str, self._task.id if self._task else "?",
                )
        except Exception as e:
            logger.warning("end() 补调 {} 失败: {} ({}) [task={}]", missing_str, e, type(e).__name__, self._task.id if self._task else "?")


def create_macro_agent(make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> MacroAgent:
    return MacroAgent(make_chat_model, **kwargs)
