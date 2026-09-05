"""复盘 Agent —— StageAgent 子类（组合模式：内部编排多个子 agent + Tree of Thought）

子类扩展点：
- _prepare_context(): 在复盘前预处理上下文
- _build_overall_prompt(): 动态修改 overall prompt
- run(): 完全覆盖，自定义编排流程（当前已覆盖）

注意：传入子 agent 上下文的 key 不能以 _ 开头，否则会被 _build_llm_context 过滤。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from src.agents.base import StageAgent
from src.agents.tree_of_thought import TreeOfThoughtAgent
from src.observability import safe_observation_value, start_observation
from src.tools import get_session_registry, reset_session_registry
from src.tools.registry import get


def _make_write_filter():
    """返回一个 _filter_tool_calls 兼容的回调，限制 create_feedback / append_preference 各最多调用 1 次（跨迭代累计）。"""
    _max = {"create_feedback": 1, "append_preference": 1}
    _counts: dict[str, int] = {}

    def _filter(_self: Any, stage_name: str, response: Any) -> list:
        accepted: list = []
        blocked: list = []
        for tc in (response.tool_calls or []):
            name = tc.get("name", "")
            limit = _max.get(name)
            if limit is not None:
                cnt = _counts.get(name, 0)
                if cnt >= limit:
                    blocked.append(name)
                    continue
                _counts[name] = cnt + 1
            accepted.append(tc)
        response.tool_calls = accepted
        if blocked:
            return [HumanMessage(
                content=f"工具调用被拦截：{', '.join(blocked)} 已达到本阶段最大调用次数。请基于已有数据继续。"
            )]
        return []

    return _filter

SYS_OVERALL = """你是交易复盘Agent。深度分析到了复盘时间，回顾当时判断并对比实际走势。

4个阶段：
1. **回顾原始分析** → 获取分析报告、交易记录、WorldNode状态
2. **市场验证** → 拉取复盘区间价格走势、市场快照、相关资讯
3. **Tree of Thought深度复盘** → 分叉假设、独立验证、数据裁决、提炼经验
4. **结果落地** → 写入复盘报告、更新行业偏好

每个阶段完成后输出总结，系统自动推进到下一阶段。

每个阶段应在 2-5 次工具调用内完成并输出总结。完成比完美重要，不要因追求穷尽而反复调用工具。
批量调用：同一阶段中如果需要多个数据（如同时 read 多份报告、同时拉取多个图表），请在一次回复中批量调用，减少轮次。"""

SYS_REVIEW = """【当前阶段：回顾原始分析】
【可用工具】search_kb、read、get_node_history

回顾当时的判断：

- 分析报告和交易记录：用 search_kb 搜索后续资讯，用 read 读取正文
- 获取涉及 WorldNode 的历史状态变化（get_node_history），对比当时状态和当前状态的差异

明确：当时的核心论点？关键驱动因素？预期时间窗口？

收敛规则：
- 理想迭代：2-3次。7次是安全上限，不是目标。
- 先 search_kb 定位相关实体，再 read 读取正文。
- 仅当需要了解节点状态演变过程时调用 get_node_history。
- 完成后输出回顾总结，停止调用工具。"""

SYS_MARKET_VERIFY = """【当前阶段：市场验证】
【可用工具】get_market_snapshot、get_price_chart、get_technical_chart、get_multi_index_chart

获取复盘区间内的实际情况：

- 标的价格走势与技术指标：建议不填 from_date/to_date（默认最近240个交易日，涵盖足够技术指标历史）；若填则至少覆盖20个交易日
- 复盘时点市场快照：仅支持最近约30个交易日，不要传入复盘区间之外的过远日期
- 四大指数同图（get_multi_index_chart）：确认大盘环境，判断标的走势是独立行情还是跟随大盘

验证重点：
- 价格实际走向与分析预期是否一致？
- 关键驱动因素是否真的驱动了价格？
- 影响链各环节实际发生了什么？

收敛规则：
- 理想迭代：2-3次。5次是安全上限，不是目标。
- 获取价格走势+市场快照后即可输出验证总结，不要反复获取不同参数。
- 输出以"市场验证总结："开头，停止调用工具。"""

SYS_WRITE = """【当前阶段：复盘结果落地】
【可用工具】create_feedback、append_preference

将复盘结论落地到系统：

【必做】
- create_feedback：写入复盘报告（**仅调用一次**，将 review_summary、market_summary、tot_conclusion 合并为一份完整的复盘报告）

【按需】（根据复盘结论判断是否需要）
- append_preference：增量更新相关行业的偏好认知（**同一 sector 仅调用一次**，不要对同一行业反复追加；合并后再写入）

