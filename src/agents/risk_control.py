"""Risk Agent —— 3 阶段 ReAct 交易时机分析 (LangChain)"""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from src.agents.base import StageAgent, _execute_tool_calls, _sanitize_orphan_tool_calls, _clear_all_tool_call_fields
from src.market.charts import (
    generate_market_snapshot_chart,
    generate_multi_index_chart,
    generate_price_chart,
    generate_technical_chart,
)
from src.tools.registry import get
from src.utils.oss_uploader import upload_bytes

SYS_OVERALL = """你是交易时机分析Agent。职责是判断"何时执行"而非"能不能执行"。

3阶段：盘面时机→逻辑验证→时机决策，每阶段完成后自动推进。

决策三选一：现在入场（窗口有利）→ 等待条件X（逻辑对时机错）→ 逻辑硬伤放弃（撤销交易）。

核心：让正确的交易发生在正确的时间。大多数交易逻辑合理，只需判断时机。"等待"是常态，不因不完美就放弃。

【硬性规则】每个阶段必须调用该阶段提供的工具获取数据后再输出结论。禁止跳过工具调用直接输出分析。工具调用是获取实时数据的唯一途径，不调用工具的分析等同于凭空猜测。

初始消息中已包含深度分析报告（analyses 字段）和交易建议（trades 字段），请首先完整阅读，理解分析逻辑与交易理由后再进行时机评估。"""

SYS_TIMING_TECHNICAL = """【当前阶段：盘面时机】
【工具】get_technical_chart、get_price_chart、get_market_snapshot、get_sector_snapshot_chart、get_multi_index_chart

已预注入：深度分析报告（含方向判断/影响链/风险/交易建议）、四大指数日线、市场快照、标的技术面板+价格走势、宏观摘要。

首先阅读初始消息中的深度分析报告，理解交易标的、方向、核心理由和目标价位。然后按以下框架判断最佳入场时机：

1. 趋势方向：顺势→可入场；逆势有反转信号→可左侧；逆势无反转→等待
2. 技术指标：RSI极端→不宜入场；RSI中位+动能→好时机；布林收窄→关注突破；MACD金叉/死叉→趋势确认
3. 价格位置：近支撑→好时机（止损明确）；悬空→等回调；刚突破阻力→可入场但防假突破
4. 市场环境：大盘同向→顺风；大盘逆向→等（除非独立逻辑）；板块同步走强→加分

收敛：先调工具1-3次获取数据，然后输出结论。上限10次。输出以"盘面时机评估："开头，给出结论（优/良/可/不佳）。"""

SYS_TIMING_LOGIC = """【当前阶段：逻辑验证】
【工具】search_kb、read、get_node_history

已通过初始消息注入完整的深度分析报告，确认核心理由、目标价位和风险判断。

然后验证分析报告的论证质量，发现经验教训，而非推翻分析：

1. 搜索类似逻辑的历史交易结果——成功率高→加分，频繁失败→记录坑点但不到放弃阶段不定论
2. 搜索 feedbacks 表，"判断对但时机错"的教训带入下一阶段
3. 检查 WorldNode 风险因素和逻辑一致性

关键：你在验证时机，不是审判对错。历史失败可能是时机问题。

收敛：先搜索2-3次覆盖关键实体。上限6次。输出以"逻辑验证总结："开头，给出评分（充分印证/基本支持/有疑点/有硬伤）。"""

