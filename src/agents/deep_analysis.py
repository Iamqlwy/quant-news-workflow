"""深度分析 Agent —— 4 阶段 ReAct (LangChain)

子类扩展点：
- _prepare_context(): 在分析前预处理上下文（如注入额外背景信息）
- _build_overall_prompt(): 动态修改 overall prompt
"""

from collections.abc import Callable
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.base import StageAgent
from src.tools.registry import get

# ── 早退检测 ───────────────────────────────────────────
# 模型被要求输出 "【无深度分析价值】原因：xxx"，但实际可能使用略有差异的措辞/格式。
# 该正则捕获核心语义信号，作为精确匹配的回退。
_EARLY_EXIT_RE = re.compile(
    r"(?:"
    r"[\[\(\（\【]?无(?:需|须)?深度分析(?:价值|意义|必要|条件)?[\]\)\）\】]?"
    r"|"
    r"不(?:具备|值得|需要?)深度分析"
    r"|"
    r"没有深度分析(?:的)?(?:价值|必要|意义)"
    r")",
    re.IGNORECASE,
)
# ─────────────────────────────────────────────────────


SYS_OVERALL = """你是一个A股深度分析Agent。对输入的金融资讯按以下4阶段依次处理：

1. **知识库检索** → 搜索历史资讯、过往分析、类似案例、WorldNode状态、行业偏好
2. **反向证伪** → 针对资讯中的关键论断，主动搜索可能推翻它的证据
3. **综合分析** → 方向判断、影响链、风险、交易建议
4. **结果落地** → 写入分析报告、更新WorldNode、生成交易建议

每个阶段完成后输出总结，系统自动推进到下一阶段。

【硬性规则】
每个阶段只能使用该阶段 SystemMessage 中列出的"可用工具"。不在列表中的工具一律不可调用——调用会报错并浪费迭代次数。

【关键原则】
- 批量调用：同一轮中需要调用多个互不依赖的工具时，请在一次回复中同时发出多个工具调用
- 研究不必穷尽：3-5次高质量搜索通常已足够，不要用微变关键词反复搜索同一实体
- 完成比完美重要：用已有信息做出判断即可，不要因追求完美而反复搜索
"""

# 阶段 system prompt

SYS_RESEARCH = """【当前阶段：研究(知识库)】
【当前阶段可用工具】search_kb、read、get_preferences

在知识库中对这条资讯进行全面检索。

系统已预注入"市场全局偏好认知"（风格轮动、风险偏好、板块方向等），分析时请结合当前市场环境。

检索内容：
- 搜索相关历史资讯、过往分析、类似案例
- 获取涉及的WorldNode的投资逻辑
- 拉取相关行业的偏好认知文本
- 搜索历史经验教训

对资讯中每个关键实体逐一搜索，覆盖相关的行业和概念。完成后输出研究总结。

收敛规则：
- 理想迭代：4-6次。12次是安全上限，不是目标
- 对同一实体不要用微变关键词反复搜索——2次没找到就说明知识库中信息不足
- 每轮工具调用后自问：关键实体都已搜索了吗？高相关文章都读了吗？→ 如果是，立即输出研究总结
- 输出以"研究总结："开头，停止调用工具

例外：以下情况应输出"【无深度分析价值】原因：xxx"并停止工具调用，系统将跳过后续阶段：
1. 资讯确实无深度分析价值（如股价异动无关联逻辑、回购公告不改变任何节点判断、例行公告无新增信息等，无法强映射到任何A股公司）
2. 若输入中包含"本次重点关注"（focus_on），说明该分析由触发器自动触发。检索后如发现资讯与关注点相比无实质变化、无需更新任何分析结论，同样输出"【无深度分析价值】"早退"""

SYS_FALSIFY = """【当前阶段：反向证伪】
【当前阶段可用工具】search_kb、read

基于上一阶段的研究结果，主动寻找可能推翻这条资讯逻辑的证据：

- 提炼资讯中的 2-3 个核心论断（如"X 将导致 Y 上涨"、"政策 Z 利好行业 W"）
- 对每个论断，用 search_kb 搜索相反的证据——历史上类似的论断是否被证伪过？
- 重点搜索 feedback 表，看类似场景的复盘结论中有没有"判断错误"的教训
- 对每个关键实体，搜索是否有被忽略的利空因素

目的不是否定资讯，而是确保综合分析时能看到完整的图景。完成后输出证伪总结：哪些论断找到了反面证据，哪些暂时找不到。

收敛规则：
- 理想迭代：3-5次。8次是安全上限，不是目标
- 每个论断 1-2 次反向搜索即可，不要穷举所有反查询角度
- 连续2次搜索没找到有力反面证据 → 直接记录"暂未发现明显证伪信息"
- 输出以"证伪总结："开头，格式：论断→反面证据[有/无]→具体内容"""

SYS_ANALYZE = """【当前阶段：综合分析】
【当前阶段可用工具】无（纯推理阶段）
【注意】本阶段只有 1 次响应机会，必须一次性输出完整分析

基于所有信息进行综合分析。严格按以下框架输出：

1. **方向判断**：做多/做空/中性，给出置信度
2. **影响链**：从源头到行业到公司的传导路径
3. **关键驱动因素**：什么将决定这个判断正确与否
4. **风险评估**：最大的不确定性在哪里（结合反向证伪的发现）
5. **时间维度**：短期(<1周) / 中期(1-4周) / 长期(>1月)
6. **交易建议**：方向、标的、仓位逻辑、止损止盈（信号不明确可跳过）
7. **反向观点**：最强的反驳理由（引用反向证伪阶段找到的具体证据）
8. **监控计划**：后续追踪什么来验证或证伪
9. **复盘建议**：建议多长时间后复盘，为什么

对每个受影响的WorldNode，给出状态更新建议（core_logic / primary_drivers / risks / focus_points）"""

