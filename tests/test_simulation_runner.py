from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.runners.simulation as simulation
from src.core.timezone import BEIJING_TZ
from src.runners.simulation import TRIGGER_ONLY_DEFAULT_START, SimulationRunner


class FakeClock:
    def __init__(self, now: datetime, tick_minutes: int = 180) -> None:
        self._now = now
        self._tick = timedelta(minutes=tick_minutes)

    @property
    def now(self) -> datetime:
        return self._now

    def reset_to(self, target: datetime) -> None:
        self._now = target

    def advance(self) -> None:
        self._now += self._tick

    def advance_by(self, delta: timedelta) -> None:
        self._now += delta


class FakeMarket:
    def __init__(self, trading_days: list[str]) -> None:
        self.trading_days = trading_days
        self.refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


class FakeTriggerEngine:
    def __init__(self) -> None:
        self._on_trigger = None
        self.evaluate_times: list[datetime] = []
        self.flush_count = 0

    async def _evaluate_all(self) -> int:
        self.evaluate_times.append(self.clock.now)
        return 0

    async def flush_pending(self) -> None:
        self.flush_count += 1


def make_runner(
    *,
    now: datetime,
    trading_days: list[str] | None = None,
    tick_minutes: int = 180,
) -> SimulationRunner:
    clock = FakeClock(now, tick_minutes=tick_minutes)
    engine = FakeTriggerEngine()
    engine.clock = clock
    components = SimpleNamespace(
        simulation_mode=True,
        clock=clock,
        market=FakeMarket(trading_days or []),
        trigger_engine=engine,
    )
    return SimulationRunner(components)


def dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING_TZ)


def test_ensure_trigger_only_start_defaults_to_20260524() -> None:
    runner = make_runner(now=dt(2026, 5, 23, 0, 0))

    runner._ensure_trigger_only_start()

    assert runner.clock.now == TRIGGER_ONLY_DEFAULT_START


def test_calculate_trading_windows_includes_1500_close_eval() -> None:
    runner = make_runner(now=dt(2026, 5, 25, 0, 0))

    windows = runner._calculate_trading_windows(
        dt(2026, 5, 25, 0, 0),
        dt(2026, 5, 25, 23, 59),
    )

    assert windows == [
        (dt(2026, 5, 25, 9, 30), dt(2026, 5, 25, 11, 30)),
        (dt(2026, 5, 25, 13, 0), dt(2026, 5, 25, 15, 1)),
    ]


@pytest.mark.asyncio
async def test_replay_triggers_in_window_evaluates_trading_minutes_only() -> None:
    runner = make_runner(now=dt(2026, 5, 25, 9, 29), trading_days=["20260525"])

    eval_count = await runner._replay_triggers_in_window(
        dt(2026, 5, 25, 9, 29),
        dt(2026, 5, 25, 9, 33),
    )

    assert eval_count == 3
    assert runner.trigger_engine.evaluate_times == [
        dt(2026, 5, 25, 9, 30),
        dt(2026, 5, 25, 9, 31),
        dt(2026, 5, 25, 9, 32),
    ]
    assert runner.trigger_engine.flush_count == 3
    assert runner.clock.now == dt(2026, 5, 25, 9, 33)


@pytest.mark.asyncio
async def test_replay_triggers_in_window_skips_non_trading_day() -> None:
    runner = make_runner(now=dt(2026, 5, 24, 9, 30), trading_days=[])

    eval_count = await runner._replay_triggers_in_window(
        dt(2026, 5, 24, 9, 30),
        dt(2026, 5, 24, 9, 35),
    )

    assert eval_count == 0
    assert runner.trigger_engine.evaluate_times == []
    assert runner.clock.now == dt(2026, 5, 24, 9, 35)


def test_install_trigger_only_callback_replaces_engine_callback() -> None:
    runner = make_runner(now=dt(2026, 5, 25, 9, 30))
    original_callback = object()
    runner.trigger_engine._on_trigger = original_callback

    runner._install_trigger_only_callback()

    assert runner.trigger_engine._on_trigger is not original_callback
    assert runner.trigger_engine._on_trigger.__self__ is runner
    assert runner.trigger_engine._on_trigger.__func__ is SimulationRunner._handle_trigger_only


@pytest.mark.asyncio
async def test_handle_trigger_only_updates_trigger_status(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = make_runner(now=dt(2026, 5, 25, 10, 0))
    calls: list[object] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, statement):
            calls.append(statement)

        async def commit(self):
            calls.append("commit")

    monkeypatch.setattr(simulation, "async_session", FakeSession)
    trigger = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="g1", action_type="sell", action_params={})

    await runner._handle_trigger_only(trigger)

    assert len(calls) == 2
    assert "UPDATE triggers SET status=:status, triggered_at=:triggered_at" in str(calls[0])
    assert calls[1] == "commit"
