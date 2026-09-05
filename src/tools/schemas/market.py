from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import ToolArgsModel


class GetTechnicalChartArgs(ToolArgsModel):
    stock_name: str = Field(..., max_length=200, description="股票名称，如'贵州茅台'、'平安银行'等，禁止传入代码")
    from_date: str | None = Field(default=None, max_length=20, description="开始日期 YYYY-MM-DD。建议不填（默认取最近240个交易日，技术指标更准确）；若填则范围至少覆盖20个交易日")
    to_date: str | None = Field(default=None, max_length=20, description="结束日期 YYYY-MM-DD。建议不填（默认到最新）；若填则范围至少覆盖100个交易日")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "stock_name": ("ticker", "symbol", "stock", "name"),
                "from_date": ("start_date", "begin_date", "from", "date_from"),
                "to_date": ("end_date", "to", "date_to"),
            },
        )

    @field_validator("stock_name", mode="before")
    @classmethod
    def _coerce_stock_name(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("stock_name 不能为空")
        return text

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def _coerce_optional_date(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("from_date", "to_date", mode="after")
    @classmethod
    def _validate_optional_date(cls, v: str | None) -> str | None:
        if v is not None:
            return cls.validate_date(v)
        return v


class GetMarketSnapshotArgs(ToolArgsModel):
    date: str = Field(..., max_length=20, description="日期 YYYY-MM-DD。仅支持2026年以后的输入。")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(data, {"date": ("day", "dt", "trade_date")})

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("date 不能为空")
        return text

    @field_validator("date", mode="after")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        return cls.validate_date(v)


class GetPriceChartArgs(ToolArgsModel):
    stock_name: str = Field(..., max_length=200, description="股票名称，如'贵州茅台'、'平安银行'等，禁止传入代码")
    from_date: str | None = Field(default=None, max_length=20, description="起始日期 YYYY-MM-DD。建议不填（默认取最近240个交易日）；若填则范围至少覆盖100个交易日以看清趋势")
    to_date: str | None = Field(default=None, max_length=20, description="截止日期 YYYY-MM-DD。建议不填（默认到最新）；若填则范围至少覆盖100个交易日")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "stock_name": ("ticker", "symbol", "stock", "name"),
                "from_date": ("start_date", "begin_date", "from", "date_from"),
                "to_date": ("end_date", "to", "date_to"),
            },
        )

    @field_validator("stock_name", mode="before")
    @classmethod
    def _coerce_stock_name(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("stock_name 不能为空")
        return text

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def _coerce_optional_date(cls, v: Any) -> str | None:
        return cls.as_optional_str(v)

    @field_validator("from_date", "to_date", mode="after")
    @classmethod
    def _validate_optional_date(cls, v: str | None) -> str | None:
        if v is not None:
            return cls.validate_date(v)
        return v

class GetSectorSnapshotChartArgs(ToolArgsModel):
    sector: str = Field(..., max_length=200, description="板块代码或名称，代码格式如 885311.TI")
    date: str = Field(..., max_length=10, description="日期 YYYY-MM-DD")

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        return cls.rename_aliases(
            data,
            {
                "sector": ("sector_name", "name", "ticker", "symbol"),
                "date": ("day", "dt", "trade_date"),
            },
        )

    @field_validator("sector", "date", mode="before")
    @classmethod
    def _coerce_required_text(cls, v: Any) -> str:
        text = cls.as_str(v)
        if not text:
            raise ValueError("必填参数不能为空")
        return text

    @field_validator("date", mode="after")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        return cls.validate_date(v)


class GetMultiIndexChartArgs(ToolArgsModel):
    @model_validator(mode="before")
    @classmethod
    def _ignore_payload(cls, data: Any) -> Any:
        return {} if data is None else data
