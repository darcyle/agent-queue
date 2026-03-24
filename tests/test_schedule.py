"""Tests for schedule matching logic."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.schedule import (
    _cron_field_matches,
    _matches_days_of_month,
    _matches_days_of_week,
    _matches_times,
    describe_schedule,
    matches_schedule,
    parse_schedule,
)


# ---------------------------------------------------------------------------
# Time matching
# ---------------------------------------------------------------------------


class TestTimeMatching:
    def test_exact_time_match(self):
        """Time matches when current time equals target."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_within_tolerance(self):
        """Time matches when within tolerance window."""
        now = datetime(2026, 3, 23, 2, 0, 45, tzinfo=timezone.utc)
        assert _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_outside_tolerance(self):
        """Time does not match when outside tolerance window."""
        now = datetime(2026, 3, 23, 2, 2, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00"], now, tolerance_seconds=60)

    def test_multiple_times(self):
        """Matches any of multiple specified times."""
        now = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)
        assert _matches_times(["02:00", "14:30"], now, tolerance_seconds=60)

    def test_no_match_multiple(self):
        """No match when current time doesn't match any target."""
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00", "14:30"], now, tolerance_seconds=60)

    def test_midnight_tolerance(self):
        """Handles midnight wrap-around within tolerance."""
        now = datetime(2026, 3, 23, 23, 59, 30, tzinfo=timezone.utc)
        assert _matches_times(["00:00"], now, tolerance_seconds=60)

    def test_invalid_time_format(self):
        """Invalid time format is skipped gracefully."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not _matches_times(["invalid"], now, tolerance_seconds=60)

    def test_larger_tolerance(self):
        """Larger tolerance window captures more times."""
        now = datetime(2026, 3, 23, 2, 4, 0, tzinfo=timezone.utc)
        assert not _matches_times(["02:00"], now, tolerance_seconds=60)
        assert _matches_times(["02:00"], now, tolerance_seconds=300)


# ---------------------------------------------------------------------------
# Day-of-week matching
# ---------------------------------------------------------------------------


class TestDayOfWeekMatching:
    def test_match_by_name(self):
        """Match day by short name."""
        # 2026-03-23 is a Monday
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["mon"], now)

    def test_match_by_full_name(self):
        """Match day by full name."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["monday"], now)

    def test_no_match(self):
        """No match when day doesn't match."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not _matches_days_of_week(["tue", "wed"], now)

    def test_match_by_integer(self):
        """Match day by integer (0=Monday)."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert _matches_days_of_week([0], now)

    def test_multiple_days(self):
        """Match any of multiple days."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        assert _matches_days_of_week(["mon", "wed", "fri"], now)

    def test_case_insensitive(self):
        """Day names are case-insensitive."""
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_week(["Mon"], now)
        assert _matches_days_of_week(["MONDAY"], now)

    def test_weekend(self):
        """Saturday and Sunday matching."""
        sat = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)  # Saturday
        assert _matches_days_of_week(["sat"], sat)
        assert _matches_days_of_week([5], sat)
        sun = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc)  # Sunday
        assert _matches_days_of_week(["sun"], sun)


# ---------------------------------------------------------------------------
# Day-of-month matching
# ---------------------------------------------------------------------------


class TestDayOfMonthMatching:
    def test_match(self):
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([1, 15], now)

    def test_no_match(self):
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert not _matches_days_of_month([1, 15], now)

    def test_single_day(self):
        now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([1], now)

    def test_end_of_month(self):
        now = datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)
        assert _matches_days_of_month([31], now)


# ---------------------------------------------------------------------------
# Cron field matching
# ---------------------------------------------------------------------------


class TestCronFieldMatching:
    def test_wildcard(self):
        assert _cron_field_matches("*", 5, 0, 59)

    def test_exact(self):
        assert _cron_field_matches("5", 5, 0, 59)
        assert not _cron_field_matches("5", 6, 0, 59)

    def test_range(self):
        assert _cron_field_matches("1-5", 3, 0, 59)
        assert not _cron_field_matches("1-5", 6, 0, 59)

    def test_step(self):
        assert _cron_field_matches("*/15", 0, 0, 59)
        assert _cron_field_matches("*/15", 15, 0, 59)
        assert _cron_field_matches("*/15", 30, 0, 59)
        assert not _cron_field_matches("*/15", 10, 0, 59)

    def test_list(self):
        assert _cron_field_matches("1,3,5", 3, 0, 59)
        assert not _cron_field_matches("1,3,5", 4, 0, 59)

    def test_range_with_step(self):
        assert _cron_field_matches("1-10/2", 1, 0, 59)
        assert _cron_field_matches("1-10/2", 3, 0, 59)
        assert not _cron_field_matches("1-10/2", 2, 0, 59)
        assert not _cron_field_matches("1-10/2", 11, 0, 59)


# ---------------------------------------------------------------------------
# Full schedule matching
# ---------------------------------------------------------------------------


class TestScheduleMatching:
    def test_empty_schedule(self):
        """Empty schedule always matches."""
        assert matches_schedule({})

    def test_time_only(self):
        """Schedule with only time constraint."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"times": ["02:00"]}, now=now)

    def test_day_only(self):
        """Schedule with only day constraint."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday
        assert matches_schedule({"days_of_week": ["mon"]}, now=now)

    def test_combined_time_and_day(self):
        """Schedule with both time and day (AND logic)."""
        # Monday at 2am
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule(
            {"times": ["02:00"], "days_of_week": ["mon"]}, now=now
        )

    def test_combined_wrong_day(self):
        """Time matches but day doesn't → no match."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not matches_schedule(
            {"times": ["02:00"], "days_of_week": ["tue"]}, now=now
        )

    def test_combined_wrong_time(self):
        """Day matches but time doesn't → no match."""
        now = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)  # Monday
        assert not matches_schedule(
            {"times": ["02:00"], "days_of_week": ["mon"]}, now=now
        )

    def test_cron_simple(self):
        """Cron expression: every day at 2:00."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * *"}, now=now)

    def test_cron_wrong_time(self):
        """Cron expression doesn't match current time."""
        now = datetime(2026, 3, 23, 3, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * *"}, now=now)

    def test_cron_weekday_only(self):
        """Cron expression: weekdays only at 2am."""
        # Monday at 2am
        mon = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * 0-4"}, now=mon)

        # Saturday at 2am
        sat = datetime(2026, 3, 28, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * 0-4"}, now=sat)

    def test_dedup_same_minute(self):
        """Cron dedup: don't fire twice in the same minute."""
        now = datetime(2026, 3, 23, 2, 0, 30, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 5, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 2 * * *"}, now=now, last_run=last)

    def test_dedup_different_minute(self):
        """Cron fires if last run was in a different minute."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        last = datetime(2026, 3, 22, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 2 * * *"}, now=now, last_run=last)

    def test_time_dedup_same_window(self):
        """Time-based dedup: don't fire twice in the same time window."""
        now = datetime(2026, 3, 23, 2, 0, 30, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 10, tzinfo=timezone.utc)
        assert not matches_schedule(
            {"times": ["02:00"]}, now=now, last_run=last, tolerance_seconds=60
        )

    def test_time_dedup_different_window(self):
        """Fires at next matching time window."""
        now = datetime(2026, 3, 23, 14, 30, 0, tzinfo=timezone.utc)
        last = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule(
            {"times": ["02:00", "14:30"]}, now=now, last_run=last
        )

    def test_naive_datetime_handled(self):
        """Naive datetimes are treated as UTC."""
        now = datetime(2026, 3, 23, 2, 0, 0)  # no tzinfo
        assert matches_schedule({"times": ["02:00"]}, now=now)

    def test_every_third_day_at_3am(self):
        """Complex: 'every 3rd of month at 3 AM'."""
        now = datetime(2026, 3, 3, 3, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule(
            {"times": ["03:00"], "days_of_month": [3]}, now=now
        )
        # Wrong day
        now2 = datetime(2026, 3, 4, 3, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule(
            {"times": ["03:00"], "days_of_month": [3]}, now=now2
        )


# ---------------------------------------------------------------------------
# Cron expression matching (full 5-field)
# ---------------------------------------------------------------------------


class TestCronExpression:
    def test_every_15_minutes(self):
        """*/15 * * * * — every 15 minutes."""
        assert matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 15, 0, tzinfo=timezone.utc),
        )
        assert not matches_schedule(
            {"cron": "*/15 * * * *"},
            now=datetime(2026, 3, 23, 10, 7, 0, tzinfo=timezone.utc),
        )

    def test_specific_month(self):
        """0 0 1 1 * — midnight on Jan 1."""
        jan1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert matches_schedule({"cron": "0 0 1 1 *"}, now=jan1)

        mar1 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "0 0 1 1 *"}, now=mar1)

    def test_invalid_cron(self):
        """Invalid cron expression returns False."""
        now = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)
        assert not matches_schedule({"cron": "invalid"}, now=now)

    def test_list_in_cron(self):
        """0 2 * * 0,2,4 — Mon/Wed/Fri at 2am."""
        mon = datetime(2026, 3, 23, 2, 0, 0, tzinfo=timezone.utc)  # Monday=0
        assert matches_schedule({"cron": "0 2 * * 0,2,4"}, now=mon)

        tue = datetime(2026, 3, 24, 2, 0, 0, tzinfo=timezone.utc)  # Tuesday=1
        assert not matches_schedule({"cron": "0 2 * * 0,2,4"}, now=tue)


