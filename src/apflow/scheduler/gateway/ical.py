"""
iCalendar (iCal) Export for Scheduled Tasks

Exports scheduled tasks in iCalendar format (.ics) for integration with
calendar applications like Google Calendar, Apple Calendar, Outlook, etc.

This enables users to:
- View scheduled tasks in their favorite calendar app
- Get notifications before task execution
- Share task schedules with team members

Usage:
    from apflow.scheduler.gateway import ICalExporter

    exporter = ICalExporter()
    ical_content = await exporter.export_tasks(user_id="user123")

    # Write to file
    with open("schedule.ics", "w") as f:
        f.write(ical_content)

    # Or serve via HTTP
    return Response(ical_content, media_type="text/calendar")
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from apflow.core.utils.helpers import parse_iso_datetime
from apflow.logger import get_logger

logger = get_logger(__name__)


# iCalendar format constants
ICAL_VERSION = "2.0"
ICAL_PRODID = "-//APFlow//Task Scheduler//EN"
ICAL_LINE_LENGTH = 75  # RFC 5545 recommends lines no longer than 75 octets


def fold_line(line: str) -> str:
    """
    Fold long lines according to RFC 5545.

    Lines longer than 75 characters should be folded with CRLF followed by a space.
    """
    if len(line) <= ICAL_LINE_LENGTH:
        return line

    result = []
    while len(line) > ICAL_LINE_LENGTH:
        result.append(line[:ICAL_LINE_LENGTH])
        line = " " + line[ICAL_LINE_LENGTH:]  # Continuation starts with space
    result.append(line)
    return "\r\n".join(result)


def escape_text(text: str) -> str:
    """
    Escape special characters in iCalendar text values.
    """
    if not text:
        return ""
    # Escape backslashes first, then other special chars
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def format_datetime(dt: datetime) -> str:
    """
    Format datetime for iCalendar.

    Converts to UTC and formats as YYYYMMDDTHHMMSSZ.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def generate_uid(task_id: str, next_run: Optional[datetime] = None) -> str:
    """
    Generate a unique identifier for an iCal event.

    The UID should be globally unique and stable for the same task/run.
    """
    base = f"apflow-{task_id}"
    if next_run:
        base += f"-{format_datetime(next_run)}"
    return f"{base}@apflow.local"


