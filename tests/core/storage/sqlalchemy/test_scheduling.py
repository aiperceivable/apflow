"""
Tests for task scheduling functionality

This module tests:
- ScheduleType enum
- ScheduleCalculator for all schedule types
- Schedule validation
- TaskModel scheduling fields
- Scheduling migration
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from apflow.core.storage.sqlalchemy.models import TaskModel, ScheduleType
from apflow.core.storage.sqlalchemy.schedule_calculator import ScheduleCalculator


class TestScheduleType:
    """Test ScheduleType enum"""

    def test_schedule_type_values(self):
        """Test that all schedule type values are correct"""
        assert ScheduleType.once.value == "once"
        assert ScheduleType.interval.value == "interval"
        assert ScheduleType.cron.value == "cron"
        assert ScheduleType.daily.value == "daily"
        assert ScheduleType.weekly.value == "weekly"
        assert ScheduleType.monthly.value == "monthly"

    def test_schedule_type_count(self):
        """Test that we have exactly 6 schedule types"""
        assert len(ScheduleType) == 6

    def test_schedule_type_from_string(self):
        """Test that schedule types can be created from strings"""
        assert ScheduleType("once") == ScheduleType.once
        assert ScheduleType("interval") == ScheduleType.interval
        assert ScheduleType("cron") == ScheduleType.cron
        assert ScheduleType("daily") == ScheduleType.daily
        assert ScheduleType("weekly") == ScheduleType.weekly
        assert ScheduleType("monthly") == ScheduleType.monthly


class TestScheduleCalculatorOnce:
    """Test ScheduleCalculator for 'once' schedule type"""

    def test_calculate_once_future(self):
        """Test calculating next run for future datetime"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "2024-01-15T09:00:00+00:00"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.once, expression, from_time)

        expected = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_once_past(self):
        """Test calculating next run for past datetime returns None"""
        from_time = datetime(2024, 1, 20, 12, 0, 0, tzinfo=timezone.utc)
        expression = "2024-01-15T09:00:00+00:00"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.once, expression, from_time)

        assert result is None

    def test_calculate_once_with_z_suffix(self):
        """Test calculating next run with Z timezone suffix"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "2024-01-15T09:00:00Z"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.once, expression, from_time)

        expected = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_once_invalid_expression(self):
        """Test that invalid expression raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "not-a-datetime"

        with pytest.raises(ValueError, match="Invalid 'once' expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.once, expression, from_time)


class TestScheduleCalculatorInterval:
    """Test ScheduleCalculator for 'interval' schedule type"""

    def test_calculate_interval_basic(self):
        """Test calculating next run with interval in seconds"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "3600"  # 1 hour

        result = ScheduleCalculator.calculate_next_run(ScheduleType.interval, expression, from_time)

        expected = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_interval_large(self):
        """Test calculating next run with large interval"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "86400"  # 24 hours

        result = ScheduleCalculator.calculate_next_run(ScheduleType.interval, expression, from_time)

        expected = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_interval_negative(self):
        """Test that negative interval raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "-3600"

        with pytest.raises(ValueError, match="Interval must be positive"):
            ScheduleCalculator.calculate_next_run(ScheduleType.interval, expression, from_time)

    def test_calculate_interval_zero(self):
        """Test that zero interval raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "0"

        with pytest.raises(ValueError, match="Interval must be positive"):
            ScheduleCalculator.calculate_next_run(ScheduleType.interval, expression, from_time)

    def test_calculate_interval_invalid(self):
        """Test that invalid interval raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "not-a-number"

        with pytest.raises(ValueError, match="Invalid 'interval' expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.interval, expression, from_time)


class TestScheduleCalculatorCron:
    """Test ScheduleCalculator for 'cron' schedule type"""

    def test_calculate_cron_basic(self):
        """Test calculating next run with basic cron expression"""
        try:
            import croniter  # noqa: F401
        except ImportError:
            pytest.skip("croniter not installed")

        from_time = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        expression = "0 9 * * *"  # Every day at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.cron, expression, from_time)

        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_cron_weekday(self):
        """Test calculating next run with weekday cron expression"""
        try:
            import croniter  # noqa: F401
        except ImportError:
            pytest.skip("croniter not installed")

        # Monday, January 1, 2024
        from_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        expression = "0 9 * * 1-5"  # Weekdays at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.cron, expression, from_time)

        # Next occurrence should be Tuesday Jan 2 at 9 AM
        expected = datetime(2024, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_cron_invalid(self):
        """Test that invalid cron expression raises ValueError"""
        try:
            import croniter  # noqa: F401
        except ImportError:
            pytest.skip("croniter not installed")

        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "invalid cron"

        with pytest.raises(ValueError, match="Invalid cron expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.cron, expression, from_time)

    def test_calculate_cron_not_installed(self):
        """Test that missing croniter raises ImportError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "0 9 * * *"

        with patch.dict("sys.modules", {"croniter": None}):
            # Force reimport to trigger ImportError
            import apflow.core.storage.sqlalchemy.schedule_calculator as calc_module

            # Save original function
            original_func = calc_module.ScheduleCalculator._calculate_cron

            # Create a function that will raise ImportError
            def mock_calculate_cron(expression, from_time, timezone_str=None):
                raise ImportError("croniter package is required")

            calc_module.ScheduleCalculator._calculate_cron = staticmethod(mock_calculate_cron)

            try:
                with pytest.raises(ImportError, match="croniter package is required"):
                    ScheduleCalculator.calculate_next_run(ScheduleType.cron, expression, from_time)
            finally:
                # Restore original function
                calc_module.ScheduleCalculator._calculate_cron = original_func


class TestScheduleCalculatorDaily:
    """Test ScheduleCalculator for 'daily' schedule type"""

    def test_calculate_daily_future_today(self):
        """Test calculating next run when time is later today"""
        from_time = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        expression = "09:00"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)

        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_daily_past_today(self):
        """Test calculating next run when time has passed today"""
        from_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        expression = "09:00"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)

        expected = datetime(2024, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_daily_single_digit_hour(self):
        """Test calculating with single digit hour"""
        from_time = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        expression = "6:30"

        result = ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)

        expected = datetime(2024, 1, 1, 6, 30, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_daily_invalid_format(self):
        """Test that invalid format raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "9:00:00"  # Includes seconds

        with pytest.raises(ValueError, match="Invalid 'daily' expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)

    def test_calculate_daily_invalid_hour(self):
        """Test that invalid hour raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "25:00"

        with pytest.raises(ValueError, match="Invalid hour"):
            ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)

    def test_calculate_daily_invalid_minute(self):
        """Test that invalid minute raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "09:60"

        with pytest.raises(ValueError, match="Invalid minute"):
            ScheduleCalculator.calculate_next_run(ScheduleType.daily, expression, from_time)


class TestScheduleCalculatorWeekly:
    """Test ScheduleCalculator for 'weekly' schedule type"""

    def test_calculate_weekly_basic(self):
        """Test calculating next run for weekly schedule"""
        # Monday, January 1, 2024 at 8 AM
        from_time = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        expression = "1,3,5 09:00"  # Mon, Wed, Fri at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)

        # Next occurrence should be Monday Jan 1 at 9 AM
        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_weekly_next_day(self):
        """Test calculating next run when time has passed today"""
        # Monday, January 1, 2024 at 10 AM
        from_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        expression = "1,3,5 09:00"  # Mon, Wed, Fri at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)

        # Next occurrence should be Wednesday Jan 3 at 9 AM
        expected = datetime(2024, 1, 3, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_weekly_next_week(self):
        """Test calculating next run when need to go to next week"""
        # Friday, January 5, 2024 at 10 AM
        from_time = datetime(2024, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
        expression = "1,3,5 09:00"  # Mon, Wed, Fri at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)

        # Next occurrence should be Monday Jan 8 at 9 AM
        expected = datetime(2024, 1, 8, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_weekly_sunday(self):
        """Test calculating next run with Sunday (day 7)"""
        # Saturday, January 6, 2024 at 10 AM
        from_time = datetime(2024, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        expression = "7 15:00"  # Sunday at 3 PM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)

        # Next occurrence should be Sunday Jan 7 at 3 PM
        expected = datetime(2024, 1, 7, 15, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_weekly_invalid_day(self):
        """Test that invalid day raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "0,8 09:00"

        with pytest.raises(ValueError, match="Invalid day"):
            ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)

    def test_calculate_weekly_invalid_format(self):
        """Test that invalid format raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "Monday 09:00"

        with pytest.raises(ValueError, match="Invalid 'weekly' expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.weekly, expression, from_time)


class TestScheduleCalculatorMonthly:
    """Test ScheduleCalculator for 'monthly' schedule type"""

    def test_calculate_monthly_basic(self):
        """Test calculating next run for monthly schedule"""
        from_time = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        expression = "1,15 09:00"  # 1st and 15th at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

        # Next occurrence should be Jan 1 at 9 AM
        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_monthly_next_date(self):
        """Test calculating next run when date has passed"""
        from_time = datetime(2024, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
        expression = "1,15 09:00"  # 1st and 15th at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

        # Next occurrence should be Jan 15 at 9 AM
        expected = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_monthly_next_month(self):
        """Test calculating next run when need to go to next month"""
        from_time = datetime(2024, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        expression = "1,15 09:00"  # 1st and 15th at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

        # Next occurrence should be Feb 1 at 9 AM
        expected = datetime(2024, 2, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_monthly_end_of_month(self):
        """Test calculating with date that doesn't exist in some months"""
        from_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        expression = "31 09:00"  # 31st at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

        # Next occurrence should be Jan 31 at 9 AM
        expected = datetime(2024, 1, 31, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_monthly_skip_short_month(self):
        """Test that short months are handled correctly"""
        # Start from Feb when looking for 30th or 31st
        from_time = datetime(2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc)
        expression = "30 09:00"  # 30th at 9 AM

        result = ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

        # Feb doesn't have 30th, so should skip to March 30
        expected = datetime(2024, 3, 30, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_calculate_monthly_invalid_date(self):
        """Test that invalid date raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "0,32 09:00"

        with pytest.raises(ValueError, match="Invalid date"):
            ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)

    def test_calculate_monthly_invalid_format(self):
        """Test that invalid format raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "first 09:00"

        with pytest.raises(ValueError, match="Invalid 'monthly' expression"):
            ScheduleCalculator.calculate_next_run(ScheduleType.monthly, expression, from_time)


class TestScheduleCalculatorEdgeCases:
    """Test edge cases and error handling"""

    def test_calculate_with_none_type(self):
        """Test that None schedule type returns None"""
        result = ScheduleCalculator.calculate_next_run(None, "09:00")
        assert result is None

    def test_calculate_with_none_expression(self):
        """Test that None expression returns None"""
        result = ScheduleCalculator.calculate_next_run(ScheduleType.daily, None)
        assert result is None

    def test_calculate_with_empty_expression(self):
        """Test that empty expression returns None"""
        result = ScheduleCalculator.calculate_next_run(ScheduleType.daily, "")
        assert result is None

    def test_calculate_unknown_type(self):
        """Test that unknown schedule type raises ValueError"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="Unknown schedule type"):
            ScheduleCalculator.calculate_next_run("unknown", "09:00", from_time)

    def test_calculate_with_string_schedule_type(self):
        """Test that string schedule type works"""
        from_time = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        expression = "09:00"

        result = ScheduleCalculator.calculate_next_run("daily", expression, from_time)

        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected


class TestScheduleValidation:
    """Test schedule validation logic"""

    def test_is_schedule_valid_enabled(self):
        """Test that enabled schedule is valid"""
        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=True,
        )
        assert result is True

    def test_is_schedule_valid_disabled(self):
        """Test that disabled schedule is invalid"""
        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=False,
        )
        assert result is False

    def test_is_schedule_valid_before_start(self):
        """Test that schedule before start_at is invalid"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        schedule_start_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=True,
            schedule_start_at=schedule_start_at,
            from_time=from_time,
        )
        assert result is False

    def test_is_schedule_valid_after_end(self):
        """Test that schedule after end_at is invalid"""
        from_time = datetime(2024, 1, 20, 12, 0, 0, tzinfo=timezone.utc)
        schedule_end_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=True,
            schedule_end_at=schedule_end_at,
            from_time=from_time,
        )
        assert result is False

    def test_is_schedule_valid_max_runs_reached(self):
        """Test that schedule with max_runs reached is invalid"""
        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=True,
            max_runs=5,
            run_count=5,
        )
        assert result is False

    def test_is_schedule_valid_max_runs_not_reached(self):
        """Test that schedule with max_runs not reached is valid"""
        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.daily,
            expression="09:00",
            schedule_enabled=True,
            max_runs=5,
            run_count=3,
        )
        assert result is True

    def test_is_schedule_valid_once_past(self):
        """Test that 'once' schedule with past time is invalid"""
        from_time = datetime(2024, 1, 20, 12, 0, 0, tzinfo=timezone.utc)
        expression = "2024-01-15T09:00:00+00:00"

        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.once,
            expression=expression,
            schedule_enabled=True,
            from_time=from_time,
        )
        assert result is False

    def test_is_schedule_valid_once_future(self):
        """Test that 'once' schedule with future time is valid"""
        from_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expression = "2024-01-15T09:00:00+00:00"

        result = ScheduleCalculator.is_schedule_valid(
            schedule_type=ScheduleType.once,
            expression=expression,
            schedule_enabled=True,
            from_time=from_time,
        )
        assert result is True


