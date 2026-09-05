from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.append(str(Path(__file__).parent.parent))

from src.core.timezone import BEIJING_TZ
from src.utils.datetime_utils import ensure_beijing_datetime


def test_ensure_beijing_datetime_from_iso_string() -> None:
    dt = ensure_beijing_datetime("2026-03-28T15:00:00+08:00", field_name="not_after")
    assert dt == datetime(2026, 3, 28, 15, 0, 0, tzinfo=BEIJING_TZ)


def test_ensure_beijing_datetime_from_naive_datetime() -> None:
    dt = ensure_beijing_datetime(datetime(2026, 3, 28, 15, 0, 0))
    assert dt == datetime(2026, 3, 28, 15, 0, 0, tzinfo=BEIJING_TZ)


def test_ensure_beijing_datetime_from_aware_datetime() -> None:
    src = datetime(2026, 3, 28, 23, 0, 0, tzinfo=timezone.utc)
    dt = ensure_beijing_datetime(src)
    assert dt == datetime(2026, 3, 29, 7, 0, 0, tzinfo=BEIJING_TZ)
