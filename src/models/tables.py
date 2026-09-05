"""本地 SQL 表模型 —— wfuse"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.timezone import BEIJING_TZ
from src.db import Base


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    """ORM default 安全网：调用方应显式传入 clock.now，模拟模式下此默认值为挂钟时间。"""
    return datetime.now(BEIJING_TZ)


class Task(Base):
    """每个资讯项在 workflow 流水线中的状态"""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    raw_info_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(50), nullable=False, default="crawled", index=True)
    info_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # macro/industry/concept/company

    # 重要性判断
    significance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    significance_rationale: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # KB 中的外键（支持多轮分析/交易/反馈，按时间顺序追加）
    analysis_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    trade_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    feedback_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)

    # 复盘时间
    reflection_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 触发来源（由 trigger 触发 deep_analysis 时记录）
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PriceMonitor(Base):
    """活跃的价格监控 —— 最多几百条"""

    __tablename__ = "price_monitors"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    trade_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # long / short
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / triggered / expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TriggerRecord(Base):
    """一次性触发器"""

    __tablename__ = "triggers"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    source_analysis_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # 单一条件树（JSON AND/OR）
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 触发后分析重点（Agent 输入的关注文本）
    focus_on: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # 触发动作
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="deep_analysis"
    )  # deep_analysis / trade
    action_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # JSON
    trade_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # 时间窗口（编译时从 NL 提取）
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Entity(Base):
    """每次 Agent 运行涉及的实体记录（A/T/F/N/G/R），source 区分 search / read / create"""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    entity_type: Mapped[str] = mapped_column(String(5), nullable=False, index=True)  # A/T/F/N/G/R
    entity_uuid: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)  # 短引用，如 A1/T2
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="read")  # read / create
    task_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CrawlerState(Base):
    """爬虫各源进度"""

    __tablename__ = "crawler_state"

    source_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_article_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    articles_crawled: Mapped[int] = mapped_column(Integer, default=0)

