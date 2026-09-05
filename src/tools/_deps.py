"""共享依赖与工具辅助函数

保留: session 注册表, 格式化工具, 实体引用解析, ok/err
移除: contextvars 依赖注入, init_tool_deps, _tool 工厂
"""

from __future__ import annotations

import contextvars as _ctxvars
import json as _json
from typing import Any

# ═══════════════════════════════════════════════
# Task context (contextvar)
# ═══════════════════════════════════════════════

_task_ctx_var: _ctxvars.ContextVar[dict[str, Any] | None] = _ctxvars.ContextVar("task_context", default=None)


def set_task_context(task_id: str | None = None, analysis_ids: list[str] | None = None) -> None:
    ctx = _task_ctx_var.get() or {}
    if task_id:
        ctx["task_id"] = task_id
    if analysis_ids:
        ctx["analysis_ids"] = analysis_ids
    _task_ctx_var.set(ctx)


def get_task_context() -> dict[str, Any]:
    return _task_ctx_var.get() or {}


# ═══════════════════════════════════════════════
# 统一结果格式
# ═══════════════════════════════════════════════


def ok(data: Any) -> str:
    """结构化成功结果"""
    return _json.dumps({"status": "ok", "data": data}, ensure_ascii=False, indent=2)


def err(message: str) -> str:
    """结构化错误结果"""
    return _json.dumps({"status": "error", "message": message}, ensure_ascii=False)


def format_exception(exc: BaseException) -> str:
    """将异常格式化为用户可读的错误信息。

    策略：
    1. 收集 detail 属性（QuantClientError 系列），若有信息量则记入
    2. 遍历 __cause__ 链寻找底层根因的具体描述
    3. 包含 HTTP status_code 和 error_code（如果存在）
    4. 最终回退到异常类型名
    """
    parts: list[str] = []

    # 1) QuantClientError 系列有 detail + error_code
    detail = getattr(exc, "detail", None)
    if detail and str(detail).strip():
        parts.append(str(detail))
    error_code = getattr(exc, "error_code", None)
    if error_code:
        parts.append(f"error_code={error_code}")

    # 2) HTTP 异常附加 status_code
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"HTTP {status}")

    # 3) 追 __cause__ 链获取底层异常的有意义描述
    cause = exc
    while cause is not None:
        msg = str(cause)
        if msg and msg != cause.__class__.__name__ and msg not in parts:
            parts.append(msg)
        cause = cause.__cause__

    if parts:
        return "；".join(parts)
    return exc.__class__.__name__


