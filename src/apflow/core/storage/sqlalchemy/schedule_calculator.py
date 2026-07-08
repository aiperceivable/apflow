"""
Schedule calculator for task scheduling

This module provides the ScheduleCalculator class for calculating next run times
based on different schedule types (once, interval, cron, daily, weekly, monthly).

Expression Formats:
- once: ISO datetime string (e.g., "2024-01-15T09:00:00Z")
- interval: Seconds as integer string (e.g., "3600" for 1 hour)
- cron: Standard cron expression (e.g., "0 9 * * 1-5" for 9 AM on weekdays)
- daily: HH:MM format (e.g., "09:00")
- weekly: "days HH:MM" format where days are 1-7 (1=Mon, 7=Sun), e.g., "1,3,5 09:00"
- monthly: "dates HH:MM" format where dates are 1-31, e.g., "1,15 09:00"
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import re

from apflow.core.storage.sqlalchemy.models import ScheduleType
from apflow.core.utils.helpers import parse_iso_datetime


class ScheduleCalculator:
    """
    Calculator for determining next run times based on schedule configuration.

    This class handles the calculation of next execution times for scheduled tasks
    based on their schedule type and expression.
    """

    @staticmethod
    def calculate_next_run(
        schedule_type: str,
        expression: str,
        from_time: Optional[datetime] = None,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate the next run time based on schedule type and expression.

        Args:
            schedule_type: The type of schedule (once, interval, cron, daily, weekly, monthly)
            expression: The schedule expression (format depends on schedule_type)
            from_time: The reference time to calculate from (default: now)
            timezone_str: Optional timezone string (e.g., "America/New_York")

        Returns:
            The next scheduled run time as a datetime, or None if invalid

        Raises:
            ValueError: If the schedule_type or expression is invalid
        """
        if not schedule_type or not expression:
            return None

        # Normalize schedule_type to string value
        if isinstance(schedule_type, ScheduleType):
            schedule_type = schedule_type.value

        # Get timezone-aware from_time
        if from_time is None:
            from_time = ScheduleCalculator._get_current_time(timezone_str)
        elif from_time.tzinfo is None and timezone_str:
            from_time = ScheduleCalculator._localize_time(from_time, timezone_str)

        # Dispatch to appropriate calculator method
        calculators = {
            ScheduleType.once.value: ScheduleCalculator._calculate_once,
            ScheduleType.interval.value: ScheduleCalculator._calculate_interval,
            ScheduleType.cron.value: ScheduleCalculator._calculate_cron,
            ScheduleType.daily.value: ScheduleCalculator._calculate_daily,
            ScheduleType.weekly.value: ScheduleCalculator._calculate_weekly,
            ScheduleType.monthly.value: ScheduleCalculator._calculate_monthly,
        }

        calculator = calculators.get(schedule_type)
        if calculator is None:
            raise ValueError(f"Unknown schedule type: {schedule_type}")

        return calculator(expression, from_time, timezone_str)

    @staticmethod
    def _get_current_time(timezone_str: Optional[str] = None) -> datetime:
        """Get current time, optionally in a specific timezone."""
        from datetime import timezone as tz

        now = datetime.now(tz.utc)

        if timezone_str:
            try:
                import pytz

                tz_obj = pytz.timezone(timezone_str)
                now = now.astimezone(tz_obj)
            except ImportError:
                pass  # pytz not available, use UTC
            except Exception:
                pass  # Invalid timezone, use UTC

        return now

    @staticmethod
    def _localize_time(dt: datetime, timezone_str: str) -> datetime:
        """Localize a naive datetime to a timezone."""
        try:
            import pytz

            tz_obj = pytz.timezone(timezone_str)
            if dt.tzinfo is None:
                return tz_obj.localize(dt)
            return dt.astimezone(tz_obj)
        except ImportError:
            from datetime import timezone as tz

            if dt.tzinfo is None:
                return dt.replace(tzinfo=tz.utc)
            return dt
        except Exception:
            from datetime import timezone as tz

            if dt.tzinfo is None:
                return dt.replace(tzinfo=tz.utc)
            return dt

    @staticmethod
    def _calculate_once(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'once' schedule type.

        Expression format: ISO datetime string (e.g., "2024-01-15T09:00:00Z")

        Returns the scheduled time if it's in the future, None otherwise.
        """
        try:
            # Parse ISO datetime
            scheduled_time = parse_iso_datetime(expression)

            # An expression with no explicit UTC offset parses as naive, which
            # would otherwise raise TypeError when compared against
            # from_time (always timezone-aware) below — caught by the
            # except clause and misreported as an invalid expression, even
            # though the expression itself is a valid ISO datetime.
            if scheduled_time.tzinfo is None:
                scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)

            # If the scheduled time is in the future, return it
            if scheduled_time > from_time:
                return scheduled_time

            return None
        except (ValueError, TypeError):
            raise ValueError(f"Invalid 'once' expression: {expression}. Expected ISO datetime.")

    @staticmethod
    def _calculate_interval(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'interval' schedule type.

        Expression format: Seconds as integer string (e.g., "3600" for 1 hour)

        Returns from_time + interval seconds.
        """
        try:
            seconds = int(expression)
            if seconds <= 0:
                raise ValueError(f"Interval must be positive: {expression}")

            return from_time + timedelta(seconds=seconds)
        except (ValueError, TypeError) as e:
            if "Interval must be positive" in str(e):
                raise
            raise ValueError(
                f"Invalid 'interval' expression: {expression}. Expected integer seconds."
            )

    @staticmethod
    def _calculate_cron(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'cron' schedule type.

        Expression format: Standard cron expression (e.g., "0 9 * * 1-5")

        Requires croniter package. Returns None if croniter is not installed.
        """
        try:
            from croniter import croniter

            # Create croniter instance
            cron = croniter(expression, from_time)

            # Get next run time
            next_time = cron.get_next(datetime)

            return next_time
        except ImportError:
            raise ImportError(
                "croniter package is required for cron scheduling. "
                "Install it with: pip install apflow[scheduling]"
            )
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid cron expression: {expression}. Error: {e}")

    @staticmethod
    def _calculate_daily(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'daily' schedule type.

        Expression format: HH:MM (e.g., "09:00")

        Returns the next occurrence of the specified time.
        """
        try:
            # Parse HH:MM format
            match = re.match(r"^(\d{1,2}):(\d{2})$", expression.strip())
            if not match:
                raise ValueError(
                    f"Invalid 'daily' expression: {expression}. Expected HH:MM format."
                )

            hour = int(match.group(1))
            minute = int(match.group(2))

            if hour < 0 or hour > 23:
                raise ValueError(f"Invalid hour in 'daily' expression: {hour}. Expected 0-23.")
            if minute < 0 or minute > 59:
                raise ValueError(f"Invalid minute in 'daily' expression: {minute}. Expected 0-59.")

            # Create target time for today
            target_time = from_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If target time is in the past, move to tomorrow
            if target_time <= from_time:
                target_time += timedelta(days=1)

            return target_time
        except ValueError as e:
            if "Invalid" in str(e):
                raise
            raise ValueError(f"Invalid 'daily' expression: {expression}. Expected HH:MM format.")

    @staticmethod
    def _calculate_weekly(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'weekly' schedule type.

        Expression format: "days HH:MM" where days are 1-7 (1=Mon, 7=Sun)
        Example: "1,3,5 09:00" for Monday, Wednesday, Friday at 9 AM

        Returns the next occurrence on one of the specified days.
        """
        try:
            # Parse expression: "days HH:MM"
            match = re.match(r"^([\d,]+)\s+(\d{1,2}):(\d{2})$", expression.strip())
            if not match:
                raise ValueError(
                    f"Invalid 'weekly' expression: {expression}. "
                    "Expected format: 'days HH:MM' (e.g., '1,3,5 09:00')"
                )

            days_str = match.group(1)
            hour = int(match.group(2))
            minute = int(match.group(3))

            # Parse days (1=Monday, 7=Sunday)
            days = []
            for day_str in days_str.split(","):
                day = int(day_str.strip())
                if day < 1 or day > 7:
                    raise ValueError(
                        f"Invalid day in 'weekly' expression: {day}. Expected 1-7 (1=Mon, 7=Sun)."
                    )
                days.append(day)

            if not days:
                raise ValueError("No valid days specified in 'weekly' expression.")

            if hour < 0 or hour > 23:
                raise ValueError(f"Invalid hour in 'weekly' expression: {hour}. Expected 0-23.")
            if minute < 0 or minute > 59:
                raise ValueError(f"Invalid minute in 'weekly' expression: {minute}. Expected 0-59.")

            # Sort days for consistent iteration
            days = sorted(set(days))

            # Get current weekday (1=Monday, 7=Sunday)
            from_time.isoweekday()

            # Find the next valid day
            for days_ahead in range(8):  # Check up to 7 days ahead
                check_date = from_time + timedelta(days=days_ahead)
                check_weekday = check_date.isoweekday()

                if check_weekday in days:
                    target_time = check_date.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )

                    # If it's today but the time has passed, skip to next occurrence
                    if days_ahead == 0 and target_time <= from_time:
                        continue

                    return target_time

            # Shouldn't reach here, but return None if no valid day found
            return None
        except ValueError as e:
            if "Invalid" in str(e) or "No valid" in str(e):
                raise
            raise ValueError(
                f"Invalid 'weekly' expression: {expression}. "
                "Expected format: 'days HH:MM' (e.g., '1,3,5 09:00')"
            )

    @staticmethod
    def _calculate_monthly(
        expression: str,
        from_time: datetime,
        timezone_str: Optional[str] = None,
    ) -> Optional[datetime]:
        """
        Calculate next run for 'monthly' schedule type.

        Expression format: "dates HH:MM" where dates are 1-31
        Example: "1,15 09:00" for 1st and 15th of each month at 9 AM

        Returns the next occurrence on one of the specified dates.
        """
        try:
            # Parse expression: "dates HH:MM"
            match = re.match(r"^([\d,]+)\s+(\d{1,2}):(\d{2})$", expression.strip())
            if not match:
                raise ValueError(
                    f"Invalid 'monthly' expression: {expression}. "
                    "Expected format: 'dates HH:MM' (e.g., '1,15 09:00')"
                )

            dates_str = match.group(1)
            hour = int(match.group(2))
            minute = int(match.group(3))

            # Parse dates (1-31)
            dates = []
            for date_str in dates_str.split(","):
                date = int(date_str.strip())
                if date < 1 or date > 31:
                    raise ValueError(
                        f"Invalid date in 'monthly' expression: {date}. Expected 1-31."
                    )
                dates.append(date)

            if not dates:
                raise ValueError("No valid dates specified in 'monthly' expression.")

            if hour < 0 or hour > 23:
                raise ValueError(f"Invalid hour in 'monthly' expression: {hour}. Expected 0-23.")
            if minute < 0 or minute > 59:
                raise ValueError(
                    f"Invalid minute in 'monthly' expression: {minute}. Expected 0-59."
                )

            # Sort dates for consistent iteration
            dates = sorted(set(dates))

            # Look for next valid date (check up to 12 months ahead)
            check_date = from_time
            for _ in range(366):  # Check up to a year ahead
                for target_date in dates:
                    try:
                        target_time = check_date.replace(
                            day=target_date, hour=hour, minute=minute, second=0, microsecond=0
                        )

                        if target_time > from_time:
                            return target_time
                    except ValueError:
                        # Invalid day for this month (e.g., Feb 30)
                        continue

                # Move to next month
                if check_date.month == 12:
                    check_date = check_date.replace(year=check_date.year + 1, month=1, day=1)
                else:
                    check_date = check_date.replace(month=check_date.month + 1, day=1)

            # Shouldn't reach here, but return None if no valid date found
            return None
        except ValueError as e:
            if "Invalid" in str(e) or "No valid" in str(e):
                raise
            raise ValueError(
                f"Invalid 'monthly' expression: {expression}. "
                "Expected format: 'dates HH:MM' (e.g., '1,15 09:00')"
            )

    @staticmethod
    def is_schedule_valid(
        schedule_type: str,
        expression: str,
        schedule_enabled: bool = True,
        schedule_start_at: Optional[datetime] = None,
        schedule_end_at: Optional[datetime] = None,
        max_runs: Optional[int] = None,
        run_count: int = 0,
        from_time: Optional[datetime] = None,
    ) -> bool:
        """
        Check if a schedule is still valid (should continue running).

        A schedule is invalid if:
        - schedule_enabled is False
        - Current time is before schedule_start_at
        - Current time is after schedule_end_at
        - max_runs is set and run_count >= max_runs
        - For 'once' type: the scheduled time has passed

        Args:
            schedule_type: The type of schedule
            expression: The schedule expression
            schedule_enabled: Whether the schedule is enabled
            schedule_start_at: Earliest time the schedule can trigger
            schedule_end_at: Latest time the schedule can trigger
            max_runs: Maximum number of runs (None = unlimited)
            run_count: Current number of runs
            from_time: Reference time for validation (default: now)

        Returns:
            True if the schedule is valid, False otherwise
        """
        # Check if schedule is enabled
        if not schedule_enabled:
            return False

        # Get current time if not provided
        if from_time is None:
            from datetime import timezone as tz

            from_time = datetime.now(tz.utc)

        # Check schedule boundaries
        if schedule_start_at and from_time < schedule_start_at:
            return False

        if schedule_end_at and from_time > schedule_end_at:
            return False

        # Check run count limit
        if max_runs is not None and run_count >= max_runs:
            return False

        # For 'once' type, check if the time has passed
        if schedule_type == ScheduleType.once.value or schedule_type == ScheduleType.once:
            try:
                next_run = ScheduleCalculator._calculate_once(expression, from_time)
                if next_run is None:
                    return False
            except (ValueError, TypeError):
                return False

        return True
