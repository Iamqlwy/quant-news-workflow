from src.tools.schemas.knowledge import ReadArgs, SearchKBArgs
from src.tools.schemas.review import GetNodeHistoryArgs
import pytest
from pydantic import ValidationError

from src.tools.schemas.writer import (
    CreateAnalysisArgs,
    CreateFeedbackArgs,
    CreateNodeArgs,
    CreateTradeArgs,
    UpdateNodeStateArgs,
)


def test_knowledge_schema_accepts_separated_strings():
    search = SearchKBArgs.model_validate(
        {
            "query_text": "AI",
        }
    )
    read = ReadArgs.model_validate({"refs": "R1，A2\nF3"})

    assert read.refs == "R1,A2,F3"


def test_create_analysis_schema_normalizes_common_human_inputs():
    args = CreateAnalysisArgs.model_validate(
        {
            "title": "测试",
            "content": "正文",
            "analysis_kind": "影响分析",
            "term": "中期",
            "confidence": "80%",
            "evidence_ids": "R1，A2",
        }
    )

    assert args.analysis_type == "impact_analysis"
    assert args.time_horizon == "medium_term"
    assert args.confidence == 0.8
    assert args.root_raw_info_ids == ["R1", "A2"]


def test_update_node_state_schema_accepts_stringified_collections():
    args = UpdateNodeStateArgs.model_validate(
        {
            "name": "AI板块",
            "type": "板块",
            "drivers": '{"driver":"AI","evidence_ids":"R1"}',
            "evidence_ids": "R1，A2",
            "summary": {"brief": "继续跟踪"},
        }
    )

    assert args.node_name == "AI板块"
    assert args.node_type == "sector"
    assert args.primary_drivers == [{"driver": "AI", "evidence_ids": "R1"}]
    assert args.key_evidence_ids == ["R1", "A2"]
    assert args.state_summary == '继续跟踪'


def test_create_trade_schema_accepts_units_and_human_choices():
    args = CreateTradeArgs.model_validate(
        {
            "action": "买入",
            "stock_name": "贵州茅台",
            "quantity": "1.5万股",
            "price": "15.2元",
            "risk": "高风险",
            "rationale": {"why": "放量突破"},
        }
    )

    assert args.operation_type == "buy"
    assert args.symbol == "贵州茅台"
    assert args.quantity == 15000
    assert args.price == 15.2
    assert args.risk_level == "high"
    assert args.rationale == '放量突破'


def test_feedback_and_node_history_schema_accepts_common_aliases():
    feedback = CreateFeedbackArgs.model_validate(
        {
            "title": "复盘",
            "correct": "正确",
            "lesson": "控制仓位",
            "snapshot": '{"style":"risk_on"}',
        }
    )
    node_history = GetNodeHistoryArgs.model_validate({"stock_name": "宁德时代", "type": "公司"})
    node = CreateNodeArgs.model_validate({"name": "AI应用", "type": "概念", "alias": "AI应用,人工智能应用"})

    assert feedback.judgment_correct is True
    assert feedback.lessons_learned == "控制仓位"
    assert feedback.market_environment_snapshot == {"style": "risk_on"}
    assert node_history.node_name == "宁德时代"
    assert node_history.node_type == "company"
    assert node.node_type == "concept"
    assert node.aliases == ["AI应用", "人工智能应用"]

# ---------------------------------------------------------------------------
# 加固测试：字符串长度上限
# ---------------------------------------------------------------------------

def test_string_max_length_enforced():
    from src.tools.schemas.writer import CreateTriggerArgs

    with pytest.raises(ValidationError):
        CreateAnalysisArgs.model_validate({'title': 'x' * 501, 'content': '正文', 'analysis_type': 'impact_analysis'})

    with pytest.raises(ValidationError):
        CreateAnalysisArgs.model_validate({'title': '测试', 'content': 'x' * 100001, 'analysis_type': 'impact_analysis'})

    with pytest.raises(ValidationError):
        SearchKBArgs.model_validate({'query_text': 'x' * 2001})

    with pytest.raises(ValidationError):
        ReadArgs.model_validate({'refs': 'x' * 5001})

    with pytest.raises(ValidationError):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': 'x' * 10001, 'risk_level': 'low'})

    with pytest.raises(ValidationError):
        CreateFeedbackArgs.model_validate({'title': '复盘', 'judgment_correct': True, 'lessons_learned': 'x' * 10001})

    with pytest.raises(ValidationError):
        CreateTriggerArgs.model_validate({'name': 'x' * 201, 'condition_nl': '涨', 'action_nl': '卖'})


# ---------------------------------------------------------------------------
# 加固测试：数值范围
# ---------------------------------------------------------------------------

