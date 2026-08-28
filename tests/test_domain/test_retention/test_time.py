"""Tests for TimeRetentionPolicy.

Tests TimeRetentionPolicy: boundary validation, cutoff calculations,
and UTC timezone handling. These are pure domain objects with no external
dependencies, making them ideal for fast unit tests with high coverage of
edge cases.

Design Notes:
- Boundary tests validate min/max constraints (not just happy path)
- TimeRetentionPolicy uses UTC for consistency across timezones
- Cutoff calculations are verified with tolerance for execution time variance
"""

from datetime import UTC, datetime, timedelta

import pytest

from fact_inventory.domain.retention.time import TimeRetentionPolicy


def assert_cutoff_is_approximately_days_ago(days: int) -> None:
    """Assert the computed cutoff is close to the expected age."""
    policy = TimeRetentionPolicy(days=days)
    delta = datetime.now(tz=UTC) - policy.cutoff_datetime
    assert abs(delta - timedelta(days=days)) < timedelta(seconds=1)


@pytest.mark.parametrize(
    "days", [TimeRetentionPolicy.MIN_DAYS, TimeRetentionPolicy.MAX_DAYS]
)
def test_time_retention_policy_accepts_boundary_values(days: int) -> None:
    """TimeRetentionPolicy accepts the supported lower and upper bounds."""
    TimeRetentionPolicy(days=days)


@pytest.mark.parametrize(
    "days", [TimeRetentionPolicy.MIN_DAYS - 1, -1, TimeRetentionPolicy.MAX_DAYS + 1]
)
def test_time_retention_policy_rejects_invalid_values(days: int) -> None:
    """TimeRetentionPolicy rejects values outside the supported range."""
    with pytest.raises(ValueError):
        TimeRetentionPolicy(days=days)


def test_time_retention_policy_cutoff_is_in_the_past() -> None:
    """Cutoff datetime is strictly in the past and in UTC."""
    cutoff = TimeRetentionPolicy(days=30).cutoff_datetime
    assert cutoff < datetime.now(tz=UTC)
    # Verify cutoff is in UTC timezone
    assert cutoff.tzinfo is UTC


def test_time_retention_policy_cutoff_is_approximately_n_days_ago() -> None:
    """Cutoff is approximately n days in the past (with 1 second tolerance)."""
    assert_cutoff_is_approximately_days_ago(30)


def test_time_retention_policy_cutoff_one_day_is_approximately_24h_ago() -> None:
    """Minimum retention cutoff is approximately 24 hours in the past."""
    assert_cutoff_is_approximately_days_ago(1)


def test_time_retention_policy_cutoff_at_max_boundary() -> None:
    """Maximum boundary cutoff is approximately MAX_DAYS in the past."""
    assert_cutoff_is_approximately_days_ago(TimeRetentionPolicy.MAX_DAYS)
