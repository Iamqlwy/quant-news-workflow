"""SectorService —— 板块分析服务。"""

from __future__ import annotations

from typing import cast

from loguru import logger

import pandas as pd

from src.core.clock import Clock
from src.market.compute.bars import calc_pct_chg
from src.market.data.cache import CacheManager
from src.market.data.loader import load_concept_members_one
from src.market.services.bars import BarService
from src.market.services.price import PriceService
from src.market.types import SectorOverviewDict



class SectorService:
    """板块分析。"""

    def __init__(
        self,
        cache: CacheManager,
        bar_svc: BarService,
        price_svc: PriceService,
        clock: Clock,
        klines_path: str,
    ) -> None:
        self._cache = cache
        self._bar_svc = bar_svc
        self._price_svc = price_svc
        self._clock = clock
        self._klines_path = klines_path

    # ── 概念列表与成员 ──

    def get_concept_list(self, con_type: str = "all") -> list[dict]:
        """获取概念/行业/地域列表（从 session 预计算缓存读取）。"""
        all_list = self._cache.session.adhoc.get("concept_list")
        if not isinstance(all_list, list) or not all_list:
            return []
        typed_list = cast(list[dict], all_list)
        if con_type == "all":
            return typed_list
        return [c for c in typed_list if c.get("type") == con_type]

    def get_concept_members(self, concept_code: str) -> list[str]:
        """获取概念板块的成员股票列表。"""
        all_members = self._cache.session.all_members
        if all_members is not None and not all_members.empty and "con_code" in all_members.columns:
            # classification 中的 code 可能带 .TI 后缀，但 all_members 中不带
            clean_code = concept_code.replace(".TI", "")
            for test_code in (concept_code, clean_code):
                members = all_members[all_members["con_code"] == test_code]
                if not members.empty:
                    return members["ts_code"].astype(str).tolist()

        from pathlib import Path

        return load_concept_members_one(concept_code, Path(self._klines_path))

    def get_stock_concepts(self, ticker: str) -> dict:
        """获取股票所属的概念板块。"""
        classification = self._cache.session.classification
        stock_concepts_df = classification.get("stock_concepts")
        if stock_concepts_df is None or stock_concepts_df.empty:
            return {"concept": [], "industry": [], "region": []}

        if "ts_code" in stock_concepts_df.columns:
            matches = stock_concepts_df[stock_concepts_df["ts_code"] == ticker]
            return {
                "concept": matches[matches["type"] == "concept"]["con_name"].tolist() if "con_name" in matches.columns else [],
                "industry": matches[matches["type"] == "industry"]["con_name"].tolist() if "con_name" in matches.columns else [],
                "region": matches[matches["type"] == "region"]["con_name"].tolist() if "con_name" in matches.columns else [],
            }
        return {"concept": [], "industry": [], "region": []}

    # ── 板块概览 ──

    def get_sector_overview(self, sector: str) -> SectorOverviewDict:
        """获取板块概览。"""
        sector_code = self._resolve_sector_code(sector)
        if sector_code is None:
            return SectorOverviewDict(code=sector, name=sector, pct_chg=0, up_count=0, down_count=0, total_count=0)

        intraday = self.get_sector_intraday(sector_code)
        leader = self.get_sector_leader(sector_code)
        return SectorOverviewDict(
            code=sector_code,
            name=sector,
            pct_chg=intraday.get("pct_chg", 0),
            up_count=intraday.get("up_count", 0),
            down_count=intraday.get("down_count", 0),
            total_count=intraday.get("total_count", 0),
            turnover=intraday.get("turnover"),
            leader=leader,
        )

    def get_sector_intraday(self, sector_code: str, include_bars: bool = False) -> dict:
        """获取板块日内实时数据。

        实盘：直接读 DailyTicker（on_tick 实时更新）。
        模拟：盘前/盘后用日线，盘中用 1m 最新价。
        """
        members = self.get_concept_members(sector_code)
        if not members:
            logger.warning("get_sector_intraday: no members for {}", sector_code)
            return {"code": sector_code, "pct_chg": 0, "up_count": 0, "down_count": 0, "total_count": 0}

        # ── 实盘 ──
        if self._clock.is_realtime:
            effective_day = self._bar_svc._effective_trading_day()
            daily_all = (
                self._cache.session.today_daily_ticker
                if effective_day == self._clock.today_str
                else self._cache.session.last_daily_ticker
            )
            return self._sector_from_daily(sector_code, members, daily_all)

        # ── 模拟 ──
        minutes = self._clock.minutes_since_midnight
        # 盘前/盘后：直接用日线
        if minutes < 9 * 60 + 30:
            return self._sector_from_daily(sector_code, members, self._cache.session.last_daily_ticker)
        if minutes >= 15 * 60:
            daily = self._cache.session.today_daily_ticker
            if daily:
                return self._sector_from_daily(sector_code, members, daily)
        # 盘中：1m 最新价
        return self._sector_from_1m(sector_code, members, minutes)

    def _sector_from_daily(self, sector_code: str, members: list[str], daily_all: dict) -> dict:
        """从 DailyTicker 计算板块统计。"""
        pct_sum = 0.0
        up = down = count = 0
        today_vol = 0.0
        yesterday_vol = 0.0
        yesterday_turnover_sum = 0.0
        last_daily = self._cache.session.last_daily_ticker
        for tkr in members:
            dt = daily_all.get(tkr)
            if dt is None:
                continue
            pct = calc_pct_chg(dt.close, dt.pre_close)
            if pct is not None:
                pct_sum += pct
                count += 1
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
            today_vol += dt.volume
            ydt = last_daily.get(tkr)
            if ydt is not None:
                yesterday_vol += ydt.volume
                yesterday_turnover_sum += ydt.turnover_rate * ydt.volume

        turnover = None
        if yesterday_vol > 0 and today_vol > 0:
            avg_yesterday_turnover = yesterday_turnover_sum / yesterday_vol
            turnover = round(avg_yesterday_turnover * today_vol / yesterday_vol, 4)

        return {
            "code": sector_code,
            "pct_chg": round(pct_sum / count, 2) if count > 0 else 0.0,
            "up_count": up, "down_count": down, "total_count": len(members),
            "turnover": turnover,
        }

    def _sector_from_1m(self, sector_code: str, members: list[str], minutes: int) -> dict:
        """从 1m 最新价 + 昨日收盘计算板块统计。"""
        pct_sum = 0.0
        up = down = count = 0
        today_vol = 0.0
        yesterday_vol = 0.0
        yesterday_turnover_sum = 0.0
        for tkr in members:
            last_dt = self._cache.session.last_daily_ticker.get(tkr)
            if last_dt is None:
                continue

            df_1m = self._cache.session.today_1m_ticker.get(tkr)
            if df_1m is None or df_1m.empty:
                df_1m = self._cache.session.last_1m_ticker.get(tkr)
                if df_1m is None or df_1m.empty:
                    continue

            # 昨天数据只统计有 1m 数据的成员，保持分子分母范围一致
            yesterday_vol += last_dt.volume
            yesterday_turnover_sum += last_dt.turnover_rate * last_dt.volume

            idx = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else minutes - (9 * 60 + 30)
            idx = max(0, min(idx, len(df_1m) - 1))
            latest_close = float(df_1m["close"].iloc[idx])
            pct = calc_pct_chg(latest_close, last_dt.close)
            if pct is not None:
                pct_sum += pct
                count += 1
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
            today_vol += float(df_1m["volume"].iloc[: idx + 1].sum())

        turnover = None
        if yesterday_vol > 0:
            avg_yesterday_turnover = yesterday_turnover_sum / yesterday_vol
            turnover = round(avg_yesterday_turnover * today_vol / yesterday_vol, 4)

        return {
            "code": sector_code,
            "pct_chg": round(pct_sum / count, 2) if count > 0 else 0.0,
            "up_count": up, "down_count": down, "total_count": len(members),
            "turnover": turnover,
        }


    def get_sector_leader(self, sector_code: str) -> dict:
        """获取板块龙头（按流通市值+近1月涨幅排序）。"""
        members = self.get_concept_members(sector_code)
        if not members:
            logger.debug("get_sector_leader: no members for {}", sector_code)
            return {}

        # 取 ~21 个交易日前作为"一个月前"
        window = self._cache.session.daily_window
        month_ago_date = window[-21] if len(window) >= 21 else (window[0] if window else None)
        month_ago_close: dict[str, float] = {}
        if month_ago_date:
            month_ago_close = self._load_close_on_date(month_ago_date)

        candidates: list[dict] = []
        for ticker in members:
            last_dt = self._cache.get_last_daily(ticker)
            if last_dt is None:
                continue
            circ_mv = last_dt.circ_mv
            if not circ_mv or circ_mv <= 0:
                continue

            # 近1月涨幅
            m_close = month_ago_close.get(ticker, 0.0)
            month_ret = (last_dt.close - m_close) / m_close * 100 if m_close > 0 else 0.0

            candidates.append({"ticker": ticker, "circ_mv": circ_mv, "month_ret": month_ret})

        if not candidates:
            return {}

        # 按流通市值 + 近1月涨幅综合排名
        sorted_mv = sorted(candidates, key=lambda x: x["circ_mv"], reverse=True)
        sorted_ret = sorted(candidates, key=lambda x: x["month_ret"], reverse=True)

        rankings: dict[str, dict] = {}
        for i, c in enumerate(sorted_mv):
            rankings[c["ticker"]] = {"ticker": c["ticker"], "mv_rank": i + 1}
        for i, c in enumerate(sorted_ret):
            if c["ticker"] in rankings:
                rankings[c["ticker"]]["ret_rank"] = i + 1

        for _tkr, info in rankings.items():
            info["score"] = 2 * info.get("mv_rank", 1) + info.get("ret_rank", 1)

        leaders = sorted(rankings.values(), key=lambda x: x.get("score", 999))[:3]
        return {"leaders": leaders} if leaders else {}

    def _load_close_on_date(self, date_str: str) -> dict[str, float]:
        """从 all_stocks_daily CSV 读取指定日期的收盘价（带缓存）。"""
        cache_key = f"close_{date_str}"
        cached = self._cache.session.adhoc.get(cache_key)
        if cached is not None:
            return cached

        from pathlib import Path

        csv_path = Path(self._klines_path) / "extra" / "all_stocks_daily" / f"{date_str}.csv"
        if not csv_path.exists():
            return {}

        df = pd.read_csv(csv_path, usecols=["ts_code", "close"], dtype={"ts_code": str, "close": float})
        if df is None or df.empty:
            return {}
        result: dict[str, float] = {}
        for _, row in df.iterrows():
            result[str(row["ts_code"])] = float(row["close"])

        self._cache.session.adhoc[cache_key] = result
        return result

    def get_sector_volume_ratio(self, sector_code: str, n: int = 5) -> dict:
        """获取板块量比 = 今日成交量 / 近5日均量。"""
        members = self.get_concept_members(sector_code)
        if not members:
            return {"volume_ratio": None}

        volume_5d = self._cache.session.adhoc.get("volume_5d", {})

        # ── 今日量 ──
        if self._clock.is_realtime:
            daily_all = self._cache.session.today_daily_ticker
        else:
            minutes = self._clock.minutes_since_midnight
            if minutes >= 15 * 60:
                daily_all = self._cache.session.today_daily_ticker
            else:
                daily_all = self._cache.session.last_daily_ticker

        today_vol = 0.0
        avg_vol_sum = 0.0
        avg_vol_count = 0

        for tkr in members:
            dt = daily_all.get(tkr)
            if dt is not None:
                today_vol += dt.volume

            vols = volume_5d.get(tkr, [])
            if vols:
                avg_vol_sum += sum(vols[:n])
                avg_vol_count += min(len(vols), n)

        avg_vol = avg_vol_sum / avg_vol_count if avg_vol_count > 0 else 0.0
        ratio = today_vol / avg_vol if avg_vol > 0 else None
        return {"volume_ratio": round(ratio, 2) if ratio is not None else None}

    def _resolve_sector_code(self, sector: str) -> str | None:
        """将板块名称解析为代码（从 session 预计算索引 O(1) 查找）。"""
        name_to_code: dict = self._cache.session.adhoc.get("sector_name_to_code") or {}
        code = name_to_code.get(sector)
        if code:
            return code
        # 模糊匹配回退
        for name, code in name_to_code.items():
            if sector in name:
                return code
        return None
