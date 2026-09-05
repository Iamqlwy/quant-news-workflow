"""写入工具 —— 创建分析、更新节点、交易操作、复盘、偏好、触发器"""

from __future__ import annotations

import ast
import contextlib
from datetime import datetime, timedelta
from uuid import UUID

from kbquant.schemas.analysis import AnalysisCreate
from kbquant.schemas.feedback import FeedbackCreate
from kbquant.schemas.node import NodeStateCreate, WorldNodeCreate
from kbquant.schemas.trading import TradingOperationCreate, TradingOperationUpdate
from loguru import logger

from src.core.timezone import BEIJING_TZ
from src.utils.datetime_utils import ensure_beijing_datetime

from ._deps import (
    _ensure_list,
    _is_transient_tool_error,
    err,
    format_exception,
    get_latest_ref,
    get_task_context,
    ok,
    register_entity,
    resolve_node_name,
    resolve_ref,
    resolve_ref_or_original,
)
from .context import get_ctx
from .registry import register_tool
from .schemas.writer import (
    AppendMarketPreferenceArgs,
    AppendPreferenceArgs,
    CancelTriggerArgs,
    CreateAnalysisArgs,
    CreateFeedbackArgs,
    CreateNodeArgs,
    CreateTradeArgs,
    CreateTriggerArgs,
    ListMyTriggersArgs,
    ReviewTradeArgs,
    UpdateNodeStateArgs,
)


def _parse_uuid(v: str | None) -> UUID | None:
    if v is None:
        return None
    try:
        return UUID(v)
    except (ValueError, AttributeError):
        return None


def _parse_uuid_list(v: list[str] | None) -> list[UUID] | None:
    if v is None:
        return None
    result = []
    for item in v:
        resolved = resolve_ref_or_original(item)
        uid = _parse_uuid(resolved)
        if uid:
            result.append(uid)
    return result or None


def _resolve_single_ref(ref: str) -> str | None:
    """解析单个引用字符串，返回 UUID 字符串或 None。

    防御 LLM 传入逗号分隔的多引用（如 'A1,A2'）：拆开后取第一个有效 UUID。
    解析结果非有效 UUID 时返回 None。
    """
    for part in ref.split(","):
        part = part.strip()
        if not part:
            continue
        resolved = resolve_ref_or_original(part)
        if _parse_uuid(resolved):
            return resolved
    return None


def _coerce_list_items_to_dict(items: list | None) -> list | None:
    """将列表中形如 dict 的字符串转为真正的 dict。

    LLM 输出的 primary_drivers/risks/focus_points 可能是字符串而非 dict，
    如 "{'risk': '...', 'evidence_ids': ['R1']}"。
    Pydantic NodeStateCreate 要求 list[dict]，必须先转换。
    """
    if items is None:
        return None
    result = []
    for item in items:
        if isinstance(item, str):
            with contextlib.suppress(ValueError, SyntaxError):
                item = ast.literal_eval(item)
        if isinstance(item, dict):
            result.append(item)
    return result


def _resolve_evidence_ids(items: list | None) -> list | None:
    """递归解析列表中每个 dict 的 evidence_ids 字段中的 session 引用"""
    if items is None:
        return None
    for item in items:
        if isinstance(item, dict) and "evidence_ids" in item:
            ids = _ensure_list(item["evidence_ids"])
            if ids:
                item["evidence_ids"] = [resolve_ref_or_original(eid) for eid in ids]
    return items


# ═══════════════════════════════════════════════
# 工具协程
# ═══════════════════════════════════════════════


def _get_custom_time() -> datetime | None:
    """获取当前项目时钟时间（北京时间），用于 kbquant custom_time。
    实盘模式返回 None（使用服务器时间），模拟模式返回模拟时钟时间。"""
    ctx = get_ctx()
    if ctx.clock is not None and not ctx.clock.is_realtime:
        return ctx.clock.now
    return None


