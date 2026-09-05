"""各数据源的爬虫实现。"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

import akshare as ak
import pandas as pd
import requests

from crawler.dedup import (
    DedupStore,
    _key_from_content,
    _key_from_id,
    _key_from_url,
    get_dedup_store,
)
from crawler.semantic import SemanticDedupStore
from crawler.types import NewsItem

# ---------------------------------------------------------------------------
# 通用 HTTP 工具
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
}

LOCAL_TZ = datetime.now().astimezone().tzinfo


def _get_json(url: str, *, headers: dict | None = None, max_retries: int = 5, timeout: int = 30) -> dict:
    """带重试的 JSON GET 请求。"""
    from src.config import settings
    h = headers or DEFAULT_HEADERS
    delay = 1
    max_retries = max_retries or settings.crawler_max_retries
    timeout = timeout or settings.crawler_request_timeout

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                print(f"[crawler] 请求超时，重试 {attempt + 1}/{max_retries}: {url}")
                time.sleep(delay)
                delay *= 2
            else:
                print(f"[crawler] 请求超时，已达最大重试次数: {url}")
                raise
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                print(f"[crawler] HTTP 客户端错误 {exc.response.status_code}: {url}")
                raise
            if attempt < max_retries - 1:
                print(f"[crawler] HTTP 服务器错误，重试 {attempt + 1}/{max_retries}: {url}")
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                print(f"[crawler] 网络异常，重试 {attempt + 1}/{max_retries}: {url}, {exc}")
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except Exception as exc:
            print(f"[crawler] 请求异常: {url}, {exc}")
            raise

    raise RuntimeError(f"请求失败（超出重试次数）: {url}")


# ---------------------------------------------------------------------------
# 东方财富 (East Money)
# ---------------------------------------------------------------------------


def crawl_em(*, dedup: DedupStore | None = None) -> list[NewsItem]:
    """东方财富-全球财经快讯，约 200 条。"""
    df = ak.stock_info_global_em()
    return _parse_em(df, dedup or get_dedup_store())


def _parse_em(df: pd.DataFrame, dedup: DedupStore) -> list[NewsItem]:
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        url = str(row.get("链接", ""))
        key = _key_from_url("em", url)
        if dedup.has("em", key):
            continue
        dedup.add("em", key)
        items.append(
            NewsItem(
                title=str(row.get("标题", "")),
                url=url,
                source="em",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 新浪财经
# ---------------------------------------------------------------------------


def crawl_sina(*, dedup: DedupStore | None = None) -> list[NewsItem]:
    """新浪财经-全球财经快讯，约 20 条。"""
    df = ak.stock_info_global_sina()
    return _parse_sina(df, dedup or get_dedup_store())


def _parse_sina(df: pd.DataFrame, dedup: DedupStore) -> list[NewsItem]:
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        content = str(row.get("内容", ""))
        key = _key_from_content("sina", content)
        if dedup.has("sina", key):
            continue
        dedup.add("sina", key)

        raw_time = str(row.get("时间", ""))
        published_at = None
        if raw_time:
            try:
                published_at = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=LOCAL_TZ
                )
            except ValueError:
                pass
        items.append(
            NewsItem(
                title=content,
                published_at=published_at,
                source="sina",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 富途牛牛
# ---------------------------------------------------------------------------


def crawl_futu(*, dedup: DedupStore | None = None) -> list[NewsItem]:
    """富途牛牛-快讯，约 50 条。"""
    df = ak.stock_info_global_futu()
    return _parse_futu(df, dedup or get_dedup_store())


def _parse_futu(df: pd.DataFrame, dedup: DedupStore) -> list[NewsItem]:
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        url = str(row.get("链接", ""))
        key = _key_from_url("futu", url)
        if dedup.has("futu", key):
            continue
        dedup.add("futu", key)
        items.append(
            NewsItem(
                title=str(row.get("标题", "")),
                url=url,
                source="futu",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 同花顺
# ---------------------------------------------------------------------------


def crawl_ths(*, dedup: DedupStore | None = None) -> list[NewsItem]:
    """同花顺财经-全球财经直播，约 20 条。"""
    df = ak.stock_info_global_ths()
    return _parse_ths(df, dedup or get_dedup_store())


def _parse_ths(df: pd.DataFrame, dedup: DedupStore) -> list[NewsItem]:
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        url = str(row.get("链接", ""))
        key = _key_from_url("ths", url)
        if dedup.has("ths", key):
            continue
        dedup.add("ths", key)
        items.append(
            NewsItem(
                title=str(row.get("标题", "")),
                url=url,
                source="ths",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 财联社 (CLS)  —— 已更新为新接口
# ---------------------------------------------------------------------------

CLS_API_URL = (
    "https://www.cls.cn/api/cache"
    "?app=CailianpressWeb&name=telegraph&os=web&sv=8.7.9"
)


def crawl_cls(symbol: str = "全部", *, dedup: DedupStore | None = None) -> list[NewsItem]:
    """财联社-电报。

    Args:
        symbol: ``"全部"`` 返回所有快讯，``"重点"`` 只返回 A/B 级别。
    """
    data = _get_json(CLS_API_URL)
    roll_data: list[dict] = data.get("data", {}).get("roll_data", [])
    return _parse_cls(roll_data, symbol, dedup or get_dedup_store())


def _parse_cls(roll_data: list[dict], symbol: str, dedup: DedupStore) -> list[NewsItem]:
    items: list[NewsItem] = []
    for r in roll_data:
        level = str(r.get("level", "C"))
        if symbol == "重点" and level not in ("A", "B"):
            continue

        item_id = str(r.get("id", ""))
        key = _key_from_id("cls", item_id)
        if dedup.has("cls", key):
            continue
        dedup.add("cls", key)

        title = str(r.get("title", ""))
        content = str(r.get("content", "") or r.get("brief", ""))
        if not title and content:
            title = content[:80] + ("…" if len(content) > 80 else "")

        ctime = r.get("ctime", 0)
        published_at = (
            datetime.fromtimestamp(int(ctime), tz=LOCAL_TZ) if ctime else None
        )

        items.append(
            NewsItem(
                title=title,
                content=content,
                url=f"https://www.cls.cn/telegraph?detail_id={item_id}",
                published_at=published_at,
                level=level,
                source="cls",
                raw=r,
            )
        )
    return items


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

CRAWLERS: dict[str, Callable[..., list[NewsItem]]] = {
    "em": crawl_em,
    "sina": crawl_sina,
    "futu": crawl_futu,
    "ths": crawl_ths,
    "cls": crawl_cls,
}


def _crawl_one(
    name: str,
    crawler: Callable[..., list[NewsItem]],
    dedup: DedupStore,
    sleep_range: tuple[float, float],
) -> tuple[str, list[NewsItem], str | None]:
    """爬取单个源，返回 (name, items, error_or_None)。"""
    delay = random.uniform(*sleep_range)
    time.sleep(delay)
    try:
        items = crawler(dedup=dedup)
        return name, items, None
    except Exception as exc:
        return name, [], str(exc)


def crawl_all(
    sources: tuple[str, ...] | None = None,
    *,
    dedup: DedupStore | None = None,
    semantic_dedup: "SemanticDedupStore | None" = None,
    concurrent: bool = False,
    sleep_range: tuple[float, float] = (0.3, 1.0),
) -> dict[str, list[NewsItem]]:
    """聚合爬取多个数据源。

    Args:
        sources: 指定数据源名称，``None`` 表示全部。
        semantic_dedup: 可选的语义去重器，对跨源改写做第二轮去重。
        concurrent: 是否并发爬取所有源。
        sleep_range: 每个源爬取前的随机睡眠范围 (min, max) 秒。
    """
    d = dedup or get_dedup_store()
    names = sources or tuple(CRAWLERS)
    result: dict[str, list[NewsItem]] = {}

    if concurrent:
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            futures = {
                pool.submit(_crawl_one, name, CRAWLERS[name], d, sleep_range): name
                for name in names
            }
            for future in as_completed(futures):
                name, items, err = future.result()
                if err:
                    print(f"[crawler] {name} 爬取失败: {err}")
                result[name] = items
    else:
        for name in names:
            _, items, err = _crawl_one(name, CRAWLERS[name], d, sleep_range)
            if err:
                print(f"[crawler] {name} 爬取失败: {err}")
            result[name] = items

    if semantic_dedup is not None:
        all_items = [item for items in result.values() for item in items]
        novel = semantic_dedup.filter_new(all_items)
        regrouped: dict[str, list[NewsItem]] = {name: [] for name in names}
        for item in novel:
            regrouped[item.source].append(item)
        result = regrouped

    return result
