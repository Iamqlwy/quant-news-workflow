"""JSON 提取与修复工具 —— 从 LLM 回复中提取 JSON，含多种回退策略"""

from __future__ import annotations

import json
import re
from typing import Any


def _fix_inner_chinese_quotes(text: str) -> str:
    """替换 JSON 字符串值内部的中文引号（LLM 常用 ASCII " 替代中文引号）

    {"error": "无法识别"布伦特原油"..."} → {"error": "无法识别「布伦特原油」..."}
    """
    # Pass 1: Chinese-quote pairs followed by another Chinese char
    text = re.sub(
        r'([^\x00-\x7f])"([^\x00-\x7f]{1,50})"(?=[^\x00-\x7f])',
        r'\1「\2」',
        text,
    )
    # Pass 2: Chinese-quote pair immediately before JSON string closer
    text = re.sub(
        r'([^\x00-\x7f])"([^\x00-\x7f]{1,50})""',
        r'\1「\2」"',
        text,
    )
    return text


def _repair_json(text: str) -> str:
    """修复 LLM 常见的 JSON 语法错误"""
    text = re.sub(r"<\s*/?\s*think\s*>", "", text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"(?<=[{,])\s*([a-zA-Z_][\w]*)\s*:", r'"\1":', text)
    text = re.sub(r"'([^']*)'", r'"\1"', text)
    text = _fix_inner_chinese_quotes(text)
    open_count = text.count("{")
    close_count = text.count("}")
    if open_count > close_count:
        text += "}" * (open_count - close_count)
    return text


def _extract_first_json_object(text: str) -> str | None:
    """用大括号深度计数找到第一个平衡的 {...} 对象"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 返回文本中提取 JSON，含修复回退"""

    def _safe_load(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            repaired = _repair_json(raw)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                m = re.search(r'"error"\s*:\s*"(.+?)"\s*[}\]]', repaired)
                if m:
                    return {"error": m.group(1)}
                raise ValueError(f"无法解析 JSON: {raw[:200]}")

    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        return _safe_load(m.group(1))

    m = re.search(r"```\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return _safe_load(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    obj = _extract_first_json_object(text)
    if obj:
        return _safe_load(obj)

    raise ValueError(f"无法从 LLM 回复中提取 JSON: {text[:500]}")