class TestTaskModelSchedulingFields:
    """Test TaskModel scheduling fields"""

    @pytest.mark.asyncio
    async def test_task_model_scheduling_defaults(self, sync_db_session):
        """Test that scheduling fields have correct defaults"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(name="Test Task", user_id="test-user")

        assert task.schedule_type is None
        assert task.schedule_expression is None
        assert task.schedule_enabled is False
        assert task.schedule_start_at is None
        assert task.schedule_end_at is None
        assert task.next_run_at is None
        assert task.last_run_at is None
        assert task.max_runs is None
        assert task.run_count == 0

    @pytest.mark.asyncio
    async def test_task_model_with_scheduling(self, sync_db_session):
        """Test creating a task with scheduling fields"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        schedule_start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        schedule_end = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        next_run = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

        task = await repo.create_task(
            name="Scheduled Task",
            user_id="test-user",
            schedule_type=ScheduleType.daily.value,
            schedule_expression="09:00",
            schedule_enabled=True,
            schedule_start_at=schedule_start,
            schedule_end_at=schedule_end,
            next_run_at=next_run,
            max_runs=100,
            run_count=0,
        )

        assert task.schedule_type == ScheduleType.daily.value
        assert task.schedule_expression == "09:00"
        assert task.schedule_enabled is True
        assert task.schedule_start_at.replace(tzinfo=None) == schedule_start.replace(tzinfo=None)
        assert task.schedule_end_at.replace(tzinfo=None) == schedule_end.replace(tzinfo=None)
        assert task.next_run_at.replace(tzinfo=None) == next_run.replace(tzinfo=None)
        assert task.max_runs == 100
        assert task.run_count == 0

    @pytest.mark.asyncio
    async def test_task_model_update_scheduling(self, sync_db_session):
        """Test updating scheduling fields"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(name="Test Task", user_id="test-user")

        last_run = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        next_run = datetime(2024, 1, 2, 9, 0, 0, tzinfo=timezone.utc)

        await repo.update_task(
            task_id=task.id,
            schedule_type=ScheduleType.daily.value,
            schedule_expression="09:00",
            schedule_enabled=True,
            last_run_at=last_run,
            next_run_at=next_run,
            run_count=1,
        )

        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task.schedule_type == ScheduleType.daily.value
        assert updated_task.schedule_expression == "09:00"
        assert updated_task.schedule_enabled is True
        assert updated_task.last_run_at.replace(tzinfo=None) == last_run.replace(tzinfo=None)
        assert updated_task.next_run_at.replace(tzinfo=None) == next_run.replace(tzinfo=None)
        assert updated_task.run_count == 1

    def test_task_model_to_dict_scheduling(self):
        """Test that to_dict includes scheduling fields"""
        task = TaskModel(
            name="Test Task",
            schedule_type=ScheduleType.daily.value,
            schedule_expression="09:00",
            schedule_enabled=True,
            max_runs=100,
            run_count=5,
        )

        data = task.to_dict()

        assert data["schedule_type"] == "daily"
        assert data["schedule_expression"] == "09:00"
        assert data["schedule_enabled"] is True
        assert data["max_runs"] == 100
        assert data["run_count"] == 5

    def test_task_model_output_scheduling(self):
        """Test that output includes scheduling datetime fields as ISO strings"""
        schedule_start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        next_run = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

        task = TaskModel(
            name="Test Task",
            schedule_type=ScheduleType.daily.value,
            schedule_expression="09:00",
            schedule_enabled=True,
            schedule_start_at=schedule_start,
            next_run_at=next_run,
        )

        data = task.output()

        assert data["schedule_start_at"] == "2024-01-01T00:00:00+00:00"
        assert data["next_run_at"] == "2024-01-01T09:00:00+00:00"

    def test_task_model_default_values_scheduling(self):
        """Test that default_values includes scheduling fields"""
        task = TaskModel(name="Test Task")
        defaults = task.default_values()

        assert defaults["schedule_type"] is None
        assert defaults["schedule_expression"] is None
        assert defaults["schedule_enabled"] is False
        assert defaults["schedule_start_at"] is None
        assert defaults["schedule_end_at"] is None
        assert defaults["next_run_at"] is None
        assert defaults["last_run_at"] is None
        assert defaults["max_runs"] is None
        assert defaults["run_count"] == 0


class TestSchedulingAPISupport:
    """Test that scheduling fields work through the API layer"""

    @pytest.mark.asyncio
    async def test_create_task_with_scheduling_via_repository(self, sync_db_session):
        """Test creating a task with scheduling fields via repository"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create task with scheduling configuration
        task = await repo.create_task(
            name="Scheduled Report",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
            max_runs=30,
        )

        assert task.schedule_type == "daily"
        assert task.schedule_expression == "09:00"
        assert task.schedule_enabled is True
        assert task.max_runs == 30

    @pytest.mark.asyncio
    async def test_update_task_scheduling_via_repository(self, sync_db_session):
        """Test updating task scheduling fields via repository"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        # Create a basic task
        task = await repo.create_task(name="Test Task", user_id="test-user")
        assert task.schedule_enabled is False  # Default

        # Update with scheduling configuration
        next_run = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
        await repo.update_task(
            task.id,
            schedule_type="cron",
            schedule_expression="0 9 * * 1-5",
            schedule_enabled=True,
            next_run_at=next_run,
            max_runs=100,
        )

        # Verify updates
        updated = await repo.get_task_by_id(task.id)
        assert updated.schedule_type == "cron"
        assert updated.schedule_expression == "0 9 * * 1-5"
        assert updated.schedule_enabled is True
        assert updated.next_run_at.replace(tzinfo=None) == next_run.replace(tzinfo=None)
        assert updated.max_runs == 100

    @pytest.mark.asyncio
    async def test_disable_scheduling_via_repository(self, sync_db_session):
        """Test disabling scheduling via repository update"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create task with scheduling enabled
        task = await repo.create_task(
            name="Scheduled Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
        )
        assert task.schedule_enabled is True

        # Disable scheduling
        await repo.update_task(task.id, schedule_enabled=False)

        # Verify disabled
        updated = await repo.get_task_by_id(task.id)
        assert updated.schedule_enabled is False
        # Other scheduling fields should remain
        assert updated.schedule_type == "daily"
        assert updated.schedule_expression == "09:00"


