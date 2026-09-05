"""去重存储 —— 记录已爬取过的条目，防止重复返回。"""

from __future__ import annotations

import hashlib
from collections import OrderedDict


class DedupStore:
    """基于 LRU 的已见 ID 集合。

    Args:
        max_size: 每个数据源最多保留的 ID 数，超出时淘汰最旧的记录。
    """

    def __init__(self, max_size: int = 10000) -> None:
        self._max_size = max_size
        self._stores: dict[str, OrderedDict[str, bool]] = {}

    def _ensure_source(self, source: str) -> OrderedDict[str, bool]:
        if source not in self._stores:
            self._stores[source] = OrderedDict()
        return self._stores[source]

    def has(self, source: str, key: str) -> bool:
        return key in self._ensure_source(source)

    def add(self, source: str, key: str) -> None:
        store = self._ensure_source(source)
        if key in store:
            store.move_to_end(key)
        else:
            store[key] = True
            while len(store) > self._max_size:
                store.popitem(last=False)

    def filter_new(self, source: str, keys: list[str]) -> list[str]:
        """返回 keys 中尚未出现过的 key 列表，并将它们全部标记为已见。"""
        new: list[str] = []
        for k in keys:
            if not self.has(source, k):
                new.append(k)
                self.add(source, k)
        return new

    def reset(self, source: str | None = None) -> None:
        """清空去重记录。

        Args:
            source: 指定数据源，``None`` 表示清空全部。
        """
        if source is None:
            self._stores.clear()
        else:
            self._stores.pop(source, None)

    def stats(self) -> dict[str, int]:
        return {s: len(v) for s, v in self._stores.items()}


# ---------------------------------------------------------------------------
# 默认全局实例
# ---------------------------------------------------------------------------

_dedup_store: DedupStore | None = None


def get_dedup_store() -> DedupStore:
    global _dedup_store
    if _dedup_store is None:
        _dedup_store = DedupStore()
    return _dedup_store


# ---------------------------------------------------------------------------
# 各数据源的 key 生成
# ---------------------------------------------------------------------------


def _key_from_url(source: str, url: str) -> str:
    """从 URL 生成去重 key（用于 em/futu/ths/em_breakfast）。"""
    if not url:
        return ""
    return f"{source}:{url}"


def _key_from_content(source: str, content: str) -> str:
    """对纯文本内容取 md5 前 16 位做 key（用于 sina）。"""
    if not content:
        return ""
    digest = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def _key_from_id(source: str, item_id: str) -> str:
    """从原始 ID 生成去重 key（用于 cls）。"""
    if not item_id:
        return ""
    return f"{source}:{item_id}"
