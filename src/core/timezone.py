"""全局时区定义 —— 北京时间 (Asia/Shanghai, UTC+8)

使用固定偏移 timedelta(hours=8) 而非 zoneinfo.ZoneInfo，因为：
- 中国不实行夏令时，固定 UTC+8 始终正确
- timezone 对象可哈希，支持 lru_cache 等场景
- 避免 zoneinfo 在某些精简 Python 环境中不可用
"""

from datetime import timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