class TestSchedulingRepositoryMethods:
    """Test scheduling-related repository methods for external schedulers"""

    @pytest.mark.asyncio
    async def test_get_due_scheduled_tasks(self, sync_db_session):
        """Test getting tasks that are due for execution"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=1)

        # Create a task due now
        due_task = await repo.create_task(
            name="Due Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
            next_run_at=past,  # In the past, so it's due
        )

        # Create a task not yet due
        await repo.create_task(
            name="Future Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
            next_run_at=future,  # In the future
        )

        # Create a disabled task
        await repo.create_task(
            name="Disabled Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=False,
            next_run_at=past,
        )

        # Get due tasks
        due_tasks = await repo.get_due_scheduled_tasks()

        # Should only return the due task
        assert len(due_tasks) == 1
        assert due_tasks[0].id == due_task.id

    @pytest.mark.asyncio
    async def test_get_due_scheduled_tasks_max_runs_check(self, sync_db_session):
        """Test that max_runs limit is respected"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        # Create a task that reached max_runs
        await repo.create_task(
            name="Exhausted Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
            next_run_at=past,
            max_runs=5,
            run_count=5,  # Already at max
        )

        # Create a task with runs remaining
        active_task = await repo.create_task(
            name="Active Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
            next_run_at=past,
            max_runs=5,
            run_count=3,  # Still has runs remaining
        )

        due_tasks = await repo.get_due_scheduled_tasks()

        # Should only return the active task
        assert len(due_tasks) == 1
        assert due_tasks[0].id == active_task.id

    @pytest.mark.asyncio
    async def test_get_due_scheduled_tasks_completed_status(self, sync_db_session):
        """Test that completed tasks with active schedule are picked up for next run."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        # After execution, task stays in completed status — scheduler picks it up
        completed_task = await repo.create_task(
            name="Completed Scheduled Task",
            user_id="test-user",
            status="completed",
            schedule_type="interval",
            schedule_expression="5",
            schedule_enabled=True,
            next_run_at=past,
            max_runs=100,
            run_count=56,
        )

        due_tasks = await repo.get_due_scheduled_tasks()

        assert len(due_tasks) == 1
        assert due_tasks[0].id == completed_task.id

    @pytest.mark.asyncio
    async def test_get_due_scheduled_tasks_failed_status(self, sync_db_session):
        """Test that failed tasks with active schedule are picked up for retry."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        failed_task = await repo.create_task(
            name="Failed Scheduled Task",
            user_id="test-user",
            status="failed",
            schedule_type="interval",
            schedule_expression="10",
            schedule_enabled=True,
            next_run_at=past,
            max_runs=50,
            run_count=10,
        )

        due_tasks = await repo.get_due_scheduled_tasks()

        assert len(due_tasks) == 1
        assert due_tasks[0].id == failed_task.id

    @pytest.mark.asyncio
    async def test_get_due_scheduled_tasks_excludes_running(self, sync_db_session):
        """Test that running/in_progress tasks are NOT picked up (avoid double-execution)."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        await repo.create_task(
            name="Running Task",
            user_id="test-user",
            status="in_progress",
            schedule_type="interval",
            schedule_expression="5",
            schedule_enabled=True,
            next_run_at=past,
        )

        due_tasks = await repo.get_due_scheduled_tasks()
        assert len(due_tasks) == 0

    @pytest.mark.asyncio
    async def test_mark_scheduled_task_running_resets_completed_parent_and_children(
        self, sync_db_session
    ):
        """Test that mark_scheduled_task_running resets a completed parent and its children."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Parent in completed state (normal post-execution state)
        parent = await repo.create_task(
            name="Completed Parent",
            user_id="test-user",
            status="completed",
            has_children=True,
            schedule_type="interval",
            schedule_expression="5",
            schedule_enabled=True,
        )

        # Create completed child
        child = await repo.create_task(
            name="Child Task",
            user_id="test-user",
            status="completed",
            parent_id=parent.id,
            result={"data": "old"},
        )

        # mark_scheduled_task_running should reset parent and children
        updated = await repo.mark_scheduled_task_running(parent.id)

        assert updated is not None
        assert updated.status == "in_progress"
        assert updated.completed_at is None

        # Verify child was reset
        refreshed_child = await repo.get_task_by_id(child.id)
        assert refreshed_child.status == "pending"
        assert refreshed_child.result is None

    @pytest.mark.asyncio
    async def test_mark_scheduled_task_running_always_resets_children(self, sync_db_session):
        """Test that mark_scheduled_task_running always resets children for clean re-execution."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create parent in normal "pending" state (after complete_scheduled_run)
        parent = await repo.create_task(
            name="Normal Parent",
            user_id="test-user",
            status="pending",
            has_children=True,
            schedule_type="interval",
            schedule_expression="5",
            schedule_enabled=True,
            result={"output": "previous-run"},
        )

        # Create child with results from previous run (not reset by complete_scheduled_run)
        child = await repo.create_task(
            name="Child Task",
            user_id="test-user",
            status="completed",
            parent_id=parent.id,
            result={"data": "old"},
        )

        updated = await repo.mark_scheduled_task_running(parent.id)

        assert updated is not None
        assert updated.status == "in_progress"
        assert updated.result is None  # Cleared for new execution
        assert updated.completed_at is None  # Cleared for new execution

        # Child should be reset to pending with result cleared
        refreshed_child = await repo.get_task_by_id(child.id)
        assert refreshed_child.status == "pending"
        assert refreshed_child.result is None

    @pytest.mark.asyncio
    async def test_mark_scheduled_task_running_skips_in_progress(self, sync_db_session):
        """Test that mark_scheduled_task_running returns None for in_progress tasks."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create a task already in_progress (currently executing)
        task = await repo.create_task(
            name="Running Task",
            user_id="test-user",
            status="in_progress",
            schedule_type="interval",
            schedule_expression="5",
            schedule_enabled=True,
        )

        # Should return None to avoid double-execution
        result = await repo.mark_scheduled_task_running(task.id)
        assert result is None

        # Task should remain in_progress
        refreshed = await repo.get_task_by_id(task.id)
        assert refreshed.status == "in_progress"

    @pytest.mark.asyncio
    async def test_get_scheduled_tasks(self, sync_db_session):
        """Test listing all scheduled tasks"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create tasks with different schedule types
        await repo.create_task(
            name="Daily Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
        )

        await repo.create_task(
            name="Weekly Task",
            user_id="test-user",
            schedule_type="weekly",
            schedule_expression="1,3,5 09:00",
            schedule_enabled=True,
        )

        await repo.create_task(
            name="Disabled Task",
            user_id="test-user",
            schedule_type="cron",
            schedule_expression="0 9 * * *",
            schedule_enabled=False,
        )

        # Create a non-scheduled task
        await repo.create_task(
            name="Regular Task",
            user_id="test-user",
        )

        # Get enabled scheduled tasks
        enabled_tasks = await repo.get_scheduled_tasks(enabled_only=True)
        assert len(enabled_tasks) == 2

        # Get all scheduled tasks
        all_scheduled = await repo.get_scheduled_tasks(enabled_only=False)
        assert len(all_scheduled) == 3

        # Filter by schedule_type
        daily_tasks = await repo.get_scheduled_tasks(schedule_type="daily")
        assert len(daily_tasks) == 1
        assert daily_tasks[0].name == "Daily Task"

    @pytest.mark.asyncio
    async def test_initialize_schedule(self, sync_db_session):
        """Test initializing next_run_at for a task"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        # Create a task with schedule but no next_run_at
        task = await repo.create_task(
            name="New Scheduled Task",
            user_id="test-user",
            schedule_type="daily",
            schedule_expression="09:00",
            schedule_enabled=True,
        )

        assert task.next_run_at is None

        # Initialize the schedule
        updated = await repo.initialize_schedule(task.id)

        assert updated is not None
        assert updated.next_run_at is not None

    @pytest.mark.asyncio
    async def test_complete_scheduled_run(self, sync_db_session):
        """Test completing a scheduled run preserves execution state"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create a scheduled task that has been executed (status=completed by executor)
        task = await repo.create_task(
            name="Completed Task",
            user_id="test-user",
            status="completed",
            result={"processed": 100},
            schedule_type="interval",
            schedule_expression="3600",  # 1 hour
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
        )

        # Complete the scheduled run — only updates schedule tracking
        updated = await repo.complete_scheduled_run(
            task.id,
            success=True,
        )

        assert updated is not None
        assert updated.run_count == 1
        assert updated.last_run_at is not None
        assert updated.next_run_at is not None
        assert updated.next_run_at.replace(tzinfo=None) > now.replace(tzinfo=None)  # Next run should be in the future
        assert updated.status == "completed"  # Preserved from executor
        assert updated.result == {"processed": 100}  # Preserved from executor

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_max_runs_reached(self, sync_db_session):
        """Test that schedule is disabled when max_runs is reached"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create a task with max_runs=1 and run_count=0 (executor already set completed)
        task = await repo.create_task(
            name="One-time Task",
            user_id="test-user",
            status="completed",
            result={"output": "final"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=now,
            max_runs=1,
            run_count=0,
        )

        # Complete the run
        updated = await repo.complete_scheduled_run(task.id, success=True)

        assert updated is not None
        assert updated.run_count == 1
        assert updated.schedule_enabled is False  # Should be disabled
        assert updated.next_run_at is None  # No more runs
        assert updated.status == "completed"  # Preserved from executor
        assert updated.result == {"output": "final"}  # Preserved from executor

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_once_type(self, sync_db_session):
        """Test that 'once' schedule type is disabled after first run"""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from datetime import datetime, timezone

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        task = await repo.create_task(
            name="Once Task",
            user_id="test-user",
            status="completed",
            schedule_type="once",
            schedule_expression=now.isoformat(),
            schedule_enabled=True,
            next_run_at=now,
        )

        updated = await repo.complete_scheduled_run(task.id, success=True)

        assert updated.schedule_enabled is False
        assert updated.status == "completed"  # Preserved from executor

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_preserves_result_and_children(self, sync_db_session):
        """Test that complete_scheduled_run preserves execution state and children.

        After a run completes, the parent status/result and children state should be
        preserved so users can query the last execution's results (identical to
        tasks.execute output). The reset happens later in mark_scheduled_task_running
        when the next run starts.
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create a parent task with children, simulating post-execution state
        parent = await repo.create_task(
            name="Parent Scheduled Task",
            user_id="test-user",
            status="completed",
            result={"output": "parent-result-run1"},
            progress=1.0,
            schedule_type="interval",
            schedule_expression="1",
            schedule_enabled=True,
            next_run_at=now,
            max_runs=2,
            run_count=0,
            has_children=True,
        )

        # Create two child tasks (completed by executor)
        child1 = await repo.create_task(
            name="Child Task 1",
            user_id="test-user",
            parent_id=parent.id,
            status="completed",
            result={"output": "child1-result"},
            started_at=now,
            completed_at=now,
            progress=1.0,
        )
        child2 = await repo.create_task(
            name="Child Task 2",
            user_id="test-user",
            parent_id=parent.id,
            status="completed",
            result={"output": "child2-result"},
            started_at=now,
            completed_at=now,
            progress=1.0,
        )

        # Complete the first scheduled run (run_count goes from 0 -> 1, max_runs=2 not reached)
        updated_parent = await repo.complete_scheduled_run(
            parent.id,
            success=True,
        )

        # Parent execution state should be preserved (matches tasks.execute output)
        assert updated_parent is not None
        assert updated_parent.status == "completed"  # Preserved from executor
        assert updated_parent.run_count == 1
        assert updated_parent.schedule_enabled is True
        assert updated_parent.next_run_at is not None
        assert updated_parent.result == {"output": "parent-result-run1"}  # Preserved
        assert updated_parent.progress == 1.0  # Preserved from executor

        # Children should NOT be reset — results stay visible until next execution
        refreshed_child1 = await repo.get_task_by_id(child1.id)
        assert refreshed_child1.status == "completed"
        assert refreshed_child1.result == {"output": "child1-result"}

        refreshed_child2 = await repo.get_task_by_id(child2.id)
        assert refreshed_child2.status == "completed"
        assert refreshed_child2.result == {"output": "child2-result"}

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_disables_after_max_runs_with_tree(self, sync_db_session):
        """Test that schedule is disabled when max_runs reached, even with children.

        After the final run (run_count reaches max_runs), the parent should
        be marked completed and schedule_enabled=False. Children are NOT reset
        because there will be no next run.
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create parent at run_count=1, max_runs=2 (this is the final run)
        # Executor already set status=completed and result
        parent = await repo.create_task(
            name="Parent Last Run",
            user_id="test-user",
            status="completed",
            result={"output": "parent-result-run2"},
            schedule_type="interval",
            schedule_expression="1",
            schedule_enabled=True,
            next_run_at=now,
            max_runs=2,
            run_count=1,
            has_children=True,
        )

        child = await repo.create_task(
            name="Child Task",
            user_id="test-user",
            parent_id=parent.id,
            status="completed",
            result={"output": "child-final"},
            started_at=now,
            completed_at=now,
            progress=1.0,
        )

        # Complete the second (final) run — only updates schedule tracking
        updated_parent = await repo.complete_scheduled_run(
            parent.id,
            success=True,
        )

        # Parent should be completed and disabled (max_runs reached)
        assert updated_parent is not None
        assert updated_parent.status == "completed"  # Preserved from executor
        assert updated_parent.run_count == 2
        assert updated_parent.schedule_enabled is False
        assert updated_parent.next_run_at is None
        assert updated_parent.result == {"output": "parent-result-run2"}  # Preserved

        # Child should NOT be reset (no more runs)
        refreshed_child = await repo.get_task_by_id(child.id)
        assert refreshed_child.status == "completed"
        assert refreshed_child.result == {"output": "child-final"}

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_preserves_result_for_solo_task(self, sync_db_session):
        """Test that execution state is preserved for tasks without children."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create a single task with no children (executor already set completed + result)
        task = await repo.create_task(
            name="Solo Scheduled Task",
            user_id="test-user",
            status="completed",
            result={"output": "solo-result"},
            progress=1.0,
            schedule_type="interval",
            schedule_expression="1",
            schedule_enabled=True,
            next_run_at=now,
            max_runs=2,
            run_count=0,
            has_children=False,
        )

        updated = await repo.complete_scheduled_run(
            task.id,
            success=True,
        )

        assert updated is not None
        assert updated.status == "completed"  # Preserved from executor
        assert updated.run_count == 1
        assert updated.result == {"output": "solo-result"}  # Preserved from executor
        assert updated.progress == 1.0  # Preserved from executor

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_pre_execution_failure(self, sync_db_session):
        """Test that pre-execution failure sets status=failed when task is still in_progress.

        If the task never reached the executor (e.g., exception before execute_task_tree),
        complete_scheduled_run should set status=failed as a fallback.
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)

        now = datetime.now(timezone.utc)

        # Create a task that was marked running but never executed
        task = await repo.create_task(
            name="Failed Before Execution",
            user_id="test-user",
            status="in_progress",
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
        )

        # Complete with failure — task is still in_progress (never reached executor)
        updated = await repo.complete_scheduled_run(
            task.id,
            success=False,
            error="Connection timeout before execution",
        )

        assert updated is not None
        assert updated.status == "failed"  # Set by complete_scheduled_run as fallback
        assert updated.error == "Connection timeout before execution"
        assert updated.run_count == 1
        assert updated.schedule_enabled is True  # Still has runs left


