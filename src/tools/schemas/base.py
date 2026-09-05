from __future__ import annotations

import ast
import datetime
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.tools._deps import _ensure_list

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ToolArgsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @classmethod
    def rename_aliases(cls, data: Any, alias_map: dict[str, tuple[str, ...]]) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for canonical, aliases in alias_map.items():
            current = normalized.get(canonical)
            if cls.has_meaningful_value(current):
                continue
            for alias in aliases:
                if alias in normalized and cls.has_meaningful_value(normalized.get(alias)):
                    normalized[canonical] = normalized.get(alias)
                    break
        return normalized

    @staticmethod
    def has_meaningful_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return True

    @staticmethod
    def as_str(value: Any) -> str:
        if isinstance(value, str):
            return _CONTROL_CHARS_RE.sub("", value).strip()
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return str(value).strip()
        if isinstance(value, dict):
            if len(value) == 1:
                return ToolArgsModel.as_str(next(iter(value.values())))
            return ToolArgsModel.as_str(json.dumps(value, ensure_ascii=False))
        if isinstance(value, (list, tuple)):
            if len(value) == 1:
                return ToolArgsModel.as_str(value[0])
            return ToolArgsModel.as_str(json.dumps(value, ensure_ascii=False))
        if isinstance(value, set):
            if len(value) == 1:
                return ToolArgsModel.as_str(next(iter(value)))
            return ToolArgsModel.as_str(json.dumps(list(value), ensure_ascii=False))
        return ToolArgsModel.as_str(str(value))

    @staticmethod
    def clean_text(value: str) -> str:
        return _CONTROL_CHARS_RE.sub("", value).strip()

    @classmethod
    def as_optional_str(cls, value: Any) -> str | None:
        text = ToolArgsModel.as_str(value)
        return text or None

    @staticmethod
    def _normalize_list_items(items: list[Any]) -> list[Any]:
        if len(items) != 1:
            return items
        first = items[0]
        if not isinstance(first, str):
            return items
        text = first.strip()
        if not text:
            return []
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            try:
                loaded = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                loaded = None
        if isinstance(loaded, list):
            return loaded
        if isinstance(loaded, tuple | set):
            return list(loaded)
        if isinstance(loaded, dict):
            return [loaded]
        if any(sep in text for sep in (",", "\n", "\r", ";", "|", "\uff0c", "\uff1b", "\u3001")):
            return [part.strip() for part in re.split(r"[,;\|\n\r ，；、。]+", text) if part.strip()]
        return items

    @staticmethod
    def as_list(value: Any) -> list[Any] | None:
        if value is None:
            return None
        parsed = _ensure_list(value)
        if parsed is not None:
            normalized = ToolArgsModel._normalize_list_items(parsed)
            return normalized or None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                try:
                    loaded = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    loaded = None
            if isinstance(loaded, list):
                return loaded
            if isinstance(loaded, tuple | set):
                return list(loaded)
            if isinstance(loaded, dict):
                return [loaded]
            if any(sep in text for sep in (",", "\n", "\r", ";", "|", "\uff0c", "\uff1b", "\u3001")):
                return [part.strip() for part in re.split(r"[,;\|\n\r ，；、。]+", text) if part.strip()]
            if text:
                return [text]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, tuple | set):
            return list(value)
        return [value]

    @staticmethod
    def as_dict(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                try:
                    loaded = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    return {"text": text}
            if isinstance(loaded, dict):
                return loaded
            return {"value": loaded}
        return {"value": value}

    @staticmethod
    def as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "y", "on", "\u662f", "\u6b63\u786e", "\u5bf9", "\u6210\u529f"}:
                return True
            if text in {"false", "0", "no", "n", "off", "\u5426", "\u9519\u8bef", "\u9519", "\u5931\u8d25"}:
                return False
        raise ValueError("\u65e0\u6cd5\u89e3\u6790\u4e3a\u5e03\u5c14\u503c")

    @staticmethod
    def as_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace(",", "").replace("\uff0c", "").replace(" ", "")
            if not text:
                return None
            match = re.fullmatch(
                r"(?P<number>[+-]?\d+(?:\.\d+)?)"
                r"(?P<multiplier>万|亿|千|k|K|m|M)?"
                r"(?P<percent>%|％)?"
                r"(?P<unit>股|元|天|日|次|笔)?",
                text,
            )
            if match:
                number = float(match.group("number"))
                multiplier = match.group("multiplier")
                if multiplier in {"\u5343", "k", "K"}:
                    number *= 1_000
                elif multiplier in {"\u4e07"}:
                    number *= 10_000
                elif multiplier in {"\u4ebf"}:
                    number *= 100_000_000
                elif multiplier in {"m", "M"}:
                    number *= 1_000_000
                if match.group("percent"):
                    number /= 100.0
                return number
            if text.endswith("%") or text.endswith("\uff05"):
                return float(text[:-1]) / 100.0
            return float(text)
        raise ValueError("\u65e0\u6cd5\u89e3\u6790\u4e3a\u6570\u503c")

    @staticmethod
    def as_int(value: Any) -> int | None:
        number = ToolArgsModel.as_float(value)
        if number is None:
            return None
        return int(number)

    @staticmethod
    def normalize_choice(value: Any, mapping: dict[str, str], *, default: str | None = None) -> str | None:
        text = ToolArgsModel.as_str(value)
        if not text:
            return default
        return mapping.get(text.lower(), mapping.get(text, text))

    @staticmethod
    def validate_date(value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"\u65e5\u671f\u683c\u5f0f\u4e0d\u6b63\u786e\uff08\u9700\u8981 YYYY-MM-DD\uff09\uff1a{value!r}")
        try:
            datetime.date.fromisoformat(value)
        except ValueError as e:
            raise ValueError(f"\u65e5\u671f\u65e0\u6548\uff1a{value!r}") from e
        return value