@register_tool(
    name="create_analysis",
    description="将深度分析报告写入知识库，返回分析引用（如 A1）。"
    "analysis_type 类型：impact_analysis（影响分析）/ driver_assessment（驱动因素评估）/ risk_evaluation（风险评估）/ sentiment（情绪分析）。"
    "适用：完成一轮分析研究后，将分析结论持久化保存、供后续引用和复盘。"
    "不适用：仅做初步探索或临时推演；更新节点投资逻辑时用 update_node_state；记录复盘反思时用 create_feedback。"
    "重要：content 中不要出现 R1/R2/A1 等会话引用，改用描述替代。",
    category="writer",
    args_schema=CreateAnalysisArgs,
)
async def create_analysis(
    title: str,
    content: str,
    analysis_type: str,
    agent_id: str | None = None,
    confidence: float | None = None,
    time_horizon: str | None = None,
    root_raw_info_ids: list[str] | None = None,
) -> str:
    try:
        resolved_ids = None
        if root_raw_info_ids:
            resolved_ids = [resolve_ref_or_original(rid) for rid in root_raw_info_ids]
        quant = get_ctx().quant
        result = await quant.analysis.create(
            AnalysisCreate(
                title=title,
                content=content,
                analysis_type=analysis_type,
                agent_id=agent_id,
                confidence=confidence,
                time_horizon=time_horizon,
                root_raw_info_ids=_parse_uuid_list(resolved_ids),
                custom_time=_get_custom_time(),
            )
        )
        analysis_id = str(result.id)
        ref = register_entity("A", analysis_id, source="create")
        return ok({"status": "created", "analysis_ref": ref})
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("create_analysis failed: {}", e)
        return err(f"创建分析失败：{e}")


@register_tool(
    name="update_node_state",
    description="更新 WorldNode 投资逻辑状态，推送新版本。node_name 填名称（如'贵州茅台'）而非 ID。"
    "所有字段均为可选，只需填写有变化的字段（增量更新），未填字段保持原值不变。"
    "适用：基于新分析结论或市场信息，更新对某个标的/板块/主题的认知状态。"
    "不适用：创建新分析报告时用 create_analysis；记录交易反思时用 create_feedback。",
    category="writer",
    args_schema=UpdateNodeStateArgs,
)
async def update_node_state(
    node_name: str,
    core_logic: str | None = None,
    primary_drivers: list | None = None,
    risks: list | None = None,
    focus_points: list | None = None,
    recent_changes: str | None = None,
    key_evidence_ids: list[str] | None = None,
    state_summary: str | None = None,
) -> str:
    try:
        node_id = await resolve_node_name(node_name)
        quant = get_ctx().quant
        await quant.nodes.update_state(
            node_id,
            NodeStateCreate(
                core_logic=core_logic,
                primary_drivers=_resolve_evidence_ids(_coerce_list_items_to_dict(primary_drivers)),
                risks=_resolve_evidence_ids(_coerce_list_items_to_dict(risks)),
                focus_points=_resolve_evidence_ids(_coerce_list_items_to_dict(focus_points)),
                recent_changes=recent_changes,
                key_evidence_ids=_parse_uuid_list(key_evidence_ids),
                state_summary=state_summary,
                custom_time=_get_custom_time(),
            ),
        )
        return ok({"status": "updated"})
    except ValueError as e:
        logger.warning("update_node_state: node not found: {}", e)
        return err(f"找不到节点「{node_name}」：{e}")
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("update_node_state failed: {}", e)
        return err(f"更新节点状态失败：{e}")


