"""工具调用测试 —— 验证所有 LangChain 工具可正常调用"""
import asyncio
from loguru import logger
from unittest.mock import AsyncMock, Mock

from src.tools import init_ctx, set_task_context
from src.tools.registry import get_coroutine


def setup_mocks():
    """初始化 mock 依赖"""
    quant = Mock()
    market = Mock()
    clock = Mock()

    # ---- QuantClient 子模块 mocks ----
    quant.search = Mock()
    quant.search.hybrid = AsyncMock(return_value=Mock(
        items=[
            Mock(title="测试资讯A", result_type="raw_information"),
            Mock(title="测试资讯B", result_type="raw_information"),
        ],
        total=2,
    ))
    quant.search.fetch_by_ids = AsyncMock(return_value=Mock(data={}))

    quant.feedback = Mock()
    quant.feedback.get_lessons = AsyncMock(return_value=[
        {"title": "教训1：追高不可取"},
        {"title": "教训2：止损要果断"},
    ])

    quant.search.similar_cases = AsyncMock(return_value=Mock(similar_cases=[
        Mock(raw_info={"title": "相似案例1"}, similarity_score=0.95),
        Mock(raw_info={"title": "相似案例2"}, similarity_score=0.88),
    ]))

    quant.nodes = Mock()
    quant.nodes.get_current_state = AsyncMock(return_value=Mock(
        core_logic="做多科技股",
        primary_drivers=["AI", "降息"],
        risks=["估值过高"],
        focus_points=["美联会议"],
        recent_changes="无",
        state_summary=None,
    ))
    quant.nodes.get_state_history = AsyncMock(return_value=[
        Mock(version=1, effective_from="2025-01-01", effective_to="2025-01-15", state_summary="初期"),
        Mock(version=2, effective_from="2025-01-15", effective_to=None, state_summary="更新后"),
    ])
    quant.nodes.update_state = AsyncMock(return_value=Mock(id="state-123"))

    quant.analysis = Mock()
    quant.analysis.get = AsyncMock(return_value=Mock(
        title="深度分析：AI板块",
        analysis_type="impact_analysis",
        confidence=0.85,
        time_horizon="medium_term",
        created_at="2025-01-01T03:00:00",
        content="## 分析\nAI板块有望持续走强...",
    ))
    quant.analysis.create = AsyncMock(return_value=Mock(id="analysis-123"))

    quant.trading = Mock()
    quant.trading.get = AsyncMock(return_value=Mock(
        id="trade-001", operation_type="buy", symbol="000001.SZ",
        price=15.5, model_dump=Mock(return_value={"id": "trade-001", "operation_type": "buy", "symbol": "000001.SZ", "price": 15.5}),
    ))
    quant.trading.create = AsyncMock(return_value=Mock(id="trade-456"))
    quant.trading.update = AsyncMock(return_value={"status": "ok"})

    quant.feedback.create = AsyncMock(return_value=Mock(id="feedback-789"))
    quant.feedback.get = AsyncMock(return_value=Mock(
        id="test-feedback-001",
        title="复盘测试",
        expected_outcome="涨5%", actual_outcome="涨8%",
        judgment_correct=True, error_reason=None,
        missed_factors=None, adjustment_suggestions=None,
        lessons_learned="测试经验",
        created_at="2025-01-01T03:00:00",
    ))

    quant.information = Mock()
    quant.information.get = AsyncMock(return_value=Mock(
        id="test-info-001",
        title="测试资讯", body="测试正文",
        source="sina", source_url=None,
        published_at="2025-01-01T03:00:00",
        info_type="news", importance_score=0.85,
    ))
    quant.information.list = AsyncMock(return_value=Mock(items=[
        Mock(title="宏观资讯1", source="sina"),
        Mock(title="宏观资讯2", source="cls"),
    ]))

    quant.macro_report = Mock()
    quant.macro_report.get_current = AsyncMock(return_value={
        "summary": "宏观报告摘要",
        "content": "# 宏观报告\n内容...",
    })
    quant.macro_report.update = AsyncMock(return_value={"status": "ok"})

    # resolve_node_name 需要 hybrid search 返回匹配的实体
    def _make_search_item(name, item_id):
        item = Mock()
        item.title = name
        item.model_dump = Mock(return_value={"name": name, "id": item_id, "title": name})
        return item

    quant.search.hybrid.side_effect = None
    quant.search.hybrid.return_value = Mock(
        items=[
            _make_search_item("node-1", "entity-001"),
            _make_search_item("贵州茅台", "entity-002"),
        ],
        total=2,
    )

    # ---- Market mock 返回值 ----
    market.get_realtime_price = AsyncMock(return_value={"price": 100.5, "change_pct": 2.3, "volume": 1000000})
    market.get_sector_overview = AsyncMock(return_value={"sector": "白酒", "avg_change_pct": 1.5, "up_count": 15, "down_count": 5})
    market.get_technical_indicators = AsyncMock(return_value={"ma5": 100, "ma20": 98, "rsi": 55, "macd": 0.5, "bollinger_upper": 105, "bollinger_lower": 92})
    market.get_market_breadth = AsyncMock(return_value={"up_count": 2500, "down_count": 1500, "avg_change": 0.5})
    market.get_price_history.return_value = {
        "data": [
            {"date": "2025-01-01", "open": 100, "high": 105, "low": 98, "close": 103, "volume": 1000000},
            {"date": "2025-01-02", "open": 103, "high": 110, "low": 102, "close": 108, "volume": 1200000},
            {"date": "2025-01-03", "open": 108, "high": 112, "low": 107, "close": 110, "volume": 900000},
        ],
        "count": 3,
    }
    market.get_market_snapshot.return_value = {"date": "2025-01-01", "total_stocks": 5000, "avg_change": 0.3}
    market.get_today_market_summary.return_value = {"indices": {"shanghai": 3300, "shenzhen": 10500}, "total_volume": 80000000000}
    market.get_index_overview.return_value = {"shanghai": {"price": 3300, "change_pct": 0.5}, "hs300": {"price": 4000, "change_pct": 0.3}}

    # ---- Preferences mock (via quant.preferences) ----
    quant.preferences = Mock()
    quant.preferences.get_industry_cognition = AsyncMock()
    quant.preferences.get_industry_cognition.return_value = Mock(
        text="科技行业偏好：关注AI、半导体、云计算", append_count=2
    )
    quant.preferences.append_industry_cognition = AsyncMock()
    quant.preferences.append_industry_cognition.return_value = Mock(
        sector="科技", status="appended"
    )

    # ---- Clock mock 返回值 ----
    clock.today = Mock()
    clock.today.isoformat.return_value = "2025-01-01"

    init_ctx(quant=quant, market=market, compiler=None, clock=clock)
    set_task_context(task_id="task-001", analysis_ids=["analysis-123"])
    return quant, market, clock


