"""Test the database dialect registry's PostgreSQL driver availability check"""

import sys

from apflow.core.storage.dialects import registry


class TestPostgresDriverAvailabilityCheck:
    """Regression: 'lazy' PostgreSQL registration previously always succeeded
    regardless of driver availability, since postgres.py has no driver
    imports of its own — the ImportError guard around it could never fire.
    This broke factory.py's SQLite fallback: get_dialect_config("postgresql")
    would report the dialect as available even with no driver installed, so
    the real failure only surfaced later as an unhandled ModuleNotFoundError
    deep inside SQLAlchemy's create_engine(). (Review #37)
    """

    def test_returns_true_when_drivers_installed(self):
        assert registry._postgres_drivers_available() is True

    def test_returns_false_when_psycopg2_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psycopg2", None)
        assert registry._postgres_drivers_available() is False

    def test_returns_false_when_asyncpg_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "asyncpg", None)
        assert registry._postgres_drivers_available() is False
