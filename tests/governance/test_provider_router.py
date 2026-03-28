"""Tests for provider_router module"""

import pytest
from apflow.governance.provider_router import ProviderRouter


class TestSelectModel:
    def test_first(self):
        result = ProviderRouter().select_model(["opus", "sonnet", "haiku"], 0)
        assert result.model == "opus"
        assert not result.is_downgraded
        assert result.index == 0

    def test_downgraded(self):
        result = ProviderRouter().select_model(["opus", "sonnet", "haiku"], 1)
        assert result.model == "sonnet"
        assert result.is_downgraded

    def test_last(self):
        result = ProviderRouter().select_model(["opus", "sonnet", "haiku"], 2)
        assert result.model == "haiku"
        assert result.is_downgraded

    def test_empty_chain_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            ProviderRouter().select_model([], 0)

    def test_negative_index_raises(self):
        with pytest.raises(ValueError):
            ProviderRouter().select_model(["opus"], -1)

    def test_out_of_bounds_raises(self):
        with pytest.raises(ValueError, match="out of bounds"):
            ProviderRouter().select_model(["opus", "sonnet"], 2)

    def test_single_model(self):
        result = ProviderRouter().select_model(["opus"], 0)
        assert result.model == "opus"
        assert not result.is_downgraded


class TestGetNextModel:
    def test_next(self):
        result = ProviderRouter().get_next_model(["opus", "sonnet", "haiku"], 0)
        assert result is not None
        assert result.model == "sonnet"
        assert result.is_downgraded

    def test_exhausted(self):
        result = ProviderRouter().get_next_model(["opus", "sonnet"], 1)
        assert result is None

    def test_single_model_exhausted(self):
        result = ProviderRouter().get_next_model(["opus"], 0)
        assert result is None
