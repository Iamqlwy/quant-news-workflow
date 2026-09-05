"""公共类型定义 —— 缓存结构、返回类型、TypedDict。

所有对外返回类型在此定义，避免裸 dict 传参。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

import pandas as pd

# ═══════════════════════════════════════════════════════════════════════════════
# Session 级缓存 —— refresh 时整体替换，跨 cycle 存活
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class DailyTicker:
    """日线数据。"""
    ts_code: str  # 股票代码
    open: float  # 开盘价
    close: float  # 收盘价
    high: float  # 最高价
    low: float  # 最低价
    pre_close: float  # 前收盘价
    volume: float  # 成交量
    amount: float  # 成交额
    volume_ratio: float  # 量比
    turnover_rate: float  # 换手率（%）
    turnover_rate_f: float  # 换手率（自由流通股）
    pe: float  # 市盈率（总市值/净利润， 亏损的PE为空）
    pe_ttm: float  # 市盈率（TTM，亏损的PE为空）
    pb: float  # 市净率（总市值/净资产）
    ps: float  # 市销率
    ps_ttm: float  # 市销率（TTM）
    dv_ratio: float  # 股息率 （%），除息日发生在去年期间的派现
    dv_ttm: float  # 股息率（TTM）（%），除息日在近12个月且分红报告期在12个月以内的派现
    total_share: float  # 总股本 （万股）
    float_share: float  # 流通股本 （万股）
    free_share: float  # 自由流通股本 （万）
    total_mv: float  # 总市值 （万元）
    circ_mv: float  # 流通市值（万元）
    timestamp: int  # 时间戳，单位毫秒



@dataclass
class SessionData:
    """refresh() 构建的只读行情数据集。

    所有数据在此已完成列名归一化和单位转换：
    - 成交额：万元
    - 成交量：万股
    - 金额，市值：万元
    - 时间：时间戳，单位毫秒
    """

    daily_window: list[str] = field(default_factory=list)  # 交易日窗口 YYYYMMDD

    # 个股，指数，概念全部统一，不再区分。key 为 ts_code
    today_daily_ticker: dict[str, DailyTicker] = field(default_factory=dict)  # ticker → 日线  没有就空着
    last_daily_ticker: dict[str, DailyTicker] = field(default_factory=dict)  # ticker → 日线   上一个交易日的

    last_1m_ticker: dict[str, pd.DataFrame] = field(default_factory=dict)  # ticker → 1m DataFrame  上一个交易日的
    today_1m_ticker: dict[str, pd.DataFrame] = field(default_factory=dict)  # ticker → 1m DataFrame 没有就空着

    classification: dict[str, pd.DataFrame] = field(default_factory=dict)  # concept/industry/region 分类
    all_members: pd.DataFrame = field(default_factory=pd.DataFrame)  # 概念成员 [con_code, ts_code]
    stock_basic: pd.DataFrame | None = None  # 股票基本信息
    hk_basic: pd.DataFrame | None = None  # 港股基本信息
    stock_name_history: pd.DataFrame | None = None  # 股票曾用名
    adhoc: dict[str, object] = field(default_factory=dict)  # 临时数据
    daily_history: dict[str, pd.DataFrame] = field(default_factory=dict)  # ticker -> 日线历史 DataFrame

    zdt_today: list[ZdtRecordDict] | None = None  # 今日ZDT记录，若无则空
    zdt_yesterday: list[ZdtRecordDict] | None = None  # 上个交易日ZDT记录，若无则空
    zdt_before_yesterday: list[ZdtRecordDict] | None = None  # 上上个交易日ZDT记录，若无则空






# ═══════════════════════════════════════════════════════════════════════════════
# 返回类型 TypedDict
# ═══════════════════════════════════════════════════════════════════════════════


class PriceDict(TypedDict, total=False):
    """实时价格返回类型。"""

    ticker: str
    price: float
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    pct_chg: float | None
    volume: float
    amount: float
    source: str
    available: bool


class SectorOverviewDict(TypedDict, total=False):
    code: str
    name: str
    pct_chg: float
    up_count: int
    down_count: int
    total_count: int
    turnover: float | None
    leader: dict


class MarketBreadthDict(TypedDict, total=False):
    up_count: int
    down_count: int
    flat_count: int
    total_count: int
    avg_pct_chg: float
    up_ratio: float
    total_amount: float


class ZdtRecordDict(TypedDict, total=False):
    ticker: str
    tag: str
    board_type: str
    limit_type: str  # "涨停池" / "连扳池" / "涨停" / "跌停"
    limit_price: float
    prev_close: float
    first_limit_time: str
    latest_price: float
    pct_chg: float
    limit_up_suc_rate: float
    board_count: int
    days: int
    is_limit: bool


class SnapshotDict(TypedDict, total=False):
    date: str
    total_stocks: int
    up_count: int
    down_count: int
    avg_pct_chg: float
    total_amount: float
    top_sectors_industry: list[dict]
    top_sectors_concept: list[dict]
