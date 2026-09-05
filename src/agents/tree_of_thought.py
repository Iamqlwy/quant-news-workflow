"""Tree of Thought Agent —— 通用树搜索算法框架

六阶段：数据收集 → 数据驱动分叉 → 独立深化 → 独立验证 → 打分剪枝 → 综合输出

与 StageAgent 的设计哲学一致：框架只负责执行算法，提示词由调用方提供。
每个分支独立调用工具验证自己的假设（并行），被数据推翻的分支剪掉。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from src.agents.base import StageAgent
from src.llm.json_utils import _extract_json
from src.observability import observe_langchain_generation, safe_observation_value, start_observation


@dataclass
class ThoughtBranch:
    """树中的一个分支节点"""

    id: str  # "A", "B", "C"
    hypothesis: str  # 核心假设
    deepened: str = ""  # 深化追问结果
    verification_result: str = ""  # 工具验证输出
    confidence: float = 0.0  # 0.0 ~ 1.0
    status: str = "pending"  # pending | exploring | verified | falsified


class TreeOfThoughtAgent(StageAgent):
    """通用 Tree of Thought 算法框架。

    用法::

        agent = TreeOfThoughtAgent(
            generate_prompt="基于数据差异生成假设...",
            deepen_prompt="如果该假设成立，还应该观察到什么？",
            verify_prompt="用数据检验该假设...",
            score_prompt="基于验证结果打分...",
            synthesize_prompt="综合树搜索结果，输出最终结论...",
            verify_tools=[...],
            max_branches=3,
        )
        result = await agent.run({"input": "..."})

    可选的数据收集阶段（在分叉前执行）::

        agent = TreeOfThoughtAgent(
            ...,
            collect_prompt="收集关键数据...",
            collect_tools=[...],
        )
    """

    def __init__(
        self,
        *,
        # LLM 工厂
        make_chat_model: Callable[[], BaseChatModel],
        # 分叉生成
        generate_prompt: str,
        # 深化
        deepen_prompt: str,
        # 验证
        verify_prompt: str,
        verify_tools: list[BaseTool] | None = None,
        max_verify_iterations: int = 3,
        # 打分
        score_prompt: str,
        # 综合
        synthesize_prompt: str,
        # 数据收集（可选，在分叉前执行）
        collect_prompt: str | None = None,
        collect_tools: list[BaseTool] | None = None,
        max_collect_iterations: int = 3,
        # 参数
        max_branches: int = 3,
        confidence_threshold: float = 0.6,
    ) -> None:
        super().__init__(make_chat_model=make_chat_model, overall_system_prompt="", reset_registry=False)
        self._generate_prompt = generate_prompt
        self._deepen_prompt = deepen_prompt
        self._verify_prompt = verify_prompt
        self._verify_tools = {t.name: t for t in (verify_tools or [])}
        self._max_verify_iterations = max_verify_iterations
        self._score_prompt = score_prompt
        self._synthesize_prompt = synthesize_prompt
        self._collect_prompt = collect_prompt
        self._collect_tools = {t.name: t for t in (collect_tools or [])}
        self._max_collect_iterations = max_collect_iterations
        self.max_branches = max_branches
        self.confidence_threshold = confidence_threshold

    # ── 主入口 ──────────────────────────────────────────

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """执行完整的 Tree of Thought 流程。

        context 会被序列化为 JSON 作为各阶段的输入数据。
        返回 {"content": 最终结论, "branches": 分支列表}。
        """
        with start_observation(
            name=self.__class__.__name__,
            as_type="agent",
            input=safe_observation_value(context),
            metadata={"max_branches": self.max_branches},
        ) as agent_obs:
            user_input = json.dumps(context, ensure_ascii=False, indent=2, default=str)
            llm = self._make_chat_model()

            try:
                # Phase 0: 数据收集（可选）
                collected_data = ""
                if self._collect_prompt and self._collect_tools:
                    collected_data = await self._collect_data(llm, user_input)

                # Phase 1: 数据驱动分叉
                branches = await self._generate_branches(llm, user_input, collected_data)

                # Phase 2: 独立深化（并行）
                if branches:
                    branches = await self._deepen_all(branches, user_input, collected_data)

                # Phase 3: 独立验证（并行）
                if branches:
                    branches = await self._verify_all(branches)

                # Phase 4: 打分剪枝
                if branches:
                    branches = await self._score_branches(llm, branches)

                # Phase 5: 综合输出
                conclusion = await self._synthesize(llm, branches, user_input, collected_data)
            except Exception as exc:
                if agent_obs is not None:
                    agent_obs.update(
                        level="ERROR",
                        status_message=str(exc),
                        output={"error": str(exc)},
                    )
                raise

            result = {
                "content": conclusion,
                "branches": [
                    {
                        "id": b.id,
                        "hypothesis": b.hypothesis,
                        "confidence": b.confidence,
                        "status": b.status,
                    }
                    for b in branches
                ],
            }
            if agent_obs is not None:
                agent_obs.update(output=safe_observation_value(result))
            return result

    # ── Phase 0: 数据收集 ──────────────────────────────

    async def _collect_data(self, _llm: BaseChatModel, user_input: str) -> str:
        """在分叉前收集基础数据 —— 委托给 _run_stage_impl 执行 ReAct 循环。"""
        if not self._collect_prompt or not self._collect_tools:
            return ""
        tool_list = list(self._collect_tools.values())
        messages: list = [HumanMessage(content=user_input)]
        with start_observation(
            name="phase:collect",
            as_type="chain",
            input={"max_iterations": self._max_collect_iterations, "tools": list(self._collect_tools.keys())},
        ):
            return await self._run_stage_impl(
                stage_name="collect",
                stage_tools=tool_list,
                max_iter=self._max_collect_iterations,
                stage_label=f"{self.__class__.__name__}:collect",
                system_prompt=self._collect_prompt,
                messages=messages,
                task_id="tot",
            )

    # ── Phase 1: 数据驱动分叉 ──────────────────────────

    async def _generate_branches(self, llm: BaseChatModel, user_input: str, collected_data: str) -> list[ThoughtBranch]:
        """基于输入数据和收集的数据，生成候选假设分支。"""
        data_section = f"\n\n## 补充收集的数据\n{collected_data}" if collected_data else ""

        messages = [
            SystemMessage(content=self._generate_prompt),
            HumanMessage(content=f"{user_input}{data_section}"),
        ]

        with start_observation(name="phase:generate", as_type="chain", input={"max_branches": self.max_branches}):
            response = await observe_langchain_generation(
                name="llm:generate",
                llm=llm,
                messages=messages,
                invoke=lambda: llm.ainvoke(messages),
                metadata={"phase": "generate"},
            )
        content = response.content or ""

        try:
            data = _extract_json(content)
            # 兼容 LLM 返回单个对象而非数组的情况
            if isinstance(data, dict):
                data = [data]
            return [
                ThoughtBranch(
                    id=item.get("id", chr(65 + i)) if isinstance(item, dict) else chr(65 + i),
                    hypothesis=item["hypothesis"] if isinstance(item, dict) else str(item),
                )
                for i, item in enumerate(data[: self.max_branches])
            ]
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            hypothesis = content[:300].strip()
            if not hypothesis:
                return []
            return [ThoughtBranch(id="A", hypothesis=hypothesis)]

    # ── Phase 2: 独立深化 ──────────────────────────────

    async def _deepen_all(
        self,
        branches: list[ThoughtBranch],
        user_input: str,
        collected_data: str,
    ) -> list[ThoughtBranch]:
        """并行深化所有分支。"""

        async def deepen_one(b: ThoughtBranch) -> ThoughtBranch:
            return await self._deepen_branch(b, user_input, collected_data)

        return list(await asyncio.gather(*[deepen_one(b) for b in branches]))

    async def _deepen_branch(self, branch: ThoughtBranch, user_input: str, collected_data: str) -> ThoughtBranch:
        """深化单个分支。"""
        llm = self._make_chat_model()
        data_section = f"\n\n## 补充数据\n{collected_data}" if collected_data else ""

        messages = [
            SystemMessage(content=self._deepen_prompt),
            HumanMessage(
                content=f"""## 假设 {branch.id}
{branch.hypothesis}
{data_section}

