from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import ToolArgsModel


class UpdateMacroReportArgs(ToolArgsModel):
    content: str = Field(..., max_length=100000, description="本日生成的增量报告内容（Markdown）")
    summary: str = Field(..., max_length=2000, description="一句话摘要")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "content": ("markdown", "report", "body", "text"),
                "summary": ("abstract", "digest", "desc"),
            },
        )

    @field_validator("content", "summary", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("文本内容不能为空")
        return text
