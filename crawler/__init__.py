"""财经快讯爬虫模块 —— 聚合东方财富/新浪/富途/同花顺/财联社 5 个数据源。"""

from crawler.dedup import DedupStore, get_dedup_store
from crawler.semantic import SemanticDedupStore
from crawler.sources import (
    crawl_all,
    crawl_cls,
    crawl_em,
    crawl_futu,
    crawl_sina,
    crawl_ths,
)
from crawler.types import NewsItem

__all__ = [
    "NewsItem",
    "DedupStore",
    "SemanticDedupStore",
    "get_dedup_store",
    "crawl_em",
    "crawl_sina",
    "crawl_futu",
    "crawl_ths",
    "crawl_cls",
    "crawl_all",
]
