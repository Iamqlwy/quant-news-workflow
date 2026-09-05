from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langfuse import Langfuse

from src.config import settings
from src.observability import start_observation

NEWS_TITLE = "恒瑞医药2026一季报：营收81.41亿元，增12.98%，净利22.82亿元，增21.78%"
NEWS_BODY = (
    "4月22日晚，恒瑞医药发布2026年一季度业绩。报告期内，公司实现营业收入81.41亿元，"
    "同比增长12.98%；归母净利润22.82亿元，同比增长21.78%；扣非净利润21.72亿元，"
    "同比增长16.59%。创新药销售收入45.26亿元，同比增长25.75%，占药品销售收入61.69%。"
    "公司持续加大创新力度，一季度研发投入22.24亿元，占营收27.32%。"
)
LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
LANGFUSE_BASE_URL = settings.langfuse_base_url


def install_kbquant_stubs() -> None:
    if "kbquant" in sys.modules:
        return

    kbquant = types.ModuleType("kbquant")
    schemas = types.ModuleType("kbquant.schemas")
    search = types.ModuleType("kbquant.schemas.search")
    analysis = types.ModuleType("kbquant.schemas.analysis")
    feedback = types.ModuleType("kbquant.schemas.feedback")
    node = types.ModuleType("kbquant.schemas.node")
    trading = types.ModuleType("kbquant.schemas.trading")

    class _Model:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def model_dump(self, **kwargs):
            return dict(self.__dict__)

    search.HybridSearchRequest = _Model
    search.FetchByIdsRequest = _Model
    analysis.AnalysisCreate = _Model
    feedback.FeedbackCreate = _Model
    node.NodeStateCreate = _Model
    trading.TradingOperationCreate = _Model
    trading.TradingOperationUpdate = _Model

    schemas.search = search
    schemas.analysis = analysis
    schemas.feedback = feedback
    schemas.node = node
    schemas.trading = trading
    kbquant.schemas = schemas

    sys.modules["kbquant"] = kbquant
    sys.modules["kbquant.schemas"] = schemas
    sys.modules["kbquant.schemas.search"] = search
    sys.modules["kbquant.schemas.analysis"] = analysis
    sys.modules["kbquant.schemas.feedback"] = feedback
    sys.modules["kbquant.schemas.node"] = node
    sys.modules["kbquant.schemas.trading"] = trading


@dataclass
class FakeScore:
    total: float


class FakeSearchItem:
    def __init__(
        self,
        *,
        item_id: str,
        result_type: str,
        title: str,
        snippet: str,
        name: str | None = None,
        score: float = 0.9,
    ) -> None:
        self.id = item_id
        self.result_type = result_type
        self.title = title
        self.snippet = snippet
        self.name = name or title
        self.score = FakeScore(total=score)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "result_type": self.result_type,
            "title": self.title,
            "name": self.name,
            "snippet": self.snippet,
        }


class FakeSearchService:
    async def hybrid(self, request) -> SimpleNamespace:
        query = getattr(request, "query_text", "")
        filters = getattr(request, "filters", None) or {}
        target_tables = set(filters.get("target_tables", []))

        if query.strip() in {"恒瑞医药", "江苏恒瑞医药股份有限公司"}:
            return SimpleNamespace(
                total=1,
                items=[
                    FakeSearchItem(
                        item_id="4e2e4a7b-6c70-44ec-8517-413ef8c65f9e",
                        result_type="node",
                        title="恒瑞医药",
                        name="恒瑞医药",
                        snippet="国内创新药龙头，核心看点是创新药放量与研发转化。",
                    )
                ],
            )

        items: list[FakeSearchItem] = []
        if not target_tables or "raw_information" in target_tables:
            items.append(
                FakeSearchItem(
                    item_id="52b3775a-6408-4773-a5ca-7d7e947f9949",
                    result_type="raw_information",
                    title="2025年恒瑞医药创新药收入继续提升",
                    snippet="历史上创新药占比提升时，市场通常给予估值溢价。",
                )
            )
        if not target_tables or "analyses" in target_tables:
            items.append(
                FakeSearchItem(
                    item_id="0993d4d9-e72f-4147-a794-6964a08b6ee0",
                    result_type="analysis",
                    title="恒瑞医药：研发投入高位，利润释放来自产品结构优化",
                    snippet="过去分析指出：短期看利润释放，中期看创新药商业化持续性。",
                    score=0.87,
                )
            )
        return SimpleNamespace(total=len(items), items=items)

    async def fetch_by_ids(self, request) -> SimpleNamespace:
        return SimpleNamespace(
            data={
                "raw_information": [
                    {
                        "id": "52b3775a-6408-4773-a5ca-7d7e947f9949",
                        "title": "2025年恒瑞医药创新药收入继续提升",
                        "content": "历史资讯显示，创新药收入占比提升往往会强化成长逻辑。",
                    }
                ],
                "analyses": [
                    {
                        "id": "0993d4d9-e72f-4147-a794-6964a08b6ee0",
                        "title": "恒瑞医药：研发投入高位，利润释放来自产品结构优化",
                        "content": "过往分析认为公司处于创新转型的兑现窗口。",
                    }
                ],
            }
        )


class FakeAnalysisService:
    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))