@register_tool(
    name="create_node",
    description="创建新的 WorldNode（如新股、新概念板块、新政策主题等）。"
    "node_type 可选值：company（公司）/ sector（板块）/ macro_theme（宏观主题）/ concept（概念）/ product（产品）/ policy（政策）/ institution（机构）/ region（地区）/ person（人物）。"
    "ticker 参数仅在 node_type='company' 时需要填写，填股票名称（如'贵州茅台'），系统自动解析为代码。"
    "适用：分析中发现重要的新标的、新概念、新政策方向，需要在知识库中建立对应节点。"
    "不适用：更新已有节点的投资逻辑时用 update_node_state。",
    category="writer",
    args_schema=CreateNodeArgs,
)
async def create_node(
    name: str,
    node_type: str,
    description: str | None = None,
    ticker: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    try:
        # 若 ticker 为股票名称，解析为标准化代码
        resolved_ticker = None
        if ticker and node_type == "company":
            market = get_ctx().market if get_ctx() else None
            if market:
                from src.tools.market import _resolve_to_ticker as _resolve_stock

                try:
                    resolved_ticker = _resolve_stock(market, ticker, allow_non_a_share=True)
                except ValueError as e:
                    logger.warning("create_node 解析股票名称失败: {}", e)
                    return err(f"无法解析股票名称「{ticker}」：{e}")

        quant = get_ctx().quant

        # ---- 预创建去重检查 ----
        similarity_warnings: list[str] = []
        try:
            all_items = await quant.nodes.list_names_and_aliases()
            search_name_lower = name.strip().lower()

            exact_same_type = None
            exact_diff_type: list[dict] = []
            fuzzy_matches: list[str] = []

            for it in all_items:
                it_dict = it if isinstance(it, dict) else it.model_dump()
                it_name = str(it_dict.get("name", "")).strip().lower()
                it_type = str(it_dict.get("node_type", ""))

                if it_name == search_name_lower:
                    if it_type == node_type:
                        exact_same_type = it_dict
                    else:
                        exact_diff_type.append(it_dict)
                elif search_name_lower in it_name or it_name in search_name_lower:
                    from difflib import SequenceMatcher
                    ratio = SequenceMatcher(None, search_name_lower, it_name).ratio()
                    if ratio >= 0.7:
                        fuzzy_matches.append(f"「{it_dict.get('name', '')}」(类型:{it_type})")

            if exact_same_type:
                existing_id = str(exact_same_type["id"])
                ref = register_entity("N", existing_id, source="read")
                return ok({
                    "status": "already_exists",
                    "node_ref": ref,
                    "name": name,
                    "node_type": node_type,
                    "warning": f"节点「{name}」(类型:{node_type}) 已存在，返回已有节点。",
                })

            if exact_diff_type:
                type_names = [f"「{d['name']}」(类型:{d['node_type']})" for d in exact_diff_type[:3]]
                similarity_warnings.append(f"已存在同名但不同类型的节点: {', '.join(type_names)}")

            if fuzzy_matches:
                similarity_warnings.append(f"存在相似节点: {', '.join(fuzzy_matches[:5])}")

        except Exception as exc:
            logger.debug("create_node 去重检查失败: {}", exc)

        result = await quant.nodes.create(
            WorldNodeCreate(
                name=name,
                node_type=node_type,
                description=description,
                ticker=resolved_ticker,
                aliases=aliases,
                custom_time=_get_custom_time(),
            )
        )
        node_id = str(result.id)
        ref = register_entity("N", node_id, source="create")
        resp: dict[str, object] = {"status": "created", "node_ref": ref}
        if similarity_warnings:
            resp["similarity_warnings"] = similarity_warnings
        return ok(resp)
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("create_node failed: {}", e)
        return err(f"创建节点失败：{e}")


@register_tool(
    name="create_trade",
    description="生成模拟交易建议并写入系统，返回交易引用（如 T1）。target_node_name 填节点名称而非 ID。"
    "仅支持 A 股交易，symbol 请使用 A 股名称（如「贵州茅台」「平安银行」），系统自动解析为代码。"
    "港股、美股均不支持，传入会被拒绝。"
    "operation_type 可选值：buy（买入）/ sell（卖出）/ skip（观望）。"
    "risk_level 可选值：low（低风险）/ medium（中等风险）/ high（高风险）/ critical（严重风险）。"
    "适用：分析完成后需要执行买卖操作、或明确选择观望/跟踪。"
    "不适用：仅做分析不做交易建议时只用 create_analysis；审批交易时用 review_trade。",
    category="writer",
    args_schema=CreateTradeArgs,
)
async def create_trade(
    operation_type: str,
    rationale: str,
    risk_level: str,
    target_node_name: str | None = None,
    trigger_analysis_ref: str | None = None,
    symbol: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    expected_impact: str | None = None,
) -> str:
    try:
        resolved_node_id = None
        if target_node_name:
            try:
                resolved_node_id = await resolve_node_name(target_node_name)
            except ValueError as e:
                logger.warning("create_trade: node not found '{}': {}", target_node_name, e)
                resolved_node_id = None
        resolved_analysis_id = None
        if trigger_analysis_ref:
            resolved_analysis_id = resolve_ref_or_original(trigger_analysis_ref)

        # 若 symbol 为股票名称，解析为标准化代码
        resolved_symbol = None
        if symbol:
            market = get_ctx().market if get_ctx() else None
            if market:
                from src.tools.market import _resolve_to_ticker as _resolve_stock

                try:
                    resolved_symbol = _resolve_stock(market, symbol)
                except ValueError as e:
                    logger.warning("create_trade: stock name resolution failed: {}", e)
                    return err(str(e))
            else:
                resolved_symbol = symbol

        quant = get_ctx().quant
        result = await quant.trading.create(
            TradingOperationCreate(
                operation_type=operation_type,
                target_node_id=_parse_uuid(resolved_node_id),
                trigger_analysis_id=_parse_uuid(resolved_analysis_id),
                symbol=resolved_symbol,
                quantity=quantity,
                price=price,
                rationale=rationale,
                expected_impact=expected_impact,
                risk_level=risk_level,
                custom_time=_get_custom_time(),
            )
        )
        trade_id = str(result.id)
        ref = register_entity("T", trade_id, source="create")
        return ok({"status": "created", "trade_ref": ref})
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("create_trade failed: {}", e)
        return err(f"创建交易失败：{e}")


@register_tool(
    name="review_trade",
    description="审批交易：批准或拒绝。action='approve' 批准执行，action='reject' 拒绝。"
    "可不填 trade_ref，系统自动选最新一笔交易。"
    "适用：风控审核后做出最终裁决——批准表示风险可控可执行，拒绝表示风险过高或逻辑有缺陷。"
    "不适用：分析过程中不应调用此工具，仅在风控综合裁决阶段使用。批准后应创建卖出触发器，拒绝后可选创建买入触发器。",
    category="writer",
    args_schema=ReviewTradeArgs,
)
async def review_trade(action: str = "approve", trade_ref: str | None = None, note: str = "") -> str:
    try:
        if trade_ref is None:
            trade_ref = get_latest_ref("T")
            if trade_ref is None:
                return err("没有可审批的交易（当前 session 中无交易记录）")
        trade_id = resolve_ref(trade_ref)
        if trade_id is None:
            return err(f"无效的交易引用：{trade_ref}")
        quant = get_ctx().quant
        if action == "approve":
            await quant.trading.update(trade_id, TradingOperationUpdate(status="approved", reason=note, custom_time=_get_custom_time()))
            return ok({"status": "approved", "trade_ref": trade_ref, "note": note or ""})
        elif action == "reject":
            await quant.trading.update(trade_id, TradingOperationUpdate(status="rejected", reason=note, custom_time=_get_custom_time()))
            return ok({"status": "rejected", "trade_ref": trade_ref, "note": note or ""})
        else:
            return err(f"无效的审批动作：'{action}'，请使用 'approve' 或 'reject'")
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("review_trade failed: {}", e)
        return err(f"审批交易失败：{e}")


@register_tool(
    name="create_feedback",
    description="写入复盘反思报告，记录预期/实际结果、判断正误、错误原因、遗漏因素、调整建议和经验教训。返回 feedback_ref（如 F1）。"
    "适用：交易执行后或分析结论验证后，总结经验教训、提炼规则、调整策略。"
    "不适用：创建首次分析报告时用 create_analysis；增量追加行业认知时用 append_preference。",
    category="writer",
    args_schema=CreateFeedbackArgs,
)
async def create_feedback(
    title: str,
    judgment_correct: bool,
    lessons_learned: str,
    trigger_analysis_ref: str | None = None,
    trigger_trade_ref: str | None = None,
    expected_outcome: str | None = None,
    actual_outcome: str | None = None,
    error_reason: str | None = None,
    missed_factors: str | None = None,
    adjustment_suggestions: str | None = None,
    market_environment_snapshot: dict | None = None,
) -> str:
    try:
        resolved_analysis_id = resolve_ref_or_original(trigger_analysis_ref) if trigger_analysis_ref else None
        resolved_trade_id = resolve_ref_or_original(trigger_trade_ref) if trigger_trade_ref else None
        quant = get_ctx().quant
        result = await quant.feedback.create(
            FeedbackCreate(
                title=title,
                trigger_analysis_id=_parse_uuid(resolved_analysis_id),
                trigger_trade_id=_parse_uuid(resolved_trade_id),
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                judgment_correct=judgment_correct,
                error_reason=error_reason,
                missed_factors=missed_factors,
                adjustment_suggestions=adjustment_suggestions,
                market_environment_snapshot=market_environment_snapshot,
                lessons_learned=lessons_learned,
                custom_time=_get_custom_time(),
            )
        )
        feedback_id = str(result.id)
        ref = register_entity("F", feedback_id, source="create")
        return ok({"status": "created", "feedback_ref": ref})
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("create_feedback failed: {}", e)
        return err(f"创建复盘失败：{e}")


@register_tool(
    name="append_preference",
    description="增量追加行业偏好认知文本，新内容追加到已有认知后面。"
    "适用：复盘或分析中获得关于某个行业的新认知、新观察、新策略倾向。"
    "不适用：创建完整分析报告时用 create_analysis；查看当前行业偏好时用 get_preferences。",
    category="writer",
    args_schema=AppendPreferenceArgs,
)
async def append_preference(sector: str, text: str) -> str:
    try:
        quant = get_ctx().quant
        resp = await quant.preferences.append_industry_cognition(sector, text, custom_time=_get_custom_time())
        return ok({"status": resp.status, "sector": resp.sector})
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        msg = format_exception(e)
        logger.error("append_preference 失败: {} ({})", msg, e.__class__.__name__)
        return err(f"追加行业偏好失败：{msg}")


@register_tool(
    name="append_market_preference",
    description="增量追加市场整体偏好认知文本，新内容追加到已有认知后面。"
    "适用：宏观分析或市场观察中获得关于整体市场的新认知、新判断、新策略倾向（市场风格、风险偏好、板块轮动、大盘趋势等）。"
    "不适用：行业/板块级别的偏好认知请用 append_preference；查看当前市场偏好时用 get_preferences（不填 sector）。",
    category="writer",
    args_schema=AppendMarketPreferenceArgs,
)
async def append_market_preference(text: str) -> str:
    try:
        quant = get_ctx().quant
        resp = await quant.preferences.append_market_cognition(text, custom_time=_get_custom_time())
        return ok({"status": resp.status})
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        msg = format_exception(e)
        logger.error("append_market_preference 失败: {} ({})", msg, e.__class__.__name__)
        return err(f"追加市场偏好失败：{msg}")


@register_tool(
    name="create_trigger",
    description=(
        "为A股个股或主要大盘指数创建后台自动触发规则。"
        "当用户想设置股票提醒、买入信号、卖出信号、止盈止损、重新分析或定期评估时，应使用本工具。"

        "应根据当前上下文主动整理出完整的 name、condition_nl 和 action_nl，"
        "包括标的、触发方向、阈值、周期、时间约束、触发后的动作和动作原因。"
        "不要把过短、模糊、口语化的片段直接传入工具。"

        "支持对象："
        "A股个股中文名，例如「贵州茅台」「宁德时代」「比亚迪」；"
        "主要指数，例如「上证指数」「深证成指」「创业板指」「沪深300」「中证500」「中证1000」。"

        "支持动作："
        "buy=买入/开仓/加仓；"
        "sell=卖出/止盈/止损/减仓/清仓；"
        "deep_analysis=提醒、观察、重新分析、复盘、评估。"

        "focus_on 用于指定触发后分析时需要关注的因果逻辑和传导链条（如'重新评估突破背后的基本面逻辑'），而非行情走向或技术指标。"

        "支持条件："
        "价格涨跌、市场情绪、突破/跌破价位、均线、MACD、KDJ、RSI、成交量、换手率、趋势结构、日内动态、板块表现、时间约束等技术面条件。"

        "边界："
        "本工具主要支持技术面和时间类条件。"
        "如果用户明确要求基本面、资金面、宏观、新闻舆情、研报评级、机构持仓等不可直接监控维度，"
        "不要强行编译这些部分；应优先使用相近的技术面替代条件，例如价格突破、趋势确认、放量、跌破均线、板块异动。"
    ),
    category="writer",
    args_schema=CreateTriggerArgs,
)
async def create_trigger(
    name: str,
    condition_nl: str,
    action_nl: str,
    trade_ref: str | None = None,
    source_analysis_ref: str | None = None,
    focus_on: str | None = None,
) -> str:
    try:
        ctx = get_ctx()
        compiler = ctx.compiler
        if not compiler:
            return err("触发器编译器未初始化，当前环境不支持创建触发器")

        resolved_trade_id = _resolve_single_ref(trade_ref) if trade_ref else None
        resolved_analysis_id = _resolve_single_ref(source_analysis_ref) if source_analysis_ref else None

        task_ctx = get_task_context()
        source_task_id = task_ctx.get("task_id")

        now = ctx.clock.now if ctx.clock else datetime.now(BEIJING_TZ)

        compiled = await compiler.compile(
            name=name,
            condition_nl=condition_nl,
            action_nl=action_nl,
            source_task_id=source_task_id,
            source_analysis_id=resolved_analysis_id,
            now=now,
        )

        if "error" in compiled:
            return err(f"条件编译失败：{compiled['error']}")

        from src.tools._db import create_trigger_record as _create_trigger

        # 确保新 trigger 有最小冷却窗口，防止立即被下一轮评估命中（反馈回路）
        resolved_not_before = ensure_beijing_datetime(compiled.get("not_before"), field_name="not_before")
        if resolved_not_before is None:
            from src.config import settings as _settings
            resolved_not_before = now + timedelta(seconds=max(5, _settings.trigger_eval_interval_seconds * 2))
        elif resolved_not_before <= now:
            from src.config import settings as _settings
            resolved_not_before = now + timedelta(seconds=min(5, _settings.trigger_eval_interval_seconds))

        trigger_id = await _create_trigger(
            name=name,
            condition=compiled["condition"],
            action_type=compiled["action_type"],
            action_params=compiled.get("action_params") or None,
            trade_id=resolved_trade_id,
            source_task_id=source_task_id,
            source_analysis_id=resolved_analysis_id,
            not_before=resolved_not_before,
            not_after=ensure_beijing_datetime(compiled.get("not_after"), field_name="not_after"),
            focus_on=focus_on,
            created_at=now,
        )

        ref = register_entity("G", str(trigger_id), source="create")
        return ok(
            {
                "status": "trigger_created",
                "trigger_ref": ref,
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("create_trigger failed: {}", e)
        return err(f"创建触发器失败：{e}")


@register_tool(
    name="list_my_triggers",
    description="列出与指定股票名称相关的本任务触发器（包括等待中、已触发、已取消），返回触发器列表及会话引用（G1、G2...）。"
    "自动限定在当前 task 和当前 analysis 范围内。"
    "适用：检查某个标的是否已有触发器、了解触发条件状态。",
    category="writer",
    args_schema=ListMyTriggersArgs,
)
async def list_my_triggers(stock_name: str) -> str:
    try:
        from src.tools._db import list_trigger_records as _list_triggers

        ctx = get_task_context()
        task_id = ctx.get("task_id")
        analysis_ids = ctx.get("analysis_ids", [])

        triggers = await _list_triggers(stock_name, task_id, analysis_ids)

        if not triggers:
            return ok(
                {
                    "count": 0,
                    "message": f"未找到与 '{stock_name}' 相关的触发器",
                }
            )

        items = []
        for t in triggers:
            ref = register_entity("G", str(t.id), source="read")
            items.append(
                {
                    "ref": ref,
                    "id": str(t.id),
                    "name": t.name,
                    "status": t.status,
                    "action_type": t.action_type,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
            )

        return ok(
            {
                "count": len(items),
                "triggers": items,
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("list_my_triggers failed: {}", e)
        return err(f"列出触发器失败：{e}")


@register_tool(
    name="cancel_trigger",
    description="取消一个等待中的触发器。传入 list_my_triggers 或 create_trigger 返回的触发器引用（如 G1）。"
    "只能取消状态为 'waiting' 的触发器。"
    "适用：条件已变化、不再需要监控时撤销触发器。"
    "不适用：修改触发条件——应先取消再重新创建。",
    category="writer",
    args_schema=CancelTriggerArgs,
)
async def cancel_trigger(trigger_ref: str) -> str:
    try:
        from uuid import UUID as _UUID

        from src.tools._db import cancel_trigger_in_db as _cancel_trigger
        from src.tools._db import get_trigger_by_id as _get_trigger

        trigger_id_str = resolve_ref_or_original(trigger_ref)
        trigger_id = _UUID(trigger_id_str)

        trigger = await _get_trigger(trigger_id)
        if trigger is None:
            return err(f"未找到触发器：{trigger_ref}")

        if trigger.status != "waiting":
            return err(f"触发器 {trigger_ref} 状态为 '{trigger.status}'，只有 waiting 状态的触发器可以取消")

        await _cancel_trigger(trigger_id)

        return ok(
            {
                "status": "cancelled",
                "trigger_ref": trigger_ref,
                "name": trigger.name,
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("cancel_trigger failed: {}", e)
        return err(f"取消触发器失败：{e}")
