from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, ToolMessage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langfuse import Langfuse

from src.config import settings
from src.observability import start_observation
from scripts.run_deep_analysis_smoke import install_kbquant_stubs, FakeCompiler, FakeClock

LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
LANGFUSE_BASE_URL = settings.langfuse_base_url


class FakeSearchService:
    async def hybrid(self, request) -> SimpleNamespace:
        query = getattr(request, "query_text", "")
        if query.strip() in {"恒瑞医药", "江苏恒瑞医药股份有限公司"}:
            return SimpleNamespace(
                total=1,
                items=[
                    SimpleNamespace(
                        id="node-hengrui-001",
                        result_type="node",
                        title="恒瑞医药",
                        name="恒瑞医药",
                        snippet="创新药龙头，复盘重点关注业绩兑现与估值切换。",
                        score=SimpleNamespace(total=0.96),
                        model_dump=lambda: {
                            "id": "node-hengrui-001",
                            "title": "恒瑞医药",
                            "name": "恒瑞医药",
                        },
                    )
                ],
            )

        return SimpleNamespace(
            total=2,
            items=[
                SimpleNamespace(
                    id="raw-info-001",
                    result_type="raw_information",
                    title="恒瑞医药2026Q1业绩快报",
                    snippet="收入和利润双增，创新药收入占比继续提升。",
                    score=SimpleNamespace(total=0.92),
                ),
                SimpleNamespace(
                    id="analysis-001",
                    result_type="analysis",
                    title="恒瑞医药：创新药主线强化，等待回调再上车",
                    snippet="原始分析的核心是业绩超预期、估值有修复空间。",
                    score=SimpleNamespace(total=0.88),
                ),
            ],
        )

    async def fetch_by_ids(self, request) -> SimpleNamespace:
        return SimpleNamespace(
            data={
                "analyses": [
                    {
                        "id": "analysis-001",
                        "title": "恒瑞医药：创新药主线强化，等待回调再上车",
                        "content": (
                            "原始分析判断为偏多，认为创新药收入高增会推动估值修复，"
                            "但更适合等待回调到均线附近后跟踪介入。"
                        ),
                    }
                ],
                "raw_information": [
                    {
                        "id": "raw-info-001",
                        "title": "恒瑞医药2026Q1业绩快报",
                        "content": "营收81.41亿元，同比增长12.98%；净利润22.82亿元，同比增长21.78%。",
                    }
                ],
            }
        )


class FakeTradingService:
    async def get(self, trade_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=trade_id,
            symbol="600276.SH",
            operation_type="track",
            rationale="等待回调到MA5附近再考虑介入",
            risk_level="medium",
            price=55.9,
            quantity=None,
            model_dump=lambda mode="json": {
                "id": trade_id,
                "symbol": "600276.SH",
                "operation_type": "track",
                "rationale": "等待回调到MA5附近再考虑介入",
                "risk_level": "medium",
                "price": 55.9,
            },
        )

    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))

    async def update(self, trade_id: str, request) -> None:
        return None


class FakeNodesService:
    async def get_state_history(self, node_id: str):
        return [
            {
                "version": 3,
                "effective_from": "2026-04-22T17:30:00Z",
                "effective_to": None,
                "core_logic": "创新药商业化进入兑现期，但高研发投入带来波动。",
                "state_summary": "当前逻辑强调创新药兑现和回调买点。",
            },
            {
                "version": 2,
                "effective_from": "2025-12-31T15:00:00Z",
                "effective_to": "2026-04-22T17:30:00Z",
                "core_logic": "市场仍在观察创新药收入占比拐点。",
                "state_summary": "旧逻辑更偏预期交易。",
            },
        ]

    async def update_state(self, node_id: str, request) -> None:
        return None


class FakeFeedbackService:
    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))


class FakeAnalysisService:
    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))


class FakeQuant:
    def __init__(self) -> None:
        self.search = FakeSearchService()
        self.trading = FakeTradingService()
        self.nodes = FakeNodesService()
        self.feedback = FakeFeedbackService()
        self.analysis = FakeAnalysisService()