SYS_WRITE = """【当前阶段：落地(写入)】
【当前阶段可用工具】create_analysis、update_node_state、create_trade、create_node、search_kb

将分析结论落地到系统：

【必做】
- create_analysis：将阶段3的综合分析全文写入知识库

【按需】（根据阶段3的分析结论判断是否需要）
- update_node_state：如果分析改变了对某个WorldNode的认知，增量更新（只改有变化的字段，其余留空）
- create_trade：仅当方向判断明确且置信度≥0.6时创建。不确定时不交易
- create_node：仅在发现知识库中不存在的新标的/概念/政策主题时创建
- search_kb：仅在写入过程中缺乏节点名称时才补充查询，不要重复搜索

收敛规则：
- 理想迭代：3-6次。10次是安全上限，不是目标
- 先调 create_analysis（必做），再逐个判断其他操作是否必要
- 每写入一个实体后检查：是否还有未落地的结论？没有则立即完成
- 所有必做+按需操作处理完毕后，输出最终确认，停止调用工具
"""


class DeepAnalysisAgent(StageAgent):
    """深度分析 Agent —— 4 阶段 ReAct

    子类可覆盖 _prepare_context() 在分析前注入额外上下文，
    或覆盖 _build_overall_prompt() 修改 overall system prompt。
    """

    def _filter_tool_calls(self, stage_name: str, response: Any) -> list:
        """落地阶段限制 create_analysis 最多 1 次（跨迭代累计）。"""
        if stage_name != "落地(写入)":
            return []
        accepted: list = []
        blocked: list = []
        for tc in (response.tool_calls or []):
            name = tc.get("name", "")
            if name == "create_analysis":
                cnt = self._filter_counts.get(name, 0)
                if cnt >= 1:
                    blocked.append(name)
                    continue
                self._filter_counts[name] = cnt + 1
            accepted.append(tc)
        response.tool_calls = accepted
        if blocked:
            return [HumanMessage(
                content=f"工具调用被拦截：{', '.join(blocked)} 已在本阶段调用过，跳过。请基于已有数据继续。"
            )]
        return []

    def _should_early_exit(self, stage_name: str, stage_index: int, output: str) -> bool:
        """研究阶段（S1）结束后检查：如果资讯无深度分析价值，跳过剩余阶段。"""
        if stage_name != "研究(知识库)":
            return False
        return "【无深度分析价值】" in output or bool(_EARLY_EXIT_RE.search(output))

    async def _on_stage_start(self, stage_name: str, stage_index: int, messages: list) -> None:
        """在第一个阶段开始时注入市场全局偏好认知和触发器关注点"""
        if stage_index != 0:
            return

        from src.tools.context import get_ctx

        quant = get_ctx().quant

        # 市场全局偏好认知
        try:
            from src.utils.http_resilience import retry_api_call
            mc = await retry_api_call(
                lambda: quant.preferences.get_market_cognition(),
                name="预取市场偏好认知",
                task_id=str(getattr(self._task, "id", "?")),
            )
            text = mc.text if mc and mc.text else "暂无市场偏好认知记录"
        except Exception:
            text = "暂无市场偏好认知记录"

        messages.append(SystemMessage(content=f"【当前市场全局偏好认知】\n{text}"))

        # 触发器关注点（focus_on）
        ctx = self._get_run_context()
        if ctx and ctx.get("trigger_id"):
            try:
                from src.tools._db import get_trigger_by_id
                t = await get_trigger_by_id(ctx["trigger_id"])
                if t and t.focus_on:
                    messages.append(SystemMessage(
                        content=(
                            f"【本次触发关注点】{t.focus_on}\n"
                            f"（该分析由触发器【{t.name}】自动触发。"
                            f"请结合此关注点判断资讯是否与触发器关注方向相关、是否有实质性新变化。"
                            f"如果无变化或与关注点无关，可以输出【无深度分析价值】早退。）"
                        )
                    ))
            except Exception:
                pass

    def __init__(self, make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> None:
        super().__init__(
            make_chat_model=make_chat_model,
            overall_system_prompt=SYS_OVERALL,
            stages=[
                {
                    "name": "研究(知识库)",
                    "system_prompt": SYS_RESEARCH,
                    "tools": get("search_kb", "read", "get_preferences"),
                    "max_iterations": 12,
                },
                {
                    "name": "反向证伪",
                    "system_prompt": SYS_FALSIFY,
                    "tools": get("search_kb", "read"),
                    "max_iterations": 8,
                },
                {
                    "name": "综合分析",
                    "system_prompt": SYS_ANALYZE,
                    "tools": [],
                    "max_iterations": 1,
                },
                {
                    "name": "落地(写入)",
                    "system_prompt": SYS_WRITE,
                    "tools": get(
                        "create_analysis",
                        "update_node_state",
                        "create_trade",
                        "create_node",
                        "search_kb",
                    ),
                    "max_iterations": 10,
                },
            ],
            **kwargs,
        )
        self.max_context_tokens = 1_000_000
        self._filter_counts: dict[str, int] = {}


def create_deep_analysis_agent(make_chat_model: Callable[[], BaseChatModel], **kwargs: Any) -> DeepAnalysisAgent:
    return DeepAnalysisAgent(make_chat_model, **kwargs)
