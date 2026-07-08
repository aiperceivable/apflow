"""Tests for core/extensions/executor_metadata.py"""

from apflow.core.extensions import get_registry
from apflow.core.extensions.executor_metadata import (
    get_all_executor_metadata,
    get_executor_metadata,
)
from apflow.core.extensions.types import ExtensionCategory


def setup_function():
    """Clear extension registry state before each test"""
    registry = get_registry()
    registry._executor_classes.clear()
    registry._factory_functions.clear()
    registry._by_id.clear()
    registry._by_category.clear()


class TestGetExecutorMetadata:
    """Regression: get_executor_metadata raised AttributeError instead of
    the documented None-return contract for extensions lacking a
    description attribute. (Review CRITICAL #33)
    """

    def test_missing_description_attribute_does_not_raise(self):
        class FakeExtension:
            id = "broken_metadata_executor"
            name = "Broken"
            category = ExtensionCategory.EXECUTOR
            type = "default"
            # No `description`, `execute`, or `get_input_schema` attribute.

        registry = get_registry()
        registry._by_id["broken_metadata_executor"] = FakeExtension()  # type: ignore[assignment]

        metadata = get_executor_metadata("broken_metadata_executor")

        assert metadata is not None
        assert metadata["description"] == ""
        assert metadata["id"] == "broken_metadata_executor"
        assert metadata["name"] == "Broken"

    def test_not_found_returns_none(self):
        assert get_executor_metadata("nonexistent_executor") is None


class TestGetAllExecutorMetadata:
    """Regression: a single bad executor raising during metadata collection
    aborted get_all_executor_metadata() entirely, losing metadata for every
    other registered executor. (Review CRITICAL #33)
    """

    def test_one_bad_executor_does_not_abort_the_batch(self):
        class ExplodingExtension:
            id = "exploding_executor"
            type = "default"

            @property
            def category(self):
                raise RuntimeError("boom")

        class GoodExtension:
            id = "good_executor"
            name = "Good"
            description = "A working executor"
            category = ExtensionCategory.EXECUTOR
            type = "default"

        registry = get_registry()
        exploding = ExplodingExtension()
        good = GoodExtension()
        registry._by_id["exploding_executor"] = exploding  # type: ignore[assignment]
        registry._by_id["good_executor"] = good  # type: ignore[assignment]
        registry._by_category[ExtensionCategory.EXECUTOR] = {  # type: ignore[assignment]
            "default": [exploding, good],
        }

        # Must not raise despite exploding_executor's category property
        # raising on every access.
        all_metadata = get_all_executor_metadata()

        assert "exploding_executor" not in all_metadata
        assert "good_executor" in all_metadata
        assert all_metadata["good_executor"]["description"] == "A working executor"
