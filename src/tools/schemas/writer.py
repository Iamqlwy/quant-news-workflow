from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import ToolArgsModel


def _normalize_str_list(items: list[Any] | None) -> list[str] | None:
    if not items:
        return None
    result = []
    for item in items:
        text = ToolArgsModel.as_str(item)
        if text:
            result.append(text)
    return result or None


def _normalize_node_type(value: Any) -> str | None:
    return ToolArgsModel.normalize_choice(
        value,
        {
            "company": "company",
            "\u516c\u53f8": "company",
            "\u4e2a\u80a1": "company",
            "stock": "company",
            "sector": "sector",
            "\u884c\u4e1a": "sector",
            "\u677f\u5757": "sector",
            "macro_theme": "macro_theme",
            "\u5b8f\u89c2\u4e3b\u9898": "macro_theme",
            "macro": "macro_theme",
            "concept": "concept",
            "\u6982\u5ff5": "concept",
            "product": "product",
            "\u4ea7\u54c1": "product",
            "policy": "policy",
            "\u653f\u7b56": "policy",
            "institution": "institution",
            "\u673a\u6784": "institution",
            "region": "region",
            "\u5730\u533a": "region",
            "person": "person",
            "\u4eba\u7269": "person",
        },
    )


_VALID_NODE_TYPES = {"company", "sector", "macro_theme", "concept", "product", "policy", "institution", "region", "person"}

_VALID_ANALYSIS_TYPES = {"impact_analysis", "driver_assessment", "risk_evaluation", "sentiment", "unknown"}

_VALID_TIME_HORIZONS = {"short_term", "medium_term", "long_term"}

_VALID_OPERATION_TYPES = {"buy", "sell", "skip"}

_VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}

_VALID_ACTIONS = {"approve", "reject"}


def _normalize_analysis_type(value: Any) -> str | None:
    text = ToolArgsModel.as_str(value)
    if not text:
        return None
    result = ToolArgsModel.normalize_choice(
        text,
        {
            "impact_analysis": "impact_analysis",
            "impact": "impact_analysis",
            "影响分析": "impact_analysis",
            "事件影响分析": "impact_analysis",
            "事件影响": "impact_analysis",
            "影响评估": "impact_analysis",
            "driver_assessment": "driver_assessment",
            "driver": "driver_assessment",
            "drivers": "driver_assessment",
            "驱动评估": "driver_assessment",
            "驱动因素评估": "driver_assessment",
            "事件驱动": "driver_assessment",
            "驱动分析": "driver_assessment",
            "驱动因子": "driver_assessment",
            "驱动因子分析": "driver_assessment",
            "驱动力分析": "driver_assessment",
            "因素驱动": "driver_assessment",
            "risk_evaluation": "risk_evaluation",
            "risk": "risk_evaluation",
            "风险评估": "risk_evaluation",
            "风险分析": "risk_evaluation",
            "风险识别": "risk_evaluation",
            "sentiment": "sentiment",
            "情绪": "sentiment",
            "情绪分析": "sentiment",
            "市场情绪": "sentiment",
            "市场情绪分析": "sentiment",
            "情绪面": "sentiment",
            "情绪面分析": "sentiment",
            "舆情": "sentiment",
            "舆情分析": "sentiment",
        },
    )
    if result in _VALID_ANALYSIS_TYPES:
        return result
    return "unknown"


def _normalize_time_horizon(value: Any) -> str | None:
    return ToolArgsModel.normalize_choice(
        value,
        {
            "short_term": "short_term",
            "\u77ed\u671f": "short_term",
            "short": "short_term",
            "medium_term": "medium_term",
            "\u4e2d\u671f": "medium_term",
            "medium": "medium_term",
            "mid_term": "medium_term",
            "long_term": "long_term",
            "\u957f\u671f": "long_term",
            "long": "long_term",
        },
    )


