"""LimitTracker —— 涨跌停跟踪。"""

from __future__ import annotations

import re

from src.core.clock import Clock
from src.market.compute.bars import calc_pct_chg
from src.market.data.cache import CacheManager
from src.market.services.bars import BarService
from src.market.types import ZdtRecordDict


class LimitTracker:
    """涨跌停板跟踪。"""

    # 默认板块涨跌幅（ticker 前缀 → 比例），当 stock_basic 不可用时回退使用
    _DEFAULT_LIMIT_PCT: dict[str, float] = {"30": 0.20, "68": 0.20, "92": 0.30}

    def _get_limit_pct(self, ticker: str) -> float:
        """获取股票涨跌停比例。优先从 stock_basic 获取板块信息，回退到前缀匹配。"""
        pct = self._get_limit_pct_from_basic(ticker)
        if pct is not None:
            return pct
        return self._get_limit_pct_from_prefix(ticker)

    def _get_limit_pct_from_basic(self, ticker: str) -> float | None:
        """从 stock_basic 获取涨跌停比例。支持 ST/板块类型判断。"""
        basic = self._cache.session.stock_basic
        if basic is None or basic.empty or "ts_code" not in basic.columns:
            return None

        row = basic[basic["ts_code"] == ticker]
        if row.empty:
            return None

        info = row.iloc[0]
        name = str(info.get("name", ""))
        is_st = "ST" in name.upper()

        # 双创板块（创业板/科创板）ST 股涨跌停保持 20%
        for col in ("market", "board_type", "exchange"):
            if col in info.index:
                val = str(info.get(col, "")).upper()
                if val in ("GEM", "CHINEXT", "STAR", "KCB", "科创板", "创业板"):
                    return 0.20

        # 北交所 ST 30%
        for col in ("market", "board_type", "exchange"):
            if col in info.index:
                val = str(info.get(col, "")).upper()
                if val in ("BSE", "北交所", "BJ"):
                    return 0.30

        # 主板/中小板 ST 股 5%
        if is_st:
            return 0.05

        return None

    @staticmethod
    def _get_limit_pct_from_prefix(ticker: str) -> float:
        """回退方案：根据 ticker 前缀判断。"""
        for prefix, rate in LimitTracker._DEFAULT_LIMIT_PCT.items():
            if ticker.startswith(prefix):
                return rate
        return 0.10

    def __init__(self, cache: CacheManager, bar_svc: BarService, clock: Clock) -> None:
        self._cache = cache
        self._bar_svc = bar_svc
        self._clock = clock

    # ── 涨跌停记录 ──

    def get_zdt_record(self, ticker: str) -> ZdtRecordDict | None:
        """获取涨跌停记录。

        时段策略：
        - 盘前：直接从昨日 ZDT 数据查询
        - 盘中：实时从 1m bar 计算
        - 盘后：优先查今日 ZDT，没有则从今日数据实时计算
        """
        session = self._cache.session

        if self._clock.is_pre_market:
            return self._lookup_zdt(ticker, session.zdt_yesterday)

        if self._clock.is_post_market:
            record = self._lookup_zdt(ticker, session.zdt_today)
            if record is not None:
                return record
            # 今日 ZDT 文件尚未生成，降级到实时计算

        return self._compute_zdt_record(ticker)

    def _lookup_zdt(self, ticker: str, zdt_list: list[dict] | None) -> ZdtRecordDict | None:
        """在预加载的 ZDT 列表中查找 ticker。"""
        if not zdt_list:
            return None
        for rec in zdt_list:
            if rec.get("ticker") == ticker:
                return ZdtRecordDict(
                    ticker=ticker,
                    tag=str(rec.get("tag", "")),
                    board_type=str(rec.get("board_type", "")),
                    limit_type=str(rec.get("limit_type", "")),
                    pct_chg=float(rec.get("pct_chg", 0)),
                    limit_up_suc_rate=float(rec.get("limit_up_suc_rate", 0)),
                    is_limit=True,
                )
        return None

    def _compute_zdt_record(self, ticker: str) -> ZdtRecordDict | None:
        """从 1m bar 实时计算涨跌停状态（盘中/盘后通用）。"""
        df_1m = self._bar_svc.get_live_1m(ticker)
        if df_1m is None or df_1m.empty:
            return None

        last_dt = self._cache.get_last_daily(ticker)
        if last_dt is None:
            return None

        prev_close = last_dt.pre_close
        limit_pct = self._get_limit_pct(ticker)
        limit_up = round(prev_close * (1 + limit_pct), 2)
        limit_down = round(prev_close * (1 - limit_pct), 2)

        closes = df_1m["close"].to_numpy(dtype=float)
        latest = float(closes[-1])

        is_limit_up = latest >= limit_up - 0.01
        is_limit_down = latest <= limit_down + 0.01

        if not is_limit_up and not is_limit_down:
            return ZdtRecordDict(
                ticker=ticker,
                tag="",
                board_type="",
                limit_type="",
                limit_price=limit_up,
                prev_close=prev_close,
                is_limit=False,
            )

        limit_type = "涨停" if is_limit_up else "跌停"
        limit_price = limit_up if is_limit_up else limit_down

        limit_bars = df_1m[abs(closes - limit_price) < 0.01]
        first_limit_time = str(limit_bars.iloc[0]["timestamp"]) if not limit_bars.empty else ""
        limit_count = len(limit_bars)

        first_hit_idx = df_1m.index[abs(closes - limit_price) < 0.01].min() if limit_count > 0 else -1
        if first_hit_idx > 0:
            prior_volume = df_1m.iloc[:first_hit_idx]["volume"].sum()
            board_type = "换手板" if prior_volume > 0 else "一字板"
        else:
            board_type = "一字板" if limit_count > 0 else ""

        limit_suc_rate = round(limit_count / len(df_1m) * 100, 2) if len(df_1m) > 0 else 0.0

        prev_record = self._get_prev_zdt_record(ticker)
        tag = self._infer_tag(prev_record)

        pct_chg = round(calc_pct_chg(latest, prev_close) or 0.0, 2)

        return ZdtRecordDict(
            ticker=ticker,
            tag=tag,
            board_type=board_type,
            limit_type=limit_type,
            limit_price=limit_price,
            prev_close=prev_close,
            first_limit_time=first_limit_time,
            latest_price=latest,
            pct_chg=pct_chg,
            limit_up_suc_rate=limit_suc_rate,
            board_count=limit_count,
            days=1,
            is_limit=True,
        )


    def _get_prev_zdt_record(self, ticker: str) -> dict | None:
        """从前两个交易日的 zdt 记录中获取该 ticker 的涨停信息。"""
        session = self._cache.session
        for zdt_list in (session.zdt_yesterday, session.zdt_before_yesterday):
            if not zdt_list:
                continue
            for rec in zdt_list:
                if rec.get("ticker") == ticker:
                    return {"tag": rec.get("tag", ""), "board_type": rec.get("board_type", "")}
        return None

    @staticmethod
    def _infer_tag(prev_record: dict | None) -> str:
        """从昨日涨停记录推算今日 tag。"""
        if prev_record is None:
            return "首板"
        prev_tag = str(prev_record.get("tag", ""))
        if prev_tag == "首板":
            return "2天2板"
        m = re.match(r"^(\d+)天(\d+)板$", prev_tag)
        if m:
            n = int(m.group(1)) + 1
            board = int(m.group(2)) + 1
            return f"{n}天{board}板"
        return "首板"

    # ── 涨停股次日表现 ──

    def get_zdt_follow_through(self) -> dict:
        """计算昨日涨停股今日表现（赚钱效应指标）。

        直接从 SessionData 读取（build_session 时已预加载）。

        时段策略：
        - 盘前（is_pre_market）：无今日数据，退守计算 前天涨停→昨天表现
        - 盘中/盘后：正常计算 昨天涨停→今天表现

        返回:
          - zdt_follow_pct: 涨停股次日平均涨幅
          - zdt_follow_rate: 涨停股中上涨的比例
          - zdt_consecutive_rate: 连板率（继续涨停的比例）
        """
        session = self._cache.session
        zdt_limit_pct = self._DEFAULT_LIMIT_PCT

        if self._clock.is_pre_market:
            # 盘前：前天涨停 → 昨天表现（last_daily_ticker 是昨天的完整日线）
            zdt_source = session.zdt_before_yesterday
            ref_close_map = session.last_daily_ticker  # 昨天的 DailyTicker
        else:
            # 盘中/盘后：昨天涨停 → 今天表现
            zdt_source = session.zdt_yesterday
            ref_close_map = None  # 盘中用 1m 实时价，盘后用 today_daily

        if not zdt_source:
            return {"zdt_follow_pct": 0.0, "zdt_follow_rate": 0.0, "zdt_consecutive_rate": 0.0}

        limit_up_tickers = [r["ticker"] for r in zdt_source if r.get("limit_type") == "涨停"]
        if not limit_up_tickers:
            return {"zdt_follow_pct": 0.0, "zdt_follow_rate": 0.0, "zdt_consecutive_rate": 0.0}

        is_post_market = self._clock.is_post_market

        pct_chgs: list[float] = []
        consecutive_count = 0

        for ticker in limit_up_tickers:
            last_dt = self._cache.get_last_daily(ticker)
            if last_dt is None:
                continue

            if ref_close_map is not None:
                # 盘前模式：直接用昨日的 DailyTicker close
                ref_dt = ref_close_map.get(ticker)
                if ref_dt is not None:
                    pct = (ref_dt.close - last_dt.close) / last_dt.close * 100 if last_dt.close else 0.0
                else:
                    continue
            elif is_post_market:
                today_dt = session.today_daily_ticker.get(ticker)
                if today_dt is not None:
                    pct = (today_dt.close - last_dt.close) / last_dt.close * 100 if last_dt.close else 0.0
                else:
                    continue
            else:
                today_1m = session.today_1m_ticker.get(ticker)
                if today_1m is not None and not today_1m.empty:
                    latest_close = float(today_1m["close"].iloc[-1])
                    pct = (latest_close - last_dt.close) / last_dt.close * 100 if last_dt.close else 0.0
                else:
                    continue

            pct_chgs.append(pct)

            limit_pct = zdt_limit_pct.get(ticker[:4], 0.10)
            if pct >= limit_pct * 100 - 0.5:
                consecutive_count += 1

        if not pct_chgs:
            return {"zdt_follow_pct": 0.0, "zdt_follow_rate": 0.0, "zdt_consecutive_rate": 0.0}

        return {
            "zdt_follow_pct": round(sum(pct_chgs) / len(pct_chgs), 2),
            "zdt_follow_rate": round(sum(1 for p in pct_chgs if p > 0) / len(pct_chgs), 2),
            "zdt_consecutive_rate": round(consecutive_count / len(pct_chgs), 2),
        }
