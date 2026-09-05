"""实时行情 + 历史数据工具 —— 个股行情、板块概况、技术指标、图表、市场概况、价格历史"""

from __future__ import annotations

import json as _json
import re as _re_market
import time

from loguru import logger

from src.market.charts import (
    generate_market_snapshot_chart,
    generate_multi_index_chart,
    generate_price_chart,
    generate_sector_snapshot_chart,
    generate_technical_chart,
)
from src.market.provider import MarketDataProvider
from src.utils.oss_uploader import upload_bytes

from ._deps import _is_transient_tool_error, err, ok
from .context import get_ctx
from .registry import register_tool
from .schemas.market import (
    GetMarketSnapshotArgs,
    GetMultiIndexChartArgs,
    GetPriceChartArgs,
    GetSectorSnapshotChartArgs,
    GetTechnicalChartArgs,
)

# ═══════════════════════════════════════════════
# 代码格式规范化
# ═══════════════════════════════════════════════


def _normalize_ticker(ticker: str) -> str:
    """确保股票代码带有交易所后缀。若缺少后缀则根据代码前缀自动推断。

    A股规则：
    - 000xxx / 001xxx / 002xxx / 300xxx / 301xxx → .SZ（深交所）
    - 600xxx / 601xxx / 603xxx / 605xxx / 688xxx → .SH（上交所）
    - 4xxxxx / 8xxxxx → .BJ（北交所）
    - 已有 "." 则直接返回
    """
    ticker = ticker.strip()
    if "." in ticker:
        return ticker
    if _re_market.match(r"^(000|001|002|300|301|302)", ticker):
        return f"{ticker}.SZ"
    if _re_market.match(r"^(600|601|603|605|688)", ticker):
        return f"{ticker}.SH"
    if _re_market.match(r"^[92]", ticker):
        return f"{ticker}.BJ"
    return ticker



def _resolve_to_ticker(market: MarketDataProvider, stock_name: str, allow_non_a_share: bool = False) -> str:
    """将股票/指数名称解析为标准化代码。支持 A 股、港股、A 股指数。

    A 股返回名称（兼容现有 kbquant），港股返回 ts_code（如 00700.HK），
    指数返回指数代码（如 000688.SH）。
    未匹配抛出 ValueError 并说明市场信息。

    Args:
        allow_non_a_share: True 时允许港股/美股通过（如 create_node），False 时
           仅允许 A 股（如 create_trade、图表工具）。
    """
    name = stock_name.strip()

    # 0. 尝试指数名称解析
    index_code = market.resolve_index_name(name)
    if index_code is not None:
        return index_code

    # 1. 尝试 A 股
    a_matches = market.resolve_stock_ticker(name)
    if len(a_matches) == 1:
        return a_matches[0][0]
    if len(a_matches) > 1:
        options = "、".join([f"{n}({c})" for n, c in a_matches])
        raise ValueError(f"股票名称「{stock_name}」匹配到多个 A 股结果：{options}，请指定更精确的名称")

    # 2. 港股匹配
    inferred = market.infer_stock_market(name)
    if inferred:
        market_type, ticker, rname = inferred
        if market_type == "hk":
            if allow_non_a_share:
                return ticker
            raise ValueError(
                f"「{stock_name}」为港股（{ticker} {rname}），当前交易仅支持 A 股。"
            )
        if market_type == "a_share":
            return ticker

    # 3. 纯英文名 → 可能是美股
    import re
    if re.fullmatch(r"[A-Za-z0-9 .&(),-]+", name):
        if allow_non_a_share:
            raise ValueError(
                f"「{stock_name}」未在 A 股或港股中找到，可能是美股或未收录标的，请确认名称是否正确。"
            )
        raise ValueError(
            f"「{stock_name}」未在 A 股中找到，且为纯英文名，可能是美股。当前交易仅支持 A 股。"
        )

    # 4. 完全未匹配 —— 尝试港股去后缀模糊匹配以给出更准确的错误消息
    hk_hint = _try_hk_fuzzy_match(market, name)
    if allow_non_a_share:
        if hk_hint:
            raise ValueError(f"未找到「{stock_name}」的匹配标的。但找到相似港股: {hk_hint}。请确认名称是否正确。")
        raise ValueError(f"未找到「{stock_name}」的匹配标的，请确认名称是否正确（如「贵州茅台」「平安银行」）。")
    if hk_hint:
        raise ValueError(f"「{stock_name}」为港股（{hk_hint}），当前交易仅支持 A 股。")
    raise ValueError(f"未找到 A 股「{stock_name}」，请检查名称是否正确（如「贵州茅台」「平安银行」）。")


