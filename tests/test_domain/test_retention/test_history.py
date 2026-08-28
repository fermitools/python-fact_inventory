"""Tests for HistoryRetentionPolicy.

Tests HistoryRetentionPolicy: boundary validation and property access.
These are pure domain objects with no external dependencies, making them
ideal for fast unit tests with high coverage of edge cases.

Design Notes:
- Boundary tests validate min/max constraints (not just happy path)
- HistoryRetentionPolicy per-client limits prevent unbounded growth
"""

import pytest

from fact_inventory.domain.retention.history import HistoryRetentionPolicy


@pytest.mark.parametrize(
    "max_entries",
    [HistoryRetentionPolicy.MIN_ENTRIES, HistoryRetentionPolicy.MAX_ENTRIES],
)
def test_history_retention_policy_accepts_boundary_values(max_entries: int) -> None:
    """HistoryRetentionPolicy accepts the supported lower and upper bounds."""
    HistoryRetentionPolicy(max_entries=max_entries)


@pytest.mark.parametrize(
    "max_entries",
    [
        HistoryRetentionPolicy.MIN_ENTRIES - 1,
        -1,
        HistoryRetentionPolicy.MAX_ENTRIES + 1,
    ],
)
def test_history_retention_policy_rejects_invalid_values(max_entries: int) -> None:
    """HistoryRetentionPolicy rejects values outside the supported range."""
    with pytest.raises(ValueError):
        HistoryRetentionPolicy(max_entries=max_entries)


def test_history_retention_policy_max_entries_property() -> None:
    """max_entries property returns configured value."""
    policy = HistoryRetentionPolicy(max_entries=100)
    assert policy.max_entries == 100
