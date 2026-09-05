"""知识检索工具 —— search_kb（唯一搜索入口）+ read（统一读取接口）+ 偏好/节点状态"""

from __future__ import annotations

from kbquant.client._base import QuantClientHTTPError, QuantClientNotFoundError
from kbquant.schemas.search import FetchByIdsRequest, SearchRequest
from loguru import logger

from ._deps import (
    _is_transient_tool_error,
    err,
    format_exception,
    mark_registry_source,
    ok,
    register_entity,
    resolve_ref,
)
from .context import get_ctx
from .registry import register_tool
from .schemas.knowledge import GetPreferencesArgs, ReadArgs, SearchKBArgs

# ═══════════════════════════════════════════════
# 工具协程
# ═══════════════════════════════════════════════


@register_tool(
    name="search_kb",
    description="混合搜索知识库（BM25+向量+结构权重），返回摘要列表。每条结果分配一个会话引用（R1/A1/F1/N1），用 read 工具读取全文。"
    "注意：R1/A1/F1 等会话引用仅在本会话内有效，写入报告时应使用资讯时间+标题替代。"
    "适用：这是唯一的知识库搜索入口，用于查找历史资讯、分析报告、复盘反馈、节点状态。",
    category="knowledge",
    args_schema=SearchKBArgs,
)
async def search_kb(
    query_text: str,
    limit: int = 10,
    only_tables: list[str] | None = None,
) -> str:
    try:
        limit = min(limit, 20)
        quant = get_ctx().quant
        result = await quant.search.search(
            SearchRequest(
                query_text=query_text,
                limit=limit,
                only_tables=only_tables,
            )
        )

        _TYPE_TO_PREFIX = {
            "raw_information": "R",
            "analysis": "A",
            "analyses": "A",
            "feedback": "F",
            "feedbacks": "F",
            "node": "N",
            "nodes": "N",
        }

        items = []
        for r in (result.items or []):
            prefix = _TYPE_TO_PREFIX.get(r.result_type, "?")
            ref = register_entity(prefix, str(r.id), source="search")
            items.append(
                {
                    "ref": ref,
                    "time": r.time.isoformat() if r.time else None,
                    "title": r.title,
                    "snippet": r.snippet or "",
                    "score": r.score.total,
                }
            )

        return ok(
            {
                "total": result.total,
                "items": items,
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        msg = format_exception(e)
        logger.error("search_kb 失败: {} ({})", msg, e.__class__.__name__)
        return err(f"搜索失败：{msg}")




_TABLE_TRIM_FIELDS = {
    "raw_information": {"title", "body", "source", "published_at", "info_type", "importance_score"},
    "analyses": {"title", "content", "analysis_type", "confidence", "time_horizon", "created_at"},
    "feedbacks": {"title", "expected_outcome", "actual_outcome", "judgment_correct",
                  "error_reason", "missed_factors", "adjustment_suggestions", "lessons_learned", "updated_at"},
    "trading_operations": {"operation_type", "symbol", "quantity", "price", "rationale",
                           "expected_impact", "risk_level", "status", "executed_at", "updated_at"},
    "nodes": {"name", "node_type", "description", "ticker", "updated_at", "current_state"},
    "_node_state": {"core_logic", "primary_drivers", "risks", "focus_points", "recent_changes", "state_summary"},
}


def _trim_records(data: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """裁剪 kbquant 返回的完整记录，只保留对 LLM 有意义的字段，并去除 None 值。"""
    trimmed: dict[str, list[dict]] = {}
    for table, records in data.items():
        fields = _TABLE_TRIM_FIELDS.get(table, set())
        if not fields:
            trimmed[table] = records
            continue
        kept: list[dict] = []
        for rec in records:
            t = {k: v for k, v in rec.items() if k in fields and v is not None}
            # 裁剪 nested current_state
            if table == "nodes" and "current_state" in t and isinstance(t["current_state"], dict):
                state_fields = _TABLE_TRIM_FIELDS["_node_state"]
                t["current_state"] = {k: v for k, v in t["current_state"].items()
                                      if k in state_fields and v is not None}
            kept.append(t)
        trimmed[table] = kept
    return trimmed


@register_tool(
    name="read",
    description="批量读取完整内容。refs 为逗号分隔的会话引用，如 'R1,A2,F3'。"
    "引用来自 search_kb 返回的 items[].ref（R/A/F/N 开头），或写入操作（分析/交易/复盘）返回的 ref。"
    "一次调用可读取多条，减少往返次数。",
    category="knowledge",
    args_schema=ReadArgs,
)
async def read(refs: str) -> str:
    """批量读取，一次服务端调用。refs: 'R1,A2,F3'"""
    try:
        # 前缀 → 服务端表名
        _PFX_TABLE = {
            "R": "raw_information",
            "A": "analyses",
            "F": "feedbacks",
            "T": "trading_operations",
            "N": "nodes",
        }

        grouped: dict[str, list[str]] = {}

        for ref in refs.split(","):
            ref = ref.strip()
            if not ref:
                continue

            uid = resolve_ref(ref)
            if uid is None:
                return err(f"无效的会话引用：{ref}")

            table = _PFX_TABLE.get(ref[0])
            if table is None:
                return err(f"无法识别引用类型：'{ref}'（首字母 {ref[0]} 不在 R/A/F/T/N 中）")
            grouped.setdefault(table, []).append(str(uid))
            mark_registry_source(ref, "read")

        if not grouped:
            return err("没有有效的引用")

        quant = get_ctx().quant
        result = await quant.search.fetch_by_ids(FetchByIdsRequest(table_ids=grouped))
        return ok(_trim_records(result.data))
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        msg = format_exception(e)
        logger.error("read 失败: {} ({})", msg, e.__class__.__name__)
        return err(f"读取失败：{msg}")


@register_tool(
    name="get_preferences",
    description="获取投资偏好认知文本。填 sector 返回行业偏好；不填 sector 返回市场整体偏好。"
    "适用：了解某个行业的最新投资偏好和策略倾向，或了解市场整体的风格/风险偏好/轮动方向。"
    "不适用：查看行业相关资讯时用 search_kb。",
    category="knowledge",
    args_schema=GetPreferencesArgs,
)
async def get_preferences(sector: str | None = None) -> str:
    try:
        quant = get_ctx().quant
        if sector is None:
            # 市场整体偏好
            resp = await quant.preferences.get_market_cognition()
            if resp.text:
                return ok(f"【市场整体】偏好认知：\n{resp.text}")
            return ok("【市场整体】暂无市场偏好认知记录。")

        # 获取所有有记录的行业，做模糊匹配
        all_resp = await quant.preferences.get_all_sectors()
        available = all_resp.sectors or []
        result = _match_sector(sector, available)
        if result["match"]:
            name, score = result["match"], result["score"]
            resp = await quant.preferences.get_industry_cognition(name)
            if resp.text:
                return ok(f"【{name}】行业偏好认知（输入「{sector}」匹配到）：\n{resp.text}")
            return ok(f"【{name}】暂无行业偏好认知记录。")
        if result["candidates"]:
            names = [f"{n}({s})" for n, s in result["candidates"]]
            return err(f"未找到「{sector}」的偏好记录，最接近的行业：{', '.join(names)}")
        return err(f"未找到「{sector}」的偏好记录。")
    except QuantClientNotFoundError:
        if sector is None:
            return err("【市场整体】暂无市场偏好认知记录。")
        return err(f"【{sector}】暂无行业偏好认知记录。")
    except QuantClientHTTPError as e:
        if e.status_code >= 500:
            logger.warning("偏好服务不可用 ({}): {}", e.status_code, e.detail)
            return err("偏好认知服务暂时不可用，请稍后重试。")
        raise
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        msg = format_exception(e)
        logger.error("get_preferences 失败: {} ({})", msg, e.__class__.__name__)
        return err(f"获取偏好失败：{msg}")


def _match_sector(query: str, candidates: list[str]) -> dict:
    """模糊匹配行业名，返回 {'match': str|None, 'score': float, 'candidates': [(name, label), ...]}"""

    GENERIC = {"板块", "概念", "题材", "热点", "主题", "行业", "赛道"}

    def _char_jaccard(a: str, b: str) -> float:
        sa, sb = set(a), set(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def _score(a: str, b: str) -> float:
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        # 候选是 query 的子串（用户输入了更详细的名称，如"白酒行业"含"白酒"）→ 直接匹配
        if b in a:
            return 0.8
        # query 是候选的子串（用户输入了简称）→ 长度需达到候选的60%，如"电池"(2)≥"锂电池"(3)*0.6
        if a in b and len(a) >= len(b) * 0.6:
            return 0.8
        return _char_jaccard(a, b)

    def _label(s: float) -> str:
        if s >= 0.8:
            return "高"
        if s >= 0.6:
            return "中"
        return "低"

    # 过滤泛词
    available = [c for c in candidates if c.strip() not in GENERIC]
    if not available:
        return {"match": None, "score": 0.0, "candidates": []}

    # 最佳匹配
    best = max(available, key=lambda c: _score(query, c))
    best_score = _score(query, best)
    if best_score >= 0.6:
        return {"match": best, "score": best_score, "candidates": []}

    # 返回候选列表
    scored = [(c, _score(query, c)) for c in available]
    scored.sort(key=lambda x: (-x[1], x[0]))
    top = [(name, _label(s)) for name, s in scored[:10]]
    return {"match": None, "score": 0.0, "candidates": top}
