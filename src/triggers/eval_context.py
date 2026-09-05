"""单次评估周期的数据快照容器 —— 评估器从此读取预取数据，不做 IO"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EvalContext:
    """每轮 trigger 评估开始前一次性预取全部数据，存于此容器。

    所有评估器改为同步函数，直接从 ctx 做 dict 读取，
    不再逐 atom 调用 MarketDataProvider 的 async 方法。
    """

    now: datetime
    ticker_data: dict[str, dict] = field(default_factory=dict)
    sector_data: dict[str, dict] = field(default_factory=dict)
    market_summary: dict = field(default_factory=dict)
