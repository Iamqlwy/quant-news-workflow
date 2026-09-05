"""统计 tasks 表中每日 skipped 的数量和占比（按 updated_at 分组）"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=2,
    max_overflow=2,
    pool_timeout=10,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

SQL = """
SELECT
    updated_at::date AS day,
    COUNT(*) AS total,
    COALESCE(SUM((state = 'skipped')::int), 0) AS skipped
FROM tasks
GROUP BY 1
ORDER BY 1
"""


async def main():
    async with async_session() as db:
        result = await db.execute(text(SQL))
        rows = result.all()

    print(f"{'日期':<12} {'总数':>6} {'skipped':>8} {'占比':>8}")
    print("-" * 40)
    grand_total = 0
    grand_skipped = 0
    for day, total, skipped in rows:
        pct = f"{skipped / total * 100:.1f}%" if total else "0.0%"
        print(f"{str(day):<12} {total:>6} {skipped:>8} {pct:>7}")
        grand_total += total
        grand_skipped += skipped

    print("-" * 40)
    grand_pct = f"{grand_skipped / grand_total * 100:.1f}%" if grand_total else "0.0%"
    print(f"{'合计':<12} {grand_total:>6} {grand_skipped:>8} {grand_pct:>7}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
