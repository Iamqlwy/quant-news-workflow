"""StageAgent 基类 —— 手动 ReAct 循环

阶段推进规则：LLM 不产生 tool_call → 当前阶段完成 → 进入下一阶段。
消息列表跨阶段直接继承，LLM 能看到完整的历史。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import traceback as _traceback
import types
from collections.abc import Callable
from typing import Any

import openai
from kbquant.client import QuantClient as QuantClient
from kbquant.client._base import QuantClientConnectionError, QuantClientHTTPError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import ValidationError

from src.config import settings
from src.observability import observe_langchain_generation, safe_observation_value, start_observation
from src.tools import get_session_registry, register_entity, reset_session_registry, set_task_context
from src.tools._deps import _ensure_reverse
from src.workflow_logging import log_progress, progress_span

# ═══════════════════════════════════════════════
# 工具调用分区
# ═══════════════════════════════════════════════


def _partition_tool_calls(tool_calls: list) -> list[list[tuple[int, dict]]]:
    """分组：所有工具调用放入一个批次"""
    return [list(enumerate(tool_calls, start=1))]


# ═══════════════════════════════════════════════
# Session 初始化（供 StageAgent 和子类共用）
# ═══════════════════════════════════════════════


def _register_entities(context: dict[str, Any]) -> None:
    """从 context 中的 ID 列表注册全部实体短引用"""
    for key, prefix in [("analysis_ids", "A"), ("trade_ids", "T"), ("feedback_ids", "F")]:
        for eid in context.get(key) or []:
            if eid:
                register_entity(prefix, str(eid))
    for key, prefix in [("raw_info_id", "R"), ("trigger_id", "G")]:
        eid = context.get(key)
        if eid:
            register_entity(prefix, str(eid))


def _set_task_ctx(context: dict[str, Any]) -> None:
    """从 context 提取 task_id 和 analysis_ids 设置 task context"""
    ids = [context["analysis_id"]] if context.get("analysis_id") else []
    set_task_context(
        task_id=context.get("task_id"),
        analysis_ids=list(set(ids + (context.get("analysis_ids") or []))),
    )


_IMAGE_CONTEXT_KEYS = ("market_chart_url",)


def _extract_image_urls(llm_context: dict[str, Any]) -> list[str]:
    """从 llm_context 中提取图片 URL，后续以 image_url content block 传给多模态模型"""
    urls: list[str] = []
    for key in _IMAGE_CONTEXT_KEYS:
        url = llm_context.pop(key, None)
        if url:
            urls.append(url)
    return urls


async def _retry_api_call(fn: Callable[..., Any], name: str, task_id: str = "?", max_retries: int = 5) -> Any:
    """带重试的 API 调用，仅对瞬态错误重试。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except QuantClientHTTPError as e:
            if e.status_code not in _RETRYABLE_HTTP_STATUSES or attempt >= max_retries:
                raise
            delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning("{} HTTP {} 重试 {}/{} (task={})，{:.1f}s 后重试",
                           name, e.status_code, attempt + 1, max_retries, task_id, delay)
            await asyncio.sleep(delay)
        except (asyncio.TimeoutError, QuantClientConnectionError, ConnectionRefusedError, ConnectionResetError, TimeoutError) as e:
            if attempt >= max_retries:
                raise
            delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning("{} 网络错误 ({}) 重试 {}/{} (task={})，{:.1f}s 后重试",
                           name, type(e).__name__, attempt + 1, max_retries, task_id, delay)
            await asyncio.sleep(delay)


async def _fetch_entity(quant: QuantClient, entity_type: str, entity_id: str, ref: str, fields: dict[str, str], *, task_id: str = "?") -> dict | None:
    """拉取单个实体的完整内容，返回 {ref: {field: value, ...}} 或 None"""
    from uuid import UUID as _UUID

    entity_id_str = str(entity_id)
    try:
        uid = _UUID(entity_id_str)
    except (ValueError, AttributeError, TypeError):
        logger.debug("跳过非 UUID {}_id: {}", entity_type, entity_id)
        return None
    try:
        obj = await _retry_api_call(
            lambda: getattr(quant, entity_type).get(uid),
            name=f"fetch_{entity_type}",
            task_id=task_id,
        )
        return {ref: {key: getattr(obj, attr) for key, attr in fields.items()}}
    except QuantClientHTTPError as e:
        logger.warning("获取 {} 失败: {} (HTTP {} : {}) [task={}]", entity_type, entity_id_str, e.status_code, e, task_id)
        return None
    except Exception as e:
        logger.warning("获取 {} 失败: {} ({}: {}) [task={}]", entity_type, entity_id_str, type(e).__name__, e, task_id)
        return None


_STANDARD_ENTITIES: dict[str, tuple[str, dict[str, str]]] = {
    "analysis_ids": ("analysis", {"title": "title", "content": "content", "type": "analysis_type", "confidence": "confidence"}),
    "trade_ids": ("trading", {"symbol": "symbol", "operation_type": "operation_type", "rationale": "rationale", "risk_level": "risk_level", "status": "status", "expected_impact": "expected_impact"}),
    "feedback_ids": ("feedback", {"content": "content"}),
}


async def _build_llm_context(context: dict[str, Any]) -> dict[str, Any]:
    """拉取全部实体正文 → 构建无 UUID 的 LLM 输入上下文"""
    from uuid import UUID as _UUID

    from src.tools.context import get_ctx

    market = get_ctx().market
    quant = get_ctx().quant
    uuid_to_ref = _ensure_reverse()
    task_id = context.get("task_id", "?")

    _IGNORE_KEYS = {"analysis_ids", "trade_ids", "feedback_ids", "raw_info_id", "analysis_id", "trade_id", "trigger_id"}
    llm_context: dict[str, Any] = {k: v for k, v in context.items() if not k.startswith("_") and k not in _IGNORE_KEYS}

    # raw_info（字段映射特殊，单独处理）
    raw_id = context.get("raw_info_id")
    if raw_id and str(raw_id) in uuid_to_ref:
        ref = uuid_to_ref[str(raw_id)]
        try:
            raw_uuid = _UUID(str(raw_id))
            info = await _retry_api_call(
                lambda: quant.information.get(raw_uuid),
                name="fetch_raw_info",
                task_id=str(task_id),
            )
            llm_context["raw_info"] = {
                "ref": ref, "title": info.title, "body": info.body,
                "source": getattr(info, "source", ""),
                "published_at": str(getattr(info, "published_at", "")),
            }
        except (ValueError, AttributeError, TypeError):
            logger.debug("跳过非 UUID raw_info_id: {}", raw_id)
        except Exception as e:
            logger.warning("获取 raw_info 失败: {} ({}: {}) [task={}]", raw_id, type(e).__name__, e, task_id)

    # 标准实体：analyses / trades / feedbacks
    for context_key, (entity_type, field_map) in _STANDARD_ENTITIES.items():
        entity_ids = context.get(context_key) or []
        if not entity_ids:
            continue
        if context_key == "analysis_ids":
            logger.info("[诊断] _build_llm_context: task={}, analysis_ids_in_context={}", task_id, entity_ids)
        results: dict[str, dict] = {}
        for eid in entity_ids:
            eid_str = str(eid)
            if eid_str not in uuid_to_ref:
                continue
            entry = await _fetch_entity(quant, entity_type, eid_str, uuid_to_ref[eid_str], field_map, task_id=task_id)
            if entry:
                results.update(entry)
        if results:
            key_map = {"analysis_ids": "analyses", "trade_ids": "trades", "feedback_ids": "feedbacks"}

            if context_key == "trade_ids":
                for _ref, _tr in results.items():
                    _sym = _tr.get("symbol")
                    if _sym:
                        with contextlib.suppress(Exception):
                            _tr["symbol_name"] = market.get_stock_name(str(_sym))

            llm_context[key_map[context_key]] = results

    # trigger（本地 DB，读 focus_on 文本）
    trigger_id = context.get("trigger_id")
    if trigger_id:
        from src.tools._db import get_trigger_by_id as _get_trigger
        try:
            t = await _get_trigger(trigger_id)
            if t and t.focus_on:
                llm_context["trigger"] = {"name": t.name, "focus_on": "本次重点关注：" + t.focus_on}
        except Exception as e:
            logger.warning("获取 trigger 失败: {} ({}: {}) [task={}]", trigger_id, type(e).__name__, e, task_id)

    return llm_context