SYS_TIMING_DECIDE = """【当前阶段：时机决策】
【工具】review_trade、create_trigger、create_analysis

【硬性规则】review_trade 是本阶段必调工具，无论任何决策都必须调用。不调用 review_trade 等同于没有完成本阶段。

【多交易审批】初始消息中可能包含多个交易（trades 字段中有 T1、T2...）。必须对每一个交易分别调用 review_trade，使用 trade_ref 参数指定交易引用。只审批一个交易而遗漏其他交易属于严重遗漏。

基于盘面时机和逻辑验证结论，按矩阵决策：

盘面优/良 + 逻辑充分/基本 → 现在入场
盘面优/良 + 逻辑有疑点/硬伤 → 逻辑硬伤放弃
盘面可/不佳 + 逻辑充分/基本 → 等待条件X
盘面可/不佳 + 逻辑有疑点 → 等待条件X
盘面可/不佳 + 逻辑硬伤 → 逻辑硬伤放弃

【必做】create_analysis：将本次时机分析的完整结论写入知识库（analysis_type="risk_evaluation"），内容包括：
- 盘面时机评估结论
- 逻辑验证结论
- 最终决策及理由
- 等待条件/放弃原因（如适用）
- 建议的止损止盈位（如已批准入场）

执行方式：
- 现在入场：review_trade(action="approve")+timing_note，可选加仓买入触发器
- 等待条件X：review_trade(action="reject")+timing_note写明等待条件，必须创建触发器（买入/分析）
- 逻辑硬伤放弃：review_trade(action="reject")+timing_note写明缺陷，不创建触发器

收敛：理想3次（分析+决策+触发器），上限5次。矩阵已明确，不要反复纠结。"""


