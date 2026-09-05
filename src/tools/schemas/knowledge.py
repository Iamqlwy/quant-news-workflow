from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator

from .base import ToolArgsModel


class SearchKBArgs(ToolArgsModel):
    query_text: str = Field(
        ...,
        max_length=2000,
        description="搜索文本",
        validation_alias=AliasChoices("query_text", "query_string", "query_test", "query", "keyword", "keywords", "text"),
    )
    limit: int = Field(default=10, description="返回条数（最大 20）")
    only_tables: list[str] | None = Field(
        default=None,
        description="限定搜索的表名列表。明确知道要找什么类型时尽量传入以提高精度：查历史资讯用 ['raw_information']，查分析报告用 ['analyses']，查复盘反馈用 ['feedbacks']，查实体节点用 ['nodes']。不确定类型时才不传。",
    )

    @field_validator("query_text", mode="before")
    @classmethod
    def _coerce_query_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("搜索关键词不能为空")
        return text

    @field_validator("limit", mode="before")
    @classmethod
    def _coerce_limit(cls, v: Any) -> int:
        limit = cls.as_int(v)
        if limit is None or limit <= 0:
            return 10
        return min(limit, 20)

    @field_validator("only_tables", mode="before")
    @classmethod
    def _coerce_only_tables(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        items = cls.as_list(v)
        if items is None:
            return None
        result = [cls.as_str(item) for item in items if cls.as_str(item)]
        return result if result else None


class ReadArgs(ToolArgsModel):
    refs: str = Field(..., max_length=5000, description="逗号分隔的会话引用，如 'R1,A2,F3'。从 search_kb 返回的 items[].ref 获取")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"refs": ("ref", "ids", "items", "references")})

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, v: Any) -> str:
        items = cls.as_list(v)
        text = ",".join(cls.as_str(item) for item in items if cls.as_str(item)) if items else cls.as_str(v)
        if not text:
            raise ValueError("refs 不能为空")
        return text


class GetPreferencesArgs(ToolArgsModel):
    sector: str | None = Field(default=None, max_length=200, description="行业/板块名称。不填则返回市场整体偏好认知")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"sector": ("industry", "sector_name", "name")})

    @field_validator("sector", mode="before")
    @classmethod
    def _coerce_sector(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)
