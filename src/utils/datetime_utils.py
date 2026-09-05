"""统一的时间类型转换工具。"""

from __future__ import annotations

from datetime import date, datetime, time

from src.core.timezone import BEIJING_TZ


def ensure_beijing_datetime(
    value: datetime | date | str | None,
    *,
    field_name: str = "datetime",
) -> datetime | None:
    """将外部时间值统一转换为北京时间 aware datetime。

    支持:
    - None
    - datetime
    - date（补零点）
    - ISO 8601 字符串
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min, tzinfo=BEIJING_TZ)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} 不是有效的 ISO 时间字符串: {value}") from exc
    else:
        raise TypeError(f"{field_name} 期望 datetime/date/str/None，实际为 {type(value).__name__}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)
