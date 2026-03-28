"""
Cost policy engine for AI agent tasks.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


class PolicyAction(Enum):
    """Action to take when a cost policy is triggered."""

    BLOCK = "block"
    DOWNGRADE = "downgrade"
    NOTIFY = "notify"
    CONTINUE = "continue"


@dataclass(frozen=True)
class CostPolicy:
    """A named cost governance policy.

    Args:
        name: Unique policy name (non-empty).
        action: What to do when triggered.
        threshold: Utilization threshold (0.0 < x <= 1.0).
        downgrade_chain: Model names in priority order (required if action=DOWNGRADE).
        description: Human-readable description.
    """

    name: str
    action: PolicyAction
    threshold: float
    downgrade_chain: list[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Policy name must be non-empty")
        if not (0.0 < self.threshold <= 1.0):
            raise ValueError(f"threshold must be > 0.0 and <= 1.0, got {self.threshold}")
        if self.action == PolicyAction.DOWNGRADE and not self.downgrade_chain:
            raise ValueError("downgrade_chain is required when action is DOWNGRADE")


@dataclass
class PolicyEvaluation:
    """Result of evaluating a cost policy."""

    triggered: bool
    action: PolicyAction
    message: str = ""
    model_override: Optional[str] = None


class PolicyEngine:
    """Manages and evaluates cost policies."""

    def __init__(self) -> None:
        self._policies: Dict[str, CostPolicy] = {}

    def register_policy(self, policy: CostPolicy) -> None:
        """Register a cost policy. Raises ValueError if name already exists."""
        if policy.name in self._policies:
            raise ValueError(f"Policy '{policy.name}' already registered")
        self._policies[policy.name] = policy
        logger.debug(f"Registered cost policy: {policy.name} ({policy.action.value})")

    def get_policy(self, name: str) -> Optional[CostPolicy]:
        """Get policy by name. Returns None if not found."""
        return self._policies.get(name)

    def evaluate(
        self,
        policy_name: str,
        utilization: float,
        current_model_index: int = 0,
    ) -> PolicyEvaluation:
        """Evaluate a policy against current utilization.

        Args:
            policy_name: Registered policy name.
            utilization: Current budget utilization (>= 0.0).
            current_model_index: Current position in downgrade chain.

        Returns:
            PolicyEvaluation with action and optional model_override.
        """
        if utilization < 0:
            raise ValueError(f"utilization must be >= 0.0, got {utilization}")

        policy = self._policies.get(policy_name)
        if policy is None:
            raise KeyError(f"Policy '{policy_name}' not registered")

        triggered = utilization >= policy.threshold

        if not triggered:
            return PolicyEvaluation(
                triggered=False,
                action=PolicyAction.CONTINUE,
                message=f"Budget within threshold ({utilization:.1%} < {policy.threshold:.1%})",
            )

        if policy.action == PolicyAction.BLOCK:
            return PolicyEvaluation(
                triggered=True,
                action=PolicyAction.BLOCK,
                message=f"Budget exceeded threshold ({utilization:.1%} >= {policy.threshold:.1%})",
            )

        if policy.action == PolicyAction.DOWNGRADE:
            next_index = current_model_index + 1
            if next_index < len(policy.downgrade_chain):
                next_model = policy.downgrade_chain[next_index]
                return PolicyEvaluation(
                    triggered=True,
                    action=PolicyAction.DOWNGRADE,
                    message=(
                        f"Downgrading model to {next_model} "
                        f"(utilization {utilization:.1%} >= {policy.threshold:.1%})"
                    ),
                    model_override=next_model,
                )
            else:
                return PolicyEvaluation(
                    triggered=True,
                    action=PolicyAction.BLOCK,
                    message=(
                        f"Downgrade chain exhausted at index {current_model_index}, "
                        f"blocking execution"
                    ),
                )

        if policy.action == PolicyAction.NOTIFY:
            return PolicyEvaluation(
                triggered=True,
                action=PolicyAction.NOTIFY,
                message=(
                    f"Budget warning: utilization {utilization:.1%} "
                    f">= threshold {policy.threshold:.1%}"
                ),
            )

        return PolicyEvaluation(
            triggered=True,
            action=PolicyAction.CONTINUE,
            message="Policy triggered but action is CONTINUE",
        )
