"""Tests for policy module"""

import pytest
from apflow.governance.policy import CostPolicy, PolicyAction, PolicyEngine


class TestCostPolicy:
    def test_valid(self):
        p = CostPolicy(name="default", action=PolicyAction.BLOCK, threshold=0.8)
        assert p.name == "default"
        assert p.action == PolicyAction.BLOCK

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            CostPolicy(name="", action=PolicyAction.BLOCK, threshold=0.8)

    def test_threshold_zero_raises(self):
        with pytest.raises(ValueError):
            CostPolicy(name="p", action=PolicyAction.BLOCK, threshold=0.0)

    def test_threshold_above_one_raises(self):
        with pytest.raises(ValueError):
            CostPolicy(name="p", action=PolicyAction.BLOCK, threshold=1.1)

    def test_threshold_one_valid(self):
        p = CostPolicy(name="p", action=PolicyAction.BLOCK, threshold=1.0)
        assert p.threshold == 1.0

    def test_downgrade_no_chain_raises(self):
        with pytest.raises(ValueError, match="downgrade_chain"):
            CostPolicy(name="p", action=PolicyAction.DOWNGRADE, threshold=0.8)


class TestPolicyEngine:
    def test_register_and_get(self):
        engine = PolicyEngine()
        policy = CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8)
        engine.register_policy(policy)
        assert engine.get_policy("p1") is policy

    def test_register_duplicate_raises(self):
        engine = PolicyEngine()
        policy = CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8)
        engine.register_policy(policy)
        with pytest.raises(ValueError, match="already registered"):
            engine.register_policy(policy)

    def test_get_nonexistent_returns_none(self):
        engine = PolicyEngine()
        assert engine.get_policy("nope") is None

    def test_evaluate_not_triggered(self):
        engine = PolicyEngine()
        engine.register_policy(CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8))
        result = engine.evaluate("p1", 0.5)
        assert not result.triggered
        assert result.action == PolicyAction.CONTINUE

    def test_evaluate_block(self):
        engine = PolicyEngine()
        engine.register_policy(CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8))
        result = engine.evaluate("p1", 0.85)
        assert result.triggered
        assert result.action == PolicyAction.BLOCK

    def test_evaluate_downgrade(self):
        engine = PolicyEngine()
        engine.register_policy(
            CostPolicy(
                name="p1",
                action=PolicyAction.DOWNGRADE,
                threshold=0.8,
                downgrade_chain=["opus", "sonnet", "haiku"],
            )
        )
        result = engine.evaluate("p1", 0.85, current_model_index=0)
        assert result.triggered
        assert result.action == PolicyAction.DOWNGRADE
        assert result.model_override == "sonnet"

    def test_evaluate_downgrade_chain_exhausted(self):
        engine = PolicyEngine()
        engine.register_policy(
            CostPolicy(
                name="p1",
                action=PolicyAction.DOWNGRADE,
                threshold=0.8,
                downgrade_chain=["opus"],
            )
        )
        result = engine.evaluate("p1", 0.85, current_model_index=0)
        assert result.triggered
        assert result.action == PolicyAction.BLOCK  # Chain exhausted

    def test_evaluate_notify(self):
        engine = PolicyEngine()
        engine.register_policy(CostPolicy(name="p1", action=PolicyAction.NOTIFY, threshold=0.8))
        result = engine.evaluate("p1", 0.85)
        assert result.triggered
        assert result.action == PolicyAction.NOTIFY
        assert result.model_override is None

    def test_evaluate_threshold_boundary(self):
        engine = PolicyEngine()
        engine.register_policy(CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8))
        # Just below: not triggered
        result = engine.evaluate("p1", 0.79999)
        assert not result.triggered
        # Exactly at: triggered
        result = engine.evaluate("p1", 0.8)
        assert result.triggered

    def test_evaluate_not_found_raises(self):
        engine = PolicyEngine()
        with pytest.raises(KeyError, match="not registered"):
            engine.evaluate("nope", 0.5)

    def test_evaluate_negative_utilization_raises(self):
        engine = PolicyEngine()
        engine.register_policy(CostPolicy(name="p1", action=PolicyAction.BLOCK, threshold=0.8))
        with pytest.raises(ValueError):
            engine.evaluate("p1", -0.1)