def _try_hk_fuzzy_match(market: MarketDataProvider, name: str) -> str | None:
    """尝试更激进的港股模糊匹配（去 -W/-SW/-S 等常见后缀）。
    返回匹配提示字符串或 None。
    """
    try:
        hk_df = market._cache.session.hk_basic
        if hk_df is None or hk_df.empty:
            return None
        import re
        # 剥离常见港股后缀如 -W, -SW, -S, -R 等
        base = re.sub(r"-[WSR]\b.*$", "", name).strip()
        candidates = [name]
        if base and base != name:
            candidates.append(base)
        for try_name in candidates:
            exact = hk_df[hk_df["name"] == try_name]
            if not exact.empty:
                r = exact.iloc[0]
                return f"{r['name']}({r['ts_code']})"
            fuzzy = hk_df[hk_df["name"].str.contains(try_name, na=False, regex=False)]
            if not fuzzy.empty:
                r = fuzzy.iloc[0]
                return f"{r['name']}({r['ts_code']})"
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════
# 图表工具
# ═══════════════════════════════════════════════


@register_tool(
    name="get_technical_chart",
    description="生成技术分析面板图（4联动子图：价格+MA+布林带 / 成交量 / RSI / MACD），返回 PNG 图片的公网 URL。"
    "适用：多模态模型需要进行技术面分析，直观判断趋势、超买超卖、金叉死叉、布林带突破等信号。",
    category="market",
    args_schema=GetTechnicalChartArgs,
)
async def get_technical_chart(stock_name: str, from_date: str | None = None, to_date: str | None = None) -> str:
    try:
        market = get_ctx().market
        ticker = _resolve_to_ticker(market, stock_name)
        resolved_name = market.get_stock_name(ticker) or stock_name
        png_bytes = generate_technical_chart(market, ticker, from_date=from_date or "", to_date=to_date or "")
        return ok(
            {
                "ticker": ticker,
                "stock_name": resolved_name,
                "from_date": from_date or "",
                "to_date": to_date or "",
                "chart": upload_bytes(png_bytes, f"charts/technical/{ticker}_{int(time.time() * 1000)}.png"),
            }
        )
    except ValueError as e:
        logger.warning("get_technical_chart: stock name resolution failed: {}", e)
        return err(str(e))
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_technical_chart failed: {}", e)
        return err(f"生成技术图表失败：{e}")