class CreateAnalysisArgs(ToolArgsModel):
    title: str = Field(..., max_length=500, description="分析标题")
    content: str = Field(..., max_length=100000, description="分析正文（Markdown）")
    analysis_type: str = Field(..., max_length=50, description="分析类型：impact_analysis/driver_assessment/risk_evaluation/sentiment")
    agent_id: str | None = Field(default=None, max_length=100, description="执行分析的 Agent 标识")
    confidence: float | None = Field(default=None, ge=0, le=1, description="信心水平 0.0~1.0")
    time_horizon: str | None = Field(default=None, max_length=20, description="short_term / medium_term / long_term")
    root_raw_info_ids: list[str] | None = Field(default=None, max_length=100, description="触发该分析的原始资讯 ID 列表")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "analysis_type": ("type", "analysis_kind"),
                "agent_id": ("agent",),
                "time_horizon": ("horizon", "term"),
                "root_raw_info_ids": ("root_raw_info_id", "raw_info_ids", "evidence_ids"),
            },
        )

    @field_validator("title", "content", "analysis_type", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text

    @field_validator("analysis_type", mode="after")
    @classmethod
    def _normalize_analysis_type_value(cls, v: str) -> str:
        normalized = _normalize_analysis_type(v)
        if not normalized or normalized not in _VALID_ANALYSIS_TYPES:
            raise ValueError(f"无效的 analysis_type：'{v}'，可选值：impact_analysis / driver_assessment / risk_evaluation / sentiment")
        return normalized

    @field_validator("agent_id", "time_horizon", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("time_horizon", mode="after")
    @classmethod
    def _normalize_time_horizon_value(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = _normalize_time_horizon(v)
        if not normalized or normalized not in _VALID_TIME_HORIZONS:
            raise ValueError(f"无效的 time_horizon：'{v}'，可选值：short_term / medium_term / long_term")
        return normalized

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> float | None:
        try:
            value = cls.as_float(v)
        except (ValueError, TypeError):
            return None
        if value is None:
            return None
        return value

    @field_validator("root_raw_info_ids", mode="before")
    @classmethod
    def _parse_root_ids(cls, v: Any) -> list[str] | None:
        return _normalize_str_list(cls.as_list(v))


class UpdateNodeStateArgs(ToolArgsModel):
    node_name: str = Field(..., max_length=200, description="WorldNode 名称（如'贵州茅台'）")
    core_logic: str | None = Field(default=None, max_length=20000, description="核心投资逻辑")
    primary_drivers: list | None = Field(
        default=None, max_length=100, description="主要驱动因素列表 [{driver, strength, evidence_ids}]"
    )
    risks: list | None = Field(default=None, max_length=100, description="风险列表 [{risk, severity, evidence_ids}]")
    focus_points: list | None = Field(default=None, max_length=100, description="后续关注点 [{point, priority, evidence_ids}]")
    recent_changes: str | None = Field(default=None, max_length=5000, description="最近变化摘要")
    key_evidence_ids: list[str] | None = Field(default=None, max_length=100, description="支撑该状态的关键证据 ID 列表")
    state_summary: str | None = Field(default=None, max_length=5000, description="Agent 压缩摘要")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "node_name": ("name", "target_node_name"),
                "core_logic": ("logic",),
                "primary_drivers": ("drivers",),
                "focus_points": ("points", "follow_up_points"),
                "key_evidence_ids": ("evidence_ids",),
                "state_summary": ("summary",),
            },
        )

    @field_validator("node_name", mode="before")
    @classmethod
    def _coerce_node_name(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("node_name 不能为空")
        return text

    @field_validator("core_logic", "recent_changes", "state_summary", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("primary_drivers", "risks", "focus_points", mode="before")
    @classmethod
    def _parse_object_lists(cls, v: Any) -> list | None:
        return cls.as_list(v)

    @field_validator("key_evidence_ids", mode="before")
    @classmethod
    def _parse_key_evidence_ids(cls, v: Any) -> list[str] | None:
        return _normalize_str_list(cls.as_list(v))


class CreateTradeArgs(ToolArgsModel):
    operation_type: str = Field(..., max_length=10, description="buy / sell / skip")
    target_node_name: str | None = Field(default=None, max_length=200, description="目标 WorldNode 名称（如'贵州茅台'）")
    trigger_analysis_ref: str | None = Field(default=None, max_length=100, description="触发该操作的分析引用（如 A1）")
    symbol: str | None = Field(default=None, max_length=200, description="股票名称（如'贵州茅台'），系统自动解析为代码")
    quantity: float | None = Field(default=None, ge=0, le=100000000, description="交易数量（股）")
    price: float | None = Field(default=None, ge=0, le=10000000, description="建议价格")
    rationale: str = Field(..., max_length=10000, description="操作理由")
    expected_impact: str | None = Field(default=None, max_length=2000, description="预期影响")
    risk_level: str = Field(..., max_length=10, description="风险等级：low/medium/high/critical")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "operation_type": ("action", "operation", "op"),
                "target_node_name": ("node_name", "name"),
                "trigger_analysis_ref": ("analysis_ref", "source_analysis_ref"),
                "symbol": ("stock_name", "ticker"),
                "expected_impact": ("impact",),
                "risk_level": ("risk",),
            },
        )

    @field_validator("operation_type", mode="before")
    @classmethod
    def _coerce_operation_type(cls, v: Any) -> str:
        mapping = {
            "\u4e70\u5165": "buy",
            "\u4e70": "buy",
            "buy": "buy",
            "\u5356\u51fa": "sell",
            "\u5356": "sell",
            "sell": "sell",
            "\u89c2\u671b": "skip",
            "\u8df3\u8fc7": "skip",
            "skip": "skip",
        }
        text = cls.as_str(v).lower()
        if not text:
            raise ValueError("operation_type 不能为空")
        return mapping.get(text, text)

    @field_validator("operation_type", mode="after")
    @classmethod
    def _validate_operation_type(cls, v: str) -> str:
        if v not in _VALID_OPERATION_TYPES:
            raise ValueError(f"无效的 operation_type：'{v}'，可选值：buy / sell / skip")
        return v

    @field_validator("target_node_name", "trigger_analysis_ref", "symbol", "expected_impact", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("quantity", "price", mode="before")
    @classmethod
    def _coerce_number(cls, v: Any) -> float | None:
        try:
            return cls.as_float(v)
        except (ValueError, TypeError):
            return None

    @field_validator("rationale", mode="before")
    @classmethod
    def _coerce_rationale(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("rationale 不能为空")
        return text

    @field_validator("risk_level", mode="before")
    @classmethod
    def _coerce_risk_level(cls, v: Any) -> str:
        mapping = {
            "\u4f4e": "low",
            "\u4f4e\u98ce\u9669": "low",
            "medium": "medium",
            "\u4e2d": "medium",
            "\u4e2d\u7b49": "medium",
            "\u4e2d\u98ce\u9669": "medium",
            "high": "high",
            "\u9ad8": "high",
            "\u9ad8\u98ce\u9669": "high",
            "critical": "critical",
            "\u4e25\u91cd": "critical",
            "\u4e25\u91cd\u98ce\u9669": "critical",
        }
        text = cls.as_str(v).lower()
        if not text:
            raise ValueError("risk_level 不能为空")
        return mapping.get(text, text)

    @field_validator("risk_level", mode="after")
    @classmethod
    def _validate_risk_level(cls, v: str) -> str:
        if v not in _VALID_RISK_LEVELS:
            raise ValueError(f"无效的 risk_level：'{v}'，可选值：low / medium / high / critical")
        return v


class ReviewTradeArgs(ToolArgsModel):
    action: str = Field(default="approve", max_length=10, description="审批决定：'approve'（批准）或 'reject'（拒绝）")
    trade_ref: str | None = Field(default=None, max_length=100, description="交易引用（如 T1）")
    note: str = Field(default="", max_length=5000, description="批准时的风险评估备注，或拒绝时的详细原因")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"trade_ref": ("ref",), "note": ("reason", "comment")})

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, v: Any) -> str:
        mapping = {"\u6279\u51c6": "approve", "\u901a\u8fc7": "approve", "approve": "approve", "\u62d2\u7edd": "reject", "\u9a73\u56de": "reject", "reject": "reject"}
        text = cls.as_str(v).lower()
        return mapping.get(text, text or "approve")

    @field_validator("action", mode="after")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in _VALID_ACTIONS:
            raise ValueError(f"无效的 action：'{v}'，可选值：approve / reject")
        return v

    @field_validator("trade_ref", mode="before")
    @classmethod
    def _coerce_trade_ref(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("note", mode="before")
    @classmethod
    def _coerce_note(cls, v: Any) -> str:
        return cls.as_str(v)


class CreateFeedbackArgs(ToolArgsModel):
    title: str = Field(..., max_length=500, description="复盘标题")
    trigger_analysis_ref: str | None = Field(default=None, max_length=100, description="被复盘的分析引用（如 A1）")
    trigger_trade_ref: str | None = Field(default=None, max_length=100, description="关联的交易引用（如 T1）")
    expected_outcome: str | None = Field(default=None, max_length=5000, description="当时预期的结果")
    actual_outcome: str | None = Field(default=None, max_length=5000, description="实际发生的结果")
    judgment_correct: bool = Field(default=False, description="方向判断是否正确")
    error_reason: str | None = Field(default=None, max_length=10000, description="错误原因分析")
    missed_factors: str | None = Field(default=None, max_length=10000, description="遗漏的因素")
    adjustment_suggestions: str | None = Field(default=None, max_length=10000, description="后续调整建议")
    market_environment_snapshot: dict | None = Field(default=None, description="复盘时的市场环境快照")
    lessons_learned: str = Field(..., max_length=10000, description="提炼的经验教训")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "trigger_analysis_ref": ("analysis_ref",),
                "trigger_trade_ref": ("trade_ref",),
                "judgment_correct": ("is_correct", "correct"),
                "market_environment_snapshot": ("market_snapshot", "snapshot"),
                "lessons_learned": ("lessons", "lesson"),
            },
        )

    @field_validator("title", "lessons_learned", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text

    @field_validator(
        "trigger_analysis_ref",
        "trigger_trade_ref",
        "expected_outcome",
        "actual_outcome",
        "error_reason",
        "missed_factors",
        "adjustment_suggestions",
        mode="before",
    )
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("judgment_correct", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> bool:
        return cls.as_bool(v)

    @field_validator("market_environment_snapshot", mode="before")
    @classmethod
    def _coerce_snapshot(cls, v: Any) -> dict | None:
        return cls.as_dict(v)


class AppendPreferenceArgs(ToolArgsModel):
    sector: str = Field(..., max_length=200, description="行业/板块名称")
    text: str = Field(..., max_length=10000, description="新增的认知段落")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"sector": ("industry", "sector_name", "name"), "text": ("content", "body")})

    @field_validator("sector", mode="before")
    @classmethod
    def _clean_sector(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        # 去除换行符、斜杠、反斜杠等多余字符
        for ch in ("\n", "\r", "/", "\\"):
            text = text.replace(ch, "")
        return text.strip()

    @field_validator("text", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text


class AppendMarketPreferenceArgs(ToolArgsModel):
    text: str = Field(..., max_length=10000, description="新增的市场认知段落（行情面全局观察：指数趋势、市场风格、风险偏好、板块轮动等）")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"text": ("content", "body", "summary")})

    @field_validator("text", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("text 不能为空")
        return text


class CreateTriggerArgs(ToolArgsModel):
    name: str = Field(..., max_length=200, description="触发器名称")
    condition_nl: str = Field(..., max_length=2000, description="触发条件的自然语言描述（含时间约束，如'三天后茅台突破2000元'）")
    action_nl: str = Field(..., max_length=2000, description="触发后做什么的自然语言描述（如'重新分析茅台'、'贵州茅台平仓止损'）,描述必须要详细。")
    focus_on: str | None = Field(default=None, max_length=2000, description="触发后分析时需要重点关注的因果逻辑和传导链条（如'重新评估突破背后的基本面逻辑'），非行情走向或技术指标")
    trade_ref: str | None = Field(default=None, max_length=100, description="关联的交易引用（如 T1）")
    source_analysis_ref: str | None = Field(default=None, max_length=100, description="关联的分析引用（如 A1）")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "condition_nl": ("condition",),
                "action_nl": ("action",),
                "focus_on": ("focus",),
                "trade_ref": ("trigger_trade_ref",),
                "source_analysis_ref": ("analysis_ref",),
            },
        )

    @field_validator("name", "condition_nl", "action_nl", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text

    @field_validator("focus_on", "trade_ref", "source_analysis_ref", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)


class ListMyTriggersArgs(ToolArgsModel):
    stock_name: str = Field(..., max_length=200, description="股票名称（如'贵州茅台'），用于筛选包含该名称的触发器")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"stock_name": ("ticker", "symbol", "name")})

    @field_validator("stock_name", mode="before")
    @classmethod
    def _coerce_stock_name(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("stock_name 不能为空")
        return text


class CancelTriggerArgs(ToolArgsModel):
    trigger_ref: str = Field(..., max_length=100, description="触发器的会话引用（如 G1，由 list_my_triggers 或 create_trigger 返回）")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"trigger_ref": ("ref", "id")})

    @field_validator("trigger_ref", mode="before")
    @classmethod
    def _coerce_trigger_ref(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("trigger_ref 不能为空")
        return text


class CreateNodeArgs(ToolArgsModel):
    name: str = Field(..., max_length=200, description="节点名称，如 '贵州茅台'、'白酒板块'、'货币政策'。禁止包含时间，代码，斜杠等特殊字符")
    node_type: str = Field(
        ...,
        max_length=20,
        description="节点类型：company / sector / macro_theme / concept / product / policy / institution / region / person",
    )
    description: str | None = Field(default=None, max_length=5000, description="简要描述")
    ticker: str | None = Field(default=None, max_length=20, description="股票名称（如'贵州茅台'），仅 company 类型使用，禁止传入代码")
    aliases: list[str] | None = Field(default=None, max_length=50, description="别名列表，用于实体匹配")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "node_type": ("type",),
                "ticker": ("stock_name", "symbol"),
                "aliases": ("alias",),
            },
        )

    @field_validator("name", "node_type", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text

    @field_validator("node_type", mode="after")
    @classmethod
    def _normalize_node_type_value(cls, v: str) -> str:
        normalized = _normalize_node_type(v)
        if not normalized or normalized not in _VALID_NODE_TYPES:
            raise ValueError(f"无效的 node_type：'{v}'，可选值：company / sector / macro_theme / concept / product / policy / institution / region / person")
        return normalized

    @field_validator("description", "ticker", mode="before")
    @classmethod
    def _coerce_optional_text(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("aliases", mode="before")
    @classmethod
    def _parse_aliases(cls, v: Any) -> list[str] | None:
        return _normalize_str_list(cls.as_list(v))