## 完整上下文
{user_input}"""
            ),
        ]

        with start_observation(
            name=f"phase:deepen:{branch.id}",
            as_type="chain",
            input={"branch_id": branch.id},
        ):
            response = await observe_langchain_generation(
                name="llm:deepen",
                llm=llm,
                messages=messages,
                invoke=lambda: llm.ainvoke(messages),
                metadata={"phase": "deepen", "branch_id": branch.id},
            )
        branch.deepened = response.content or ""
        branch.status = "exploring"
        return branch

    # ── Phase 3: 独立验证 ──────────────────────────────

    async def _verify_all(
        self,
        branches: list[ThoughtBranch],
    ) -> list[ThoughtBranch]:
        """并行验证所有分支。每个分支独立调工具，互不干扰。"""

        async def verify_one(b: ThoughtBranch) -> ThoughtBranch:
            return await self._verify_branch(b)

        return list(await asyncio.gather(*[verify_one(b) for b in branches]))

    async def _verify_branch(self, branch: ThoughtBranch) -> ThoughtBranch:
        """独立验证单个分支 —— 委托给 _run_stage_impl 执行 ReAct 循环。

        这是 Tree of Thought 的核心：每个分支有自己的 ReAct 循环，
        独立决定调用哪些工具、如何解读结果。
        """
        tool_list = list(self._verify_tools.values())
        messages: list = [
            HumanMessage(
                content=f"""## 待验证假设
{branch.hypothesis}