# ---------------------------------------------------------------------------
# parse_schedule / describe_schedule
# ---------------------------------------------------------------------------


class TestUtilities:
    def test_parse_schedule_present(self):
        trigger = {"type": "periodic", "interval_seconds": 3600, "schedule": {"times": ["02:00"]}}
        assert parse_schedule(trigger) == {"times": ["02:00"]}

    def test_parse_schedule_absent(self):
        trigger = {"type": "periodic", "interval_seconds": 3600}
        assert parse_schedule(trigger) is None

    def test_describe_time(self):
        desc = describe_schedule({"times": ["02:00", "14:30"]})
        assert "02:00" in desc
        assert "14:30" in desc

    def test_describe_days(self):
        desc = describe_schedule({"days_of_week": ["mon", "fri"]})
        assert "mon" in desc
        assert "fri" in desc

    def test_describe_cron(self):
        desc = describe_schedule({"cron": "0 2 * * *"})
        assert "Cron:" in desc

    def test_describe_empty(self):
        desc = describe_schedule({})
        assert "No schedule" in desc

    def test_describe_none(self):
        desc = describe_schedule(None)
        assert "No schedule" in desc


# ---------------------------------------------------------------------------
# Integration: schedule in hook tick (via HookEngine)
# ---------------------------------------------------------------------------


