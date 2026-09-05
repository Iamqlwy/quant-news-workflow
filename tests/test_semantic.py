"""语义去重测试 —— 覆盖文本模式和 embedding 模式。"""

from __future__ import annotations

from datetime import datetime, timedelta

from crawler.semantic import SemanticDedupStore
from crawler.types import NewsItem


def _make_item(title: str, content: str = "", source: str = "test", minutes_ago: int = 0) -> NewsItem:
    ts = datetime.now().astimezone() - timedelta(minutes=minutes_ago)
    return NewsItem(title=title, content=content, source=source, published_at=ts)


# ------------------------------------------------------------------
# 通用测试（两种模式共用）
# ------------------------------------------------------------------


def _run_common_tests(threshold: float, label: str):
    """每个测试独立 store 实例，避免跨测试污染。"""

    def t(name: str, fn):
        try:
            fn()
            print(f"  PASS  [{label}] {name}")
        except AssertionError as e:
            print(f"  FAIL  [{label}] {name}: {e}")
        except Exception as e:
            print(f"  ERROR [{label}] {name}: {e}")

    def test_empty_window():
        s = SemanticDedupStore(threshold=threshold, model_name=None)
        items = [
            _make_item("央行降准0.5个百分点", source="cls"),
            _make_item("特斯拉宣布全球涨价3%", source="em"),
            _make_item("现货黄金日内涨超1%", source="sina"),
        ]
        result = s.filter_new(items)
        assert len(result) == 3, f"空窗口应全部通过, got {len(result)}"

    def test_same_source_not_compared():
        s = SemanticDedupStore(threshold=threshold, model_name=None)
        s.filter_new([_make_item("xx公司发布年报 利润增长20%", source="em")])
        result = s.filter_new([_make_item("xx公司发布年报 利润增长20%", source="em")])
        assert len(result) == 1, f"同源不应比较, got {len(result)}"

    def test_cross_source_different_accepted():
        s = SemanticDedupStore(threshold=threshold, model_name=None)
        s.filter_new([_make_item("央行降准0.5个百分点", source="cls")])
        result = s.filter_new([_make_item("特斯拉宣布涨价3%", source="em")])
        assert len(result) == 1, f"不相关新闻应通过, got {len(result)}"

    def test_time_window_expiry():
        s = SemanticDedupStore(window_minutes=5, threshold=threshold, model_name=None)
        s.filter_new([_make_item("央行降准0.5个百分点", source="cls", minutes_ago=6)])
        assert s.stats()["active_entries"] == 0
        result = s.filter_new([_make_item("央行降准0.5个百分点", source="em", minutes_ago=0)])
        assert len(result) == 1, f"过期条目不参与比较, got {len(result)}"

    def test_no_published_at():
        s = SemanticDedupStore(threshold=threshold, model_name=None)
        item = NewsItem(title="突发新闻", content="", source="test")
        result = s.filter_new([item])
        assert len(result) == 1

    t("空窗口全部通过", test_empty_window)
    t("同源不比较", test_same_source_not_compared)
    t("不相关新闻通过", test_cross_source_different_accepted)
    t("时间窗口过期", test_time_window_expiry)
    t("无时间戳", test_no_published_at)


# ------------------------------------------------------------------
# 文本模式专用测试
# ------------------------------------------------------------------


def test_text_cross_source_identical_rejected():
    store = SemanticDedupStore(threshold=0.75, model_name=None)
    store.filter_new([_make_item("央行降准0.5个百分点", source="cls")])
    result = store.filter_new([_make_item("央行降准0.5个百分点", source="em")])
    assert len(result) == 0, "跨源相同文本应被过滤"


def test_text_length_aware():
    store = SemanticDedupStore(threshold=0.75, model_name=None)
    short1 = _make_item("港股午评", source="cls")
    short2 = _make_item("港股开盘", source="em")
    store.filter_new([short1])
    result = store.filter_new([short2])
    assert len(result) == 1, '短文本阈值更高, 港股午评 vs 港股开盘 应通过'


# ------------------------------------------------------------------
# Embedding 模式专用测试
# ------------------------------------------------------------------


def _get_embedding_store():
    return SemanticDedupStore(
        threshold=0.80,
        model_name="paraphrase-multilingual-MiniLM-L12-v2",
    )


def test_embedding_cross_source_identical_rejected():
    store = _get_embedding_store()
    store.filter_new([_make_item("央行降准0.5个百分点 释放长期资金约1万亿元", source="cls")])
    result = store.filter_new([_make_item("央行降准0.5个百分点 释放长期资金约1万亿元", source="em")])
    assert len(result) == 0, f"跨源相同文本应被过滤, got {len(result)}"


def test_embedding_paraphrase_rejected():
    store = _get_embedding_store()
    store.filter_new([_make_item("央行降准0.5个百分点 释放长期资金约1万亿元", source="cls")])
    result = store.filter_new([_make_item("央行宣布降准50bp 释放长期资金约1万亿元", source="em")])
    assert len(result) == 0, f"改写文本应被过滤, got {len(result)}"


def test_embedding_different_numbers_distinguished():
    """不同数字的相似标题应被区分（embedding 的核心优势）。"""
    store = _get_embedding_store()
    store.filter_new([_make_item("沪深两市成交额突破1万亿 较上一日此时缩量超800亿", source="em")])
    result = store.filter_new([_make_item("沪深两市成交额超2万亿元，较上日此时缩量2354亿元", source="sina")])
    # 1万亿 vs 2万亿 是不同的市场事件，不应被过滤
    assert len(result) == 1, f"1万亿 vs 2万亿 是不同事件, 应通过, got {len(result)}"


def test_embedding_mode_stats():
    store = _get_embedding_store()
    assert store.stats()["mode"] == "embedding"


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------


if __name__ == "__main__":
    print("=== 文本模式通用测试 ===")
    _run_common_tests(threshold=0.75, label="text")

    print("\n=== 文本模式专用 ===")
    for t in [test_text_cross_source_identical_rejected, test_text_length_aware]:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {t.__name__}: {e}")

    print("\n=== Embedding 模式通用测试 ===")
    try:
        _run_common_tests(threshold=0.80, label="emb")
    except Exception as e:
        print(f"  SKIP embedding tests: {e}")

    print("\n=== Embedding 模式专用 ===")
    for t in [
        test_embedding_cross_source_identical_rejected,
        test_embedding_paraphrase_rejected,
        test_embedding_different_numbers_distinguished,
        test_embedding_mode_stats,
    ]:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {t.__name__}: {e}")