收敛规则：
- 理想迭代：2-3次。4次是安全上限，不是目标。
- 第一步调 create_feedback（必做，只调一次），第二步判断是否需要偏好更新。
- 所有操作处理完毕后，输出最终确认，停止调用工具。"""


# ── Tree of Thought 各阶段提示词 ──────────────────────

TOT_COLLECT_PROMPT = """你是数据收集器。复盘需要客观数据来支撑因果分析。

根据回顾总结和市场初步验证，收集后续分析需要的关键数据。最多3轮工具调用。

输出一份结构化的"数据报告"：
- 关键价格数据点（时间、价格、涨跌幅）
- 市场环境判断（牛/熊/震荡，板块表现）
- 技术指标关键信号
- **实际走势与分析预期的差异点**（加粗标注）——这是后续分叉的核心依据"""

TOT_BRANCH_PROMPT = """你是交易复盘分析师。基于实际市场数据和当时分析的对比，找出"实际走势与分析预期的差异"，并为每个差异生成一个可验证的因果假设。

返回 JSON 数组：
```json
[
  {
    "id": "A",
    "hypothesis": "因素X主导了走势，因为数据中观察到[具体数据点]"
  }
]
```

规则：
- 每个假设必须引用具体数据点（"价格在X日下跌Y%"、"板块Z同期上涨W%"）
- 假设之间应互斥或覆盖不同角度
- 2-3个假设
- 每个假设一句话说清因果链
- 只返回 JSON 数组，不要其他内容"""

TOT_DEEPEN_PROMPT = """你是交易复盘分析师。针对一个具体的因果假设，向下深挖一层。

追问：如果这个假设成立，那么：
1. 当时的分析遗漏了什么关键信息或逻辑环节？
2. 应该能在数据中观察到什么额外信号？
3. 这个假设的"必要条件"是什么——如果假设为真，哪些数据必须呈现特定模式？

输出一段文字（200字以内），直接回答以上问题。不要评价假设本身是否正确。"""

TOT_VERIFY_PROMPT = """你是交易复盘验证员。用数据检验一个具体假设。

根据假设内容和深化分析，调用工具获取能验证或推翻该假设的数据。
- 假设说"因素P主导"→ 查因素P相关数据
- 假设说"遗漏了因素Q"→ 查因素Q在复盘区间的表现
- 每次工具调用后评估：数据支持还是削弱了该假设？

最多3轮工具调用，最后输出验证总结（200字以内）：
- 假设是否正确？
- 关键证据（引用具体数据）
- 置信度建议（高/中/低）"""

TOT_SCORE_PROMPT = """你是交易复盘裁决员。根据每个假设及其验证结果打分。

返回 JSON 数组：
```json
[{"id": "A", "confidence": 0.85, "verdict": "verified", "reasoning": "一句话理由"}]
```

打分标准：
- 0.8-1.0：数据明确支持该假设
- 0.6-0.8：数据部分支持，但仍有疑点
- 0.4-0.6：证据不足，无法判断
- 0.0-0.4：数据推翻该假设

只返回 JSON 数组。"""

TOT_SYNTHESIZE_PROMPT = """你是交易复盘总结员。综合树搜索的全部结果，输出最终复盘结论。

需要回答：
1. **判断对/错？** 当时的分析预判是否正确？先验证大盘环境，再判定分析对错
2. **最可能原因？** 综合考虑所有存活假设，根本原因是什么？
3. **遗漏了哪些因素？** 被推翻的假设提供了"不应归因于什么"的信息
4. **对行业认知的新认识？** 这次复盘对后续该行业的分析有什么指导？

