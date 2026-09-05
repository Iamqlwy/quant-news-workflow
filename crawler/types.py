"""统一的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    """一条财经快讯。"""

    title: str
    content: str = ""
    url: str = ""
    source: str = ""
    published_at: datetime | None = None
    level: str = ""  # A/B/C 等重要性分级，由具体数据源提供
    raw: dict = field(default_factory=dict, repr=False)
