from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import ToolArgsModel


class GetTradeArgs(ToolArgsModel):
    trade_ref: str = Field(..., max_length=100, description="交易引用（如 T1）")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"trade_ref": ("ref", "trade_id", "id")})

    @field_validator("trade_ref", mode="before")
    @classmethod
    def _coerce_trade_ref(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("trade_ref 不能为空")
        return text


class GetNodeHistoryArgs(ToolArgsModel):
    node_name: str = Field(..., max_length=200, description="WorldNode 名称（如'贵州茅台'）")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "node_name": ("name", "target_node_name", "stock_name", "sector_name"),
            },
        )

    @field_validator("node_name", mode="before")
    @classmethod
    def _coerce_node_name(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("node_name 不能为空")
        return text