def test_numeric_range_enforced():
    with pytest.raises(ValidationError):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'quantity': -1})

    with pytest.raises(ValidationError):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'quantity': 100000001})

    with pytest.raises(ValidationError):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'price': -0.01})

    with pytest.raises(ValidationError):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'price': 10000001})

    with pytest.raises(ValidationError):
        CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'confidence': -0.1})

    with pytest.raises(ValidationError):
        CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'confidence': 1.1})


# ---------------------------------------------------------------------------
# 加固测试：列表上限
# ---------------------------------------------------------------------------

def test_list_max_length_enforced():
    ids_101 = [f'id_{i}' for i in range(101)]
    with pytest.raises(ValidationError):
        CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'root_raw_info_ids': ids_101})

    aliases_51 = [f'alias_{i}' for i in range(51)]
    with pytest.raises(ValidationError):
        CreateNodeArgs.model_validate({'name': '测试', 'node_type': 'concept', 'aliases': aliases_51})


# ---------------------------------------------------------------------------
# 加固测试：日期格式校验
# ---------------------------------------------------------------------------

def test_date_format_validation():
    from src.tools.schemas.market import GetMarketSnapshotArgs, GetPriceChartArgs, GetTechnicalChartArgs

    snapshot = GetMarketSnapshotArgs.model_validate({'date': '2025-06-11'})
    assert snapshot.date == '2025-06-11'

    with pytest.raises(ValidationError, match='日期无效'):
        GetMarketSnapshotArgs.model_validate({'date': '2025-13-01'})

    with pytest.raises(ValidationError, match='日期无效'):
        GetMarketSnapshotArgs.model_validate({'date': '2025-02-30'})

    with pytest.raises(ValidationError):
        GetMarketSnapshotArgs.model_validate({'date': 'not_a_date'})

    with pytest.raises(ValidationError):
        GetPriceChartArgs.model_validate({'stock_name': '茅台', 'from_date': 'not-a-date', 'to_date': '2025-01-01'})

    tech = GetTechnicalChartArgs.model_validate({'stock_name': '茅台', 'from_date': '2025-01-01'})
    assert tech.from_date == '2025-01-01'
    assert tech.to_date is None
    with pytest.raises(ValidationError):
        GetTechnicalChartArgs.model_validate({'stock_name': '茅台', 'from_date': 'bad-date'})


# ---------------------------------------------------------------------------
# 加固测试：枚举类 strict rejection
# ---------------------------------------------------------------------------

def test_strict_enum_rejection():
    from src.tools.schemas.writer import ReviewTradeArgs

    # 未识别的 analysis_type 现在回退为 "unknown" 而非报错
    args = CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'garbage'})
    assert args.analysis_type == 'unknown'

    with pytest.raises(ValidationError, match='无效的 time_horizon'):
        CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'time_horizon': 'forever'})

    with pytest.raises(ValidationError, match='无效的 operation_type'):
        CreateTradeArgs.model_validate({'operation_type': 'garbage', 'rationale': '测试', 'risk_level': 'low'})

    with pytest.raises(ValidationError, match='无效的 risk_level'):
        CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'garbage'})

    with pytest.raises(ValidationError, match='无效的 action'):
        ReviewTradeArgs.model_validate({'action': 'garbage'})

    with pytest.raises(ValidationError, match='无效的 node_type'):
        CreateNodeArgs.model_validate({'name': '测试', 'node_type': 'garbage'})


# ---------------------------------------------------------------------------
# 加固测试：边界值恰好通过
# ---------------------------------------------------------------------------

def test_boundary_values_pass():
    analysis = CreateAnalysisArgs.model_validate({'title': 'x' * 500, 'content': '正文', 'analysis_type': 'sentiment'})
    assert len(analysis.title) == 500

    ids_100 = [f'id_{i}' for i in range(100)]
    analysis = CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'root_raw_info_ids': ids_100})
    assert len(analysis.root_raw_info_ids) == 100

    trade = CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'quantity': 0, 'price': 0})
    assert trade.quantity == 0
    assert trade.price == 0

    trade = CreateTradeArgs.model_validate({'operation_type': 'buy', 'rationale': '测试', 'risk_level': 'low', 'quantity': 100000000, 'price': 10000000})
    assert trade.quantity == 100000000
    assert trade.price == 10000000

    analysis = CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'confidence': 0})
    assert analysis.confidence == 0
    analysis = CreateAnalysisArgs.model_validate({'title': '测试', 'content': '正文', 'analysis_type': 'impact_analysis', 'confidence': 1})
    assert analysis.confidence == 1


# ---------------------------------------------------------------------------
# 加固测试：alias + hardening 组合
# ---------------------------------------------------------------------------

