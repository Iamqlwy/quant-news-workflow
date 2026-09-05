"""语义去重 —— 时间窗口 + embedding 余弦相似度（可回退到 difflib）。

规则：
1. 同源不比较 —— 精确去重已处理同源重复，语义层只做跨源去重。
2. 优先使用 embedding 模型（需 sentence-transformers + GPU），否则回退到 difflib。
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import numpy as np

_EMBEDDING_MODEL = None  # 全局单例，惰性加载


def _load_embedding_model(model_name: str):
    """惰性加载 sentence-transformers 模型，优先 GPU。"""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is not None:
        return _EMBEDDING_MODEL

    try:
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _EMBEDDING_MODEL = SentenceTransformer(
            model_name, device=device, local_files_only=True
        )
        _ = _EMBEDDING_MODEL.encode("warmup", normalize_embeddings=True)
        return _EMBEDDING_MODEL
    except Exception as exc:
        warnings.warn(f"无法加载 embedding 模型 ({model_name}): {exc}")
        return None


class SemanticDedupStore:
    """滑动时间窗口去重器，跨源检测同一事件的改写报道。

    当 ``model_name`` 指定且 sentence-transformers 可用时，
    使用 embedding 余弦相似度；否则回退到 difflib 文本相似度。

    Args:
        window_minutes: 只比较最近 N 分钟内的条目。
        threshold: 相似度阈值。embedding 模式默认 0.80，文本模式默认 0.75。
        model_name: sentence-transformers 模型名，如 ``"paraphrase-multilingual-MiniLM-L12-v2"``。
        max_text_len: 每条合并文本的最大字符数。
    """

    def __init__(
        self,
        *,
        window_minutes: int = 10,
        threshold: float | None = None,
        model_name: str | None = None,
        max_text_len: int = 500,
    ) -> None:
        self._window_minutes = window_minutes
        self._max_text_len = max_text_len

        self._model = None
        self._dim: int | None = None
        if model_name:
            self._model = _load_embedding_model(model_name)
            if self._model is not None:
                self._dim = self._model.get_sentence_embedding_dimension()

        self._use_embedding = self._model is not None
        default_threshold = 0.80 if self._use_embedding else 0.75
        self._threshold = threshold if threshold is not None else default_threshold

        # (ts, text, source, embedding_or_None)
        self._entries: list[tuple[datetime, str, str, np.ndarray | None]] = []

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def filter_new(self, items: list) -> list:
        """过滤 -- 只返回语义上不重复的条目。"""
        if not items:
            return []

        from crawler.types import NewsItem

        now = datetime.now().astimezone()
        self._purge_expired(now)

        texts = [self._item_text(it) for it in items]
        sources = [getattr(it, "source", "") or "" for it in items]

        if self._use_embedding:
            embeddings = self._embed_batch(texts)
        else:
            embeddings = [None] * len(items)

        kept: list[NewsItem] = []
        for i, item in enumerate(items):
            if self._is_duplicate(texts[i], sources[i], embeddings[i]):
                continue
            ts = item.published_at or now
            self._entries.append((ts, texts[i], sources[i], embeddings[i]))
            kept.append(item)

        return kept

    def reset(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        now = datetime.now().astimezone()
        active = sum(
            1 for ts, _, _, _ in self._entries
            if now - ts < timedelta(minutes=self._window_minutes)
        )
        return {
            "total_entries": len(self._entries),
            "active_entries": active,
            "window_minutes": self._window_minutes,
            "threshold": self._threshold,
            "mode": "embedding" if self._use_embedding else "text",
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _item_text(self, item) -> str:
        title = getattr(item, "title", "") or ""
        content = getattr(item, "content", "") or ""
        combined = f"{title} {content}".strip()
        if len(combined) > self._max_text_len:
            combined = combined[:self._max_text_len]
        return combined

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        """批量嵌入。返回 (N, dim) float32 数组。"""
        if self._model is None:
            raise RuntimeError("embedding 模型未加载")
        return self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    def _purge_expired(self, now: datetime) -> None:
        cutoff = (now - timedelta(minutes=self._window_minutes)).replace(tzinfo=None)
        self._entries = [
            e for e in self._entries if self._naive(e[0]) >= cutoff
        ]

    def _is_duplicate(
        self, text: str, source: str, embedding: np.ndarray | None
    ) -> bool:
        if not text:
            return False

        cutoff = (datetime.now().astimezone() - timedelta(minutes=self._window_minutes)).replace(tzinfo=None)

        for existing_ts, existing_text, existing_source, existing_emb in self._entries:
            if existing_source == source:
                continue
            if self._naive(existing_ts) < cutoff:
                continue

            if self._use_embedding and embedding is not None and existing_emb is not None:
                sim = float(np.dot(existing_emb, embedding))
            else:
                sim = SequenceMatcher(None, text, existing_text).ratio()
                effective = self._effective_threshold(len(text), len(existing_text))
                if sim < effective:
                    continue
                return True

            if sim >= self._threshold:
                return True
        return False

    @staticmethod
    def _cos_sim(a: np.ndarray, b: np.ndarray) -> float:
        a_norm = a / (np.linalg.norm(a) + 1e-10)
        b_norm = b / (np.linalg.norm(b) + 1e-10)
        return float(np.dot(a_norm, b_norm))

    def _effective_threshold(self, len_a: int, len_b: int) -> float:
        min_len = min(len_a, len_b)
        if min_len <= 20:
            return min(self._threshold + 0.15, 0.95)
        elif min_len <= 50:
            return self._threshold + 0.08
        elif min_len <= 100:
            return self._threshold + 0.03
        else:
            return self._threshold

    @staticmethod
    def _naive(ts: datetime) -> datetime:
        return ts.replace(tzinfo=None)