class FakeNodesService:
    async def update_state(self, node_id: str, request) -> None:
        return None


class FakeTradingService:
    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))

    async def update(self, trade_id: str, request) -> None:
        return None


class FakeFeedbackService:
    async def create(self, request) -> SimpleNamespace:
        return SimpleNamespace(id=str(uuid4()))


class FakeQuant:
    def __init__(self) -> None:
        self.search = FakeSearchService()
        self.analysis = FakeAnalysisService()
        self.nodes = FakeNodesService()
        self.trading = FakeTradingService()
        self.feedback = FakeFeedbackService()
        self.preferences = FakePrefsClient()


class FakeMarket:
    async def get_realtime_price(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "price": 56.88,
            "change_pct": 2.36,
            "volume": 18345500,
            "high": 57.2,
            "low": 55.6,
        }

    async def get_sector_overview(self, sector: str) -> dict:
        return {
            "sector": sector,
            "change_pct": 1.82,
            "up_count": 31,
            "down_count": 7,
            "leading_stock": "恒瑞医药",
        }

    async def get_technical_indicators(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "ma5": 55.9,
            "ma10": 54.7,
            "ma20": 52.8,
            "rsi14": 61.5,
            "macd": {"dif": 1.22, "dea": 0.88, "signal": "golden_cross"},
        }

    def get_stock_name(self, ticker: str) -> str:
        return "恒瑞医药"


class FakePrefsClient:
    async def get_industry_cognition(self, sector: str):
        from collections import namedtuple
        R = namedtuple("CogResp", ["text", "append_count"])
        return R(text="创新药主线关注业绩兑现、医保谈判、出海授权三条逻辑。", append_count=0)

    async def append_industry_cognition(self, sector: str, text: str):
        from collections import namedtuple
        R = namedtuple("AppendResp", ["sector", "status"])
        return R(sector=sector, status="appended")


class FakeClock:
    def __init__(self) -> None:
        from datetime import datetime
        from src.core.timezone import BEIJING_TZ

        self.now = datetime.now(BEIJING_TZ)


class FakeCompiler:
    async def compile(
        self,
        *,
        name: str,
        condition_nl: str,
        action_nl: str,
        source_task_id,
        source_analysis_id,
        now,
    ) -> dict:
        return {
            "condition": {"type": "time", "nl": condition_nl},
            "action_type": "deep_analysis",
            "action_params": {"action_nl": action_nl},
        }


async def main() -> None:
    install_kbquant_stubs()

    from alembic.config import Config
    from alembic import command
    from src.agents.deep_analysis import create_deep_analysis_agent
    from src.tools import init_ctx

    command.upgrade(Config("alembic.ini"), "head")

    quant = FakeQuant()
    market = FakeMarket()
    compiler = FakeCompiler()
    clock = FakeClock()

    init_ctx(quant=quant, market=market, compiler=compiler, clock=clock)
    agent = create_deep_analysis_agent()

    langfuse = Langfuse(
        public_key=LANGFUSE_PUBLIC_KEY,
        secret_key=LANGFUSE_SECRET_KEY,
        base_url=LANGFUSE_BASE_URL,
        environment="dev",
        httpx_client=httpx.Client(trust_env=False),
    )
    print("auth_check:", langfuse.auth_check())

    run_id = f"deep-analysis-smoke-{uuid4().hex[:8]}"
    context = {
        "task_id": str(uuid4()),
        "raw_info_id": str(uuid4()),
        "raw_info_title": NEWS_TITLE,
        "raw_info_body": NEWS_BODY,
        "raw_info_source": "news.csv",
        "raw_info_published_at": "2026-04-22 17:19:20",
        "smoke_run_id": run_id,
    }

    with start_observation(
        name="deep-analysis-smoke",
        as_type="chain",
        input={"run_id": run_id, "title": NEWS_TITLE},
        metadata={"news_source": "news.csv:197289"},
    ):
        trace_id = langfuse.get_current_trace_id()
        print("trace_id:", trace_id)
        result = await agent.run(context)

    langfuse.flush()
    await asyncio.sleep(8)

    trace = langfuse.api.trace.get(trace_id)
    obs_by_id = {o.id: o for o in trace.observations}
    children: dict[str | None, list] = {}
    for o in trace.observations:
        children.setdefault(o.parent_observation_id, []).append(o)
    for k in children:
        children[k].sort(key=lambda x: x.start_time)

    def dump_tree(parent_id: str | None, depth: int) -> list[str]:
        lines: list[str] = []
        for o in children.get(parent_id, []):
            lines.append("  " * depth + f"- {o.type} {o.name}")
            lines.extend(dump_tree(o.id, depth + 1))
        return lines

    tree_lines = dump_tree(None, 0)
    summary = {
        "trace_id": trace_id,
        "trace_name": trace.name,
        "observations_count": len(trace.observations),
        "observation_types": sorted({item.type for item in trace.observations}),
        "tool_names": sorted(
            item.name for item in trace.observations if getattr(item, "type", "") == "TOOL"
        ),
        "generation_names": sorted(
            item.name for item in trace.observations if getattr(item, "type", "") == "GENERATION"
        ),
        "final_output_preview": (result.get("content") or "")[:500],
        "entities": result.get("entities"),
        "tree": tree_lines[:200],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    langfuse.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