@register_tool(
    name="get_market_snapshot",
    description="返回某个交易日的市场快照图（两行：上证vs深证成指分时 / 上证最近120个交易日走势）和文字摘要（上涨/下跌家数、平均涨跌幅、总成交额）。"
    "适用：复盘某一天的大盘强弱、了解市场整体情绪和资金活跃度。",
    category="history",
    args_schema=GetMarketSnapshotArgs,
)
async def get_market_snapshot(date: str) -> str:
    try:
        market = get_ctx().market
        snap = market.get_market_snapshot(date)
        if isinstance(snap, str):
            snap = _json.loads(snap)
        if isinstance(snap, dict) and "error" in snap:
            return err(snap["error"])

        png_bytes = generate_market_snapshot_chart(market, date)

        total = snap.get("total_stocks")
        up = snap.get("up_count")
        down = snap.get("down_count")
        avg = snap.get("avg_pct_chg")
        total_amount = snap.get("total_amount")
        total_amount_yi = round(float(total_amount) / 1e8, 1) if total_amount is not None else None

        summary_parts = [
            f"日期：{date}",
            f"上涨/下跌：{up}/{down}" if up is not None and down is not None else None,
            f"平均涨跌幅：{avg}%" if avg is not None else None,
            f"总成交额：{total_amount_yi}亿" if total_amount_yi is not None else None,
            f"覆盖股票数：{total}" if total is not None else None,
        ]
        summary = "；".join([p for p in summary_parts if p])

        return ok(
            {
                "date": date,
                "chart": upload_bytes(png_bytes, f"charts/market_snapshot/{date}_{int(time.time() * 1000)}.png"),
                "summary": summary,
                "snapshot": snap,
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_market_snapshot failed: {}", e)
        return err(f"获取市场快照失败：{e}")


@register_tool(
    name="get_price_chart",
    description="生成价格走势图表（含日内分时+均价线+日内成交量 / 日线OHLC蜡烛图+MA均线+成交量），返回 PNG 图片的公网 URL。"
    "适用：多模态模型需要通过图表直观判断趋势、支撑阻力、均线排列、成交量分布时使用。"
    "日期参数建议不填（默认最近240个交易日）；若填则至少覆盖100个交易日以看清趋势。"
    "不适用：需要技术指标面板（RSI/MACD/布林带）请用 get_technical_chart。",
    category="history",
    args_schema=GetPriceChartArgs,
)
async def get_price_chart(stock_name: str, from_date: str | None = None, to_date: str | None = None) -> str:
    try:
        market = get_ctx().market
        ticker = _resolve_to_ticker(market, stock_name)
        resolved_name = market.get_stock_name(ticker) or stock_name
        png_bytes = generate_price_chart(market, ticker, from_date or "", to_date or "")
        return ok(
            {
                "ticker": ticker,
                "stock_name": resolved_name,
                "period": f"{from_date or '?'} ~ {to_date or '?'}",
                "chart": upload_bytes(png_bytes, f"charts/price/{ticker}_{int(time.time() * 1000)}.png"),
            }
        )
    except ValueError as e:
        logger.warning("get_price_chart: stock name resolution failed: {}", e)
        return err(str(e))
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_price_chart failed: {}", e)
        return err(f"生成价格图表失败：{e}")


@register_tool(
    name="get_sector_snapshot_chart",
    description="生成板块快照图（板块 vs 深证成指 分时对比 + 板块日线走势），返回 PNG 图片的公网 URL。"
    "适用：复盘某个板块当天表现、观察板块分时强弱与近阶段趋势。"
    "不适用：查看全市场时用 get_market_snapshot；查看个股时用 get_price_chart。",
    category="history",
    args_schema=GetSectorSnapshotChartArgs,
)
async def get_sector_snapshot_chart(sector: str, date: str) -> str:
    try:
        sector = _normalize_ticker(sector)
        market = get_ctx().market
        png_bytes = generate_sector_snapshot_chart(market, sector=sector, date=date)
        return ok(
            {
                "sector": sector,
                "date": date,
                "chart": upload_bytes(png_bytes, f"charts/sector/{sector}_{int(time.time() * 1000)}.png"),
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_sector_snapshot_chart failed: {}", e)
        return err(f"生成板块快照图失败：{e}")


@register_tool(
    name="get_multi_index_chart",
    description="生成四大指数（上证指数、深证成指、创业板指、科创50）最近120个交易日走势对比图（2×2排列），返回 PNG 图片的公网 URL。"
    "适用：多模态模型需要同时对比四大指数走势，判断相对强弱和市场风格。",
    category="market",
    args_schema=GetMultiIndexChartArgs,
)
async def get_multi_index_chart() -> str:
    try:
        market = get_ctx().market
        png_bytes = generate_multi_index_chart(market)
        return ok(
            {
                "chart": upload_bytes(png_bytes, f"charts/multi_index/{int(time.time() * 1000)}.png"),
                "indices": [
                    "上证指数 (000001.SH)",
                    "深证成指 (399001.SZ)",
                    "创业板指 (399006.SZ)",
                    "科创50 (000688.SH)",
                ],
                "period": "最近120个交易日",
            }
        )
    except Exception as e:
        if _is_transient_tool_error(e):
            raise
        logger.error("get_multi_index_chart failed: {}", e)
        return err(f"生成多指数对比图失败：{e}")
