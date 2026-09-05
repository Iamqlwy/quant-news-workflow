"""图表生成 —— 用 matplotlib 生成价格走势和技术指标图表，返回 base64 PNG"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes

from src.market.compute.indicators import (
    calc_bollinger as calc_bollinger_series,
)
from src.market.compute.indicators import (
    calc_ma as calc_ma_series,
)
from src.market.compute.indicators import (
    calc_macd as calc_macd_series,
)
from src.market.compute.indicators import (
    calc_rsi as calc_rsi_series,
)
from src.market.data.normalizer import compact_to_ymd as _compact_to_ymd
from src.market.provider import MarketDataProvider

# ── 中文字体 ──
_chinese_font = None
for _name in ["SimHei", "Microsoft YaHei", "Noto Sans SC", "STXihei"]:
    for _f in fm.fontManager.ttflist:
        if _f.name == _name:
            _chinese_font = _f
            break
    if _chinese_font:
        break
if _chinese_font:
    plt.rcParams["font.family"] = _chinese_font.name
plt.rcParams["axes.unicode_minus"] = False

# ── 配色 (A股：红涨绿跌) ──
BG_DARK = "#0E1117"
PANEL_BG = "#161B22"
GRID_COLOR = "#21262D"
UP_COLOR = "#EF5350"  # 红涨
DOWN_COLOR = "#26A69A"  # 绿跌
WHITE = "#E6EDF3"
DIM = "#8B949E"
MA_COLORS = {
    5: "#F0E442",
    10: "#FFB347",
    20: "#56B4E9",
    60: "#CC79F7",
}

DISPLAY_BARS = 240
TECH_DISPLAY_BARS = 240

SSE_INDEX_CODE = "000001.SH"
SZSE_INDEX_CODE = "399001.SZ"


# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════


def to_data_uri(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _title(ticker: str, stock_name: str = "", suffix: str = "") -> str:
    if stock_name:
        return f"{stock_name}({ticker})  {suffix}".strip()
    return f"{ticker}  {suffix}".strip()


def _to_floats(items: list, field: str) -> list[float]:
    result = []
    for item in items:
        v = item.get(field, 0) if isinstance(item, dict) else getattr(item, field, 0)
        result.append(float(v))
    return result


def _to_dates(items: list) -> list[str]:
    result = []
    for item in items:
        d = (
            item.get("date", item.get("timestamp", ""))
            if isinstance(item, dict)
            else getattr(item, "date", getattr(item, "timestamp", ""))
        )
        result.append(str(d))
    return result


def _get_val(items: list, idx: int, field: str, default: float = 0.0) -> float:
    """安全取值，越界返回默认值"""
    if not items:
        return default
    if idx < 0:
        idx = len(items) + idx
    if idx < 0 or idx >= len(items):
        return default
    item = items[idx]
    v = item.get(field, default) if isinstance(item, dict) else getattr(item, field, default)
    if v is None:
        return default
    try:
        fv = float(v)
    except Exception:
        return default
    if np.isnan(fv):
        return default
    return fv


# ═══════════════════════════════════════════════
# 共享工具函数
# ═══════════════════════════════════════════════


def _build_trading_minute_axis() -> list[str]:
    """生成 09:30-11:30, 13:00-15:00 的分钟轴标签列表"""
    times: list[str] = []
    for h in range(9, 12):
        for m in range(60):
            if h == 9 and m < 30:
                continue
            if h == 11 and m > 30:
                continue
            times.append(f"{h:02d}:{m:02d}")
    for h in range(13, 16):
        for m in range(60):
            if h == 15 and m > 0:
                continue
            times.append(f"{h:02d}:{m:02d}")
    return times


def _clock_now_minutes(market: MarketDataProvider) -> int | None:
    clock = market.clock
    if clock is None:
        return None
    try:
        now = clock.now()
        return now.hour * 60 + now.minute
    except Exception:
        return None


def _is_today_for_clock(market: MarketDataProvider, date_compact: str) -> bool:
    clock = market.clock
    if clock is None:
        return False
    try:
        return clock.today().strftime("%Y%m%d") == date_compact
    except Exception:
        return False


def _plot_intraday_line(ax: Axes, axis_index: dict, series: tuple | None, color: str, label: str, linestyle: str = "-") -> None:
    """绘制 2-元组 (times, pct) 的日内分时线"""
    if series is None:
        return
    times, pct = series
    xs, ys = [], []
    for t, y in zip(times, pct, strict=False):
        idx = axis_index.get(t)
        if idx is None:
            continue
        xs.append(idx)
        ys.append(float(y))
    if xs:
        ax.plot(np.array(xs), np.array(ys), color=color, linewidth=1.5, label=label, linestyle=linestyle)


def _plot_intraday_stock(ax: Axes, axis_index: dict, series: tuple | None, color: str, label: str, linestyle: str = "-") -> None:
    """绘制 3-元组 (times, pct, vwap_pct) 的股票分时线 + 均价线"""
    if series is None:
        return
    times, pct, vwap_pct = series
    xs, ys, vwaps = [], [], []
    for t, y, vw in zip(times, pct, vwap_pct, strict=False):
        idx = axis_index.get(t)
        if idx is None:
            continue
        xs.append(idx)
        ys.append(float(y))
        vwaps.append(float(vw) if not np.isnan(vw) else np.nan)

    if xs:
        ax.plot(np.array(xs), np.array(ys), color=color, linewidth=1.5, label=label, linestyle=linestyle)
    if vwap_pct is not None:
        vwaps_arr = np.array(vwaps)
        valid = ~np.isnan(vwaps_arr)
        if valid.any():
            ax.plot(np.array(xs)[valid], vwaps_arr[valid], color="#F0E442", linewidth=1.2, label="均价", alpha=0.95)


# ═══════════════════════════════════════════════
# 价格图表 数据加载辅助函数
# ═══════════════════════════════════════════════


def _pick_intraday_date(market: MarketDataProvider, to_compact: str | None, display_bars_list: list[dict]) -> str | None:
    """选择一个可用的日内分时日期"""
    clock = market.clock
    if to_compact:
        if clock is not None:
            try:
                today_key = clock.today().strftime("%Y%m%d")
                return min(to_compact, today_key)
            except Exception:
                pass
        return to_compact
    if clock is not None:
        try:
            return clock.today().strftime("%Y%m%d")
        except Exception:
            pass
    last = display_bars_list[-1].get("timestamp", "") if display_bars_list else ""
    last = str(last).replace("-", "")
    return last if last and len(last) == 8 else None


def _prev_close_for_stock(all_bars: list[dict], date_key: str) -> float | None:
    """在日线列表中找到 date_key 之前最近一个交易日的收盘价"""
    for b in reversed(all_bars):
        d = str(b.get("timestamp", "")).replace("-", "")
        if len(d) == 8 and d < date_key:
            v = b.get("close")
            if v is not None and pd.notna(v):
                return float(v)
    return None


def _load_intraday_series(market: MarketDataProvider, code: str, date_key: str, all_bars: list[dict]) -> tuple[str, tuple | None, tuple | None]:
    """加载个股日内分时涨跌幅序列 + VWAP + 成交量柱状图数据。

    返回 (used_date, (times, pct, vwap_pct), (vol_times, vol_values, vol_colors))
    """
    klines_root = market.klines_path
    clock = market.clock
    safe_date_key = date_key
    if clock is not None:
        try:
            today_key = clock.today().strftime("%Y%m%d")
            safe_date_key = min(safe_date_key, today_key)
        except Exception:
            pass

    df = market.get_bars(code, granularity="1m")
    if (df is None or df.empty or "timestamp" not in df.columns) and klines_root is not None:
        try:
            from src.market import loader as _loader

            root = Path(klines_root)
            df = _loader.load_stock_1m(code, root, today=safe_date_key)
        except Exception:
            df = None
    if df is None or df.empty or "timestamp" not in df.columns:
        return safe_date_key, None, None

    ts = df["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, errors="coerce")
    df = df.assign(_ts=ts).dropna(subset=["_ts"]).copy()
    if df.empty:
        return safe_date_key, None, None

    used_date = safe_date_key
    sub = df[df["_ts"].dt.strftime("%Y%m%d") == used_date].copy()
    if sub.empty:
        avail_days = df["_ts"].dt.strftime("%Y%m%d")
        candidates = avail_days[avail_days <= used_date]
        if not candidates.empty:
            used_date = candidates.max()
            sub = df[avail_days == used_date].copy()
    if sub.empty:
        return used_date, None, None

    if clock is not None:
        try:
            if used_date == clock.today().strftime("%Y%m%d"):
                sub = sub.drop(columns=["_ts"], errors="ignore")
                if sub is None or sub.empty:
                    return used_date, None, None
                ts = sub["timestamp"]
                if not pd.api.types.is_datetime64_any_dtype(ts):
                    ts = pd.to_datetime(ts, errors="coerce")
                sub = sub.assign(_ts=ts).dropna(subset=["_ts"]).copy()
                if sub.empty:
                    return used_date, None, None
        except Exception:
            pass

    sub = sub.sort_values("_ts").reset_index(drop=True)
    close_s = pd.to_numeric(sub.get("close"), errors="coerce") if "close" in sub.columns else None
    if close_s is None:
        return used_date, None, None
    close_s = close_s.astype(float)
    mask = close_s.notna()
    if not mask.any():
        return used_date, None, None

    prev_close = _prev_close_for_stock(all_bars, used_date)
    base = prev_close if prev_close and prev_close > 0 else float(close_s[mask].iloc[0])
    if not base:
        return used_date, None, None

    closes = close_s[mask].to_numpy()
    pct = (closes / base - 1.0) * 100.0
    times = sub.loc[mask, "_ts"].dt.strftime("%H:%M").tolist()

    vwap_pct = np.full_like(pct, np.nan, dtype=float)
    vol_s = pd.to_numeric(sub.get("volume"), errors="coerce") if "volume" in sub.columns else None
    amt_s = pd.to_numeric(sub.get("amount"), errors="coerce") if "amount" in sub.columns else None
    if vol_s is not None and amt_s is not None:
        vol_v = vol_s.astype(float).to_numpy()
        amt_v = amt_s.astype(float).to_numpy()
        vol_m = np.isfinite(vol_v)
        amt_m = np.isfinite(amt_v)
        if vol_m.any() and amt_m.any():
            last_close = float(closes[-1]) if closes.size else 0.0
            total_vol = float(np.nansum(vol_v))
            total_amt = float(np.nansum(amt_v))
            volume_in_hands = False
            if total_vol > 0 and total_amt > 0 and last_close > 0:
                implied_price = total_amt / total_vol
                if implied_price > last_close * 20:
                    volume_in_hands = True
            shares_v = vol_v * (100.0 if volume_in_hands else 1.0)
            shares_v = np.where(np.isfinite(shares_v), shares_v, 0.0)
            amt_v = np.where(np.isfinite(amt_v), amt_v, 0.0)
            shares_cum = np.cumsum(shares_v)
            amt_cum = np.cumsum(amt_v)
            with np.errstate(divide="ignore", invalid="ignore"):
                vwap_price = np.where(shares_cum > 0, amt_cum / shares_cum, np.nan)
            vwap_all = (vwap_price / base - 1.0) * 100.0
            if len(vwap_all) == len(close_s):
                vwap_pct = vwap_all[mask.to_numpy()]

    open_s = pd.to_numeric(sub.get("open"), errors="coerce") if "open" in sub.columns else None
    if vol_s is None or open_s is None:
        return used_date, (times, pct, vwap_pct), None
    vol = vol_s.astype(float)
    open_s = open_s.astype(float)
    color_list: list[str] = []
    for o, c in zip(open_s.tolist(), close_s.tolist(), strict=False):
        if pd.isna(o) or pd.isna(c):
            color_list.append(DIM)
        else:
            color_list.append(UP_COLOR if float(c) >= float(o) else DOWN_COLOR)
    return used_date, (times, pct, vwap_pct), (sub["_ts"].dt.strftime("%H:%M").tolist(), vol.to_numpy(), color_list)


# ═══════════════════════════════════════════════
# 指数 / 板块 分时涨跌幅
# ═══════════════════════════════════════════════


def _load_intraday_pct_for_index(market: MarketDataProvider, code: str, date_compact: str) -> tuple[list[str], np.ndarray] | None:
    """从 get_bars 加载指数日内分时涨跌幅序列"""
    df = market.get_bars(code, granularity="1m")
    if df is None or df.empty:
        return None
    ts = df["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts)
    day_mask = ts.dt.strftime("%Y%m%d") == date_compact
    sub = df.loc[day_mask].copy()
    if sub.empty:
        return None
    sub = sub.sort_values("timestamp")
    close_series = pd.to_numeric(sub["close"], errors="coerce")
    ts_series = sub["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(ts_series):
        ts_series = pd.to_datetime(ts_series, errors="coerce")
    mask = close_series.notna() & ts_series.notna()
    if not mask.any():
        return None
    closes = close_series[mask].astype(float).to_numpy()
    if closes.size < 2:
        return None

    # 前日收盘价：从 get_bars 日线中取
    prev_close = None
    df_daily = market.get_bars(code, granularity="1d", start="20200101")
    if df_daily is not None and not df_daily.empty and "timestamp" in df_daily.columns and "close" in df_daily.columns:
        tsd = df_daily["timestamp"].astype(str)
        prev = df_daily.loc[tsd < date_compact].sort_values("timestamp").tail(1)
        if not prev.empty:
            v = prev.iloc[0]["close"]
            if pd.notna(v):
                prev_close = float(v)

    base = prev_close if prev_close and prev_close > 0 else (closes[0] if closes[0] else closes[1])
    if not base:
        return None
    pct = (closes / base - 1.0) * 100.0
    times = pd.to_datetime(ts_series[mask]).dt.strftime("%H:%M").tolist()
    return times, pct


def _load_sector_intraday_pct(market: MarketDataProvider, code: str, date_compact: str) -> tuple[list[str], np.ndarray] | None:
    """从板块 1m 数据中提取板块日内分时涨跌幅序列"""
    df = market.get_bars(code, granularity="1m")
    if df is None or df.empty:
        return None

    concept_yesterday_close = market._cache.session.adhoc.get("_concept_yesterday_close", {})
    clean_code = code.replace(".TI", "")
    base = concept_yesterday_close.get(clean_code)
    if not base or base <= 0:
        return None

    df = df.sort_values("timestamp").reset_index(drop=True)
    close_series = pd.to_numeric(df["close"], errors="coerce")
    ts_series = df["timestamp"]
    mask = close_series.notna() & ts_series.notna()
    if not mask.any():
        return None
    closes = close_series[mask].astype(float).to_numpy()
    if closes.size < 2:
        return None

    pct = (closes / base - 1.0) * 100.0
    times = pd.to_datetime(ts_series[mask]).dt.strftime("%H:%M").tolist()
    return times, pct


# ═══════════════════════════════════════════════
# 完整数据加载（仅股票）
# ═══════════════════════════════════════════════


def _load_full_bars(market: MarketDataProvider, ticker: str) -> list[dict]:
    """加载完整日线数据（从 2020-01-01 起，确保 MA60/MACD 等技术指标有足够历史）。"""
    df = market.get_bars(ticker, "1d", start="20200101")
    if df is None or df.empty:
        return []
    df = df.copy()
    df["timestamp"] = df["timestamp"].dt.strftime("%Y%m%d")
    return df.to_dict("records")


# ═══════════════════════════════════════════════
# 共享数据提取与绘制
# ═══════════════════════════════════════════════


def _draw_candlestick(ax: Axes, x: np.ndarray, closes: np.ndarray, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, width: float = 0.65, alpha: float = 0.95, linew: float = 0.6) -> list[str]:
    """在指定轴上绘制 OHLC 蜡烛图，返回涨跌颜色列表"""
    is_up = np.array(closes) >= np.array(opens)
    colors_bar = [UP_COLOR if u else DOWN_COLOR for u in is_up]

    ax.vlines(x, lows, highs, colors=colors_bar, linewidths=linew, alpha=alpha * 0.85)
    body_heights = abs(np.array(closes) - np.array(opens))
    body_bottoms = np.minimum(opens, closes)
    ax.bar(x, body_heights, bottom=body_bottoms, width=width, color=colors_bar, alpha=alpha, edgecolor=None)
    return colors_bar


# ═══════════════════════════════════════════════
# 信息栏
# ══════════════════════════════════════════════


def _render_info_bar(ax: Axes, bars: list[dict]) -> None:
    """在第一个子图上方绘制行情信息栏：实时价格、涨跌幅、换手率、流通市值、总市值、PE。

    取最后一根 bar 的收盘价作为实时价格，与前一 bar 的收盘价比较计算涨跌幅。
    """
    if not bars:
        return

    last_close = _get_val(bars, -1, "close")
    prev_close = _get_val(bars, -2, "close") if len(bars) >= 2 else last_close
    chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
    chg_sign = "+" if chg_pct >= 0 else ""

    def _mv_to_yi(v: float) -> float:
        return v / 10000

    def _amount_to_yi(v: float) -> float:
        return v / 1e4

    parts = [
        f"价格 {last_close:.2f}",
        f"涨跌 {chg_sign}{chg_pct:.2f}%",
    ]

    turnover = _get_val(bars, -1, "turnover_rate")
    if turnover:
        parts.append(f"换手 {turnover:.2f}%")

    circ_mv = _get_val(bars, -1, "circ_mv")
    if circ_mv:
        parts.append(f"流通 {_mv_to_yi(circ_mv):.2f}亿")

    total_mv = _get_val(bars, -1, "total_mv")
    if total_mv:
        parts.append(f"总市值 {_mv_to_yi(total_mv):.2f}亿")

    pe = _get_val(bars, -1, "pe")
    if pe:
        parts.append(f"PE {pe:.1f}")

    amount = _get_val(bars, -1, "amount")
    if amount:
        parts.append(f"金额 {_amount_to_yi(amount):.2f}亿")

    if len(parts) >= 7:
        mid = (len(parts) + 1) // 2
        info = "  │  ".join(parts[:mid]) + "\n" + "  │  ".join(parts[mid:])
    else:
        info = "  │  ".join(parts)

    ax.text(
        0.5,
        0.88,
        info,
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=10,
        color=WHITE,
        bbox={"boxstyle": "round,pad=0.3", "facecolor": PANEL_BG, "edgecolor": GRID_COLOR, "alpha": 0.92},
    )


# ═══════════════════════════════════════════════
# x 轴日期
# ═══════════════════════════════════════════════


def _set_date_ticks(ax: Axes, x: np.ndarray, dates: list[str], n: int) -> None:
    if n <= 0:
        return
    if n <= 20:
        step = 1
    elif n <= 60:
        step = max(1, n // 8)
    elif n <= 120:
        step = max(1, n // 10)
    else:
        step = max(1, n // 12)

    tick_positions = x[::step]
    tick_labels = [dates[i] for i in range(0, n, step)]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=35, ha="right", color=DIM, fontsize=11)


# ═══════════════════════════════════════════════
# 板块代码解析
# ══════════════════════════════════════════════


def _resolve_sector_code_from_market(market: MarketDataProvider, name_or_code: str) -> str | None:
    code = market.resolve_sector_code(name_or_code)
    if code:
        return code
    if "." in name_or_code:
        return name_or_code
    return None


def _sector_name_from_code(market: MarketDataProvider, code: str, sector_fallback: str = "") -> str:
    cls = market.get_classification()
    for key in ["concept", "industry", "region"]:
        df = cls.get(key) if isinstance(cls, dict) else None
        if df is None or df.empty:
            continue
        hit = df[df.get("ts_code") == code] if "ts_code" in df.columns else pd.DataFrame()
        if not hit.empty and "name" in hit.columns:
            return str(hit.iloc[0]["name"])
    return sector_fallback


# ═══════════════════════════════════════════════
# 价格走势图
# ═══════════════════════════════════════════════


def generate_price_chart(
    market: MarketDataProvider,
    ticker: str,
    from_date: str = "",
    to_date: str = "",
    display_bars: int = DISPLAY_BARS,
) -> bytes:
    # ── 1. 加载完整日线数据（get_bars 已含盘中截断 + 归一化）──
    all_bars = _load_full_bars(market, ticker)
    if not all_bars:
        raise ValueError(f"{ticker} 无历史数据")

    stock_name = market.get_stock_name(ticker)

    # ── 2. 从完整数据中提取 OHLCV 并计算指标 ─
    all_closes = _to_floats(all_bars, "close")

    if len(all_closes) < 2:
        return _empty_chart("数据不足")

    ma5_all = calc_ma_series(all_closes, 5)
    ma10_all = calc_ma_series(all_closes, 10)
    ma20_all = calc_ma_series(all_closes, 20)

    # ── 3. 确定显示窗口 ──
    from_compact = from_date.replace("-", "") if from_date else ""
    to_compact = to_date.replace("-", "") if to_date else ""

    display_bars_list = all_bars
    if from_compact or to_compact:
        filtered = []
        for b in all_bars:
            d = b.get("timestamp", b.get("date", ""))
            if isinstance(d, str) and len(d) == 8:
                pass
            elif hasattr(d, "strftime"):
                d = d.strftime("%Y%m%d")
            else:
                d = str(d).replace("-", "")
            if from_compact and d < from_compact:
                continue
            if to_compact and d > to_compact:
                continue
            filtered.append(b)
        display_bars_list = filtered

    if len(display_bars_list) > display_bars:
        display_bars_list = display_bars_list[-display_bars:]

    if not display_bars_list:
        return _empty_chart("显示范围为空")

    n = len(display_bars_list)
    x = np.arange(n)

    closes = _to_floats(display_bars_list, "close")
    opens = _to_floats(display_bars_list, "open")
    highs = _to_floats(display_bars_list, "high")
    lows = _to_floats(display_bars_list, "low")
    volumes = _to_floats(display_bars_list, "volume")
    dates = _to_dates(display_bars_list)

    # 截取显示窗口的均线
    display_start = len(all_closes) - len(display_bars_list)
    if from_compact or to_compact:
        first_display_date = _to_dates(display_bars_list)[0] if display_bars_list else ""
        display_start = 0
        for i, b in enumerate(all_bars):
            d = b.get("timestamp", b.get("date", ""))
            d = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
            if d == first_display_date.replace("-", ""):
                display_start = i
                break
    else:
        display_start = len(all_closes) - n

    ma5 = ma5_all[display_start : display_start + n]
    ma10 = ma10_all[display_start : display_start + n]
    ma20 = ma20_all[display_start : display_start + n]

    intraday_target = _pick_intraday_date(market, to_compact, display_bars_list)
    intraday_date_used = intraday_target or ""
    intraday_stock = None
    intraday_vol = None
    if intraday_target:
        intraday_date_used, intraday_stock, intraday_vol = _load_intraday_series(
            market, ticker, intraday_target, all_bars
        )

    # ── 4. 绘图 ──
    fig, axes = plt.subplots(
        4,
        1,
        figsize=(16, 12),
        gridspec_kw={"height_ratios": [1.05, 0.65, 2.3, 0.85]},
        sharex=False,
    )
    try:
        fig.patch.set_facecolor(BG_DARK)

        ax_intra, ax_intra_vol, ax_price, ax_vol = axes

        for ax in axes:
            ax.set_facecolor(PANEL_BG)
            ax.tick_params(colors=DIM, labelsize=10)
            ax.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        axis_times = _build_trading_minute_axis()
        axis_index = {t: i for i, t in enumerate(axis_times)}
        if intraday_stock is not None:
            date_disp = _compact_to_ymd(intraday_date_used) if intraday_date_used else ""
            ax_intra.set_title(
                f"{stock_name}（{ticker}）分时（{date_disp}）", color=WHITE, fontsize=12, fontweight="bold", pad=10
            )
            ax_intra.set_ylabel("涨跌幅(%)", color=WHITE, fontsize=10)
            ax_intra.axhline(0, color=DIM, linewidth=0.6, alpha=0.35)

            _plot_intraday_stock(ax_intra, axis_index, intraday_stock, "#34D399", "分时")
            if ax_intra.get_legend_handles_labels()[0]:
                ax_intra.legend(
                    loc="upper left",
                    fontsize=9,
                    facecolor=PANEL_BG,
                    edgecolor=GRID_COLOR,
                    labelcolor=WHITE,
                    framealpha=0.9,
                )

            positions = [
                i for i, t in enumerate(axis_times) if (t.endswith(":00") or t.endswith(":30")) and t != "11:30"
            ]
            if len(positions) > 12:
                step = max(1, len(positions) // 10)
                positions = positions[::step]
            if axis_times:
                last_pos = len(axis_times) - 1
                if not positions or positions[-1] != last_pos:
                    positions.append(last_pos)
            if intraday_stock is not None:
                intra_times = intraday_stock[0]
                data_xs = [axis_index.get(t) for t in intra_times if axis_index.get(t) is not None]
                x_max = max(data_xs) if data_xs else max(0, len(axis_times) - 1)
            else:
                x_max = max(0, len(axis_times) - 1)
            ax_intra.set_xlim(0, x_max)
            ax_intra.set_xticks(positions)
            ax_intra.set_xticklabels([axis_times[i] for i in positions], rotation=0, ha="center", color=DIM, fontsize=9)
        else:
            ax_intra.text(
                0.5,
                0.5,
                "无日内分时数据",
                transform=ax_intra.transAxes,
                ha="center",
                va="center",
                fontsize=13,
                color=WHITE,
            )
            ax_intra.set_xticks([])

        if intraday_vol is not None:
            times_all, vols, colors = intraday_vol
            xs = []
            ys = []
            cs = []
            for t, v, c in zip(times_all, vols.tolist(), colors, strict=False):
                idx = axis_index.get(t)
                if idx is None:
                    continue
                if pd.isna(v):
                    continue
                xs.append(idx)
                ys.append(float(v))
                cs.append(c)
            if xs:
                ax_intra_vol.bar(np.array(xs), np.array(ys), color=cs, alpha=0.60, width=0.85)
            ax_intra_vol.set_ylabel("成交量", color=WHITE, fontsize=10)
            ax_intra_vol.set_xlim(0, x_max)
            ax_intra_vol.set_xticks(positions)
            ax_intra_vol.set_xticklabels([axis_times[i] for i in positions], rotation=0, ha="center", color=DIM, fontsize=9)
        else:
            ax_intra_vol.text(
                0.5,
                0.5,
                "无日内成交量数据",
                transform=ax_intra_vol.transAxes,
                ha="center",
                va="center",
                fontsize=12,
                color=WHITE,
            )
            ax_intra_vol.set_xticks([])

        _render_info_bar(ax_price, display_bars_list)

        colors_bar = _draw_candlestick(ax_price, x, closes, opens, highs, lows)

        # ── 均线 ──
        for ma_vals, label, color in [
            (ma5, "MA5", MA_COLORS[5]),
            (ma10, "MA10", MA_COLORS[10]),
            (ma20, "MA20", MA_COLORS[20]),
        ]:
            valid = ~np.isnan(ma_vals)
            if valid.any():
                ax_price.plot(x[valid], ma_vals[valid], color=color, linewidth=1.2, label=label)

        if ax_price.get_legend_handles_labels()[0]:
            ax_price.legend(
                loc="upper left", fontsize=9, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.9
            )

        ax_price.set_title(_title(ticker, stock_name), color=WHITE, fontsize=13, fontweight="bold", pad=30)
        ax_price.set_ylabel("价格 (元)", color=WHITE, fontsize=12)

        # ── 成交量 ──
        ax_vol.bar(x, volumes, color=colors_bar, alpha=0.65, width=0.65)
        ax_vol.set_ylabel("成交量 (手)", color=WHITE, fontsize=12)

        _set_date_ticks(ax_vol, x, dates, n)

        fig.subplots_adjust(left=0.06, right=0.99, top=0.95, bottom=0.06, hspace=0.28)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════
# 技术指标面板图
# ═══════════════════════════════════════════════


def generate_technical_chart(
    market: MarketDataProvider,
    ticker: str,
    from_date: str = "",
    to_date: str = "",
) -> bytes:
    # ── 1. 加载完整日线（get_bars 已含盘中截断）──
    all_bars = _load_full_bars(market, ticker)
    if not all_bars:
        raise ValueError(f"{ticker} 无历史数据")

    stock_name = market.get_stock_name(ticker)
    total_count = len(all_bars)
    if total_count < 20:
        raise ValueError(f"{ticker} 数据不足（仅 {total_count} 个交易日）")

    # ── 2. 全量计算指标 ──
    all_closes = _to_floats(all_bars, "close")
    all_volumes = _to_floats(all_bars, "volume")
    from_compact = from_date.replace("-", "") if from_date else ""
    to_compact = to_date.replace("-", "") if to_date else ""

    ma5_all = calc_ma_series(all_closes, 5)
    ma10_all = calc_ma_series(all_closes, 10)
    ma20_all = calc_ma_series(all_closes, 20)
    ma60_all = calc_ma_series(all_closes, 60)
    bb_upper_all, bb_mid_all, bb_lower_all = calc_bollinger_series(all_closes, window=20, num_std=2.0, ddof=1)
    rsi_all = calc_rsi_series(all_closes, window=14)
    macd_line_all, signal_line_all, histogram_all = calc_macd_series(
        all_closes, fast=12, slow=26, signal=9, hist_scale=1.0
    )
    vol_ma5_all = calc_ma_series(all_volumes, 5)

    # ── 3. 截取显示窗口 ──
    display_bars_list = all_bars
    if from_compact or to_compact:
        filtered = []
        for b in all_bars:
            d = b.get("timestamp", b.get("date", ""))
            if isinstance(d, str) and len(d) == 8:
                pass
            elif hasattr(d, "strftime"):
                d = d.strftime("%Y%m%d")
            else:
                d = str(d).replace("-", "")
            if from_compact and d < from_compact:
                continue
            if to_compact and d > to_compact:
                continue
            filtered.append(b)
        display_bars_list = filtered

    if len(display_bars_list) > TECH_DISPLAY_BARS:
        display_bars_list = display_bars_list[-TECH_DISPLAY_BARS:]

    if not display_bars_list:
        return _empty_chart("显示范围为空")

    n = len(display_bars_list)
    x = np.arange(n)

    closes = _to_floats(display_bars_list, "close")
    opens = _to_floats(display_bars_list, "open")
    highs = _to_floats(display_bars_list, "high")
    lows = _to_floats(display_bars_list, "low")
    volumes = _to_floats(display_bars_list, "volume")
    dates = _to_dates(display_bars_list)

    display_start = total_count - n
    if from_compact or to_compact:
        first_display_date = _to_dates(display_bars_list)[0] if display_bars_list else ""
        display_start = 0
        for i, b in enumerate(all_bars):
            d = b.get("timestamp", b.get("date", ""))
            d = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
            if d == first_display_date.replace("-", ""):
                display_start = i
                break

    ma5 = ma5_all[display_start : display_start + n]
    ma10 = ma10_all[display_start : display_start + n]
    ma20 = ma20_all[display_start : display_start + n]
    ma60 = ma60_all[display_start : display_start + n]
    bb_upper = bb_upper_all[display_start : display_start + n]
    bb_mid = bb_mid_all[display_start : display_start + n]
    bb_lower = bb_lower_all[display_start : display_start + n]
    rsi = rsi_all[display_start : display_start + n]
    macd_line = macd_line_all[display_start : display_start + n]
    signal_line = signal_line_all[display_start : display_start + n]
    histogram = histogram_all[display_start : display_start + n]
    vol_ma5 = vol_ma5_all[display_start : display_start + n]

    fig, axes = plt.subplots(4, 1, figsize=(16, 12), gridspec_kw={"height_ratios": [2.5, 1.0, 1.0, 1.0]}, sharex=True)
    try:
        fig.patch.set_facecolor(BG_DARK)
        ax_price, ax_vol, ax_rsi, ax_macd = axes

        _render_info_bar(ax_price, display_bars_list)

        colors_bar = _draw_candlestick(ax_price, x, closes, opens, highs, lows, width=0.65, alpha=0.9, linew=0.5)

        for ma_vals, label, color in [
            (ma5, "MA5", MA_COLORS[5]),
            (ma10, "MA10", MA_COLORS[10]),
            (ma20, "MA20", MA_COLORS[20]),
            (ma60, "MA60", MA_COLORS[60]),
        ]:
            valid = ~np.isnan(ma_vals)
            if valid.any():
                ax_price.plot(x[valid], ma_vals[valid], color=color, linewidth=1.0, label=label)

        for bb_vals, label, color in [
            (bb_upper, "上轨", "#6366F1"),
            (bb_mid, "中轨", "#818CF8"),
            (bb_lower, "下轨", "#6366F1"),
        ]:
            valid = ~np.isnan(bb_vals)
            if valid.any():
                ax_price.plot(
                    x[valid],
                    bb_vals[valid],
                    color=color,
                    linewidth=0.7,
                    linestyle="--" if label != "中轨" else "-",
                    alpha=0.55,
                    label=label,
                )

        if ax_price.get_legend_handles_labels()[0]:
            ax_price.legend(
                loc="upper left",
                fontsize=8,
                facecolor=PANEL_BG,
                edgecolor=GRID_COLOR,
                labelcolor=WHITE,
                framealpha=0.85,
            )

        ax_price.set_title(_title(ticker, stock_name), color=WHITE, fontsize=13, fontweight="bold", pad=20)
        ax_price.set_ylabel("价格 (元)", color=WHITE, fontsize=11)
        ax_price.set_facecolor(PANEL_BG)
        ax_price.tick_params(colors=DIM, labelsize=10)
        ax_price.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        # ── 成交量 ──
        ax_vol.bar(x, volumes, color=colors_bar, alpha=0.65, width=0.65)
        valid_v = ~np.isnan(vol_ma5)
        if valid_v.any():
            ax_vol.plot(x[valid_v], vol_ma5[valid_v], color=MA_COLORS[5], linewidth=1.0, label="量 MA5")
        if ax_vol.get_legend_handles_labels()[0]:
            ax_vol.legend(
                loc="upper left", fontsize=8, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.8
            )
        ax_vol.set_ylabel("成交量", color=WHITE, fontsize=10)
        ax_vol.set_facecolor(PANEL_BG)
        ax_vol.tick_params(colors=DIM, labelsize=10)
        ax_vol.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        # ── RSI ──
        valid_r = ~np.isnan(rsi)
        if valid_r.any():
            ax_rsi.plot(x[valid_r], rsi[valid_r], color="#F59E0B", linewidth=1.2)
        ax_rsi.axhline(70, color=DOWN_COLOR, linewidth=0.8, linestyle="--", alpha=0.5)
        ax_rsi.axhline(30, color=UP_COLOR, linewidth=0.8, linestyle="--", alpha=0.5)
        ax_rsi.axhline(50, color=DIM, linewidth=0.4, linestyle="-", alpha=0.3)
        ax_rsi.fill_between(x, 70, 100, alpha=0.04, color=DOWN_COLOR)
        ax_rsi.fill_between(x, 0, 30, alpha=0.04, color=UP_COLOR)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI(14)", color=WHITE, fontsize=10)
        ax_rsi.set_facecolor(PANEL_BG)
        ax_rsi.tick_params(colors=DIM, labelsize=10)
        ax_rsi.set_yticks([0, 30, 50, 70, 100])
        ax_rsi.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        # ── MACD ──
        valid_m = ~np.isnan(macd_line)
        if valid_m.any():
            ax_macd.plot(x[valid_m], macd_line[valid_m], color="#3B82F6", linewidth=1.0, label="DIF")
            ax_macd.plot(x[valid_m], signal_line[valid_m], color="#F97316", linewidth=0.8, label="DEA")
            hist_colors = [UP_COLOR if h >= 0 else DOWN_COLOR for h in histogram[valid_m]]
            ax_macd.bar(x[valid_m], histogram[valid_m], color=hist_colors, alpha=0.7, width=0.65)
        ax_macd.axhline(0, color=DIM, linewidth=0.4, alpha=0.4)
        if ax_macd.get_legend_handles_labels()[0]:
            ax_macd.legend(
                loc="upper left", fontsize=8, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.8
            )
        ax_macd.set_ylabel("MACD", color=WHITE, fontsize=10)
        ax_macd.set_facecolor(PANEL_BG)
        ax_macd.tick_params(colors=DIM, labelsize=10)
        ax_macd.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        _set_date_ticks(ax_macd, x, dates, n)

        fig.subplots_adjust(left=0.06, right=0.99, top=0.94, bottom=0.08, hspace=0.28)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════
# 市场快照
# ═══════════════════════════════════════════════


def generate_market_snapshot_chart(market: MarketDataProvider, date: str) -> bytes:
    date_compact = date.replace("-", "")

    intraday_sse = _load_intraday_pct_for_index(market, SSE_INDEX_CODE, date_compact)
    intraday_sz = _load_intraday_pct_for_index(market, SZSE_INDEX_CODE, date_compact)

    # ── 日线：get_bars 已含盘中截断 + 今日实时 bar ──
    df_sse = market.get_bars(SSE_INDEX_CODE, granularity="1d", start="20200101")
    if df_sse is None or df_sse.empty:
        return _empty_chart("无指数日线数据")

    # 过滤到指定日期及之前
    ts_sse = df_sse["timestamp"].astype(str)
    daily_sse_full = df_sse[ts_sse <= date_compact].copy().sort_values("timestamp").reset_index(drop=True)
    if len(daily_sse_full) > TECH_DISPLAY_BARS:
        daily_sse = daily_sse_full.tail(TECH_DISPLAY_BARS).reset_index(drop=True)
    else:
        daily_sse = daily_sse_full.copy()

    fig, axes = plt.subplots(3, 1, figsize=(16, 11), gridspec_kw={"height_ratios": [1.05, 1.7, 0.85]}, sharex=False)
    ax_intraday, ax_daily, ax_amount = axes
    try:
        fig.patch.set_facecolor(BG_DARK)
        for ax in axes:
            ax.set_facecolor(PANEL_BG)
            ax.tick_params(colors=DIM, labelsize=10)
            ax.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        ax_intraday.set_title(f"上证 vs 深证成指 分时（{date}）", color=WHITE, fontsize=12, fontweight="bold", pad=12)
        ax_intraday.set_ylabel("涨跌幅(%)", color=WHITE, fontsize=11)
        ax_intraday.axhline(0, color=DIM, linewidth=0.6, alpha=0.35)

        axis_times = _build_trading_minute_axis()
        axis_index = {t: i for i, t in enumerate(axis_times)}

        _plot_intraday_line(ax_intraday, axis_index, intraday_sse, "#60A5FA", "上证指数")
        _plot_intraday_line(ax_intraday, axis_index, intraday_sz, "#F59E0B", "深证成指")

        if ax_intraday.get_legend_handles_labels()[0]:
            ax_intraday.legend(
                loc="upper left",
                fontsize=10,
                facecolor=PANEL_BG,
                edgecolor=GRID_COLOR,
                labelcolor=WHITE,
                framealpha=0.9,
            )

        positions = [i for i, t in enumerate(axis_times) if (t.endswith(":00") or t.endswith(":30")) and t != "11:30"]
        if len(positions) > 12:
            step = max(1, len(positions) // 10)
            positions = positions[::step]
        intra_x_max = max(0, len(axis_times) - 1)
        for data_tuple in [intraday_sse, intraday_sz]:
            if data_tuple is not None:
                data_xs = [axis_index.get(t) for t in data_tuple[0] if axis_index.get(t) is not None]
                if data_xs:
                    intra_x_max = max(intra_x_max, max(data_xs))
        ax_intraday.set_xlim(0, intra_x_max)
        ax_intraday.set_xticks(positions)
        ax_intraday.set_xticklabels([axis_times[i] for i in positions], rotation=0, ha="center", color=DIM, fontsize=9)

        ax_daily.set_title(f"上证指数 日线（最近{TECH_DISPLAY_BARS}个交易日）", color=WHITE, fontsize=12, fontweight="bold", pad=10)
        ax_daily.set_ylabel("点位", color=WHITE, fontsize=11)
        ax_amount.set_ylabel("成交额(亿)", color=WHITE, fontsize=11)

        if not daily_sse.empty and len(daily_sse) >= 2:
            opens = pd.to_numeric(daily_sse["open"], errors="coerce").astype(float).to_numpy()
            highs = pd.to_numeric(daily_sse["high"], errors="coerce").astype(float).to_numpy()
            lows = pd.to_numeric(daily_sse["low"], errors="coerce").astype(float).to_numpy()
            closes = pd.to_numeric(daily_sse["close"], errors="coerce").astype(float).to_numpy()
            amounts = pd.to_numeric(daily_sse["amount"], errors="coerce").astype(float).to_numpy() if "amount" in daily_sse.columns else np.zeros_like(closes)
            dates = [_compact_to_ymd(s) for s in daily_sse["timestamp"].astype(str).tolist()]
            n = len(closes)
            x = np.arange(n)

            colors_bar = _draw_candlestick(ax_daily, x, closes, opens, highs, lows, width=0.65, alpha=0.9, linew=0.5)
            ax_daily.set_xticks([])

            # ─ 均线：在全量上计算，再切片到显示窗口 ─
            total_count = len(daily_sse_full)
            view_start = max(0, total_count - n)
            full_closes = pd.to_numeric(daily_sse_full["close"], errors="coerce").astype(float).tolist()
            ma5_full = calc_ma_series(full_closes, 5)
            ma10_full = calc_ma_series(full_closes, 10)
            ma20_full = calc_ma_series(full_closes, 20)
            ma5 = ma5_full[view_start : view_start + n]
            ma10 = ma10_full[view_start : view_start + n]
            ma20 = ma20_full[view_start : view_start + n]
            for ma_vals, label, color in [
                (ma5, "MA5", MA_COLORS[5]),
                (ma10, "MA10", MA_COLORS[10]),
                (ma20, "MA20", MA_COLORS[20]),
            ]:
                valid = ~np.isnan(ma_vals)
                if valid.any():
                    ax_daily.plot(x[valid], ma_vals[valid], color=color, linewidth=1.0, label=label)

            if ax_daily.get_legend_handles_labels()[0]:
                ax_daily.legend(
                    loc="upper left", fontsize=8, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.9, ncol=3
                )

            amounts_yi = amounts / 1e4
            ax_amount.bar(x, amounts_yi, color=colors_bar, alpha=0.6, width=0.65)
            _set_date_ticks(ax_amount, x, dates, n)
        else:
            ax_daily.text(0.5, 0.5, "无指数日线数据", transform=ax_daily.transAxes, ha="center", va="center", fontsize=14, color=WHITE)
            ax_daily.set_xticks([])
            ax_amount.set_xticks([])

        fig.subplots_adjust(left=0.07, right=0.99, top=0.95, bottom=0.08, hspace=0.28)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════
# 板块快照
# ══════════════════════════════════════════════


def generate_sector_snapshot_chart(market: MarketDataProvider, sector: str, date: str) -> bytes:
    date_compact = date.replace("-", "")

    sector_code = _resolve_sector_code_from_market(market, sector)
    if sector_code is None:
        return _empty_chart(f"未找到板块: {sector}")
    sector_name = _sector_name_from_code(market, sector_code, sector)

    # ── 日线：get_bars 已含盘中截断 ──
    df_sec = market.get_bars(sector_code, granularity="1d", start="20200101")
    if df_sec is None or df_sec.empty:
        return _empty_chart(f"无板块日线数据: {sector}")

    ts_sec = df_sec["timestamp"].astype(str)
    daily_full_sec = df_sec[ts_sec <= date_compact].copy().sort_values("timestamp").reset_index(drop=True)
    if len(daily_full_sec) > TECH_DISPLAY_BARS:
        daily_sec = daily_full_sec.tail(TECH_DISPLAY_BARS).reset_index(drop=True)
    else:
        daily_sec = daily_full_sec.copy()

    intraday_sector = _load_sector_intraday_pct(market, sector_code, date_compact)
    intraday_sz = _load_intraday_pct_for_index(market, SZSE_INDEX_CODE, date_compact)

    axis_times = _build_trading_minute_axis()
    axis_index = {t: i for i, t in enumerate(axis_times)}

    fig, axes = plt.subplots(2, 1, figsize=(16, 9), gridspec_kw={"height_ratios": [1.05, 1.25]}, sharex=False)
    ax_intraday, ax_daily = axes
    try:
        fig.patch.set_facecolor(BG_DARK)
        for ax in axes:
            ax.set_facecolor(PANEL_BG)
            ax.tick_params(colors=DIM, labelsize=10)
            ax.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

        ax_intraday.set_title(
            f"{sector_name}（{sector_code}）vs 深证成指 分时（{date}）",
            color=WHITE, fontsize=12, fontweight="bold", pad=12,
        )
        ax_intraday.set_ylabel("涨跌幅(%)", color=WHITE, fontsize=11)
        ax_intraday.axhline(0, color=DIM, linewidth=0.6, alpha=0.35)

        _plot_intraday_line(ax_intraday, axis_index, intraday_sector, "#34D399", "板块")
        _plot_intraday_line(ax_intraday, axis_index, intraday_sz, "#F59E0B", "深证成指")

        if ax_intraday.get_legend_handles_labels()[0]:
            ax_intraday.legend(
                loc="upper left", fontsize=10, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.9
            )

        positions = [i for i, t in enumerate(axis_times) if (t.endswith(":00") or t.endswith(":30")) and t != "11:30"]
        if len(positions) > 12:
            step = max(1, len(positions) // 10)
            positions = positions[::step]
        intra_x_max = max(0, len(axis_times) - 1)
        for data_tuple in [intraday_sector, intraday_sz]:
            if data_tuple is not None:
                data_xs = [axis_index.get(t) for t in data_tuple[0] if axis_index.get(t) is not None]
                if data_xs:
                    intra_x_max = max(intra_x_max, max(data_xs))
        ax_intraday.set_xlim(0, intra_x_max)
        ax_intraday.set_xticks(positions)
        ax_intraday.set_xticklabels([axis_times[i] for i in positions], rotation=0, ha="center", color=DIM, fontsize=9)

        ax_daily.set_title(f"{sector_name} 日线（最近{TECH_DISPLAY_BARS}个交易日）", color=WHITE, fontsize=12, fontweight="bold", pad=10)
        ax_daily.set_ylabel("点位", color=WHITE, fontsize=11)

        if not daily_sec.empty and len(daily_sec) >= 2:
            opens = pd.to_numeric(daily_sec["open"], errors="coerce").astype(float).to_numpy()
            highs = pd.to_numeric(daily_sec["high"], errors="coerce").astype(float).to_numpy()
            lows = pd.to_numeric(daily_sec["low"], errors="coerce").astype(float).to_numpy()
            closes = pd.to_numeric(daily_sec["close"], errors="coerce").astype(float).to_numpy()
            dates = [_compact_to_ymd(s) for s in daily_sec["timestamp"].astype(str).tolist()]
            n = len(closes)
            x = np.arange(n)

            _render_info_bar(ax_daily, daily_sec.to_dict("records"))
            _draw_candlestick(ax_daily, x, closes, opens, highs, lows, width=0.65, alpha=0.9, linew=0.5)

            # ── 均线：全量计算后切片 ──
            total_count = len(daily_full_sec)
            view_start = max(0, total_count - n)
            full_closes = pd.to_numeric(daily_full_sec["close"], errors="coerce").astype(float).tolist()
            ma5_full = calc_ma_series(full_closes, 5)
            ma10_full = calc_ma_series(full_closes, 10)
            ma20_full = calc_ma_series(full_closes, 20)
            ma5 = ma5_full[view_start : view_start + n]
            ma10 = ma10_full[view_start : view_start + n]
            ma20 = ma20_full[view_start : view_start + n]

            for ma_vals, label, color in [
                (ma5, "MA5", MA_COLORS[5]),
                (ma10, "MA10", MA_COLORS[10]),
                (ma20, "MA20", MA_COLORS[20]),
            ]:
                valid = ~np.isnan(ma_vals)
                if valid.any():
                    ax_daily.plot(x[valid], ma_vals[valid], color=color, linewidth=1.0, label=label)

            if ax_daily.get_legend_handles_labels()[0]:
                ax_daily.legend(
                    loc="upper left", fontsize=8, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.9, ncol=3
                )

            _set_date_ticks(ax_daily, x, dates, n)
        else:
            ax_daily.text(0.5, 0.5, "无板块日线数据", transform=ax_daily.transAxes, ha="center", va="center", fontsize=14, color=WHITE)
            ax_daily.set_xticks([])

        fig.subplots_adjust(left=0.07, right=0.99, top=0.94, bottom=0.08, hspace=0.28)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════
# 四大指数同图
# ═══════════════════════════════════════════════

_MULTI_INDICES: list[tuple[str, str, str]] = [
    ("000001.SH", "上证指数", "上证指数.csv"),
    ("399001.SZ", "深证成指", "深证成指.csv"),
    ("399006.SZ", "创业板指", "创业板指.csv"),
    ("000688.SH", "科创50", "科创50.csv"),
]


def generate_multi_index_chart(market: MarketDataProvider) -> bytes:
    """四大指数日线同图排列（2×2 子图）。get_bars 已含盘中截断 + 归一化。"""
    fig, axes = plt.subplots(2, 2, figsize=(22, 14))
    try:
        fig.patch.set_facecolor(BG_DARK)

        for _idx, (ax, (code, name, _)) in enumerate(zip(axes.flat, _MULTI_INDICES, strict=False)):
            ax.set_facecolor(PANEL_BG)
            ax.tick_params(colors=DIM, labelsize=9)
            ax.grid(axis="y", alpha=0.18, color=GRID_COLOR, linewidth=0.5)

            # ── 数据：get_bars 已含盘中截断 + 今日实时 bar ──
            df_daily = market.get_bars(code, granularity="1d", start="20200101")
            if df_daily is None or df_daily.empty:
                ax.text(0.5, 0.5, f"{name} 无数据", transform=ax.transAxes, ha="center", va="center", fontsize=14, color=WHITE)
                ax.set_title(name, color=WHITE, fontsize=13, fontweight="bold", pad=10)
                continue

            df_daily = df_daily.sort_values("timestamp").reset_index(drop=True)
            daily_view = df_daily.tail(TECH_DISPLAY_BARS).reset_index(drop=True) if len(df_daily) > TECH_DISPLAY_BARS else df_daily.copy()
            full_count = len(df_daily)

            if len(daily_view) < 2:
                ax.text(0.5, 0.5, f"{name} 数据不足", transform=ax.transAxes, ha="center", va="center", fontsize=14, color=WHITE)
                ax.set_title(name, color=WHITE, fontsize=13, fontweight="bold", pad=10)
                continue

            n = len(daily_view)
            x = np.arange(n)
            opens = pd.to_numeric(daily_view["open"], errors="coerce").astype(float).to_numpy()
            highs = pd.to_numeric(daily_view["high"], errors="coerce").astype(float).to_numpy()
            lows = pd.to_numeric(daily_view["low"], errors="coerce").astype(float).to_numpy()
            closes = pd.to_numeric(daily_view["close"], errors="coerce").astype(float).to_numpy()
            dates = [_compact_to_ymd(s) for s in daily_view["timestamp"].astype(str).tolist()]

            # ── 归一化为涨跌幅(%)，以首根 bar 收盘价为基准 ──
            _base = float(closes[0]) if closes[0] else 1.0
            def _pct(arr: np.ndarray, base: float = _base) -> np.ndarray:
                return (arr / base - 1.0) * 100.0

            _draw_candlestick(ax, x, _pct(closes), _pct(opens), _pct(highs), _pct(lows), width=0.65, alpha=0.9, linew=0.5)
            ax.axhline(0, color=DIM, linewidth=0.6, alpha=0.35)
            ax.set_ylabel("涨跌幅(%)", color=WHITE, fontsize=11)

            # ── 均线：全量计算，切片，再归一化 ──
            view_start = max(0, full_count - n)
            full_closes = pd.to_numeric(df_daily["close"], errors="coerce").astype(float).tolist()
            ma5_full = np.array(calc_ma_series(full_closes, 5))
            ma10_full = np.array(calc_ma_series(full_closes, 10))
            ma20_full = np.array(calc_ma_series(full_closes, 20))
            ma5 = ma5_full[view_start : view_start + n]
            ma10 = ma10_full[view_start : view_start + n]
            ma20 = ma20_full[view_start : view_start + n]

            def _pct_slice(arr: np.ndarray, _ref_base: float = _base) -> np.ndarray:
                valid = arr[~np.isnan(arr)]
                base = float(valid[0]) if valid.size and valid[0] else _ref_base
                return (arr / base - 1.0) * 100.0 if base else np.full_like(arr, 0.0)

            for ma_vals, label, color in [
                (_pct_slice(ma5), "MA5", MA_COLORS[5]),
                (_pct_slice(ma10), "MA10", MA_COLORS[10]),
                (_pct_slice(ma20), "MA20", MA_COLORS[20]),
            ]:
                valid = ~np.isnan(ma_vals)
                if valid.any():
                    ax.plot(x[valid], ma_vals[valid], color=color, linewidth=1.0, label=label)

            if ax.get_legend_handles_labels()[0]:
                ax.legend(loc="upper left", fontsize=8, facecolor=PANEL_BG, edgecolor=GRID_COLOR, labelcolor=WHITE, framealpha=0.9, ncol=3)

            last_close = float(closes[-1])
            last_pct = float(_pct(closes)[-1])
            prev_close = float(closes[-2]) if n >= 2 else last_close
            chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
            chg_sign = "+" if chg_pct >= 0 else ""
            title = f"{name}（{code}）  最新 {last_close:.2f}  区间涨跌 {last_pct:+.2f}%  日涨跌 {chg_sign}{chg_pct:.2f}%"
            ax.set_title(title, color=WHITE, fontsize=15, fontweight="bold", pad=10)

            _set_date_ticks(ax, x, dates, n)

        fig.suptitle(f"四大指数 · 最近 {TECH_DISPLAY_BARS} 个交易日", color=WHITE, fontsize=16, fontweight="bold", y=0.98)
        fig.subplots_adjust(left=0.05, right=0.99, top=0.94, bottom=0.05, hspace=0.30, wspace=0.15)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════


def _empty_chart(message: str) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 2))
    try:
        fig.patch.set_facecolor(BG_DARK)
        ax.text(0.5, 0.5, message, ha="center", va="center", color=WHITE, fontsize=14)
        ax.set_facecolor(PANEL_BG)
        ax.set_xticks([])
        ax.set_yticks([])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
    finally:
        plt.close(fig)
    buf.seek(0)
    return buf.read()