class FakeMarket:
    def get_stock_name(self, ticker: str) -> str:
        return "恒瑞医药"

    def get_price_history(self, ticker: str, from_date: str, to_date: str) -> dict:
        return {
            "count": 5,
            "data": [
                {"date": "2026-04-23", "open": 56.2, "close": 57.8, "high": 58.0, "low": 56.0, "volume": 12000000},
                {"date": "2026-04-24", "open": 57.6, "close": 58.4, "high": 58.9, "low": 57.1, "volume": 15000000},
                {"date": "2026-04-25", "open": 58.3, "close": 57.2, "high": 58.5, "low": 56.8, "volume": 13800000},
                {"date": "2026-04-26", "open": 57.0, "close": 56.1, "high": 57.4, "low": 55.7, "volume": 14200000},
                {"date": "2026-04-27", "open": 56.0, "close": 55.4, "high": 56.3, "low": 55.0, "volume": 16000000},
            ],
        }

    def get_market_snapshot(self, date: str) -> dict:
        return {
            "date": date,
            "total_stocks": 5200,
            "up_count": 1800,
            "down_count": 3100,
            "avg_pct_chg": -0.62,
            "total_amount": 1180000000000,
        }

    async def get_technical_indicators(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "ma5": 55.9,
            "ma10": 56.7,
            "ma20": 54.8,
            "rsi14": 47.3,
            "macd": {"dif": -0.12, "dea": 0.08, "signal": "dead_cross"},
        }


def patch_chart_generators() -> None:
    import src.tools.market as market_tools

    fake_png = b"fake-png-bytes"
    market_tools.generate_market_snapshot_chart = lambda market, date: fake_png
    market_tools.generate_price_chart = lambda market, ticker, from_date, to_date: fake_png
    market_tools.generate_technical_chart = lambda market, ticker, from_date="", to_date="": fake_png


def make_tool_call(name: str, args: dict, suffix: str) -> dict:
    return {"name": name, "args": args, "id": f"call_{name}_{suffix}"}


class FakeBoundChatModel:
    def __init__(self, tools, model_name: str) -> None:
        self._tools = tools
        self.model_name = model_name
        self.model = model_name

    async def ainvoke(self, messages):
        return _scripted_response(messages)


class FakeChatModel:
    def __init__(self) -> None:
        self.model_name = "fake-reflection-smoke-model"
        self.model = self.model_name

    def bind_tools(self, tools):
        return FakeBoundChatModel(tools, self.model_name)

    async def ainvoke(self, messages):
        return _scripted_response(messages)