class RiskAgent(StageAgent):
    """交易时机分析 Agent —— 3 阶段 ReAct"""

    def __init__(
        self,
        make_chat_model: Callable[[], BaseChatModel],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            make_chat_model=make_chat_model,
            overall_system_prompt=SYS_OVERALL,
            stages=[
                {
                    "name": "盘面时机",
                    "system_prompt": SYS_TIMING_TECHNICAL,
                    "tools": get(
                        "get_technical_chart",
                        "get_price_chart",
                        "get_market_snapshot",
                        "get_sector_snapshot_chart",
                        "get_multi_index_chart",
                    ),
                    "max_iterations": 10,
                },
                {
                    "name": "逻辑验证",
                    "system_prompt": SYS_TIMING_LOGIC,
                    "tools": get("search_kb", "read", "get_node_history"),
                    "max_iterations": 6,
                },
                {
                    "name": "时机决策",
                    "system_prompt": SYS_TIMING_DECIDE,
                    "tools": get("review_trade", "create_trigger", "create_analysis"),
                    "max_iterations": 5,
                },
            ],
            **kwargs,
        )
        self.max_context_tokens = 1_000_000
        self._filter_counts: dict[str, int] = {}

    def _filter_tool_calls(self, stage_name: str, response: Any) -> list:
        """时机决策阶段限制 create_analysis 最多 1 次（跨迭代累计）。"""
        if stage_name != "时机决策":
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



    async def end(self) -> None:
        """重载 end：检查 review_trade 调用次数是否匹配 trade 数量。
        若不足，补发一条消息让模型并行调用补齐。
        """
        run_ctx = self._get_run_context()
        trade_ids = (run_ctx or {}).get("trade_ids") or []
        expected_trade_count = len(trade_ids)
        task_id = str(self._task.id) if self._task else "?"

        if expected_trade_count == 0:
            if "review_trade" not in self.tool_use:
                logger.warning("end() 无 trade 上下文且未调用 review_trade [task={}]", task_id)
            return

        review_trade_count = sum(
            1 for m in self.messages
            if isinstance(m, ToolMessage) and getattr(m, "name", None) == "review_trade"
        )

        if review_trade_count >= expected_trade_count:
            return

        logger.warning(
            "end() review_trade 调用不足: 已调用 {} 次, 预期 {} 次 (trade_ids={}), 将补调 [task={}]",
            review_trade_count, expected_trade_count, trade_ids, task_id,
        )

        llm = self._make_chat_model()
        stage_tools = self._get_stage_tools("时机决策", self.stages[2].get("tools", []))
        review_tools = [t for t in stage_tools if t.name == "review_trade"]
        llm_with_tools = llm.bind_tools(review_tools, parallel_tool_calls=True)

        missing_refs = [f"T{i}" for i in range(1, expected_trade_count + 1)]
        ref_list = "、".join(missing_refs)
        _sanitize_orphan_tool_calls(self.messages, task_id=task_id)
        self.messages.append(
            HumanMessage(
                content=(
                    f"（当前共有 {expected_trade_count} 个交易需要审批，但你只调用了 {review_trade_count} 次 review_trade。"
                    f"请对以下交易分别调用 review_trade：{ref_list}。"
                    f"每个交易独立调用一次，使用 trade_ref 参数指定交易引用。可以一次并行调用多个。）"
                )
            )
        )
        try:
            response = await llm_with_tools.ainvoke(self.messages)
            _clear_all_tool_call_fields(response)
            self.messages.append(response)
            if response.tool_calls:
                for tc in response.tool_calls:
                    self.tool_use.add(tc["name"])
                await _execute_tool_calls(
                    response.tool_calls,
                    stage_tools,
                    "时机决策",
                    f"{self.__class__.__name__}:时机决策",
                    task_id,
                    self.messages,
                )
            else:
                logger.warning(
                    "end() 补调 LLM 未返回工具调用，review_trade 仍不足 ({}/{}) [task={}]",
                    review_trade_count, expected_trade_count, task_id,
                )
        except Exception as e:
            logger.warning("end() 补调 review_trade 失败: {} ({}) [task={}]", e, type(e).__name__, task_id)

    async def _on_stage_start(self, stage_name: str, stage_index: int, messages: list) -> None:
        if stage_index != 0:
            return

        from src.tools.context import get_ctx

        ctx = get_ctx()
        quant = ctx.quant
        market = ctx.market
        task_id = str(self._task.id) if self._task else "?"
        run_id = int(time.time() * 1000)

        # ── 宏观报告摘要 ──
        macro_text = None
        try:
            from src.utils.http_resilience import retry_api_call
            report = await retry_api_call(
                lambda: quant.macro_report.get_current(),
                name="预取宏观报告",
                task_id=task_id,
            )
            if report and report.summary:
                macro_text = report.summary
        except Exception as exc:
            logger.warning("预取宏观报告失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)

        # ── 市场全局偏好认知 ──
        pref_text = None
        try:
            from src.utils.http_resilience import retry_api_call
            mc = await retry_api_call(
                lambda: quant.preferences.get_market_cognition(),
                name="预取市场偏好认知",
                task_id=task_id,
            )
            if mc and mc.text:
                pref_text = mc.text
        except Exception as exc:
            logger.warning("预取市场偏好认知失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)

        # 仅在至少获取到一项数据时才注入
        if macro_text or pref_text:
            parts = []
            if macro_text:
                parts.append(f"【当前宏观环境摘要】\n{macro_text}")
            else:
                parts.append("【当前宏观环境摘要】\n暂无宏观报告")
            if pref_text:
                parts.append(f"【当前市场全局偏好认知】\n{pref_text}")
            else:
                parts.append("【当前市场全局偏好认知】\n暂无市场偏好记录")
            messages.append(SystemMessage(content="\n\n".join(parts)))

        # ── 预取四大指数同图 ──
        try:
            png_bytes = await asyncio.to_thread(generate_multi_index_chart, market)
            url = upload_bytes(png_bytes, f"charts/multi_index/risk_{task_id}_{run_id}.png")
            messages.append(HumanMessage(content=[
                {"type": "text", "text": "【四大指数 120 天日线同图】"},
                {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
            ]))
        except Exception as exc:
            logger.warning("预取四大指数同图失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)

        # ── 预取市场快照（日期从 clock 推导）──
        clock = ctx.clock
        date = clock.now.strftime("%Y-%m-%d") if clock else time.strftime("%Y-%m-%d")

        # 1) 数据解析
        try:
            snap = market.get_market_snapshot(date)
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("市场快照 JSON 解析失败: {} [task={}]", exc, task_id)
                    snap = None

            if isinstance(snap, dict) and "error" not in snap:
                up = snap.get("up_count")
                down = snap.get("down_count")
                avg = snap.get("avg_pct_chg")
                total_amount = snap.get("total_amount")
                total_amount_yi = round(float(total_amount) / 1e8, 1) if total_amount is not None else None
                summary_parts = [
                    f"日期：{date}",
                    f"上涨/下跌：{up}/{down}" if up is not None and down is not None else None,
                    f"平均涨跌幅：{avg}%" if avg is not None else None,
                    f"总成交额：{total_amount_yi}亿" if total_amount_yi is not None else None,
                ]
                summary = "；".join([p for p in summary_parts if p])
                messages.append(SystemMessage(content=f"【市场快照】\n{summary}"))
        except Exception as exc:
            logger.warning("预取市场快照数据失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)

        # 2) 图表生成（独立异常处理，不影响已注入的文本摘要）
        try:
            png_bytes = await asyncio.to_thread(generate_market_snapshot_chart, market, date)
            url = upload_bytes(png_bytes, f"charts/market_snapshot/{date}_{task_id}_{run_id}.png")
            messages.append(HumanMessage(content=[
                {"type": "text", "text": f"【市场快照 {date}】"},
                {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
            ]))
        except Exception as exc:
            logger.warning("预取市场快照图表失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)

        # ── 预取标的技术面板 + 价格走势（需从 trade 解析 symbol）──
        run_ctx = self._get_run_context()
        trade_ids = (run_ctx or {}).get("trade_ids") or []
        if not trade_ids:
            return

        if len(trade_ids) > 1:
            logger.info("时机分析仅审查第一个 trade: {}, 共 {} 个 [task={}]", trade_ids[0], len(trade_ids), task_id)

        try:
            from src.utils.http_resilience import retry_api_call
            tid = UUID(str(trade_ids[0]))
            trade = await retry_api_call(
                lambda: quant.trading.get(tid),
                name="获取trade详情",
                task_id=task_id,
            )
            stock_symbol = trade.symbol if trade and hasattr(trade, "symbol") else None
            if not stock_symbol:
                logger.debug("trade {} 无 symbol 信息，跳过标的图表 [task={}]", trade_ids[0], task_id)
                return

            symbol = stock_symbol.strip()
            # 反查名称：symbol 可能就是 ticker 代码
            resolved_name = market.get_stock_name(symbol)
            if resolved_name:
                ticker = symbol
                stock_name = resolved_name
            else:
                # 可能是中文名称，按名称解析
                matches = market.resolve_stock_ticker(symbol)
                if not matches:
                    logger.warning("未找到股票 {} 的代码，跳过标的图表 [task={}]", symbol, task_id)
                    return
                ticker = matches[0][0]
                stock_name = matches[0][1]

            # 技术面板
            tech_bytes = await asyncio.to_thread(generate_technical_chart, market, ticker)
            tech_url = upload_bytes(tech_bytes, f"charts/technical/risk_{ticker}_{task_id}_{run_id}.png")
            messages.append(HumanMessage(content=[
                {"type": "text", "text": f"【{stock_name} 技术分析面板】"},
                {"type": "image_url", "image_url": {"url": tech_url, "detail": "auto"}},
            ]))

            # 价格走势
            price_bytes = await asyncio.to_thread(generate_price_chart, market, ticker)
            price_url = upload_bytes(price_bytes, f"charts/price/risk_{ticker}_{task_id}_{run_id}.png")
            messages.append(HumanMessage(content=[
                {"type": "text", "text": f"【{stock_name} 价格走势】"},
                {"type": "image_url", "image_url": {"url": price_url, "detail": "auto"}},
            ]))
        except Exception as exc:
            logger.warning("预取标的图表失败: {} ({}) [task={}]", exc, type(exc).__name__, task_id)


def create_risk_agent(
    make_chat_model: Callable[[], BaseChatModel],
    **kwargs: Any,
) -> RiskAgent:
    return RiskAgent(make_chat_model, **kwargs)
