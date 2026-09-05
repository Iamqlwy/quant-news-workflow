"""宏观工具 —— 宏观报告、宏观资讯、市场概况、指数行情、报告更新"""

from __future__ import annotations

import json as _json
from datetime import datetime

from kbquant.schemas.macro_report import MacroReportUpdate
from loguru import logger

from ._deps import _is_transient_tool_error, err
from .context import get_ctx
from .registry import register_tool
from .schemas.macro import UpdateMacroReportArgs


def _get_custom_time() -> datetime | None:
    """获取当前项目时钟时间（北京时间），用于 kbquant custom_time。
    实盘模式返回 None（使用服务器时间），模拟模式返回模拟时钟时间。"""
    ctx = get_ctx()
    if ctx.clock is not None and not ctx.clock.is_realtime:
        return ctx.clock.now
    return None

# ═══════════════════════════════════════════════
# 工具协程
# ═══════════════════════════════════════════════


@register_tool(
    name="update_macro_report",
    description="写入本日宏观报告增量内容（系统每日生成一份新报告，本工具只调用一次）。提供完整正文（Markdown格式）和摘要。"
    "适用：宏观Agent完成宏观分析后，将新的宏观判断、资产观点、行业配置建议写入当日报告。",
    category="macro",
    args_schema=UpdateMacroReportArgs,
)
async def update_macro_report(
    content: str,
    summary: str,
) -> str:
    try:
        quant = get_ctx().quant
        await quant.macro_report.update(
            MacroReportUpdate(
                content=content,
                summary=summary,
                changed_sections=[],
                custom_time=_get_custom_time(),
            )
        )
        return _json.dumps(
            {
                "status": "updated",
                "summary": summary,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("update_macro_report failed: {}", e)
        return err(f"宏观报告更新失败：{e}")
