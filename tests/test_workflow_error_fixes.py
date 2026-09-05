import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "final"))

from src.core.timezone import BEIJING_TZ
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.states import TaskState
from src.tools import init_ctx
from src.tools._deps import format_exception
from src.tools.registry import get, get_coroutine


class _Clock:
    @property
    def now(self):
        from datetime import datetime

        return datetime(2026, 6, 9, tzinfo=BEIJING_TZ)


def test_format_exception_uses_type_for_empty_exception():
    class EmptyError(Exception):
        def __str__(self) -> str:
            return ""

    assert format_exception(EmptyError()) == "EmptyError"


@pytest.mark.asyncio
async def test_search_kb_tool_accepts_alias_and_stringified_values():
    quant = Mock()
    item = SimpleNamespace(
        result_type="raw_information",
        id=uuid4(),
        title="HBM 服务器",
        snippet="兼容旧字段名",
        score=SimpleNamespace(total=0.8),
    )
    quant.search.search = AsyncMock(return_value=SimpleNamespace(items=[item], total=1))
    init_ctx(quant=quant, market=Mock(), compiler=None, clock=None)

    search_kb_tool = get("search_kb")[0]
    payload = json.loads(
        await search_kb_tool.ainvoke(
            {
                "query_test": "  HBM 高带宽内存 服务器  ",
                "limit": "5",
            }
        )
    )

    assert payload["status"] == "ok"
    request = quant.search.search.await_args.args[0]
    assert request.query_text == "HBM 高带宽内存 服务器"
    assert request.limit == 5


@pytest.mark.asyncio
async def test_append_market_preference_tool_stringifies_dict_text():
    quant = Mock()
    quant.preferences = Mock()
    quant.preferences.append_market_cognition = AsyncMock(return_value=SimpleNamespace(status="appended"))
    init_ctx(quant=quant, market=Mock(), compiler=None, clock=None)

    tool = get("append_market_preference")[0]
    payload = json.loads(await tool.ainvoke({"content": {"style": "risk_on", "score": 0.8}}))

    assert payload["status"] == "ok"
    passed_text = quant.preferences.append_market_cognition.await_args.args[0]
    assert isinstance(passed_text, str)
    assert '"style": "risk_on"' in passed_text


@pytest.mark.asyncio
async def test_trade_lookup_failure_does_not_enter_risk_control():
    orchestrator = object.__new__(PipelineOrchestrator)
    orchestrator.quant = Mock()
    orchestrator.quant.trading.get = AsyncMock(side_effect=RuntimeError(""))
    orchestrator.clock = _Clock()

    task = SimpleNamespace(
        id=uuid4(),
        trade_ids=[str(uuid4())],
        state=TaskState.DEEP_ANALYZED,
        updated_at=None,
    )

    await PipelineOrchestrator._route_post_analysis(orchestrator, task)

    assert task.state == TaskState.REFLECTION_PENDING