class TestHookEngineScheduleIntegration:
    """Test that HookEngine.tick() respects schedule constraints."""

    @pytest.fixture
    async def db(self, tmp_path):
        from src.database import Database
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        yield db
        await db.close()

    @pytest.fixture
    def bus(self):
        from src.event_bus import EventBus
        return EventBus()

    @pytest.fixture
    def config(self):
        from src.config import AppConfig, HookEngineConfig
        cfg = AppConfig()
        cfg.hook_engine = HookEngineConfig(enabled=True, max_concurrent_hooks=2)
        return cfg

    @pytest.fixture
    async def engine(self, db, bus, config):
        from src.hooks import HookEngine
        engine = HookEngine(db, bus, config)
        engine._orchestrator = MagicMock()
        engine._orchestrator._notify_channel = AsyncMock()
        engine._orchestrator.db = db
        engine._orchestrator.hooks = engine
        await engine.initialize()
        yield engine
        await engine.shutdown()

    async def _create_project(self, db, project_id="test-project"):
        from src.models import Project
        project = Project(id=project_id, name="Test Project")
        await db.create_project(project)
        return project

    async def _create_hook(self, db, **overrides):
        from src.models import Hook
        defaults = dict(
            id="sched-hook",
            project_id="test-project",
            name="sched-hook",
            enabled=True,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            context_steps='[{"type": "shell", "command": "echo test", "skip_llm_if_exit_zero": true}]',
            prompt_template="Test prompt",
            cooldown_seconds=5,
        )
        defaults.update(overrides)
        hook = Hook(**defaults)
        await db.create_hook(hook)
        return hook

    @pytest.mark.asyncio
    async def test_no_schedule_fires_normally(self, db, engine):
        """Hook without schedule fires when interval elapsed (backward compat)."""
        await self._create_project(db)
        hook = await self._create_hook(db)
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_matching_schedule_fires(self, db, engine):
        """Hook fires when schedule matches current time."""
        await self._create_project(db)

        # Create a schedule that matches "now" — use a time window around current time
        now = datetime.now(timezone.utc)
        time_str = f"{now.hour:02d}:{now.minute:02d}"

        hook = await self._create_hook(
            db,
            trigger=json.dumps({
                "type": "periodic",
                "interval_seconds": 10,
                "schedule": {"times": [time_str]},
            }),
        )
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_non_matching_schedule_skips(self, db, engine):
        """Hook does NOT fire when schedule doesn't match."""
        await self._create_project(db)

        # Create a schedule for a time that's definitely not now
        hook = await self._create_hook(
            db,
            trigger=json.dumps({
                "type": "periodic",
                "interval_seconds": 10,
                "schedule": {"times": ["99:99"]},  # Invalid / won't match
            }),
        )
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id not in engine._running

    @pytest.mark.asyncio
    async def test_wrong_day_skips(self, db, engine):
        """Hook skips when day-of-week doesn't match."""
        await self._create_project(db)

        # Pick a day that's definitely not today
        now = datetime.now(timezone.utc)
        wrong_day = "sat" if now.weekday() != 5 else "mon"

        hook = await self._create_hook(
            db,
            trigger=json.dumps({
                "type": "periodic",
                "interval_seconds": 10,
                "schedule": {"days_of_week": [wrong_day]},
            }),
        )
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id not in engine._running

    @pytest.mark.asyncio
    async def test_cron_schedule_fires(self, db, engine):
        """Hook fires when cron expression matches."""
        await self._create_project(db)

        # Build a cron that matches right now
        now = datetime.now(timezone.utc)
        cron = f"{now.minute} {now.hour} * * *"

        hook = await self._create_hook(
            db,
            trigger=json.dumps({
                "type": "periodic",
                "interval_seconds": 10,
                "schedule": {"cron": cron},
            }),
        )
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_cron_schedule_skips(self, db, engine):
        """Hook skips when cron doesn't match."""
        await self._create_project(db)

        # Build a cron for a time that's not now (1 hour from now)
        now = datetime.now(timezone.utc)
        diff_hour = (now.hour + 1) % 24

        hook = await self._create_hook(
            db,
            trigger=json.dumps({
                "type": "periodic",
                "interval_seconds": 10,
                "schedule": {"cron": f"0 {diff_hour} * * *"},
            }),
        )
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id not in engine._running