def get_tests():
    # Args schemas — still importable from tool modules
    from src.tools.knowledge import (
        SearchKBArgs, ReadArgs, GetNodeStateArgs, GetPreferencesArgs,
    )
    from src.tools.market import (
        GetRealtimePriceArgs, GetSectorOverviewArgs, GetTechnicalIndicatorsArgs,
        GetTechnicalChartArgs, GetMarketOverviewArgs,
        GetPriceHistoryArgs, GetMarketSnapshotArgs, GetPriceChartArgs,
        GetSectorSnapshotChartArgs,
    )
    from src.tools.review import (
        GetTradeArgs, GetNodeHistoryArgs,
    )
    from src.tools.writer import (
        CreateAnalysisArgs, UpdateNodeStateArgs, CreateTradeArgs,
        ApproveTradeArgs, RejectTradeArgs, CreateFeedbackArgs,
        AppendPreferenceArgs, CreateTriggerArgs,
        ListMyTriggersArgs, CancelTriggerArgs,
    )
    from src.tools.macro import (
        GetCurrentMacroReportArgs, GetTodayMacroItemsArgs,
        GetIndexOverviewArgs, UpdateMacroReportArgs,
    )

    # Coroutines — retrieved from registry
    C = get_coroutine

    return [
        # Group 0: 零依赖
        ("零依赖", "create_trigger(no_compiler)", C("create_trigger"), CreateTriggerArgs(
            name="test", condition_nl="涨5%", action_nl="重新分析")),

        # Group 1: 知识检索
        ("知识检索", "search_kb", C("search_kb"), SearchKBArgs(query_text="测试")),
        ("知识检索", "read(single)", C("read"), ReadArgs(refs="R1")),
        ("知识检索", "read(batch)", C("read"), ReadArgs(refs="R1,A1,F1")),
        ("知识检索", "get_node_state", C("get_node_state"), GetNodeStateArgs(node_name="node-1")),
        ("知识检索", "get_preferences", C("get_preferences"), GetPreferencesArgs(sector="科技")),

        # Group 2: 实时行情
        ("实时行情", "get_realtime_price", C("get_realtime_price"), GetRealtimePriceArgs(ticker="000001.SZ")),
        ("实时行情", "get_sector_overview", C("get_sector_overview"), GetSectorOverviewArgs(sector="白酒")),
        ("实时行情", "get_technical_indicators", C("get_technical_indicators"), GetTechnicalIndicatorsArgs(ticker="000001.SZ")),
        ("实时行情", "get_technical_chart", C("get_technical_chart"), GetTechnicalChartArgs(ticker="000001.SZ")),
        ("实时行情", "get_market_overview", C("get_market_overview"), GetMarketOverviewArgs()),
        ("实时行情", "get_market_overview(历史)", C("get_market_overview"), GetMarketOverviewArgs(date="2025-01-01")),

        # Group 3: 历史数据
        ("历史数据", "get_price_history", C("get_price_history"), GetPriceHistoryArgs(ticker="000001.SZ", from_date="2025-01-01", to_date="2025-01-03")),
        ("历史数据", "get_market_snapshot", C("get_market_snapshot"), GetMarketSnapshotArgs(date="2025-01-01")),
        ("历史数据", "get_price_chart", C("get_price_chart"), GetPriceChartArgs(ticker="000001.SZ", from_date="2025-01-01", to_date="2025-01-03")),
        ("历史数据", "get_sector_snapshot_chart", C("get_sector_snapshot_chart"), GetSectorSnapshotChartArgs(sector="885311.TI", date="2025-01-01")),

        # Group 4: 回顾
        ("回顾", "read(ref:A1)", C("read"), ReadArgs(refs="A1")),
        ("回顾", "get_trade", C("get_trade"), GetTradeArgs(trade_ref="T1")),
        ("回顾", "get_node_history", C("get_node_history"), GetNodeHistoryArgs(node_name="node-1")),

        # Group 5: 写入
        ("写入", "create_analysis", C("create_analysis"), CreateAnalysisArgs(title="测试分析", content="# 测试", analysis_type="impact_analysis", confidence=0.8, time_horizon="short_term")),
        ("写入", "update_node_state", C("update_node_state"), UpdateNodeStateArgs(node_name="node-1", core_logic="做多", state_summary="更新")),
        ("写入", "create_trade", C("create_trade"), CreateTradeArgs(operation_type="buy", symbol="000001.SZ", quantity=1000, price=15.5, rationale="测试", risk_level="medium")),
        ("写入", "approve_trade", C("approve_trade"), ApproveTradeArgs(trade_ref="T1")),
        ("写入", "reject_trade", C("reject_trade"), RejectTradeArgs(trade_ref="T1", reason="测试")),
        ("写入", "create_feedback", C("create_feedback"), CreateFeedbackArgs(title="复盘测试", judgment_correct=True, lessons_learned="测试经验")),
        ("写入", "append_preference", C("append_preference"), AppendPreferenceArgs(sector="科技", text="新认知")),
        ("写入", "list_my_triggers", C("list_my_triggers"), ListMyTriggersArgs(stock_name="茅台")),
        ("写入", "cancel_trigger", C("cancel_trigger"), CancelTriggerArgs(trigger_id="999")),

        # Group 6: 宏观
        ("宏观", "get_current_macro_report", C("get_current_macro_report"), GetCurrentMacroReportArgs()),
        ("宏观", "get_today_macro_items", C("get_today_macro_items"), GetTodayMacroItemsArgs()),
        ("宏观", "get_index_overview", C("get_index_overview"), GetIndexOverviewArgs()),
        ("宏观", "update_macro_report", C("update_macro_report"), UpdateMacroReportArgs(content="# 报告", summary="摘要")),
    ]


async def run_tests() -> int:
    setup_mocks()
    tests = get_tests()

    passed = 0
    failed = 0
    current_group = ""

    logger.info("=== 工具调用测试 ===\n")

    for group, name, coro, args in tests:
        if group != current_group:
            current_group = group
            logger.info(f"[{group}]")

        try:
            kwargs = args.model_dump()
            result = await coro(**kwargs)
            assert result is not None, "返回 None"
            assert isinstance(result, str), f"返回类型错误: {type(result)}"
            assert len(result) > 0, "返回空字符串"
            passed += 1
            preview = result.replace("\n", "\\n")[:100]
            logger.info(f"  OK  {name:35s} -> {preview}")
        except Exception as e:
            failed += 1
            logger.info(f"  FAIL {name:35s} -> {e}")

    logger.info(f"\n结果: {passed}/{passed+failed} 通过, {failed} 失败")
    return failed


if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    raise SystemExit(exit_code)