def test_aliases_still_work_after_hardening():
    args = CreateAnalysisArgs.model_validate({
        'title': '测试', 'content': '正文',
        'analysis_kind': '驱动评估', 'term': '长期',
        'confidence': '90%', 'evidence_ids': ['R1'],
    })
    assert args.analysis_type == 'driver_assessment'
    assert args.time_horizon == 'long_term'
    assert args.confidence == 0.9

    trade = CreateTradeArgs.model_validate({
        'action': '卖出', 'rationale': '测试',
        'risk': '低风险', 'quantity': '100股',
    })
    assert trade.operation_type == 'sell'
    assert trade.risk_level == 'low'
    assert trade.quantity == 100

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 加固测试：中文映射和 unknown 回退
# ---------------------------------------------------------------------------

# --- analysis_type 中文映射全覆盖 ---

_CHINESE_ANALYSIS_TYPE_MAPPINGS = [
    # impact_analysis
    ("事件影响分析", "impact_analysis"),
    ("事件影响", "impact_analysis"),
    ("影响分析", "impact_analysis"),
    ("影响评估", "impact_analysis"),
    # driver_assessment
    ("驱动评估", "driver_assessment"),
    ("驱动因素评估", "driver_assessment"),
    ("事件驱动", "driver_assessment"),
    ("驱动分析", "driver_assessment"),
    ("驱动因子", "driver_assessment"),
    ("驱动因子分析", "driver_assessment"),
    ("驱动力分析", "driver_assessment"),
    ("因素驱动", "driver_assessment"),
    # risk_evaluation
    ("风险评估", "risk_evaluation"),
    ("风险分析", "risk_evaluation"),
    ("风险识别", "risk_evaluation"),
    # sentiment
    ("情绪", "sentiment"),
    ("情绪分析", "sentiment"),
    ("市场情绪", "sentiment"),
    ("市场情绪分析", "sentiment"),
    ("情绪面", "sentiment"),
    ("情绪面分析", "sentiment"),
    ("舆情", "sentiment"),
    ("舆情分析", "sentiment"),
]


@pytest.mark.parametrize("chinese_input,expected", _CHINESE_ANALYSIS_TYPE_MAPPINGS)
def test_analysis_type_chinese_mappings(chinese_input, expected):
    args = CreateAnalysisArgs.model_validate({"title": "测试", "content": "正文", "analysis_type": chinese_input})
    assert args.analysis_type == expected, f"{chinese_input!r} should map to {expected!r}, got {args.analysis_type!r}"


def test_analysis_type_english_mappings_still_work():
    english_cases = [
        ("impact_analysis", "impact_analysis"),
        ("driver_assessment", "driver_assessment"),
        ("risk_evaluation", "risk_evaluation"),
        ("sentiment", "sentiment"),
        ("impact", "impact_analysis"),
        ("driver", "driver_assessment"),
        ("drivers", "driver_assessment"),
        ("risk", "risk_evaluation"),
    ]
    for input_val, expected in english_cases:
        args = CreateAnalysisArgs.model_validate({"title": "测试", "content": "正文", "analysis_type": input_val})
        assert args.analysis_type == expected


def test_analysis_type_unknown_fallback():
    """完全未识别的值回退为 unknown"""
    args = CreateAnalysisArgs.model_validate({"title": "测试", "content": "正文", "analysis_type": "自定义分析类型"})
    assert args.analysis_type == "unknown"


def test_analysis_type_via_alias_with_unknown():
    """通过别名传入未识别值也应回退为 unknown"""
    args = CreateAnalysisArgs.model_validate({
        "title": "测试", "content": "正文",
        "analysis_kind": "something_weird",
    })
    assert args.analysis_type == "unknown"

# ---------------------------------------------------------------------------
# 加固测试：GetPriceChartArgs 日期可选
# ---------------------------------------------------------------------------

def test_get_price_chart_dates_optional():
    """from_date 和 to_date 均为可选"""
    from src.tools.schemas.market import GetPriceChartArgs
    args = GetPriceChartArgs.model_validate({"stock_name": "中钨高新"})
    assert args.stock_name == "中钨高新"
    assert args.from_date is None
    assert args.to_date is None

    args2 = GetPriceChartArgs.model_validate({"stock_name": "茅台", "from_date": "2025-06-01", "to_date": "2025-06-10"})
    assert args2.from_date == "2025-06-01"
    assert args2.to_date == "2025-06-10"

    # from_date / to_date 别名
    args3 = GetPriceChartArgs.model_validate({"stock_name": "茅台", "start_date": "2025-01-01", "end_date": "2025-01-15"})
    assert args3.from_date == "2025-01-01"
    assert args3.to_date == "2025-01-15"