def _scripted_response(messages) -> AIMessage:
    system_prompt = ""
    for msg in reversed(messages):
        if msg.__class__.__name__ == "SystemMessage":
            system_prompt = str(getattr(msg, "content", ""))
            break

    has_tool_result = any(isinstance(msg, ToolMessage) for msg in messages)
    last_human = ""
    for msg in reversed(messages):
        if msg.__class__.__name__ == "HumanMessage":
            last_human = str(getattr(msg, "content", ""))
            break

    if "回顾当时的判断" in system_prompt:
        if not has_tool_result:
            return AIMessage(
                content="",
                tool_calls=[
                    make_tool_call("read", {"refs": "A1"}, "review_read"),
                    make_tool_call("get_trade", {"trade_ref": "T1"}, "review_trade"),
                    make_tool_call("get_node_history", {"node_name": "恒瑞医药"}, "review_node"),
                ],
            )
        return AIMessage(content="当时核心论点是业绩兑现推动估值修复，但交易建议更偏等待回调后跟踪。")

    if "获取复盘区间内的实际情况" in system_prompt:
        if not has_tool_result:
            return AIMessage(
                content="",
                tool_calls=[
                    make_tool_call("get_price_history", {"ticker": "600276.SH", "from_date": "2026-04-23", "to_date": "2026-04-27"}, "market_hist"),
                    make_tool_call("get_market_snapshot", {"date": "2026-04-27"}, "market_snap"),
                    make_tool_call("get_price_chart", {"ticker": "600276.SH", "from_date": "2026-04-23", "to_date": "2026-04-27"}, "market_chart"),
                ],
            )
        return AIMessage(content="复盘区间内股价先冲高后回落，说明市场认可业绩但持续追价意愿不足。")

    if "将复盘结论落地到系统" in system_prompt:
        if not has_tool_result:
            return AIMessage(
                content="",
                tool_calls=[
                    make_tool_call(
                        "create_feedback",
                        {
                            "title": "恒瑞医药业绩复盘",
                            "trigger_analysis_ref": "A1",
                            "trigger_trade_ref": "T1",
                            "judgment_correct": True,
                            "lessons_learned": "业绩超预期并不等于持续上行，仍需结合市场风险偏好。",
                        },
                        "write_feedback",
                    ),
                    make_tool_call(
                        "append_preference",
                        {
                            "sector": "创新药",
                            "text": "高景气业绩催化后，若板块风险偏好不足，追高胜率未必高。",
                        },
                        "write_pref",
                    ),
                    make_tool_call(
                        "create_trigger",
                        {
                            "name": "恒瑞回调后二次观察",
                            "condition_nl": "若未来5个交易日回调至MA10附近企稳",
                            "action_nl": "重新分析恒瑞医药是否具备二次上车机会",
                            "source_analysis_ref": "A1",
                        },
                        "write_trigger",
                    ),
                ],
            )
        return AIMessage(content="复盘结果已落地，已写入反馈并补充行业偏好。")

    if "你是数据收集器" in system_prompt:
        if not has_tool_result:
            return AIMessage(
                content="",
                tool_calls=[
                    make_tool_call("get_price_history", {"ticker": "600276.SH", "from_date": "2026-04-23", "to_date": "2026-04-27"}, "tot_collect_hist"),
                    make_tool_call("get_market_snapshot", {"date": "2026-04-27"}, "tot_collect_snap"),
                ],
            )
        return AIMessage(content="关键数据报告：股价先涨后跌，市场整体偏弱，实际走势弱于原始分析预期。")

    if "基于实际市场数据和当时分析的对比" in system_prompt:
        return AIMessage(
            content=json.dumps(
                [
                    {"id": "A", "hypothesis": "市场风险偏好偏弱导致业绩利好兑现后资金选择落袋为安"},
                    {"id": "B", "hypothesis": "原始分析低估了短线拥挤交易后的回撤压力"},
                ],
                ensure_ascii=False,
            )
        )

    if "针对一个具体的因果假设" in system_prompt:
        if "假设 A" in last_human:
            return AIMessage(content="若假设A成立，则应观察到市场整体偏弱、医药板块跟涨不持续，且资金更偏防御。")
        return AIMessage(content="若假设B成立，则应观察到冲高回落和量能衰减，说明短线交易拥挤后承接不足。")

    if "用数据检验一个具体假设" in system_prompt:
        if not has_tool_result:
            if "市场风险偏好偏弱" in last_human:
                return AIMessage(
                    content="",
                    tool_calls=[
                        make_tool_call("get_market_snapshot", {"date": "2026-04-27"}, "verify_a_snap"),
                        make_tool_call("get_price_history", {"ticker": "600276.SH", "from_date": "2026-04-23", "to_date": "2026-04-27"}, "verify_a_hist"),
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    make_tool_call("get_price_history", {"ticker": "600276.SH", "from_date": "2026-04-23", "to_date": "2026-04-27"}, "verify_b_hist"),
                    make_tool_call("get_technical_chart", {"ticker": "600276.SH"}, "verify_b_chart"),
                ],
            )
        if "市场风险偏好偏弱" in last_human:
            return AIMessage(content="验证结论：假设A较强成立，市场弱势与股价回落同步出现，置信度高。")
        return AIMessage(content="验证结论：假设B部分成立，存在冲高回落和拥挤交易特征，置信度中等。")

    if "根据每个假设及其验证结果打分" in system_prompt:
        return AIMessage(
            content=json.dumps(
                [
                    {"id": "A", "confidence": 0.84, "verdict": "verified", "reasoning": "市场弱势证据明确"},
                    {"id": "B", "confidence": 0.66, "verdict": "verified", "reasoning": "拥挤交易迹象存在但不是唯一原因"},
                ],
                ensure_ascii=False,
            )
        )

    if "综合树搜索的全部结果" in system_prompt:
        return AIMessage(content="最终复盘：原始方向判断大体正确，但节奏判断偏乐观，真正主因是市场风险偏好不足导致利好难以持续演绎。")

    return AIMessage(content="默认响应")


