"""流水线状态枚举"""

from enum import StrEnum


class TaskState(StrEnum):
    # 行业/个股
    INGESTED = "ingested"
    PREPROCESSED = "preprocessed"
    DEEP_ANALYZING = "deep_analyzing"
    DEEP_ANALYZED = "deep_analyzed"
    RISK_CHECKING = "risk_checking"
    SKIPPED = "skipped"
    NO_VALUE = "no_value"  # 深度分析判定无分析价值，无需复盘
    REFLECTION_PENDING = "reflection_pending"
    REFLECTION_EXECUTING = "reflection_executing"
    REFLECTION_COMPLETE = "reflection_complete"
    RISK_VERIFIED = "risk_verified"
    EXPIRED = "expired"
    ERROR = "error"

    # 宏观
    MACRO_QUEUED = "macro_queued"  # 非紧急，等待日报消费
    MACRO_URGENT = "macro_urgent"  # 紧急，立即批量消费
    MACRO_REPORT_UPDATED = "macro_report_updated"


# orchestrator.tick() 需要轮询处理的状态
ACTIONABLE_STATES: set[TaskState] = {
    TaskState.INGESTED,
    TaskState.DEEP_ANALYZING,
    TaskState.DEEP_ANALYZED,
    TaskState.RISK_CHECKING,
    TaskState.RISK_VERIFIED,
    TaskState.REFLECTION_PENDING,
    TaskState.REFLECTION_EXECUTING,
    TaskState.MACRO_URGENT,
}

TERMINAL_STATES = {
    TaskState.SKIPPED,
    TaskState.REFLECTION_COMPLETE,
    TaskState.EXPIRED,
    TaskState.MACRO_REPORT_UPDATED,
}