用数据说话，不臆测。"""


class ReflectionAgent(StageAgent):
    """复盘 Agent：组合模式 —— 内部编排多个子 agent + Tree of Thought。

    覆盖 run() 实现自定义 4 阶段流程：
    1. 回顾原始分析（StageAgent 子 agent）
    2. 市场验证（StageAgent 子 agent）
    3. Tree of Thought 深度复盘（TreeOfThoughtAgent）
    4. 结果落地（StageAgent 子 agent）
    """

    def __init__(self, make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> None:
        # 不传 stages —— run() 完全覆盖，不走标准阶段循环
        super().__init__(make_chat_model=make_chat_model, overall_system_prompt=SYS_OVERALL, **kwargs)
        self._review_tools = get("search_kb", "read", "get_node_history")
        self._market_tools = get("get_market_snapshot", "get_price_chart", "get_technical_chart", "get_multi_index_chart")
        self._tot_tools = get("search_kb", "read", "get_market_snapshot", "get_technical_chart", "get_multi_index_chart")
        self._write_tools = get(
            "create_feedback", "append_preference"
        )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        # 子 agent 的 content 现在返回 list[str]，取最后一段作为字符串
        def _content_str(result: dict) -> str:
            c = result.get("content", "")
            return c[-1] if isinstance(c, list) and c else (c if isinstance(c, str) else "")
        with start_observation(
            name=self.__class__.__name__,
            as_type="agent",
            input=safe_observation_value(context),
            metadata={"agent_kind": "reflection"},
        ) as agent_obs:
            reset_session_registry()

            # 1. 先从 task/clock 注入 ID 和时间戳 → 2. 再注册实体短引用
            # 顺序不可交换：_register_entities 依赖 context 中已有 analysis_ids/trade_ids/feedback_ids
            self._inject_task_context(context)
            self._register_entities(context)

            # 调用 _prepare_context 钩子（子类扩展点，在 context 就绪后、拉取实体正文前）
            await self._prepare_context(context)

            # 预取宏观报告摘要（通过子 agent 的 on_stage_start 钩子注入，不放入 context）
            try:
                from src.tools.context import get_ctx
                quant = get_ctx().quant
                from src.utils.http_resilience import retry_api_call
                report = await retry_api_call(
                    lambda: quant.macro_report.get_current(),
                    name="预取宏观报告",
                    task_id=str(context.get("task_id", "?")),
                )
                macro_backdrop = report.summary if (report and hasattr(report, "summary") and report.summary) else "暂无宏观报告"
            except Exception as exc:
                logger.warning("预取宏观报告失败: {} ({})", exc, type(exc).__name__)
                macro_backdrop = "暂无宏观报告"

            enriched_context = await self._build_enriched_context(context)
            self._set_task_context(context)

            try:
                # ── 阶段 1：回顾原始分析 ──────────────────────
                with start_observation(name="phase:回顾原始分析", as_type="chain"):
                    async def _inject_macro_review(_self: Any, _stage_name: str, stage_index: int, messages: list) -> None:
                        if stage_index == 0:
                            messages.append(SystemMessage(content=f"【当前宏观环境】\n{macro_backdrop}"))

                    review_agent = self._create_sub_agent(
                        stages=[
                            {
                                "name": "回顾原始分析",
                                "system_prompt": SYS_REVIEW,
                                "tools": self._review_tools,
                                "max_iterations": 7,
                            },
                        ],
                        on_stage_start=_inject_macro_review,
                    )
                    review_result = await review_agent.run(enriched_context)
                    review_summary = _content_str(review_result)

                # ── 阶段 2：市场验证 ──────────────────────────
                with start_observation(name="phase:市场验证", as_type="chain"):
                    market_agent = self._create_sub_agent(
                        stages=[
                            {
                                "name": "市场验证",
                                "system_prompt": SYS_MARKET_VERIFY,
                                "tools": self._market_tools,
                                "max_iterations": 5,
                            },
                        ],
                    )
                    # 注意：key 不能以 _ 开头，否则被 _build_llm_context 过滤
                    market_result = await market_agent.run({**enriched_context, "review_summary": review_summary})
                    market_summary = _content_str(market_result)

                # ── 阶段 3：Tree of Thought 深度复盘 ──────────
                with start_observation(name="phase:TreeOfThought深度复盘", as_type="chain"):
                    tot = TreeOfThoughtAgent(
                        make_chat_model=self._make_chat_model,
                        collect_prompt=TOT_COLLECT_PROMPT,
                        collect_tools=self._tot_tools,
                        max_collect_iterations=3,
                        generate_prompt=TOT_BRANCH_PROMPT,
                        deepen_prompt=TOT_DEEPEN_PROMPT,
                        verify_prompt=TOT_VERIFY_PROMPT,
                        verify_tools=self._tot_tools,
                        max_verify_iterations=3,
                        score_prompt=TOT_SCORE_PROMPT,
                        synthesize_prompt=TOT_SYNTHESIZE_PROMPT,
                        max_branches=3,
                        confidence_threshold=0.6,
                    )
                    tot_result = await tot.run(
                        {
                            "review_summary": review_summary,
                            "market_summary": market_summary,
                        }
                    )
                    tot_conclusion = _content_str(tot_result)

                # ── 阶段 4：结果落地 ──────────────────────────
                with start_observation(name="phase:结果落地", as_type="chain"):
                    write_agent = self._create_sub_agent(
                        stages=[
                            {
                                "name": "复盘结果落地",
                                "system_prompt": SYS_WRITE,
                                "tools": self._write_tools,
                                "max_iterations": 4,
                            },
                        ],
                        on_filter_tool_calls=_make_write_filter(),
                    )
                    write_result = await write_agent.run(
                        {
                            **enriched_context,
                            "review_summary": review_summary,
                            "market_summary": market_summary,
                            "tot_conclusion": tot_conclusion,
                        }
                    )

                result = {"content": write_result.get("content", []), "entities": get_session_registry()}
                if agent_obs is not None:
                    agent_obs.update(output=safe_observation_value(result))
                return result
            except Exception as exc:
                if agent_obs is not None:
                    agent_obs.update(level="ERROR", status_message=str(exc), output={"error": str(exc)})
                raise


def create_reflection_agent(make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> ReflectionAgent:
    return ReflectionAgent(make_chat_model, **kwargs)