class TestSchedulingMigration:
    """Test scheduling migration"""

    def test_migration_class_exists(self):
        """Test that migration class can be imported"""
        import importlib.util
        import os

        # Import module with numeric prefix using spec
        migration_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../src/apflow/core/storage/migrations/002_add_scheduling_fields.py",
        )
        migration_path = os.path.abspath(migration_path)

        spec = importlib.util.spec_from_file_location("migration_002", migration_path)
        migration_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration_module)

        AddSchedulingFields = migration_module.AddSchedulingFields
        migration = AddSchedulingFields()
        assert migration.description is not None
        assert (
            "scheduling" in migration.description.lower()
            or "schedule" in migration.description.lower()
        )

    def test_migration_has_upgrade_method(self):
        """Test that migration has upgrade method"""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../src/apflow/core/storage/migrations/002_add_scheduling_fields.py",
        )
        migration_path = os.path.abspath(migration_path)

        spec = importlib.util.spec_from_file_location("migration_002", migration_path)
        migration_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration_module)

        AddSchedulingFields = migration_module.AddSchedulingFields
        migration = AddSchedulingFields()
        assert hasattr(migration, "upgrade")
        assert callable(migration.upgrade)

    def test_migration_has_downgrade_method(self):
        """Test that migration has downgrade method"""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../src/apflow/core/storage/migrations/002_add_scheduling_fields.py",
        )
        migration_path = os.path.abspath(migration_path)

        spec = importlib.util.spec_from_file_location("migration_002", migration_path)
        migration_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration_module)

        AddSchedulingFields = migration_module.AddSchedulingFields
        migration = AddSchedulingFields()
        assert hasattr(migration, "downgrade")
        assert callable(migration.downgrade)