# ═══════════════════════════════════════════════
# 收敛提醒 & 工具执行（模块级，无需访问 self）
# ═══════════════════════════════════════════════

_HARD_STOP_THRESHOLD = 8  # max_iter >= 此值的阶段才启用硬停止（解绑工具强制收敛）

_CONVERGENCE_MESSAGES: dict[float, str] = {
    0.85: "（剩余迭代即将耗尽。请尽快收尾，下一轮或再下一轮必须输出阶段总结。已有信息通常已足够得出结论。）",
    0.5: "（已过半程。请准备收尾，在 2-3 轮内完成工具调用并输出阶段总结。）",
}


def _estimate_single_message_tokens(m) -> int:
    """估算单条消息的 token 数（保守：2 chars/token，适应中英文混合）"""
    content = m.content
    if isinstance(content, str):
        return max(1, len(content) // 2)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text", "")) // 2
        return max(1, total)
    return 1


def _estimate_tokens(messages: list) -> int:
    """保守估算消息列表总 token 数"""
    total = 0
    for m in messages:
        total += _estimate_single_message_tokens(m)
    return total


def _inject_convergence(iterations: int, max_iter: int, messages: list, injected: set) -> None:
    """注入收敛提醒：短阶段仅最后一轮提醒，长阶段中间比例提醒+最后一轮提醒；单轮阶段跳过"""
    if max_iter <= 1:
        return

    # 最后一轮：必定注入提醒（无论长短阶段）
    if iterations >= max_iter - 1:
        if "last" not in injected:
            injected.add("last")
            messages.append(HumanMessage(content="（最后一轮！必须立即输出阶段总结文字，不得再调用任何工具！）"))
        return

    # 中间阶段：仅长阶段使用比例型提醒
    if max_iter < _HARD_STOP_THRESHOLD:
        return

    budget_ratio = iterations / max_iter
    for threshold, msg in sorted(_CONVERGENCE_MESSAGES.items()):
        if budget_ratio >= threshold and threshold not in injected:
            injected.add(threshold)
            messages.append(HumanMessage(content=msg))
            return



_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

# DeepSeek / 通义千问 内容安全审查消息关键字
_CONTENT_SAFETY_KEYWORDS: tuple[str, ...] = (
    "Content Exists Risk",
    "content filter",
    "safety",
    "content_policy_violation",
    "Risk_Control",
    "Risk_Detection",
    "Your request was rejected",
    "请自行判断",
)


class _ContentSafetyError(Exception):
    """LLM 内容安全审查拒绝，应直接退出当前 Agent。"""


def _is_content_safety_error(exc: Exception) -> bool:
    """判断异常是否属于内容安全审查拒绝（非瞬态但应优雅降级，不应重试也不应崩溃）。"""
    if isinstance(exc, openai.BadRequestError):
        msg = str(exc)
        for kw in _CONTENT_SAFETY_KEYWORDS:
            if kw.lower() in msg.lower():
                return True
    return False


def _is_transient_error(exc: Exception) -> bool:
    """判断异常是否属于瞬态错误（网络故障、超时、服务端 5xx/429），应触发重试。
    逻辑错误（如参数校验失败、404、认证错误）不重试。"""
    if isinstance(exc, (asyncio.TimeoutError, QuantClientConnectionError, ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return True
    if isinstance(exc, QuantClientHTTPError):
        return exc.status_code in _RETRYABLE_HTTP_STATUSES

    _TRANSIENT_OPENAI = (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)
    return bool(isinstance(exc, _TRANSIENT_OPENAI))


def _tool_call_id(tc) -> str | None:
    """从 LangChain tool_call 或 OpenAI raw tool_call 中取 id。"""
    if tc is None:
        return None
    if isinstance(tc, dict):
        tc_id = tc.get("id")
    else:
        tc_id = getattr(tc, "id", None)
    return str(tc_id) if tc_id else None


def _get_tool_call_ids(m: AIMessage) -> list[str]:
    """同时读取 m.tool_calls / m.invalid_tool_calls / m.additional_kwargs["tool_calls"] 中的 tool_call_id。

    LangChain serialization (langchain_openai) line 364:
        if message.tool_calls or message.invalid_tool_calls:
    三个来源的 tool_calls 都会被发送到 API，此处全部收集。
    """
    ids: list[str] = []
    for tc in (getattr(m, "tool_calls", None) or []):
        tc_id = _tool_call_id(tc)
        if tc_id:
            ids.append(tc_id)
    for tc in (getattr(m, "invalid_tool_calls", None) or []):
        tc_id = _tool_call_id(tc)
        if tc_id:
            ids.append(tc_id)
    for tc in ((getattr(m, "additional_kwargs", None) or {}).get("tool_calls") or []):
        tc_id = _tool_call_id(tc)
        if tc_id:
            ids.append(tc_id)
    return list(dict.fromkeys(ids))


def _has_tool_calls(m: AIMessage) -> bool:
    """检查 AIMessage 是否包含 tool_calls（无论在主字段还是 additional_kwargs 中）。"""
    return bool(_get_tool_call_ids(m))



def _log_tool_call_fields(messages: list, task_id: str, phase: str) -> None:
    for i, m in enumerate(messages):
        if not isinstance(m, AIMessage):
            continue
        valid_ids = [_tool_call_id(tc) for tc in (getattr(m, "tool_calls", None) or [])]
        invalid_ids = [_tool_call_id(tc) for tc in (getattr(m, "invalid_tool_calls", None) or [])]
        raw_ids = [
            _tool_call_id(tc)
            for tc in ((getattr(m, "additional_kwargs", None) or {}).get("tool_calls") or [])
        ]
        if valid_ids or invalid_ids or raw_ids:
            logger.debug(
                "tool_call字段诊断 [{}] AIMessage[{}]: valid={}, invalid={}, raw={} [task={}]",
                phase, i, valid_ids, invalid_ids, raw_ids, task_id,
            )

def _clear_all_tool_call_fields(m: AIMessage) -> None:
    with contextlib.suppress(Exception):
        m.tool_calls = []
    with contextlib.suppress(Exception):
        m.__dict__["tool_calls"] = []

    with contextlib.suppress(Exception):
        m.invalid_tool_calls = []
    with contextlib.suppress(Exception):
        m.__dict__["invalid_tool_calls"] = []

    ak = getattr(m, "additional_kwargs", None)
    if isinstance(ak, dict):
        ak.pop("tool_calls", None)



def _clear_invalid_tool_calls(m: AIMessage) -> None:
    with contextlib.suppress(Exception):
        m.invalid_tool_calls = []
    with contextlib.suppress(Exception):
        m.__dict__["invalid_tool_calls"] = []


def _filter_ai_tool_calls(m: AIMessage, keep_ids: set[str]) -> None:
    def keep(tc) -> bool:
        tc_id = _tool_call_id(tc)
        return bool(tc_id and tc_id in keep_ids)

    if not keep_ids:
        _clear_all_tool_call_fields(m)
        return

    try:
        m.tool_calls = [
            tc for tc in (getattr(m, "tool_calls", None) or [])
            if keep(tc)
        ]
    except Exception:
        with contextlib.suppress(Exception):
            m.__dict__["tool_calls"] = [
                tc for tc in (getattr(m, "tool_calls", None) or [])
                if keep(tc)
            ]

    ak = getattr(m, "additional_kwargs", None)
    if isinstance(ak, dict) and ak.get("tool_calls"):
        filtered = [tc for tc in ak["tool_calls"] if keep(tc)]
        if filtered:
            ak["tool_calls"] = filtered
        else:
            ak.pop("tool_calls", None)

    with contextlib.suppress(Exception):
        m.invalid_tool_calls = []
    with contextlib.suppress(Exception):
        m.__dict__["invalid_tool_calls"] = []

def _verify_no_orphan_tool_calls(messages: list, task_id: str) -> int:
    """防御性验证：扫描所有 AIMessage，确保 tool_calls 都有对应的 ToolMessage。

    返回修复数量。此函数在 _sanitize_orphan_tool_calls 之后调用，
    作为最后一道防线，捕获 sanitizer 遗漏的边缘情况。
    """
    fixed = 0
    # 收集所有 ToolMessage 的 tool_call_id（只取第一个）
    tool_msg_ids: dict[str, int] = {}
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            tc_id = str(getattr(m, "tool_call_id", "") or "")
            if tc_id and tc_id not in tool_msg_ids:
                tool_msg_ids[tc_id] = i

    for i, m in enumerate(messages):
        if not isinstance(m, AIMessage):
            continue
        # 收集此 AIMessage 的所有 tool_call id
        ids_from_field = []
        for tc in (getattr(m, "tool_calls", None) or []):
            tid = _tool_call_id(tc)
            if tid:
                ids_from_field.append(tid)
        ids_from_ak = []
        for tc in ((getattr(m, "additional_kwargs", None) or {}).get("tool_calls") or []):
            tid = _tool_call_id(tc)
            if tid:
                ids_from_ak.append(tid)
        all_ids = ids_from_field + ids_from_ak
        if not all_ids:
            continue

        # 检查每个 id 是否有对应的 ToolMessage
        orphan_field_ids = []
        orphan_ak_ids = []
        for tid in ids_from_field:
            if tid not in tool_msg_ids:
                orphan_field_ids.append(tid)
        for tid in ids_from_ak:
            if tid not in tool_msg_ids:
                orphan_ak_ids.append(tid)

        if orphan_field_ids or orphan_ak_ids:
            logger.warning(
                "防御性修复: AIMessage[{}] 存在孤儿 tool_calls "
                "(field_ids={}, ak_ids={}), 全部清除 [task={}]",
                i, orphan_field_ids, orphan_ak_ids, task_id,
            )
            # 彻底清除
            _clear_all_tool_call_fields(m)
            fixed += 1
            continue

        # IDs 都存在，但检查是否至少有一个匹配的 ToolMessage 在 AIMessage 之后
        # （只做快速检查：至少一个 ToolMessage 存在于后续消息中）
        has_tool_msg_after = False
        for tid in all_ids:
            tmi = tool_msg_ids.get(tid)
            if tmi is not None and tmi > i:
                has_tool_msg_after = True
                break
        if not has_tool_msg_after and all_ids:
            logger.warning(
                "防御性修复: AIMessage[{}] 的 tool_calls (ids={}) 对应的 ToolMessage "
                "不在其后，全部清除 [task={}]",
                i, list(set(all_ids)), task_id,
            )
            _clear_all_tool_call_fields(m)
            fixed += 1

    return fixed



def _sanitize_orphan_tool_calls(messages: list, task_id: str = "?") -> int:
    """修复 messages 中非法的 tool_calls / ToolMessage 顺序。

    OpenAI / DeepSeek 要求：
        AIMessage(tool_calls=[id1, id2])
        ToolMessage(tool_call_id=id1)
        ToolMessage(tool_call_id=id2)

    ToolMessage 必须紧跟在 AIMessage 后面。如果 ToolMessage 被
    HumanMessage/SystemMessage 隔开，本函数会把它搬回正确位置。
    只有真正找不到 ToolMessage 的 tool_call_id，才会从 AIMessage 中删除。
    孤儿 ToolMessage 会被删除。

    返回修复数量。
    """

    # ---- 诊断日志：记录 sanitize 前的工具调用字段状态 ----
    _log_tool_call_fields(messages, task_id, "sanitize_before")
    fixed = 0

    # 1. 收集所有 AIMessage 需要的 tool_call_id
    needed_ids: set[str] = set()
    ai_expected: dict[int, list[str]] = {}
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage):
            ids = _get_tool_call_ids(m)
            if ids:
                ai_expected[i] = ids
                needed_ids.update(ids)

    if not needed_ids:
        original_len = len(messages)
        messages[:] = [m for m in messages if not isinstance(m, ToolMessage)]
        # 彻底清除所有 AIMessage 中残留的 tool_calls：
        # m.tool_calls — LangChain 序列化时优先读取此字段
        # additional_kwargs["tool_calls"] — m.tool_calls 为空时序列化回退来源
        # invalid_tool_calls — 某些 LangChain 版本可能序列化到此处
        for m in messages:
            if isinstance(m, AIMessage):
                try:
                    m.tool_calls = []
                except (ValidationError, Exception):
                    try:
                        m.__dict__["tool_calls"] = []
                    except Exception:
                        pass
                ak = getattr(m, "additional_kwargs", None)
                if isinstance(ak, dict):
                    ak.pop("tool_calls", None)
                try:
                    m.invalid_tool_calls = []
                except (AttributeError, ValidationError):
                    pass
        removed = original_len - len(messages)
        if removed:
            logger.warning("清理孤儿 ToolMessage: 移除 {} 条 [task={}]", removed, task_id)

        # ---- 诊断日志：记录 sanitize 后的工具调用字段状态 ----
        _log_tool_call_fields(messages, task_id, "sanitize_after")

        return removed

    # 2. 收集现有 ToolMessage：同一个 tool_call_id 只保留第一条
    tool_msg_by_id: dict[str, tuple[int, ToolMessage]] = {}
    duplicate_tool_indices: set[int] = set()
    orphan_tool_indices: set[int] = set()
    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage):
            continue
        tc_id = getattr(m, "tool_call_id", None)
        tc_id = str(tc_id) if tc_id else ""
        if not tc_id or tc_id not in needed_ids:
            orphan_tool_indices.add(i)
            continue
        if tc_id in tool_msg_by_id:
            duplicate_tool_indices.add(i)
            continue
        tool_msg_by_id[tc_id] = (i, m)

    # 3. 重建 messages：遇到 AIMessage(tool_calls) 时，立即插入对应 ToolMessage
    new_messages: list = []
    consumed_tool_indices: set[int] = set()
    moved_count = 0
    missing_count = 0
    removed_orphan_count = 0
    removed_duplicate_count = 0

    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", None)
            tc_id = str(tc_id) if tc_id else ""
            if i in duplicate_tool_indices:
                removed_duplicate_count += 1
            elif i in orphan_tool_indices:
                removed_orphan_count += 1
            elif i not in consumed_tool_indices and tc_id in needed_ids:
                pass  # 属于某个 AIMessage，但还没轮到；不要留在原位置
            elif i in consumed_tool_indices:
                pass
            else:
                removed_orphan_count += 1
            continue

        if not isinstance(m, AIMessage):
            new_messages.append(m)
            continue

        expected_ids = ai_expected.get(i)
        if not expected_ids:
            new_messages.append(m)
            continue

        present_ids: list[str] = []
        tool_messages_to_insert: list[tuple[str, int, ToolMessage]] = []
        for tc_id in expected_ids:
            found = tool_msg_by_id.get(tc_id)
            if not found:
                missing_count += 1
                continue
            original_idx, tool_msg = found
            if original_idx in consumed_tool_indices:
                missing_count += 1
                continue
            present_ids.append(tc_id)
            tool_messages_to_insert.append((tc_id, original_idx, tool_msg))
            consumed_tool_indices.add(original_idx)

        keep_ids = set(present_ids)
        _filter_ai_tool_calls(m, keep_ids)
        new_messages.append(m)

        for offset, (_tc_id, original_idx, tool_msg) in enumerate(tool_messages_to_insert, start=1):
            new_messages.append(tool_msg)
            if original_idx != i + offset:
                moved_count += 1

    # 4. 兜底：没有被消费的 ToolMessage 都删除
    unconsumed_needed_tool_indices = {
        idx
        for tc_id, (idx, _msg) in tool_msg_by_id.items()
        if idx not in consumed_tool_indices
    }
    if unconsumed_needed_tool_indices:
        removed_orphan_count += len(unconsumed_needed_tool_indices)

    messages[:] = new_messages
    fixed = moved_count + missing_count + removed_orphan_count + removed_duplicate_count

    if fixed:
        logger.warning(
            "修复 tool_calls 协议: moved={}, missing_tool_calls={}, "
            "removed_orphan_tool_messages={}, removed_duplicate_tool_messages={} [task={}]",
            moved_count, missing_count, removed_orphan_count, removed_duplicate_count, task_id,
        )


    # ---- 诊断日志：记录 sanitize 后的工具调用字段状态 ----
    _log_tool_call_fields(messages, task_id, "sanitize_after")

    return fixed

async def _invoke_one_tool(
    tc: dict, idx: int, stage_tools: list[BaseTool], stage_name: str, stage_label: str, task_id: str
) -> tuple:
    """调用单个工具（含重试），返回 (tc_id, t_name, t_result, idx)"""
    t_name: str = tc["name"]
    t_args: dict = tc["args"]
    t_id: str = tc["id"]
    t_tool = next((t for t in stage_tools if t.name == t_name), None)

    if t_tool is None:
        available = [t.name for t in stage_tools]
        if available:
            t_result = f"错误：当前阶段 '{stage_name}' 没有名为 '{t_name}' 的工具。\n当前可用的工具：{'、'.join(available)}。\n请仅使用上述工具。"
        else:
            t_result = f"错误：当前阶段 '{stage_name}' 没有名为 '{t_name}' 的工具。当前阶段是纯推理阶段。"
        with start_observation(name=t_name, as_type="tool", input=safe_observation_value(t_args),
                               metadata={"missing_tool": True, "stage_name": stage_name}) as tool_obs:
            if tool_obs is not None:
                tool_obs.update(level="WARNING", output=t_result)
        return (t_id, t_name, t_result, idx)

    max_retries = settings.agent_tool_max_retries
    effective_timeout = AGENT_TOOL_TIMEOUT_OVERRIDES.get(t_name, settings.agent_tool_timeout_seconds)
    t_result = "工具执行异常：未知错误"

    try:
        for attempt in range(max_retries):
            retry_suffix = f"（重试{attempt + 1}/{max_retries}）" if attempt > 0 else ""
            log_progress(f"{stage_label}:Tool:{t_name}", f"开始{retry_suffix}", task_id=task_id, tool_call=str(idx))
            try:
                result = await asyncio.wait_for(t_tool.ainvoke(t_args), timeout=effective_timeout)
            except TimeoutError:
                if attempt >= max_retries - 1:
                    raise
                delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
                logger.warning("工具 {} 超时，第 {}/{} 次重试 (task={})，{:.1f}s 后重试",
                               t_name, attempt + 1, max_retries - 1, task_id, delay)
                await asyncio.sleep(delay)
                continue
            except Exception as exc:
                if attempt >= max_retries - 1 or not _is_transient_error(exc):
                    raise
                delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
                logger.warning("工具 {} 执行失败 ({})，第 {}/{} 次重试 (task={})，{:.1f}s 后重试",
                               t_name, exc.__class__.__name__, attempt + 1, max_retries - 1, task_id, delay)
                await asyncio.sleep(delay)
                continue
            log_progress(f"{stage_label}:Tool:{t_name}", "完成", task_id=task_id, tool_call=str(idx),
                         result_len=len(str(result)))
            return (t_id, t_name, str(result), idx)
    except TimeoutError:
        t_result = f"工具 '{t_name}' 执行超时（{effective_timeout:.0f}s），已重试 {max_retries} 次，请基于已有信息继续。"
        logger.error("工具 {} 执行超时，已重试 {} 次全部失败 (task={})", t_name, max_retries, task_id)
    except ValidationError as exc:
        missing = []
        invalid = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            if err["type"] == "missing":
                missing.append(loc)
            else:
                invalid.append(f"{loc}: {err["msg"]}")
        parts = []
        if missing:
            parts.append(f"缺少必填参数：{'、'.join(missing)}")
        if invalid:
            parts.append(f"参数值错误：{'；'.join(invalid)}")
        t_result = f"工具 '{t_name}' 参数校验失败，{'；'.join(parts)}。请修正参数后重试。"
        logger.warning("工具 {} 参数校验失败 (task={}): {}", t_name, task_id, exc)
    except Exception as exc:
        if _is_transient_error(exc):
            t_result = f"工具 '{t_name}' 执行失败（{exc.__class__.__name__}），已重试 {max_retries} 次，请基于已有信息继续。"
            logger.error("工具 {} 执行失败，已重试 {} 次全部失败 (task={})", t_name, max_retries, task_id)
        else:
            t_result = f"工具执行异常：{exc}"
            logger.error("工具 {} 执行异常 (task={}): {}", t_name, task_id, exc)

    return (t_id, t_name, str(t_result), idx)

def _inject_chart_images(tool_result: str, tool_name: str, messages: list) -> None:
    """如果工具返回了 chart URL（图表工具），在 ToolMessage 之后追加一个 HumanMessage，
    把图片以 image_url content block 传给多模态 LLM。

    OpenAI 兼容 API（DeepSeek/Qwen）的 tool 角色只接受 string content，
    不支持 content block 数组。因此图片必须走 user 角色的 HumanMessage。"""
    try:
        data = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return
    if data.get("status") != "ok":
        return
    inner = data.get("data", {})
    if not isinstance(inner, dict):
        return
    url = inner.get("chart")
    if isinstance(url, str) and url.startswith("http"):
        messages.append(HumanMessage(content=[
            {"type": "text", "text": f"图表由工具 {tool_name} 生成"},
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
        ]))


async def _execute_tool_calls(
    tool_calls: list, stage_tools: list[BaseTool], stage_name: str, stage_label: str, task_id: str, messages: list
) -> None:
    """分批并发执行工具调用，结果按原始顺序追加 ToolMessage，
    图表类工具还会额外注入多模态 image_url block。"""
    groups = _partition_tool_calls(tool_calls)
    all_results: list = []
    for g in groups:
        g_results = await asyncio.gather(
            *[_invoke_one_tool(tc, idx, stage_tools, stage_name, stage_label, task_id) for idx, tc in g],
            return_exceptions=True,
        )
        for idx, orig_tc in g:
            tc_id = orig_tc["id"]
            tc_name = orig_tc["name"]
            if not any(r[0] == tc_id for r in g_results if not isinstance(r, BaseException)):
                g_results.append((tc_id, tc_name, "工具执行异常（未捕获错误），请基于已有信息继续。", idx))
        all_results.extend(r for r in g_results if not isinstance(r, BaseException))

    sorted_results = sorted(all_results, key=lambda r: r[3])
    for tc_id, t_name, t_result, _ in sorted_results:
        messages.append(ToolMessage(content=str(t_result), tool_call_id=tc_id, name=t_name))

    for _, t_name, t_result, _ in sorted_results:
        _inject_chart_images(str(t_result), t_name, messages)


# ═══════════════════════════════════════════════
# StageAgent
# ═══════════════════════════════════════════════

# -- Agent 工具调用超时覆盖（按工具名）：{'create_trigger': 300} 表示该工具超时 300s
AGENT_TOOL_TIMEOUT_OVERRIDES: dict[str, int] = {"create_trigger": 300}

TOOL_STRATEGY_MSG = (
    "工具调用规则：\n"
    "1. 批量调用：同一轮中需要多个互不依赖的数据时，在一次回复中同时发出所有工具调用，"
    "不要拆成多轮逐个调用。\n"
    "2. 失败即切换：工具返回错误或无相关结果时，不要用相同参数重试——换一个工具、"
    "换一组参数，或基于已有信息继续推进。连续2次无收获即停止该方向。"
)


class StageAgent:
    """分阶段 ReAct Agent（手动控制循环）"""

    def __init__(
        self,
        make_chat_model: Callable[[], BaseChatModel],
        stages: list[dict[str, Any]] | None = None,
        overall_system_prompt: str = "",
        *,
        max_context_tokens: int | None = None,
        reset_registry: bool = True,
        task: Any = None,
        clock: Any = None,
        **kwargs: Any,
    ) -> None:
        self._make_chat_model = make_chat_model
        self.stages: list[dict[str, Any]] = stages if stages is not None else []
        self.overall_system_prompt = overall_system_prompt or self._build_overall_prompt()
        self.max_context_tokens = max_context_tokens if max_context_tokens is not None else settings.default_agent_max_context_tokens
        self._reset_registry = reset_registry
        self._task = task
        self._clock = clock
        self.messages: list = []
        self.tool_use: set[str] = set()

    # ── 子类扩展钩子 ─────────────────────────────

    def _build_overall_prompt(self) -> str:
        """构建整体 system prompt。子类可覆盖以注入动态内容（如宏观摘要、时间戳等）。

        在 __init__ 中调用，仅当构造时未传入 overall_system_prompt 时生效。
        """
        return ""

    def _inject_task_context(self, context: dict[str, Any]) -> None:
        """将 task 的实体 ID 和 clock 自动注入 context。run() 时自动调用，子类无需处理。"""
        if self._task:
            task = self._task
            context.setdefault("task_id", str(task.id))
            for list_key, entity_ids in [("analysis_ids", task.analysis_ids), ("trade_ids", task.trade_ids), ("feedback_ids", task.feedback_ids)]:
                ids = [str(x) for x in (entity_ids or [])]
                context.setdefault(list_key, ids)
                # 奇异形式（最新一条），供 _set_task_ctx 使用
                if ids and list_key in ("analysis_ids", "trade_ids"):
                    context.setdefault(list_key[:-1], ids[-1])  # analysis_ids → analysis_id / trade_ids → trade_id
            if task.raw_info_id:
                context.setdefault("raw_info_id", str(task.raw_info_id))
            if task.trigger_id:
                context.setdefault("trigger_id", str(task.trigger_id))
        if self._clock:
            context.setdefault("clock_now", self._clock.now.strftime("%Y-%m-%d %H:%M:%S") + " 北京时间")

    async def _prepare_context(self, context: dict[str, Any]) -> None:
        """在 agent 执行前预处理上下文。子类可覆盖以修改 context（如拉取额外数据、注入摘要）。

        修改直接作用于 context dict，无需返回值。
        task→context 的 ID 映射已由 _inject_task_context 自动完成。
        """

    def _create_sub_agent(
        self,
        stages: list[dict[str, Any]],
        overall_prompt: str = "",
        reset_registry: bool = False,
        on_stage_start: Callable | None = None,
        on_filter_tool_calls: Callable | None = None,
    ) -> StageAgent:
        """创建共享同一 LLM 的子 StageAgent。用于组合模式（如 ReflectionAgent 内部编排多个子 agent）。

        可选 on_stage_start 回调：签名与 _on_stage_start 一致，用于在阶段开始时注入上下文消息。
        可选 on_filter_tool_calls 回调：签名与 _filter_tool_calls 一致，用于过滤 tool_calls。
        """
        agent = StageAgent(
            make_chat_model=self._make_chat_model,
            stages=stages,
            overall_system_prompt=overall_prompt,
            reset_registry=reset_registry,
        )
        if on_stage_start is not None:
            agent._on_stage_start = types.MethodType(on_stage_start, agent)
        if on_filter_tool_calls is not None:
            agent._filter_tool_calls = types.MethodType(on_filter_tool_calls, agent)
        return agent

    # ── 阶段级钩子（子类可覆盖）────────────────────

    def _get_run_context(self) -> dict[str, Any] | None:
        """获取当前 run() 的上下文 dict。阶段钩子中调用，用于读取运行时数据。

        仅在 run() 执行期间可用，返回 None 表示当前不在 run() 中。
        """
        return getattr(self, "_current_context", None)

    def _get_stage_prompt(self, stage_name: str, default_prompt: str) -> str:
        """返回阶段的 system prompt。子类按 stage_name 匹配后修改，无需重写整个 stages 列表。

        默认返回 default_prompt（不变）。
        """
        return default_prompt

    def _get_stage_tools(self, stage_name: str, default_tools: list[BaseTool]) -> list[BaseTool]:
        """返回阶段的工具列表。子类按 stage_name 匹配后增减工具，无需重写整个 stages 列表。

        默认返回 default_tools（不变）。
        """
        return default_tools

    async def _on_stage_start(self, stage_name: str, stage_index: int, messages: list) -> None:
        """阶段开始前调用。子类可注入额外 SystemMessage/HumanMessage 到 messages 列表。

        默认 no-op。
        """

    async def _on_stage_end(self, stage_name: str, stage_index: int, messages: list, output: str) -> None:
        """阶段结束后调用。子类可后处理输出、记录日志或注入后续消息。

        默认 no-op。
        """

    def _should_early_exit(self, stage_name: str, stage_index: int, output: str) -> bool:
        """阶段完成后调用，返回 True 则跳过剩余阶段直接结束。

        子类覆盖此方法实现阶段特定的早退逻辑。
        默认返回 False。
        """
        return False

    def _filter_tool_calls(
        self, stage_name: str, response: Any
    ) -> list:
        """LLM 返回 response 后、执行前调用。子类可覆盖做去重/限额过滤。

        在 response.tool_calls 上原地修改（移除被拦截的调用），
        返回要追加到 messages 的 HumanMessage 列表（告知 LLM 哪些调用被拦截）。
        默认透传，返回空列表。
        """
        return []

    # ── 受保护的上下文工具方法（子类可调用）─────────

    @staticmethod
    def _register_entities(context: dict[str, Any]) -> None:
        """从 context 中的 ID 列表注册实体短引用（A1/T1/F1/R1）"""
        _register_entities(context)

    @staticmethod
    def _set_task_context(context: dict[str, Any]) -> None:
        """从 context 提取 task_id 和 analysis_ids 设置 task context"""
        _set_task_ctx(context)

    @staticmethod
    async def _build_enriched_context(context: dict[str, Any]) -> dict[str, Any]:
        """拉取全部实体正文，构建无 UUID 的 LLM 输入上下文"""
        return await _build_llm_context(context)

    @staticmethod
    def _extract_image_urls(context: dict[str, Any]) -> list[str]:
        """从 context 中提取图片 URL 并从 context 中移除"""
        return _extract_image_urls(context)

    # ── run ────────────────────────────────────

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        self._current_context = context
        try:
            with start_observation(
                name=self.__class__.__name__,
                as_type="agent",
                input=safe_observation_value(context),
                metadata={
                    "stage_count": len(self.stages),
                    "reset_registry": self._reset_registry,
                },
            ) as agent_obs:
                self._inject_task_context(context)
                await self._prepare_context(context)
                messages, task_id, agent_label = await self._init_session(context)
                all_outputs: list[str] = []
                early_exit = False
                llm_content_risk = False
                failed_stage = "unknown"
                try:
                    for i, stage in enumerate(self.stages):
                        failed_stage = stage.get("name", "unknown")
                        stage_output = await self._run_stage(
                            stage,
                            i,
                            messages,
                            task_id,
                            agent_label,
                        )
                        if stage_output:
                            all_outputs.append(stage_output)
                        if self._should_early_exit(stage.get("name", ""), i, stage_output or ""):
                            log_progress(agent_label, "早退", level="info", task_id=task_id, stage=stage.get("name", ""), reason="agent 判定无需继续")
                            early_exit = True
                            break
                except _ContentSafetyError:
                    logger.warning("{} 遇到 Content Exists Risk，直接退出 [task={}]", agent_label, task_id)
                    llm_content_risk = True
                except Exception as exc:
                    tb_str = _traceback.format_exc()
                    error_str = repr(exc)
                    tb_short = tb_str[:2000]
                    log_progress(agent_label, f"失败(stage={failed_stage})", level="error", task_id=task_id, error=error_str)
                    if agent_obs is not None:
                        agent_obs.update(level="ERROR", status_message=error_str,
                                         output={"error": error_str, "stage": failed_stage, "traceback": tb_short})
                    raise

                result = {"content": all_outputs, "entities": get_session_registry(), "early_exit": early_exit, "llm_content_risk": llm_content_risk}
                await self.end()
                total_len = sum(len(o) for o in all_outputs)
                log_progress(
                    agent_label, "完成", task_id=task_id, output_len=total_len, stage_count=len(all_outputs), entity_count=len(result["entities"])
                )
                if agent_obs is not None:
                    agent_obs.update(output=safe_observation_value(result))
                return result
        finally:
            del self._current_context

    async def end(self) -> None:
        """运行结束时的钩子，子类可覆盖以执行收尾逻辑（如自动补调工具）。"""
        return

    # ── session 初始化 ─────────────────────────

    async def _init_session(self, context: dict[str, Any]) -> tuple[list, str, str]:
        """reset → 注册实体 → 拉取正文 → 构建初始 messages"""
        if self._reset_registry:
            reset_session_registry()

        _register_entities(context)

        llm_context = await _build_llm_context(context)
        _set_task_ctx(context)

        # 提取图片 URL，以 image_url content block 传给多模态模型
        image_urls = _extract_image_urls(llm_context)
        text_content = json.dumps(llm_context, ensure_ascii=False, indent=2, default=str)

        messages: list = []
        if self.overall_system_prompt:
            messages.append(SystemMessage(content=self.overall_system_prompt))
        if image_urls:
            content_blocks: list[dict] = [{"type": "text", "text": text_content}]
            for url in image_urls:
                content_blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
            messages.append(HumanMessage(content=content_blocks))
        else:
            messages.append(HumanMessage(content=text_content))

        self.messages = messages
        self.tool_use = set()
        agent_label = f"{self.__class__.__name__}"
        task_id = context.get("task_id") or "-"
        log_progress(agent_label, "开始", level="info", task_id=task_id, stage_count=len(self.stages))
        return messages, str(task_id), agent_label

    # ── 阶段执行 ───────────────────────────────

    async def _run_stage(
        self, stage: dict[str, Any], stage_index: int, messages: list, task_id: str, agent_label: str
    ) -> str:
        """运行单个阶段，返回收敛时的文本输出"""
        stage_name = stage.get("name") or stage.get("stage_name") or stage.get("title") or f"stage_{stage_index + 1}"
        default_tools: list[BaseTool] = stage.get("tools", [])
        max_iter = stage.get("max_iterations", 10)
        stage_label = f"{self.__class__.__name__}:{stage_name}"

        # ── 钩子先行：动态 prompt 和 tools ──
        system_prompt = self._get_stage_prompt(stage_name, stage["system_prompt"])
        stage_tools = self._get_stage_tools(stage_name, default_tools)

        # ── 钩子：阶段前置 ──
        await self._on_stage_start(stage_name, stage_index, messages)

        # ── 观测记录的是钩子修改后的实际值 ──
        with (
            start_observation(
                name=f"stage:{stage_name}",
                as_type="chain",
                input={
                    "stage_index": stage_index + 1,
                    "stage_name": stage_name,
                    "max_iterations": max_iter,
                    "tools": [tool.name for tool in stage_tools],
                },
            ),
            progress_span(
                stage_label,
                stage_index=stage_index + 1,
                stage_total=len(self.stages),
                max_iterations=max_iter,
                tools=[tool.name for tool in stage_tools],
                task_id=task_id,
            ),
        ):
            output = await self._run_stage_impl(
                stage_name,
                stage_tools,
                max_iter,
                stage_label,
                system_prompt,
                messages,
                task_id,
            )

        # ── 钩子：阶段后置 ──
        await self._on_stage_end(stage_name, stage_index, messages, output)

        return output

    async def _run_stage_impl(
        self,
        stage_name: str,
        stage_tools: list[BaseTool],
        max_iter: int,
        stage_label: str,
        system_prompt: str,
        messages: list,
        task_id: str,
    ) -> str:
        """阶段内部循环（迭代 → LLM 调用 → 工具执行）"""
        messages.append(SystemMessage(content=system_prompt))
        if stage_tools:
            messages.append(SystemMessage(content=TOOL_STRATEGY_MSG))

        llm = self._make_chat_model()
        llm_with_tools = llm.bind_tools(stage_tools, parallel_tool_calls=True) if stage_tools else llm

        iterations = 0
        injected: set = set()
        final_output = ""

        # 消息历史 token 上限（保守估算：2 chars/token）
        MAX_TOKENS = self.max_context_tokens

        while iterations < max_iter:
            iteration = iterations + 1
            log_progress(stage_label, "迭代", task_id=task_id, iteration=f"{iteration}/{max_iter}")

            _inject_convergence(iterations, max_iter, messages, injected)

            # 消息历史压缩：按 token 估算
            estimated_tokens = _estimate_tokens(messages)
            if estimated_tokens > MAX_TOKENS:

                # 按 token 估算确定尾部保留范围
                tail_tokens = 0
                tail_limit = MAX_TOKENS - 1  # 留 1 token 给头部兜底
                tail_start = len(messages)
                for j in range(len(messages) - 1, -1, -1):
                    msg_tokens = _estimate_single_message_tokens(messages[j])
                    if tail_tokens + msg_tokens > tail_limit:
                        break
                    tail_tokens += msg_tokens
                    tail_start = j

                # 头部：只保留开头少量设置消息（最多 ~10% token 预算）
                HEAD_BUDGET = max(1, MAX_TOKENS // 10)
                head_end = 0
                head_tokens = 0
                for j in range(min(10, tail_start)):
                    msg_tokens = _estimate_single_message_tokens(messages[j])
                    if head_tokens + msg_tokens > HEAD_BUDGET:
                        break
                    head_tokens += msg_tokens
                    head_end = j + 1

                # 安全边界：头部不能以 tool_calls 结尾（否则对应 ToolMessage 被丢弃，LLM 拒收）
                while (
                    head_end > 0
                    and isinstance(messages[head_end - 1], AIMessage)
                    and _has_tool_calls(messages[head_end - 1])
                ):
                    head_end -= 1
                preserved_head = list(messages[:head_end])

                # 安全边界：尾部不能以孤儿 ToolMessage 开头（tool_calls 已在丢弃区）
                if tail_start > head_end:
                    recent_tail_first = messages[tail_start:]
                    if recent_tail_first:
                        first = recent_tail_first[0]
                        if isinstance(first, ToolMessage):
                            orphan_tc_id = first.tool_call_id
                            for j in range(tail_start - 1, -1, -1):
                                if isinstance(messages[j], AIMessage) and _has_tool_calls(messages[j]):
                                    if any(tc.get("id") == orphan_tc_id for tc in (messages[j].tool_calls or []) + (messages[j].additional_kwargs or {}).get("tool_calls", [])):
                                        tail_start = j
                                        break

                # 安全边界：尾部中 AIMessage(tool_calls) 如有 ToolMessage 落在丢弃区，回退 tail_start
                if tail_start > head_end:
                    # 收集丢弃区内所有 ToolMessage 的 tool_call_id
                    dropped_tc_ids: set[str] = set()
                    for m in messages[head_end:tail_start]:
                        if isinstance(m, ToolMessage):
                            dropped_tc_ids.add(m.tool_call_id)
                    if dropped_tc_ids:
                        # 检查尾部中是否有 AIMessage 引用了这些孤儿 tool_call_id
                        for j in range(tail_start, len(messages)):
                            m = messages[j]
                            if isinstance(m, AIMessage) and _has_tool_calls(m):
                                if any(tc.get("id") in dropped_tc_ids for tc in (m.tool_calls or []) + (m.additional_kwargs or {}).get("tool_calls", [])):
                                    # 将该 AIMessage 及其后续全部保留（回退 tail_start）
                                    tail_start = j
                                    break

                # 边界内完整性校验：head 中任意 AIMessage(tool_calls) 的 ToolMessage
                # 如有任一落在丢弃区，则撤回该 AIMessage（否则 API 400 拒收）
                if tail_start > head_end:
                    dropped_start = head_end
                    dropped_end = tail_start
                    dropped_tc_ids_2: set[str] = set()
                    for m in messages[dropped_start:dropped_end]:
                        if isinstance(m, ToolMessage):
                            dropped_tc_ids_2.add(m.tool_call_id)
                    if dropped_tc_ids_2:
                        new_head_end = len(preserved_head)
                        for i in range(len(preserved_head) - 1, -1, -1):
                            m = preserved_head[i]
                            if isinstance(m, AIMessage) and _has_tool_calls(m):
                                if any(tc.get("id") in dropped_tc_ids_2 for tc in (m.tool_calls or []) + (m.additional_kwargs or {}).get("tool_calls", [])):
                                    new_head_end = i
                        if new_head_end < len(preserved_head):
                            preserved_head = list(preserved_head[:new_head_end])
                            logger.warning(
                                "消息压缩：撤回 {} 条含孤儿 tool_calls 的 AIMessage（保留 {} 条 head + {} 条 tail） [task={}]",
                                len(preserved_head) - new_head_end + len(messages[:head_end]) - len(preserved_head),
                                len(preserved_head), len(messages) - tail_start, task_id,
                            )

                recent_tail = list(messages[tail_start:])

                dropped_count = len(messages) - len(preserved_head) - len(recent_tail)
                logger.info(
                    "消息压缩：丢弃中间 {} 条消息（{} → {}），尾部保留约 {} tokens [task={}]",
                    dropped_count, len(messages), len(preserved_head) + len(recent_tail) + 1,
                    _estimate_tokens(recent_tail), task_id,
                )
                logger.debug(
                    "消息压缩详情（~{} 估量）：保留头部 {} + 尾部 {} [task={}]",
                    estimated_tokens, len(preserved_head), len(recent_tail), task_id,
                )
                messages[:] = preserved_head + [
                    HumanMessage(content=f"（系统已压缩对话历史，丢弃了中间 {dropped_count} 条消息以控制上下文长度）")
                ] + recent_tail

            # 长阶段最后一轮：解绑工具，强制文本输出
            is_last = iterations >= max_iter - 1
            force_converge = is_last and max_iter >= _HARD_STOP_THRESHOLD and stage_tools
            invoke_llm = llm if force_converge else llm_with_tools

            llm_retries = 0
            llm_max_retries = settings.agent_llm_max_retries
            llm_retry_suffix = ""
            while True:
                try:
                    log_progress(
                        f"{stage_label}:LLM",
                        f"开始{llm_retry_suffix}",
                        task_id=task_id,
                        iteration=f"{iteration}/{max_iter}",
                        tool_count=len(stage_tools),
                    )
                    fixed = _sanitize_orphan_tool_calls(messages, task_id=task_id)
                    if fixed:
                        logger.warning(
                            "LLM 调用前修复 {} 条消息的 tool_calls 协议问题 [task={}]",
                            fixed, task_id,
                        )
                    verified = _verify_no_orphan_tool_calls(messages, task_id)
                    if verified:
                        logger.warning(
                            "LLM 调用前防御性修复 {} 条 AIMessage 的孤儿 tool_calls [task={}]",
                            verified, task_id,
                        )
                        fixed2 = _sanitize_orphan_tool_calls(messages, task_id=task_id)
                        if fixed2:
                            logger.warning(
                                "防御性修复后再次清理 {} 条 tool_calls/ToolMessage 协议问题 [task={}]",
                                fixed2, task_id,
                            )
                    response = await observe_langchain_generation(
                        name=f"llm:{stage_name}",
                        llm=llm,
                        messages=messages,
                        invoke=lambda ivk=invoke_llm, msgs=messages: ivk.ainvoke(msgs),
                        metadata={
                            "stage_index": 0,
                            "stage_name": stage_name,
                            "iteration": iteration,
                            "tools": [t.name for t in stage_tools],
                        },
                    )
                    break
                except (TimeoutError, Exception) as exc:
                    is_transient = isinstance(exc, asyncio.TimeoutError) or _is_transient_error(exc)
                    if not is_transient:
                        if _is_content_safety_error(exc):
                            raise _ContentSafetyError(str(exc)) from exc
                        raise
                    llm_retries += 1
                    if llm_retries >= llm_max_retries:
                        raise
                    delay = 1.0 * (2 ** (llm_retries - 1)) + random.uniform(0, 1.0)
                    exc_name = exc.__class__.__name__
                    reason = "超时" if isinstance(exc, asyncio.TimeoutError) else exc_name
                    logger.warning(
                        "LLM 调用失败 stage={} iter={}/{}: {} ({})，第 {}/{} 次重试 (task={})，{:.1f}s 后重试",
                        stage_name, iteration, max_iter, str(exc), exc_name, llm_retries, llm_max_retries, task_id, delay,
                    )
                    log_progress(
                        f"{stage_label}:LLM",
                        f"失败({reason})，{llm_retries}/{llm_max_retries}次重试",
                        task_id=task_id,
                        iteration=f"{iteration}/{max_iter}",
                    )
                    llm_retry_suffix = f"（重试{llm_retries}/{llm_max_retries}）"
                    await asyncio.sleep(delay)

            log_progress(
                f"{stage_label}:LLM",
                "完成",
                task_id=task_id,
                iteration=f"{iteration}/{max_iter}",
                tool_calls=len(response.tool_calls or []),
                content_len=len(response.content or ""),
            )

            # 收敛分支：LLM 未返回 tool_calls — 但仍需清理 additional_kwargs 残留
            # langchain-openai 序列化时若 tool_calls 为 falsy 会回退读
            # additional_kwargs["tool_calls"]，所以必须两边都清除。
            if not (response.tool_calls or []):
                final_output = response.content or ""
                log_progress(
                    stage_label,
                    "无工具调用，阶段收敛",
                    task_id=task_id,
                    iteration=f"{iteration}/{max_iter}",
                    content_len=len(final_output),
                )
                _clear_all_tool_call_fields(response)
                messages.append(response)
                break

            # 最后一轮：模型仍调用了工具
            # - 长阶段：工具已解绑，理论上不走这里（安全网兜底）
            # - 短阶段：工具未解绑，尊重模型选择，取已有文本
            if is_last:
                final_output = response.content or ""
                log_progress(
                    stage_label,
                    "最后一轮收敛",
                    task_id=task_id,
                    iteration=f"{iteration}/{max_iter}",
                    content_len=len(final_output),
                )
                _clear_all_tool_call_fields(response)
                messages.append(response)
                break

            # Clear invalid_tool_calls before appending to history
            _clear_invalid_tool_calls(response)

            # 钩子：对 tool_calls 做去重/限额过滤（子类覆盖）
            extra_messages = self._filter_tool_calls(stage_name, response)

            messages.append(response)
            for extra_msg in extra_messages:
                messages.append(extra_msg)
            for tc in response.tool_calls:
                self.tool_use.add(tc["name"])
            await _execute_tool_calls(
                response.tool_calls,
                stage_tools,
                stage_name,
                stage_label,
                task_id,
                messages,
            )
            iterations += 1
            log_progress(
                stage_label,
                "迭代完成",
                task_id=task_id,
                iteration=f"{iterations}/{max_iter}",
                tool_calls=len(response.tool_calls),
            )

        if iterations >= max_iter and not final_output:
            messages.append(HumanMessage(content="（达到最大工具调用次数，阶段未正常结束）"))
            log_progress(stage_label, "达到最大工具调用次数", level="warning", task_id=task_id, max_iterations=max_iter)
        # 不引入额外无用信息
        # messages.append(
        #     HumanMessage(content=f"（--- 阶段'{stage_name}'已完成，上述是该阶段全部输出。系统正在进入下一阶段 ---）")
        # )
        return final_output
