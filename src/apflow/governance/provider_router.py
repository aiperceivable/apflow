"""
Provider-agnostic model routing for cost governance.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelSelection:
    """Result of model selection from a downgrade chain."""

    model: str
    index: int
    is_downgraded: bool


class ProviderRouter:
    """Routes to models based on downgrade chain position."""

    def select_model(
        self,
        downgrade_chain: list[str],
        current_index: int = 0,
    ) -> ModelSelection:
        """Select model at current_index in the downgrade chain.

        Args:
            downgrade_chain: Model names in priority order (at least 1 entry).
            current_index: Position in chain (>= 0, < len(chain)).

        Returns:
            ModelSelection with model name and downgrade status.
        """
        if not downgrade_chain:
            raise ValueError("downgrade_chain must have at least 1 entry")
        if current_index < 0:
            raise ValueError(f"current_index must be >= 0, got {current_index}")
        if current_index >= len(downgrade_chain):
            raise ValueError(
                f"current_index {current_index} out of bounds for chain of length "
                f"{len(downgrade_chain)}"
            )

        return ModelSelection(
            model=downgrade_chain[current_index],
            index=current_index,
            is_downgraded=current_index > 0,
        )

    def get_next_model(
        self,
        downgrade_chain: list[str],
        current_index: int,
    ) -> Optional[ModelSelection]:
        """Get next cheaper model in the chain.

        Returns:
            ModelSelection for next model, or None if chain is exhausted.
        """
        next_index = current_index + 1
        if next_index >= len(downgrade_chain):
            return None

        return ModelSelection(
            model=downgrade_chain[next_index],
            index=next_index,
            is_downgraded=True,
        )
