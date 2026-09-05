"""1m CSV 二进制偏移索引 —— 建索引 + 快速 seek 读取。

功能与原 market/indexer.py 一致：
- 为每个 1m CSV 建立日期→字节偏移索引
- 支持合并索引（all_1m.pkl）批量加载
- 通过 bisect 定位 + seek 读取避免全文件扫描
"""

from __future__ import annotations

import bisect
import pickle
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pandas as pd
from loguru import logger

from src.market.data.normalizer import normalize_1m_columns



_INDEX_DIR_NAME = "index_for_1m_read"
_MERGED_IDX_NAME = "all_1m.pkl"


def _idx_path(csv_path: Path, klines_root: Path | None = None) -> Path:
    """给定 1m CSV 路径，返回对应的 .idx 路径。"""
    if klines_root is not None:
        rel = csv_path.relative_to(klines_root / "1m")
        idx_dir = klines_root / _INDEX_DIR_NAME / "1m"
        idx_dir.mkdir(parents=True, exist_ok=True)
        return idx_dir / rel.with_suffix(".idx")
    return csv_path.with_suffix(".idx")


# ═══════════════════════════════════════════════════════════════════════════════
# 建索引
# ═══════════════════════════════════════════════════════════════════════════════


def build_1m_index(csv_path: Path) -> Path:
    """为单个 1m CSV 建立字节偏移索引。

    整个文件读入内存（~9MB），一次 split 获取所有行。
    索引写入 {klines_root}/index_for_1m_read/1m/{ticker}.idx
    """
    klines_root = csv_path.parent.parent
    idx_p = _idx_path(csv_path, klines_root)

    with open(csv_path, "rb") as f:
        header = f.readline()
        body = f.read()

    header_len = len(header)
    dates: list[str] = []
    offsets: list[int] = []
    last_date: str | None = None
    abs_offset = header_len

    newline = b"\r\n" if b"\r\n" in body else b"\n"
    newline_len = len(newline)
    lines = body.split(newline)
    has_trailing_newline = body.endswith(newline)

    for i, line in enumerate(lines):
        is_last = i == (len(lines) - 1)
        delim_len = newline_len if (has_trailing_newline or not is_last) else 0
        if not line:
            abs_offset += delim_len
            continue

        date_bytes = line[:10]
        if len(date_bytes) < 10:
            abs_offset += len(line) + delim_len
            continue

        try:
            date = date_bytes.decode("ascii")
        except UnicodeDecodeError:
            abs_offset += len(line) + delim_len
            continue

        if date != last_date:
            dates.append(date)
            offsets.append(abs_offset)
            last_date = date

        abs_offset += len(line) + delim_len

    obj = {
        "header": header,
        "dates": dates,
        "offsets": offsets,
        "eof": abs_offset,
        "time_col_idx": 0,
    }

    with open(idx_p, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)

    return idx_p


def build_all_1m_indexes(klines_root: Path, workers: int = 16) -> int:
    """并行建立所有 1m CSV 的索引，同时生成合并索引文件。"""
    m1_dir = klines_root / "1m"
    if not m1_dir.exists():
        return 0

    idx_dir = klines_root / _INDEX_DIR_NAME / "1m"
    idx_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in idx_dir.glob("*.idx")}

    pending: list[Path] = []
    for p in sorted(m1_dir.glob("*.csv")):
        if p.stem not in existing:
            pending.append(p)

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(build_1m_index, pending))

    _write_merged_idx(klines_root)
    return len(pending)


def _write_merged_idx(klines_root: Path) -> Path:
    """将所有独立 .idx 文件合并为一个 pickle 文件。"""
    idx_dir = klines_root / _INDEX_DIR_NAME / "1m"
    merged: dict[str, dict] = {}

    for idx_path in sorted(idx_dir.glob("*.idx")):
        with open(idx_path, "rb") as f:
            merged[idx_path.stem] = pickle.load(f)

    merged_path = klines_root / _INDEX_DIR_NAME / _MERGED_IDX_NAME
    with open(merged_path, "wb") as f:
        pickle.dump(merged, f, protocol=pickle.HIGHEST_PROTOCOL)

    return merged_path


def _load_merged_idx(klines_root: Path) -> dict[str, dict]:
    """加载合并索引文件。"""
    merged_path = klines_root / _INDEX_DIR_NAME / _MERGED_IDX_NAME
    if not merged_path.exists():
        return {}
    with open(merged_path, "rb") as f:
        return pickle.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
# 索引加载（单文件）
# ═══════════════════════════════════════════════════════════════════════════════


