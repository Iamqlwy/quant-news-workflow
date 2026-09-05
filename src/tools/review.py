"""回顾工具 —— 交易记录、节点历史状态（分析报告读取已合并到 knowledge.py 的 read 工具）"""

from __future__ import annotations

import contextlib
from typing import Any

from loguru import logger

from ._deps import _is_transient_tool_error, err, ok, resolve_node_name, resolve_ref
from .context import get_ctx
from .registry import register_tool
from .schemas.review import GetNodeHistoryArgs, GetTradeArgs


def _get_field(item: dict | object, key: str, default: Any = "") -> Any:
    """Extract a field from either a dict or an object."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


# ═══════════════════════════════════════════════
# 工具协程
# ═══════════════════════════════════════════════


@register_tool(
    name="get_trade",
    description="根据交易引用（如 T1）读取交易操作记录：操作类型、标的、价格、数量、理由等。"
    "适用：查看之前生成的交易建议详情，或复盘时回顾交易决策。"
    "不适用：创建新交易建议时用 create_trade；审批或拒绝交易时用 review_trade。",
    category="review",
    args_schema=GetTradeArgs,
)
async def get_trade(trade_ref: str) -> str:
    try:
        trade_id = resolve_ref(trade_ref)
        if trade_id is None:
            return err(f"无效的交易引用：{trade_ref}（当前 session 中不存在）")
        quant = get_ctx().quant
        result = await quant.trading.get(trade_id)
        data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        data["ref"] = trade_ref
        return ok(data)
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_trade failed: {}", e)
        return err(f"读取交易记录失败：{e}")


@register_tool(
    name="get_node_history",
    description="读取 WorldNode 最近 5 个历史状态版本，返回每个版本的时间范围和核心逻辑摘要。"
    "适用：追溯某个标的/板块投资逻辑的演变过程。"
    "不适用：更新节点状态时用 update_node_state；搜索节点信息时用 search_kb。",
    category="review",
    args_schema=GetNodeHistoryArgs,
)
async def get_node_history(node_name: str) -> str:
    try:
        node_id = await resolve_node_name(node_name)
        quant = get_ctx().quant
        result = await quant.nodes.get_state_history(node_id)
        if isinstance(result, list):
            history = result
        elif isinstance(result, dict):
            history = result.get("items", [])
        else:
            history = []

        if not history:
            return ok(f"节点 {node_name} 无历史状态记录")

        with contextlib.suppress(ValueError, TypeError):
            history.sort(key=lambda h: int(_get_field(h, "version", 0)), reverse=True)

        summaries = []
        for h in history[:5]:
            v = _get_field(h, "version", "?")
            ef = _get_field(h, "effective_from", "?")
            et = _get_field(h, "effective_to", "?")
            summary = _get_field(h, "state_summary") or _get_field(h, "core_logic")
            summaries.append(f"v{v} ({ef} ~ {et}): {str(summary)[:200]}")

        result_text = f"节点 {node_name} 历史状态（最近 5 个版本）：\n" + "\n".join(summaries)
        if len(history) > 5:
            result_text += f"\n\n（共 {len(history)} 个版本，仅显示最近 5 个）"
        return ok(result_text)
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.warning("get_node_history failed: {}", e)
        return err(f"获取节点历史失败：{e}")
