"""Fixtures for explicitly enabled disposable PostgreSQL acceptance databases."""

from collections.abc import Iterator
import os

import pytest

from tests.integration.postgres_helpers import disposable_postgres_database


@pytest.fixture
def postgres_database_url() -> Iterator[str]:
    """Keep live checks opt-in and isolated from application databases."""
    configured_url = os.getenv("APFLOW_TEST_POSTGRES_URL")
    if not configured_url:
        pytest.skip("APFLOW_TEST_POSTGRES_URL is required for live PostgreSQL acceptance tests")
    with disposable_postgres_database(configured_url) as database_url:
        yield database_url
