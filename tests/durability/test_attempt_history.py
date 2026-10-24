"""Failure history retains origin and usage separately from business output."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from apflow.core.execution.errors import http_error_info
from apflow.core.storage.sqlalchemy.models import Base
from apflow.durability.attempts import AttemptRecorder, list_execution_attempts


@pytest.mark.asyncio
async def test_attempt_history_persists_structured_failure_and_retry_time(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    engine = create_engine(f"sqlite:///{tmp_path / 'attempts.db'}")
    Base.metadata.create_all(engine)
    retry_at = datetime.now(timezone.utc)
    with Session(engine) as session:
        recorder = AttemptRecorder(session)
        record = await recorder.start("t", 2, 1)
        await recorder.finish(
            record, "failed", error_info=http_error_info(503, "GET"), token_usage={"total": 12}
        )
        await recorder.schedule_retry(record, retry_at)
        await recorder.finish(record, "completed", token_usage={"total": 0})
    with Session(engine) as session:
        rows = await list_execution_attempts("t", session)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_info"]["code"] == "HTTP_503"
        assert rows[0]["token_usage"] == {"total": 12}
        assert rows[0]["attempt_id"] == 2 and rows[0]["attempt_number"] == 1
        assert rows[0]["next_retry_at"] is not None
        assert await list_execution_attempts("other", session) == []
    engine.dispose()
