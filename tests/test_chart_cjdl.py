"""长电科技图表生成测试 —— clock 固定在 2026-05-26 14:59"""
from datetime import datetime, timedelta
from loguru import logger
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.market.charts import generate_price_chart, generate_technical_chart

TICKER = "600584.SH"
OUT_DIR = "tests/output"


def setup():
    """启用模拟模式，创建固定在 2026-05-26 14:59 的 clock 和 market provider"""
    settings.simulation_enabled = True

    start_dt = datetime(2026, 5, 27, 13, 0, 0)
    clock_config = TimeConfig(
        start_time=start_dt,
        tick_duration=timedelta(minutes=360),
        realtime=False,
    )
    clock = Clock(clock_config)
    market = MarketDataProvider(clock=clock)
    return clock, market


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)

    clock, market = setup()
    logger.info(f"clock.today = {clock.today.isoformat()}")
    logger.info(f"clock.now   = {clock.now.isoformat()}\n")

    # ── 价格走势图 ──
    from_date = "2026-01-01"
    to_date = "2026-05-27"

    logger.info(f"生成 {TICKER} 价格走势图 ({from_date} ~ {to_date}) ...")
    png_bytes = generate_price_chart(market, TICKER, from_date, to_date)
    price_path = os.path.join(OUT_DIR, f"{TICKER}_price_chart.png")
    with open(price_path, "wb") as f:
        f.write(png_bytes)
    logger.info(f"  -> {price_path} ({len(png_bytes):,} bytes)")

    # ── 技术指标面板图 ──
    logger.info(f"生成 {TICKER} 技术指标面板图 ({from_date} ~ {to_date}) ...")
    png_bytes = generate_technical_chart(market, TICKER, from_date, to_date)
    tech_path = os.path.join(OUT_DIR, f"{TICKER}_technical_chart.png")
    with open(tech_path, "wb") as f:
        f.write(png_bytes)
    logger.info(f"  -> {tech_path} ({len(png_bytes):,} bytes)")

    logger.info("\n完成。")


if __name__ == "__main__":
    main()
