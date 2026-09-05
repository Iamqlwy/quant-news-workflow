from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import settings


COLUMNS: list[tuple[str, str]] = [
    ("tasks", "reflection_at"),
    ("tasks", "created_at"),
    ("tasks", "updated_at"),
    ("price_monitors", "created_at"),
    ("price_monitors", "triggered_at"),
    ("triggers", "not_before"),
    ("triggers", "not_after"),
    ("triggers", "created_at"),
    ("triggers", "triggered_at"),
    ("crawler_state", "last_crawl_at"),
]


async def column_type(conn, table_name: str, column_name: str) -> str | None:
    result = await conn.execute(
        text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    row = result.first()
    return row[0] if row else None


async def migrate() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.begin() as conn:
            for table_name, column_name in COLUMNS:
                dtype = await column_type(conn, table_name, column_name)
                if dtype is None:
                    print(f"[skip] {table_name}.{column_name}: column not found")
                    continue
                if dtype == "timestamp with time zone":
                    print(f"[ok]   {table_name}.{column_name}: already timestamptz")
                    continue
                if dtype != "timestamp without time zone":
                    print(f"[skip] {table_name}.{column_name}: unexpected type {dtype}")
                    continue

                await conn.execute(
                    text(
                        f"""
                        ALTER TABLE "{table_name}"
                        ALTER COLUMN "{column_name}"
                        TYPE TIMESTAMP WITH TIME ZONE
                        USING "{column_name}" AT TIME ZONE 'UTC'
                        """
                    )
                )
                print(f"[done] {table_name}.{column_name}: converted to timestamptz")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