def _is_transient_tool_error(exc: BaseException) -> bool:
    """判断工具调用中的异常是否属于瞬态错误（网络/超时），应由 _invoke_one_tool 重试。

    工具协程的 except Exception 应调用此函数：若返回 True 则 raise 原异常让
    _invoke_one_tool 的重试循环接管；若返回 False 则返回 err() 字符串。
    """
    try:
        from kbquant.client._base import QuantClientConnectionError, QuantClientHTTPError
    except ImportError:
        QuantClientConnectionError = QuantClientHTTPError = type(None)  # type: ignore[assignment]
    import asyncio

    if isinstance(exc, (asyncio.TimeoutError, QuantClientConnectionError,
                        ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return True
    if isinstance(exc, QuantClientHTTPError):
        # 4xx 不重试；5xx/429 重试
        _RETRYABLE = frozenset({429, 502, 503, 504})
        return getattr(exc, "status_code", 0) in _RETRYABLE
    return False


# ═══════════════════════════════════════════════
# Session 级短引用注册表 (contextvar)
# ═══════════════════════════════════════════════

_registry_var: _ctxvars.ContextVar[dict[str, dict] | None] = _ctxvars.ContextVar("tool_registry", default=None)
_reverse_var: _ctxvars.ContextVar[dict[str, str] | None] = _ctxvars.ContextVar("tool_reverse", default=None)
_counters_var: _ctxvars.ContextVar[dict[str, int] | None] = _ctxvars.ContextVar("tool_counters", default=None)


def _ensure_registry() -> dict[str, dict]:
    reg = _registry_var.get()
    if reg is None:
        reg = {}
        _registry_var.set(reg)
    return reg


def _ensure_reverse() -> dict[str, str]:
    rev = _reverse_var.get()
    if rev is None:
        rev = {}
        _reverse_var.set(rev)
    return rev


def _ensure_counters() -> dict[str, int]:
    counters = _counters_var.get()
    if counters is None:
        counters = {"A": 0, "T": 0, "F": 0, "R": 0, "N": 0, "G": 0}
        _counters_var.set(counters)
    return counters


def reset_session_registry() -> None:
    _registry_var.set({})
    _reverse_var.set({})
    _counters_var.set({"A": 0, "T": 0, "F": 0, "R": 0, "N": 0, "G": 0})


_SOURCE_PRECEDENCE = {"search": 1, "read": 2, "create": 3}


def register_entity(prefix: str, uuid_str: str, source: str = "read") -> str:
    reverse = _ensure_reverse()
    existing = reverse.get(uuid_str)
    if existing is not None:
        # 仅当新 source 优先级更高时才覆盖（search→read→create），不允许反向降级
        info = _ensure_registry().get(existing)
        if info is not None:
            current_rank = _SOURCE_PRECEDENCE.get(info["source"], 0)
            new_rank = _SOURCE_PRECEDENCE.get(source, 0)
            if new_rank > current_rank:
                info["source"] = source
        return existing
    registry = _ensure_registry()
    counters = _ensure_counters()
    counters[prefix] = counters.get(prefix, 0) + 1
    ref = f"{prefix}{counters[prefix]}"
    registry[ref] = {"uuid": uuid_str, "source": source}
    reverse[uuid_str] = ref
    return ref


def resolve_ref(ref: str) -> str | None:
    info = _ensure_registry().get(ref)
    return info["uuid"] if info else None


def resolve_ref_or_original(value: str) -> str:
    info = _ensure_registry().get(value)
    return info["uuid"] if info else value


def get_latest_ref(prefix: str) -> str | None:
    n = _ensure_counters().get(prefix, 0)
    if n == 0:
        return None
    return f"{prefix}{n}"


def get_session_registry() -> dict[str, str]:
    """返回 {ref: uuid_str} 的向后兼容映射"""
    return {ref: info["uuid"] for ref, info in _ensure_registry().items()}


def get_registry_items() -> dict[str, dict]:
    """返回 {ref: {"uuid": str, "source": str}} 的完整映射"""
    return dict(_ensure_registry())


def mark_registry_source(ref: str, source: str) -> None:
    """更新已注册实体的 source 标记（仅允许升级，不允许降级）"""
    info = _ensure_registry().get(ref)
    if info is not None:
        current_rank = _SOURCE_PRECEDENCE.get(info["source"], 0)
        new_rank = _SOURCE_PRECEDENCE.get(source, 0)
        if new_rank > current_rank:
            info["source"] = source


# ═══════════════════════════════════════════════

async def resolve_node_name(name: str, node_type: str | None = None) -> str:
    """通过名称搜索 WorldNode，返回最匹配的 UUID。

    使用 /nodes/names-aliases 一次性获取所有节点名称，做 Python 侧精确匹配。
    匹配策略（依次尝试）：
    1. 精确匹配（名称或别名）
    2. 子串匹配（唯一候选）
    3. node_type 过滤消歧
    4. 模糊匹配（edit distance >= 0.6）

    Unicode NFKC 归一化：将全角英文字母/数字归一化为半角 ASCII 等价形式，
    解决 A 股名称中"京东方Ａ"(全角)与"京东方A"(半角)不匹配的问题。
    """
    import unicodedata

    from .context import get_ctx

    def _norm(s: str) -> str:
        """NFKC 归一化 + casefold：全角→半角，统一大小写"""
        return unicodedata.normalize("NFKC", s.strip()).casefold()

    quant = get_ctx().quant
    search_name = _norm(name)

    # 一次性获取所有节点名称
    from src.utils.http_resilience import retry_api_call
    raw_items = await retry_api_call(
        lambda: quant.nodes.list_names_and_aliases(),
        name="获取节点名称列表",
        task_id="resolve_node_name",
    )
    items: list[dict] = []
    for it in raw_items:
        if isinstance(it, dict):
            items.append(it)
        else:
            d = it.model_dump()
            # 如果 schema 没有 id，尝试从原始对象获取
            if "id" not in d and hasattr(it, "id"):
                d["id"] = str(it.id)
            items.append(d)

    # 按 node_type 预过滤（如果指定）
    all_items_backup = items[:]  # 保留未过滤副本，用于跨类型诊断
    if node_type:
        items = [it for it in items if str(it.get("node_type", "")).lower() == node_type.lower()]

    # 确保每条记录有 id
    items = [it for it in items if it.get("id")]

    # Step 1: 精确匹配（名称或别名）
    for it in items:
        it_name = _norm(str(it.get("name", "")))
        if it_name == search_name:
            return str(it["id"])
        # 检查别名
        aliases = it.get("aliases") or []
        for alias in aliases:
            if _norm(str(alias)) == search_name:
                return str(it["id"])

    # Step 2: 子串匹配
    candidates: list[tuple[str, str]] = []
    for it in items:
        it_name = _norm(str(it.get("name", "")))
        if search_name in it_name or it_name in search_name:
            candidates.append((str(it.get("name", "")), str(it["id"])))

    if len(candidates) == 1:
        return candidates[0][1]

    # Step 3: 多个候选时，模糊匹配选最佳
    if candidates:
        import difflib

        best = max(candidates, key=lambda c: difflib.SequenceMatcher(None, search_name, _norm(c[0])).ratio())
        return best[1]

    # Step 4: 全量模糊匹配回退（仅在指定类型的 items 中搜索）
    import difflib

    best_ratio = 0.0
    best_id = ""
    best_title = ""
    for it in items:
        it_name = _norm(str(it.get("name", "")))
        if not it_name:
            continue
        ratio = difflib.SequenceMatcher(None, search_name, it_name).ratio()
        if ratio > best_ratio and ratio >= 0.8:
            best_ratio = ratio
            best_id = str(it["id"])
            best_title = str(it.get("name", ""))
    if best_id:
        from loguru import logger

        logger.debug("fuzzy matched node '{}' -> '{}' (similarity {:.2f})", name, best_title, best_ratio)
        return best_id

    # 构建增强的错误消息，包含跨类型匹配建议
    error_msg = f"找不到名为「{name}」的节点"
    if node_type:
        error_msg += f"（类型: {node_type}）"
    else:
        error_msg += "（不限类型）"

    # 在所有类型中搜索可能的匹配（用于诊断提示）
    cross_type_hints = []
    cross_type_best_id = ""
    cross_type_best_name = ""
    cross_type_best_ratio = 0.0
    if node_type and all_items_backup:
        for it in all_items_backup:
            it_name = _norm(str(it.get("name", "")))
            it_type = str(it.get("node_type", ""))
            if it_type.lower() == node_type.lower():
                continue  # 已在本类型中搜索过
            ratio = difflib.SequenceMatcher(None, search_name, it_name).ratio()
            if search_name in it_name or it_name in search_name:
                cross_type_hints.append(f"「{it.get('name', '')}」(类型:{it_type})")
                if ratio > cross_type_best_ratio and it.get("id"):
                    cross_type_best_ratio = ratio
                    cross_type_best_id = str(it["id"])
                    cross_type_best_name = str(it.get("name", ""))
            elif ratio >= 0.7:
                cross_type_hints.append(f"「{it.get('name', '')}」(类型:{it_type}, 相似)")
                if ratio > cross_type_best_ratio and it.get("id"):
                    cross_type_best_ratio = ratio
                    cross_type_best_id = str(it["id"])
                    cross_type_best_name = str(it.get("name", ""))

    # 如果只有一个明确的跨类型匹配（包含或高相似度），直接使用它而不报错
    if cross_type_best_id and len(cross_type_hints) == 1:
        from loguru import logger as _cross_logger

        _cross_logger.info(
            "resolve_node_name: '{}' 的 node_type={} 不匹配，已自动使用跨类型匹配: '{}'",
            name, node_type, cross_type_best_name,
        )
        return cross_type_best_id

    if cross_type_hints:
        error_msg += f"。但在其他类型中找到相似节点: {', '.join(cross_type_hints[:5])}"
        if len(cross_type_hints) > 5:
            error_msg += f" 等{len(cross_type_hints)}个"

    error_msg += "，请确认节点名称拼写是否正确。可通过搜索功能查询已存在的节点"

    from loguru import logger
    logger.debug(
        "resolve_node_name failed: name='{}', node_type='{}', "
        "all_items={}, filtered={}, "
        "cross_type_matches={}",
        name, node_type, len(all_items_backup), len(items), len(cross_type_hints)
    )

    raise ValueError(error_msg)


def _ensure_list(v: Any) -> list[str] | None:
    """兼容 LLM 将 list 参数传为 JSON 字符串的情况"""
    if v is None:
        return None
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = _json.loads(v)
            if isinstance(parsed, list) and len(parsed) <= 100:
                return [str(x) for x in parsed]
        except (_json.JSONDecodeError, TypeError):
            pass
    return [str(v)]
