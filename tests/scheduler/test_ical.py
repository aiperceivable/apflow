"""Tests for ICalExporter (apflow.scheduler.gateway.ical)."""

from apflow.scheduler.gateway.ical import ICalExporter, escape_text


class TestGenerateDescription:
    def test_description_uses_real_newline_not_literal_backslash_n(self):
        """Regression: _generate_description joined its parts with the
        literal two-character sequence '\\n' (backslash + n) instead of a
        real newline character. escape_text's backslash-doubling step then
        turned that literal backslash into two backslashes, so calendar
        apps rendered '\\n' as text instead of a line break. (Review
        CRITICAL #66)
        """
        exporter = ICalExporter()
        description = exporter._generate_description({"id": "t1", "status": "pending"})
        assert "\n" in description
        assert "\\n" not in description

    def test_escaped_description_has_single_backslash_before_n(self):
        exporter = ICalExporter()
        description = exporter._generate_description({"id": "t1", "status": "pending"})
        escaped = escape_text(description)
        assert "\\n" in escaped
        assert "\\\\n" not in escaped