def build_tree(trace) -> list[str]:
    children: dict[str | None, list] = {}
    for obs in trace.observations:
        children.setdefault(obs.parent_observation_id, []).append(obs)
    for key in children:
        children[key].sort(key=lambda x: x.start_time)

    def walk(parent_id: str | None, depth: int) -> list[str]:
        lines: list[str] = []
        for obs in children.get(parent_id, []):
            lines.append("  " * depth + f"- {obs.type} {obs.name}")
            lines.extend(walk(obs.id, depth + 1))
        return lines

    return walk(None, 0)


async def fetch_trace_with_retry(langfuse: Langfuse, trace_id: str, retries: int = 8, delay_seconds: int = 5):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            fresh_client = Langfuse(
                public_key=LANGFUSE_PUBLIC_KEY,
                secret_key=LANGFUSE_SECRET_KEY,
                base_url=LANGFUSE_BASE_URL,
                environment="dev",
                httpx_client=httpx.Client(trust_env=False),
            )
            return fresh_client.api.trace.get(trace_id)
        except Exception as exc:
            last_exc = exc
            print(f"trace_get_retry {attempt}/{retries}: {exc}")
            await asyncio.sleep(delay_seconds)
    raise last_exc


async def main() -> None:
    install_kbquant_stubs()
    patch_chart_generators()

    from alembic.config import Config
    from alembic import command
    import src.agents.base as base_module
    import src.agents.tree_of_thought as tot_module
    from src.tools import init_ctx
    from src.agents.reflection import create_reflection_agent

    base_module._make_chat_model = lambda: FakeChatModel()

    command.upgrade(Config("alembic.ini"), "head")

    quant = FakeQuant()
    market = FakeMarket()
    compiler = FakeCompiler()
    clock = FakeClock()

    init_ctx(quant=quant, market=market, compiler=compiler, clock=clock)

    langfuse = Langfuse(
        public_key=LANGFUSE_PUBLIC_KEY,
        secret_key=LANGFUSE_SECRET_KEY,
        base_url=LANGFUSE_BASE_URL,
        environment="dev",
        httpx_client=httpx.Client(trust_env=False),
    )
    try:
        print("auth_check:", langfuse.auth_check())
    except Exception as exc:
        print("auth_check_warning:", str(exc))

    agent = create_reflection_agent()
    run_id = f"reflection-smoke-{uuid4().hex[:8]}"
    context = {
        "task_id": str(uuid4()),
        "analysis_id": "analysis-001",
        "trade_id": "trade-001",
        "raw_info_id": "raw-info-001",
        "raw_info_title": "恒瑞医药2026Q1业绩快报",
        "raw_info_body": "营收利润双增，创新药收入占比继续提升。",
        "smoke_run_id": run_id,
    }

    with start_observation(
        name="reflection-smoke",
        as_type="chain",
        input={"run_id": run_id, "analysis_id": context["analysis_id"]},
        metadata={"source": "scripts/run_reflection_smoke.py"},
    ):
        trace_id = langfuse.get_current_trace_id()
        print("trace_id:", trace_id)
        result = await agent.run(context)

    langfuse.flush()
    await asyncio.sleep(8)

    trace = await fetch_trace_with_retry(langfuse, trace_id)
    tree = build_tree(trace)
    summary = {
        "trace_id": trace_id,
        "trace_name": trace.name,
        "observations_count": len(trace.observations),
        "observation_types": sorted({o.type for o in trace.observations}),
        "tree": tree[:300],
        "tool_names": sorted({o.name for o in trace.observations if o.type == "TOOL"}),
        "generation_names": sorted({o.name for o in trace.observations if o.type == "GENERATION"}),
        "final_output_preview": (result.get("content") or "")[:500],
        "entities": result.get("entities"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    langfuse.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