def _load_idx(klines_root: Path, ticker: str) -> dict | None:
    idx_p = klines_root / _INDEX_DIR_NAME / "1m" / f"{ticker}.idx"
    if not idx_p.exists():
        logger.debug("_load_idx: index not found for {}", ticker)
        return None
    with open(idx_p, "rb") as f:
        return pickle.load(f)


def load_1m_block_via_index(csv_path: Path, start_date: str, n_days: int) -> bytes | None:
    """通过索引读取 N 个交易日的原始 bytes（含 header）。

    若索引不存在或日期不在范围内则返回 None。
    """
    klines_root = csv_path.parent.parent
    ticker = csv_path.stem
    idx = _load_idx(klines_root, ticker)
    if idx is None:
        return None

    dates = idx["dates"]
    offsets = idx["offsets"]

    pos = bisect.bisect_left(dates, start_date)
    if pos >= len(dates):
        return None

    start_off = offsets[pos]
    end_pos = pos + n_days
    end_off = offsets[end_pos] if end_pos < len(offsets) else idx["eof"]

    with open(csv_path, "rb") as f:
        f.seek(start_off)
        block = f.read(end_off - start_off)

    return idx["header"] + block


# ═══════════════════════════════════════════════════════════════════════════════
# 批量加载
# ═══════════════════════════════════════════════════════════════════════════════


def load_1m_bulk(
    tickers: list[str],
    klines_root: Path,
    start_date: str,
    n_days: int = 2,
    workers: int = 32,
) -> tuple[bytes | None, bytes | None]:
    """并行读取多个 ticker 的 1m 数据，返回拼接后的 (header_bytes, data_bytes)。"""
    all_idx = _load_merged_idx(klines_root)
    if not all_idx:
        logger.warning("load_1m_bulk: merged index not found, build indexes first")
        return None, None

    first_idx = next((all_idx.get(t) for t in tickers if all_idx.get(t)), None)
    if first_idx is None:
        return None, None

    raw_header = first_idx["header"]
    if raw_header[:3] == b"\xef\xbb\xbf":
        raw_header = raw_header[3:]
    unified_header = b"symbol," + raw_header.rstrip(b"\r\n")

    def _read_one(tkr: str) -> bytes | None:
        idx = all_idx.get(tkr)
        if idx is None:
            return None

        dates = idx["dates"]
        offsets = idx["offsets"]
        pos = bisect.bisect_left(dates, start_date)
        if pos >= len(dates):
            return None

        start_off = offsets[pos]
        end_pos = pos + n_days
        end_off = offsets[end_pos] if end_pos < len(offsets) else idx["eof"]

        csv_p = klines_root / "1m" / f"{tkr}.csv"
        with open(csv_p, "rb") as f:
            f.seek(start_off)
            block = f.read(end_off - start_off)

        if not block:
            return None

        block = block.replace(b"\r\n", b"\n").replace(b"\r", b"\n").rstrip(b"\n")
        if not block:
            return None

        ticker_bytes = tkr.encode("ascii")
        prefixed = block.replace(b"\n", b"\n" + ticker_bytes + b",")
        prefixed = ticker_bytes + b"," + prefixed + b"\n"
        return prefixed

    with ThreadPoolExecutor(max_workers=workers) as ex:
        blocks = list(ex.map(_read_one, tickers))

    data_blocks = [b for b in blocks if b]
    if not data_blocks:
        return None, None

    return unified_header, b"".join(data_blocks)


def load_1m_bulk_df(
    tickers: list[str],
    klines_root: Path,
    start_date: str,
    n_days: int = 2,
    workers: int = 32,
) -> pd.DataFrame | None:
    """批量加载 1m 数据，返回归一化后的 DataFrame。

    列名: ticker, timestamp, open, high, low, close, volume, amount
    """
    header, data = load_1m_bulk(tickers, klines_root, start_date, n_days, workers)
    if header is None:
        return None

    raw = header + b"\n" + data

    _1M_DTYPE = {
        "symbol": str,
        "日期": str,
        "开盘": float,
        "最高": float,
        "最低": float,
        "收盘": float,
        "成交量(股)": str,
        "成交额(元)": str,
    }

    df = pd.read_csv(
        BytesIO(raw),
        dtype=_1M_DTYPE,
        parse_dates=["日期"],
        date_format="%Y-%m-%d %H:%M:%S",
        engine="c",
        low_memory=False,
    )

    if df.empty:
        return None

    df = df.rename(columns={"symbol": "ticker"})
    return normalize_1m_columns(df, keep_extra=["ticker"])
