"""SnapshotService —— 市场快照与个股日内快照。"""

from __future__ import annotations

from loguru import logger

import numpy as np
import pandas as pd

from src.core.clock import Clock
from src.market.compute.bars import calc_pct_chg
from src.market.data.cache import CacheManager
from src.market.services.bars import BarService
from src.market.types import SnapshotDict



class SnapshotService:
    """市场快照与个股日内快照。"""

    def __init__(self, cache: CacheManager, bar_svc: BarService, clock: Clock, klines_path: str) -> None:
        self._cache = cache
        self._bar_svc = bar_svc
        self._clock = clock
        self._klines_path = klines_path

    # ── 市场快照 ──

    def get_market_snapshot(self, date: str) -> SnapshotDict:
        """获取指定日期的全市场快照。"""
        today_str = self._clock.today_str
        date_compact = date.replace("-", "") if date else ""

        if date_compact == today_str:
            return self._today_snapshot(date, date_compact)
        return self._historical_snapshot(date_compact)

    def _today_snapshot(self, date: str, date_compact: str) -> SnapshotDict:
        """今日快照。"""
        if self._clock.is_realtime:
            return self._today_snapshot_realtime(date)
        return self._today_snapshot_simulation(date)

    def _today_snapshot_realtime(self, date: str) -> SnapshotDict:
        """实盘快照：从 xtquant 推送的 DailyTicker 直接计算。"""
        effective_day = self._bar_svc._effective_trading_day()
        if effective_day == self._clock.today_str:
            daily_all = self._cache.session.today_daily_ticker
        else:
            daily_all = self._cache.session.last_daily_ticker

        up = down = 0
        pct_chgs: list[float] = []
        total_amount = 0.0

        for _ticker, dt in daily_all.items():
            pct = calc_pct_chg(dt.close, dt.pre_close)
            if pct is not None:
                pct_chgs.append(pct)
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
            total_amount += dt.amount

        total = up + down
        return SnapshotDict(
            date=date,
            total_stocks=total,
            up_count=up,
            down_count=down,
            avg_pct_chg=round(sum(pct_chgs) / len(pct_chgs), 2) if pct_chgs else 0.0,
            total_amount=round(total_amount, 2),
        )

    def _today_snapshot_simulation(self, date: str) -> SnapshotDict:
        """模拟快照：盘前/盘后直接用日线，盘中用 1m 最新价。"""
        minutes = self._clock.minutes_since_midnight

        # 盘前（0-9:30）：昨日日线已是最终数据
        if minutes < 9 * 60 + 30:
            return self._snapshot_from_daily(date, self._cache.session.last_daily_ticker)

        # 盘后（15:00+）：今日日线已是最终数据
        if minutes >= 15 * 60:
            daily = self._cache.session.today_daily_ticker
            if daily:
                return self._snapshot_from_daily(date, daily)

        # 午休（11:31-12:59）：无实时数据，返回截止上午的 1m 快照
        if not self._clock.is_trading_session:
            minutes = min(minutes, 690)  # 截止到 11:30（上午收盘）

        # 盘中（9:30-15:00）：1m 最新价 + 昨日收盘
        return self._snapshot_from_1m(date, minutes)

    def _snapshot_from_daily(self, date: str, daily_all: dict) -> SnapshotDict:
        """从 DailyTicker 字典直接计算快照。"""
        up = down = 0
        pct_chgs: list[float] = []
        total_amount = 0.0

        for _ticker, dt in daily_all.items():
            pct = calc_pct_chg(dt.close, dt.pre_close)
            if pct is not None:
                pct_chgs.append(pct)
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
            total_amount += dt.amount

        total = up + down
        return SnapshotDict(
            date=date, total_stocks=total, up_count=up, down_count=down,
            avg_pct_chg=round(sum(pct_chgs) / len(pct_chgs), 2) if pct_chgs else 0.0,
            total_amount=round(total_amount, 2),
        )

    def _snapshot_from_1m(self, date: str, minutes: int) -> SnapshotDict:
        """从 1m 最新价 + 昨日收盘计算快照。

        使用 session 构建时预提取的 close/cum_amount numpy 数组，
        避免 per-ticker 的 pandas 索引和切片求和开销。
        """
        bar_index = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else min(minutes - (9 * 60 + 30), 119)

        close_arrays: dict = self._cache.session.adhoc.get("_close_arrays") or {}
        cum_amt_arrays: dict = self._cache.session.adhoc.get("_cum_amt_arrays") or {}
        last_daily = self._cache.session.last_daily_ticker

        up = down = 0
        pct_chgs: list[float] = []
        total_amount = 0.0

        for ticker, close_arr in close_arrays.items():
            last_dt = last_daily.get(ticker)
            if last_dt is None:
                continue

            idx = bar_index if bar_index < len(close_arr) else len(close_arr) - 1
            if idx < 0:
                continue

            latest_close = close_arr[idx]
            if np.isnan(latest_close):
                continue
            pct = calc_pct_chg(float(latest_close), last_dt.close)
            if pct is not None:
                pct_chgs.append(pct)
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1

            cum_amt = cum_amt_arrays.get(ticker)
            if cum_amt is not None and idx < len(cum_amt):
                total_amount += float(cum_amt[idx])

        total = up + down
        return SnapshotDict(
            date=date, total_stocks=total, up_count=up, down_count=down,
            avg_pct_chg=round(sum(pct_chgs) / len(pct_chgs), 2) if pct_chgs else 0.0,
            total_amount=round(total_amount, 2),
        )


    def _historical_snapshot(self, date_compact: str) -> SnapshotDict:
        """历史快照（从 CSV 文件读取）。"""
        from pathlib import Path

        kl = Path(self._klines_path)
        trading_days = self._cache.session.daily_window

        def _resolve_path(target: str):
            return kl / "extra" / "all_stocks_daily" / f"{target}.csv"

        csv_path = _resolve_path(date_compact)

        if not csv_path.exists():
            # 构建候选日期列表：优先用 session 的 trading_days，兜底扫描目录
            if trading_days:
                candidates = sorted([d for d in trading_days if d <= date_compact], reverse=True)
            else:
                logger.warning("no trading days found, fallback to scan directory")
                candidates = []
                try:
                    snap_dir = kl / "extra" / "all_stocks_daily"
                    if snap_dir.exists():
                        candidates = sorted(
                            [p.stem for p in snap_dir.glob("*.csv") if p.stem.isdigit() and p.stem <= date_compact],
                            reverse=True,
                        )
                except Exception:
                    pass

            for fallback in candidates:
                fallback_path = _resolve_path(fallback)
                if fallback_path.exists():
                    logger.debug("historical snapshot fallback to {} (requested {})", fallback, date_compact)
                    csv_path = fallback_path
                    date_compact = fallback
                    break

        if not csv_path.exists():
            logger.warning("historical snapshot file not found: {}", csv_path)
            return SnapshotDict(date=date_compact, total_stocks=0, up_count=0, down_count=0)

        df = pd.read_csv(csv_path)
        if df.empty:
            return SnapshotDict(date=date_compact, total_stocks=0, up_count=0, down_count=0)

        up_count = int((df["pct_chg"] > 0).sum()) if "pct_chg" in df.columns else 0
        down_count = int((df["pct_chg"] < 0).sum()) if "pct_chg" in df.columns else 0
        total = len(df)
        avg_pct = round(float(df["pct_chg"].mean()), 2) if "pct_chg" in df.columns else 0.0
        total_amt = round(float(df["amount"].sum()) / 10.0, 2) if "amount" in df.columns else 0.0  # 千元→万元（来源：tushare pro.daily）

        result: SnapshotDict = {
            "date": date_compact,
            "total_stocks": total,
            "up_count": up_count,
            "down_count": down_count,
            "avg_pct_chg": avg_pct,
            "total_amount": total_amt,
        }
        return result

    def _top_sectors_intraday(self, sector_type: str) -> list[dict]:
        """日内板块排名。"""
        if self._clock.is_realtime:
            return self._top_sectors_realtime(sector_type)
        return self._top_sectors_simulation(sector_type)

    def _top_sectors_realtime(self, sector_type: str) -> list[dict]:
        """实盘：从 DailyTicker 直接计算板块涨跌幅。"""
        classification = self._cache.session.classification
        clf_df = classification.get(sector_type)
        if clf_df is None or clf_df.empty:
            logger.debug("no classification data for sector_type={}", sector_type)
            return []

        effective_day = self._bar_svc._effective_trading_day()
        daily_all = (
            self._cache.session.today_daily_ticker
            if effective_day == self._clock.today_str
            else self._cache.session.last_daily_ticker
        )

        all_members = self._cache.session.all_members
        results: list[dict] = []

        for _, row in clf_df.iterrows():
            code = str(row.iloc[0])
            name = str(row.iloc[1]) if len(row) > 1 else code
            # 获取板块成员
            members = all_members[all_members["con_code"] == code]["ts_code"] if not all_members.empty else []
            if members.empty:
                continue

            pct_sum = 0.0
            count = 0
            for tkr in members:
                dt = daily_all.get(tkr)
                if dt is None:
                    continue
                pct = calc_pct_chg(dt.close, dt.pre_close)
                if pct is not None:
                    pct_sum += pct
                    count += 1

            if count > 0:
                results.append({"code": code, "name": name, "pct_chg": round(pct_sum / count, 2)})

        results.sort(key=lambda x: x["pct_chg"], reverse=True)
        return results[:6]

    def _top_sectors_simulation(self, sector_type: str) -> list[dict]:
        """模拟板块排名：盘前/盘后用日线，盘中用 1m 最新价。"""
        minutes = self._clock.minutes_since_midnight

        # 盘前/盘后：直接用日线
        if minutes < 9 * 60 + 30:
            return self._top_sectors_from_daily(sector_type, self._cache.session.last_daily_ticker)
        if minutes >= 15 * 60:
            daily = self._cache.session.today_daily_ticker
            if daily:
                return self._top_sectors_from_daily(sector_type, daily)

        # 盘中：1m 最新价
        return self._top_sectors_from_1m(sector_type, minutes)

    def _top_sectors_from_daily(self, sector_type: str, daily_all: dict) -> list[dict]:
        """从 DailyTicker 直接计算板块排名。"""
        classification = self._cache.session.classification
        clf_df = classification.get(sector_type)
        if clf_df is None or clf_df.empty:
            logger.debug("no classification data for sector_type={}", sector_type)
            return []

        all_members = self._cache.session.all_members
        results: list[dict] = []

        for _, row in clf_df.iterrows():
            code = str(row.iloc[0])
            name = str(row.iloc[1]) if len(row) > 1 else code
            members = all_members[all_members["con_code"] == code]["ts_code"] if not all_members.empty else []
            if members.empty:
                continue

            pct_sum = 0.0
            count = 0
            for tkr in members:
                dt = daily_all.get(tkr)
                if dt is None:
                    continue
                pct = calc_pct_chg(dt.close, dt.pre_close)
                if pct is not None:
                    pct_sum += pct
                    count += 1

            if count > 0:
                results.append({"code": code, "name": name, "pct_chg": round(pct_sum / count, 2)})

        results.sort(key=lambda x: x["pct_chg"], reverse=True)
        return results[:6]

    def _top_sectors_from_1m(self, sector_type: str, minutes: int) -> list[dict]:
        """从 1m 最新价 + 昨日收盘计算板块排名。"""
        classification = self._cache.session.classification
        clf_df = classification.get(sector_type)
        if clf_df is None or clf_df.empty:
            logger.debug("no classification data for sector_type={}", sector_type)
            return []

        all_members = self._cache.session.all_members
        results: list[dict] = []

        for _, row in clf_df.iterrows():
            code = str(row.iloc[0])
            name = str(row.iloc[1]) if len(row) > 1 else code
            members = all_members[all_members["con_code"] == code]["ts_code"] if not all_members.empty else []
            if members.empty:
                continue

            pct_sum = 0.0
            count = 0
            for tkr in members:
                last_dt = self._cache.session.last_daily_ticker.get(tkr)
                if last_dt is None:
                    continue
                prev_close = last_dt.close

                df_1m = self._cache.session.today_1m_ticker.get(tkr)
                if df_1m is None or df_1m.empty:
                    df_1m = self._cache.session.last_1m_ticker.get(tkr)
                    if df_1m is None or df_1m.empty:
                        continue

                idx = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else minutes - (9 * 60 + 30)
                idx = max(0, min(idx, len(df_1m) - 1))
                df_1m = df_1m.iloc[:idx + 1]

                latest_close = float(df_1m["close"].iloc[-1])
                pct = calc_pct_chg(latest_close, prev_close)
                if pct is not None:
                    pct_sum += pct
                    count += 1

            if count > 0:
                results.append({"code": code, "name": name, "pct_chg": round(pct_sum / count, 2)})

        results.sort(key=lambda x: x["pct_chg"], reverse=True)
        return results[:6]

