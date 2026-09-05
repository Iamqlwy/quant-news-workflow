"""market_new —— 重构后的行情数据模块。

统一入口：MarketDataProvider

架构：
    provider.py (门面)
      ├─ services/* (领域服务)
      │    ├─ data/cache.py (缓存)
      │    └─ compute/* (纯计算函数)
      ├─ data/loader.py (CSV I/O)
      └─ live/xt_provider.py (实时连接)
"""

from __future__ import annotations

from src.market.provider import MarketDataProvider

__all__ = ["MarketDataProvider"]