class ICalExporter:
    """
    Export scheduled tasks as iCalendar format.

    Supports:
    - Single task export
    - Multi-task export (calendar feed)
    - Recurring event rules (RRULE) for repeating schedules
    - Task details in event description
    """

    def __init__(
        self,
        calendar_name: str = "APFlow Tasks",
        include_description: bool = True,
        include_url: bool = True,
        base_url: Optional[str] = None,
        default_duration_minutes: int = 30,
    ):
        """
        Initialize iCal exporter.

        Args:
            calendar_name: Name for the calendar
            include_description: Include task details in description
            include_url: Include task URL in event
            base_url: Base URL for task links
            default_duration_minutes: Default event duration if not specified
        """
        self.calendar_name = calendar_name
        self.include_description = include_description
        self.include_url = include_url
        self.base_url = base_url
        self.default_duration_minutes = default_duration_minutes

    async def export_tasks(
        self,
        user_id: Optional[str] = None,
        schedule_type: Optional[str] = None,
        enabled_only: bool = True,
        limit: int = 100,
    ) -> str:
        """
        Export scheduled tasks as iCalendar string.

        Args:
            user_id: Filter by user ID
            schedule_type: Filter by schedule type
            enabled_only: Only include enabled schedules
            limit: Maximum number of tasks

        Returns:
            iCalendar formatted string
        """
        try:
            from apflow.core.storage import create_pooled_session
            from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)

                tasks = await task_repository.get_scheduled_tasks(
                    enabled_only=enabled_only,
                    user_id=user_id,
                    schedule_type=schedule_type,
                    limit=limit,
                    offset=0,
                )

                # Convert tasks to dicts
                task_dicts = [task.to_dict() for task in tasks]

                return self.generate_ical(task_dicts)

        except Exception as e:
            logger.error(f"Failed to export tasks: {e}", exc_info=True)
            raise

    def export_task(self, task: Dict[str, Any]) -> str:
        """
        Export a single task as iCalendar string.

        Args:
            task: Task dictionary

        Returns:
            iCalendar formatted string
        """
        return self.generate_ical([task])

    def generate_ical(self, tasks: List[Dict[str, Any]]) -> str:
        """
        Generate iCalendar content from task list.

        Args:
            tasks: List of task dictionaries

        Returns:
            iCalendar formatted string
        """
        lines = []

        # Calendar header
        lines.append("BEGIN:VCALENDAR")
        lines.append(f"VERSION:{ICAL_VERSION}")
        lines.append(fold_line(f"PRODID:{ICAL_PRODID}"))
        lines.append("CALSCALE:GREGORIAN")
        lines.append("METHOD:PUBLISH")
        lines.append(fold_line(f"X-WR-CALNAME:{escape_text(self.calendar_name)}"))

        # Generate events for each task
        for task in tasks:
            event_lines = self._generate_event(task)
            lines.extend(event_lines)

        # Calendar footer
        lines.append("END:VCALENDAR")

        # Join with CRLF (RFC 5545 requirement)
        return "\r\n".join(lines) + "\r\n"

    def _generate_event(self, task: Dict[str, Any]) -> List[str]:
        """
        Generate VEVENT for a task.

        Args:
            task: Task dictionary

        Returns:
            List of iCalendar lines for the event
        """
        lines = []

        task_id = task.get("id", str(uuid4()))
        task_name = task.get("name", "Unnamed Task")
        schedule_type = task.get("schedule_type")
        next_run_at = task.get("next_run_at")
        schedule_end_at = task.get("schedule_end_at")

        # Skip tasks without next_run_at
        if not next_run_at:
            return []

        # Parse datetime if string
        if isinstance(next_run_at, str):
            next_run_at = parse_iso_datetime(next_run_at)

        lines.append("BEGIN:VEVENT")

        # UID
        uid = generate_uid(task_id, next_run_at)
        lines.append(fold_line(f"UID:{uid}"))

        # Timestamps
        now = datetime.now(timezone.utc)
        lines.append(f"DTSTAMP:{format_datetime(now)}")
        lines.append(f"DTSTART:{format_datetime(next_run_at)}")

        # End time (default duration)
        end_time = next_run_at + timedelta(minutes=self.default_duration_minutes)
        lines.append(f"DTEND:{format_datetime(end_time)}")

        # Summary (title)
        lines.append(fold_line(f"SUMMARY:{escape_text(task_name)}"))

        # Description
        if self.include_description:
            description = self._generate_description(task)
            lines.append(fold_line(f"DESCRIPTION:{escape_text(description)}"))

        # URL
        if self.include_url and self.base_url:
            url = f"{self.base_url.rstrip('/')}/tasks/{task_id}"
            lines.append(fold_line(f"URL:{url}"))

        # Categories
        lines.append("CATEGORIES:APFlow,Task")
        if schedule_type:
            lines.append(f"CATEGORIES:{schedule_type.upper()}")

        # Status
        lines.append("STATUS:CONFIRMED")

        # Recurrence rule (if applicable)
        rrule = self._generate_rrule(task)
        if rrule:
            lines.append(fold_line(f"RRULE:{rrule}"))

            # UNTIL for schedule_end_at
            if schedule_end_at:
                if isinstance(schedule_end_at, str):
                    schedule_end_at = parse_iso_datetime(schedule_end_at)
                # Note: UNTIL is included in RRULE, not separate

        # Custom properties
        lines.append(f"X-APFLOW-TASK-ID:{task_id}")
        if schedule_type:
            lines.append(f"X-APFLOW-SCHEDULE-TYPE:{schedule_type}")

        lines.append("END:VEVENT")

        return lines

    def _generate_description(self, task: Dict[str, Any]) -> str:
        """
        Generate event description from task details.
        """
        parts = []

        parts.append(f"Task ID: {task.get('id', 'N/A')}")

        if task.get("status"):
            parts.append(f"Status: {task['status']}")

        if task.get("schedule_type"):
            parts.append(f"Schedule: {task['schedule_type']}")

        if task.get("schedule_expression"):
            parts.append(f"Expression: {task['schedule_expression']}")

        if task.get("run_count") is not None:
            max_runs = task.get("max_runs")
            if max_runs:
                parts.append(f"Runs: {task['run_count']}/{max_runs}")
            else:
                parts.append(f"Runs: {task['run_count']}")

        if task.get("inputs"):
            parts.append(f"Inputs: {task['inputs']}")

        # A real newline here, not a literal "\n" — escape_text() below turns
        # actual newlines into the correctly-escaped "\n" sequence. Joining
        # with a literal backslash-n instead double-escapes it into "\\n",
        # which calendar apps render as literal text, not a line break.
        return "\n".join(parts)

    def _generate_rrule(self, task: Dict[str, Any]) -> Optional[str]:
        """
        Generate RRULE (recurrence rule) for repeating schedules.

        Returns:
            RRULE string or None if not recurring
        """
        schedule_type = task.get("schedule_type")
        schedule_expression = task.get("schedule_expression")
        schedule_end_at = task.get("schedule_end_at")
        max_runs = task.get("max_runs")
        run_count = task.get("run_count", 0)

        if not schedule_type or schedule_type == "once":
            return None

        parts = []

        if schedule_type == "interval":
            # Interval in seconds - convert to appropriate frequency
            try:
                seconds = int(schedule_expression)
                if seconds >= 86400 and seconds % 86400 == 0:
                    # Daily interval
                    days = seconds // 86400
                    parts.append("FREQ=DAILY")
                    if days > 1:
                        parts.append(f"INTERVAL={days}")
                elif seconds >= 3600 and seconds % 3600 == 0:
                    # Hourly interval
                    hours = seconds // 3600
                    parts.append("FREQ=HOURLY")
                    if hours > 1:
                        parts.append(f"INTERVAL={hours}")
                elif seconds >= 60 and seconds % 60 == 0:
                    # Minutely interval
                    minutes = seconds // 60
                    parts.append("FREQ=MINUTELY")
                    if minutes > 1:
                        parts.append(f"INTERVAL={minutes}")
                else:
                    # Secondly (rare in calendars)
                    parts.append("FREQ=SECONDLY")
                    if seconds > 1:
                        parts.append(f"INTERVAL={seconds}")
            except (ValueError, TypeError):
                return None

        elif schedule_type == "daily":
            parts.append("FREQ=DAILY")

        elif schedule_type == "weekly":
            # Format: "1,3,5 HH:MM" (days + time)
            # Days: 1=Monday ... 7=Sunday
            parts.append("FREQ=WEEKLY")
            if schedule_expression:
                try:
                    day_part = schedule_expression.split()[0]
                    days = [int(d) for d in day_part.split(",")]
                    # Convert to iCal day names
                    day_names = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}
                    byday = ",".join(day_names.get(d, "") for d in days if d in day_names)
                    if byday:
                        parts.append(f"BYDAY={byday}")
                except (ValueError, IndexError):
                    pass

        elif schedule_type == "monthly":
            # Format: "1,15 HH:MM" (dates + time)
            parts.append("FREQ=MONTHLY")
            if schedule_expression:
                try:
                    date_part = schedule_expression.split()[0]
                    dates = [int(d) for d in date_part.split(",")]
                    bymonthday = ",".join(str(d) for d in dates)
                    if bymonthday:
                        parts.append(f"BYMONTHDAY={bymonthday}")
                except (ValueError, IndexError):
                    pass

        elif schedule_type == "cron":
            # Cron expressions are complex - generate a basic approximation
            # Full cron support would require parsing the expression
            # For now, just mark it as daily (most common case)
            parts.append("FREQ=DAILY")
            # Add comment that this is simplified
            # (iCal doesn't support cron natively)

        if not parts:
            return None

        # Add UNTIL or COUNT
        if schedule_end_at:
            if isinstance(schedule_end_at, str):
                schedule_end_at = parse_iso_datetime(schedule_end_at)
            parts.append(f"UNTIL={format_datetime(schedule_end_at)}")
        elif max_runs:
            remaining = max_runs - run_count
            if remaining > 0:
                parts.append(f"COUNT={remaining}")

        return ";".join(parts)


def generate_ical_feed_url(
    base_url: str,
    user_id: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    Generate URL for subscribing to iCal feed.

    This URL can be added to calendar applications for live updates.

    Args:
        base_url: Base URL of the apflow API
        user_id: Optional user ID filter
        api_key: Optional API key for authentication

    Returns:
        iCal feed subscription URL
    """
    url = f"{base_url.rstrip('/')}/scheduler/ical"

    params = []
    if user_id:
        params.append(f"user_id={user_id}")
    if api_key:
        params.append(f"api_key={api_key}")

    if params:
        url += "?" + "&".join(params)

    return url