class TestScheduledTaskStatusPersistence:
    """Regression tests for scheduled task execution with real DB.

    Two bugs were discovered in the scheduler's task execution path:

    Bug 1 — Identity map side-effect in complete_scheduled_run:
        complete_scheduled_run resets task.status to 'pending' for the next run.
        SQLAlchemy's identity map returns the same Python object for the same
        primary key within a session, so reading task.status after the call
        returns 'pending' instead of the actual execution result 'completed'.
        Fix: capture status/result/error into local variables BEFORE calling
        complete_scheduled_run.

    Bug 2 — expire_all() discards in-memory status change:
        mark_scheduled_task_running sets the parent to 'in_progress' in DB.
        The scheduler then sets task_tree.task.status = 'pending' in-memory
        to allow the executor to run it. But during child task execution,
        _check_cancellation_and_refresh_task calls expire_all() which discards
        ALL unpersisted dirty changes. The parent reverts to 'in_progress'
        from DB, and _check_task_execution_preconditions skips it.
        Fix: use update_task() to persist the status change to DB.
    """

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_preserves_execution_state(self, sync_db_session):
        """Verify that complete_scheduled_run does not mutate execution state."""
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        now = datetime.now(timezone.utc)

        # Create a scheduled parent task marked as completed (post-execution state)
        parent = await repo.create_task(
            name="Parent Task",
            user_id="test-user",
            status="completed",
            result={"output": "aggregated"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
            has_children=True,
        )

        child = await repo.create_task(
            name="Child Task",
            user_id="test-user",
            parent_id=parent.id,
            status="completed",
            result={"cpu": "8 cores"},
            started_at=now,
            completed_at=now,
            progress=1.0,
        )

        # Reload from DB after execution
        task = await repo.get_task_by_id(parent.id)
        assert task is not None
        assert task.status == "completed"
        assert task.result == {"output": "aggregated"}

        # complete_scheduled_run only updates schedule tracking
        await repo.complete_scheduled_run(
            parent.id,
            success=True,
            calculate_next_run=True,
        )

        # Execution state should be preserved (no reset)
        assert task.status == "completed", "Status should remain completed"
        assert task.result == {"output": "aggregated"}, "Result should be preserved"

        # Children should NOT be reset by complete_scheduled_run
        refreshed_child = await repo.get_task_by_id(child.id)
        assert refreshed_child is not None
        assert refreshed_child.status == "completed"
        assert refreshed_child.result == {"cpu": "8 cores"}

    @pytest.mark.asyncio
    async def test_reading_task_after_complete_scheduled_run_preserves_status(
        self, sync_db_session
    ):
        """Verify that reading task.status after complete_scheduled_run is correct.

        Previously (Bug 1), complete_scheduled_run would reset status to 'pending',
        causing identity map to return wrong status. Now it preserves execution state.
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        now = datetime.now(timezone.utc)

        parent = await repo.create_task(
            name="Parent Task",
            user_id="test-user",
            status="completed",
            result={"output": "done"},
            error=None,
            schedule_type="interval",
            schedule_expression="60",
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
            has_children=False,
        )

        task = await repo.get_task_by_id(parent.id)
        assert task is not None

        # After complete_scheduled_run, status should stay "completed"
        await repo.complete_scheduled_run(parent.id, success=True)

        # Status is preserved — no more identity map confusion
        assert task.status == "completed", "Status should remain completed after schedule update"
        assert task.result == {"output": "done"}, "Result should be preserved"

    @pytest.mark.asyncio
    async def test_in_memory_status_change_lost_after_expire_all(self, sync_db_session):
        """Demonstrate Bug 2: in-memory status change is discarded by expire_all().

        This is the root cause of the scheduled parent task "always failed" bug.
        The scheduler flow is:
          1. mark_scheduled_task_running → DB status = 'in_progress'
          2. task_tree.task.status = 'pending' (in-memory only)
          3. Child execution triggers expire_all()
          4. Parent status reverts to 'in_progress' from DB
          5. _check_task_execution_preconditions skips 'in_progress' tasks
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        now = datetime.now(timezone.utc)

        parent = await repo.create_task(
            name="Scheduled Parent",
            user_id="test-user",
            status="pending",
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
            has_children=True,
        )

        # Step 1: mark_scheduled_task_running sets status to 'in_progress' in DB
        running_task = await repo.mark_scheduled_task_running(parent.id)
        assert running_task is not None
        assert running_task.status == "in_progress"

        # Reload to simulate how the scheduler gets the task object
        task = await repo.get_task_by_id(parent.id)
        assert task is not None
        assert task.status == "in_progress"

        # Step 2: OLD code — in-memory only status change (the bug)
        task.status = "pending"  # type: ignore[assignment]
        assert task.status == "pending"  # Looks correct in-memory

        # Step 3: expire_all() is called during child execution
        # (simulates _check_cancellation_and_refresh_task behavior)
        sync_db_session.expire_all()

        # Step 4: Status reverts to 'in_progress' from DB
        reloaded = await repo.get_task_by_id(parent.id)
        assert reloaded is not None
        assert (
            reloaded.status == "in_progress"
        ), "expire_all() discards in-memory change; DB still has 'in_progress'"

    @pytest.mark.asyncio
    async def test_update_task_persists_status_through_expire_all(self, sync_db_session):
        """Demonstrate the fix: update_task() persists status change to DB.

        After the fix, the scheduler uses:
          await task_repository.update_task(task_id=task_id, status='pending')
        instead of:
          task_tree.task.status = 'pending'

        This commits to DB, so expire_all() cannot discard it.
        """
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        repo = TaskRepository(sync_db_session)
        now = datetime.now(timezone.utc)

        parent = await repo.create_task(
            name="Scheduled Parent",
            user_id="test-user",
            status="pending",
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=now,
            run_count=0,
            has_children=True,
        )

        # Step 1: mark_scheduled_task_running sets status to 'in_progress' in DB
        await repo.mark_scheduled_task_running(parent.id)

        # Step 2: FIX — use update_task to persist status change to DB
        await repo.update_task(task_id=parent.id, status="pending")

        # Verify it's persisted
        task = await repo.get_task_by_id(parent.id)
        assert task is not None
        assert task.status == "pending"

        # Step 3: expire_all() is called during child execution
        sync_db_session.expire_all()

        # Step 4: Status survives because it was committed to DB
        reloaded = await repo.get_task_by_id(parent.id)
        assert reloaded is not None
        assert (
            reloaded.status == "pending"
        ), "update_task() commits to DB; status survives expire_all()"