## 深化分析
{branch.deepened}"""
            ),
        ]
        with start_observation(
            name=f"phase:verify:{branch.id}",
            as_type="chain",
            input={
                "branch_id": branch.id,
                "max_iterations": self._max_verify_iterations,
                "tools": list(self._verify_tools.keys()),
            },
        ):
            branch.verification_result = await self._run_stage_impl(
                stage_name=f"verify:{branch.id}",
                stage_tools=tool_list,
                max_iter=self._max_verify_iterations,
                stage_label=f"{self.__class__.__name__}:verify:{branch.id}",
                system_prompt=self._verify_prompt,
                messages=messages,
                task_id="tot",
            )
        return branch

    # ── Phase 4: 打分剪枝 ──────────────────────────────

    async def _score_branches(
        self,
        llm: BaseChatModel,
        branches: list[ThoughtBranch],
    ) -> list[ThoughtBranch]:
        """基于验证结果打分，低于阈值的剪掉。"""
        if not branches:
            return branches

        branches_text = "\n\n".join(
            [
                f"### 分支 {b.id}\n**假设**: {b.hypothesis}\n**深化**: {b.deepened}\n**验证**: {b.verification_result}"
                for b in branches
            ]
        )

        messages = [
            SystemMessage(content=self._score_prompt),
            HumanMessage(content=branches_text),
        ]

        with start_observation(name="phase:score", as_type="chain", input={"branch_count": len(branches)}):
            response = await observe_langchain_generation(
                name="llm:score",
                llm=llm,
                messages=messages,
                invoke=lambda: llm.ainvoke(messages),
                metadata={"phase": "score"},
            )
        content = response.content or ""

        try:
            scores = _extract_json(content)
            # 兼容 LLM 返回单个对象而非数组的情况
            if isinstance(scores, dict):
                scores = [scores]
            for item in scores:
                if not isinstance(item, dict):
                    continue
                bid = item.get("id", "")
                for b in branches:
                    if b.id == bid:
                        b.confidence = float(item.get("confidence", 0.5))
                        b.status = "verified" if b.confidence >= self.confidence_threshold else "falsified"
                        break
        except (json.JSONDecodeError, ValueError, TypeError):
            for b in branches:
                b.confidence = 0.5
                b.status = "unscored"

        return branches

    # ── Phase 5: 综合输出 ──────────────────────────────

    async def _synthesize(
        self,
        llm: BaseChatModel,
        branches: list[ThoughtBranch],
        user_input: str,
        collected_data: str,
    ) -> str:
        """综合存活分支和被剪分支的教训，输出最终结论。"""
        surviving = [b for b in branches if b.status == "verified"]
        falsified = [b for b in branches if b.status == "falsified"]

        surviving_text = (
            "\n".join(
                [
                    f"- **分支{b.id}** (置信度 {b.confidence:.0%}): {b.hypothesis}\n"
                    f"  证据: {b.verification_result[:200]}"
                    for b in surviving
                ]
            )
            if surviving
            else "（无分支通过验证）"
        )

        falsified_text = (
            "\n".join(
                [
                    f"- **分支{b.id}** (置信度 {b.confidence:.0%}): {b.hypothesis}\n"
                    f"  被推翻: {b.verification_result[:200]}"
                    for b in falsified
                ]
            )
            if falsified
            else "（无被推翻分支）"
        )

        data_section = f"\n\n## 补充数据\n{collected_data}" if collected_data else ""

        messages = [
            SystemMessage(content=self._synthesize_prompt),
            HumanMessage(
                content=f"""{user_input}{data_section}

## 树搜索结果

### 存活分支（被数据支持）
{surviving_text}

### 被推翻分支
{falsified_text}"""
            ),
        ]

        with start_observation(name="phase:synthesize", as_type="chain", input={"branch_count": len(branches)}):
            response = await observe_langchain_generation(
                name="llm:synthesize",
                llm=llm,
                messages=messages,
                invoke=lambda: llm.ainvoke(messages),
                metadata={"phase": "synthesize"},
            )
            return response.content or ""
