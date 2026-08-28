"""Tests for JsonPayloadSizeValidator domain object.

Tests verify field size validation, boundary checking, and combined
size and requirements validation across various payload configurations.
This domain object enforces safety constraints to prevent database bloat
and oversized requests.

Design Notes:
- Size constraints are in MB for operator readability (not bytes)
- Boundary testing includes min/max/off-by-one cases
- Combined validation checks both size limits and required fields
- Error messages include field names and limits for debugging
"""

import json

import pytest

from fact_inventory.domain.validation.size import JsonPayloadSizeValidator
from fact_inventory.lib.exceptions import FactValidationError

ONE_MB_LIMIT = 1.0
SMALL_LIMIT_MB = 0.001
STANDARD_LIMIT_MB = 5.0


def build_validator(max_size_mb: float = STANDARD_LIMIT_MB) -> JsonPayloadSizeValidator:
    """Create a JsonPayloadSizeValidator with a clear default limit."""
    return JsonPayloadSizeValidator(max_size_mb=max_size_mb)


def test_json_payload_size_validator_validation_bounds() -> None:
    """Constructor validates max_size_mb is strictly positive."""
    # Positive value: accepted
    build_validator(SMALL_LIMIT_MB)

    # Zero: rejected
    with pytest.raises(ValueError):
        JsonPayloadSizeValidator(max_size_mb=0)

    # Negative: rejected
    with pytest.raises(ValueError):
        JsonPayloadSizeValidator(max_size_mb=-1.0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        # 1 byte under the 1 MB limit: json.dumps({"k": "x"*N}) = N+9 bytes;
        # N=1_048_566 yields 1_048_575 bytes (limit is 1_048_576).
        {"k": "x" * 1_048_566},
    ],
)
def test_json_payload_size_validator_is_valid_size_under_limit(payload: dict) -> None:
    """Payloads under size limit return True."""
    v = build_validator(ONE_MB_LIMIT)
    assert v.is_valid_size(json.dumps(payload).encode()) is True


def test_json_payload_size_validator_is_valid_size_over_limit() -> None:
    """Payloads exceeding limit return False."""
    v = build_validator(SMALL_LIMIT_MB)
    assert v.is_valid_size(json.dumps({"k": "x" * 10000}).encode()) is False


@pytest.mark.parametrize(
    "payload",
    [
        {"os": "RHEL"},
        {},
    ],
)
def test_json_payload_size_validator_validate_size(payload: dict) -> None:
    """validate_size succeeds for valid payloads."""
    build_validator().validate_size("field", payload)


def test_json_payload_size_validator_validate_size_oversized_raises() -> None:
    """validate_size raises ValueError for oversized payloads."""
    v = build_validator(SMALL_LIMIT_MB)
    with pytest.raises(ValueError):
        v.validate_size("large_field", {"x": "y" * 2000})


def test_json_payload_size_validator_instances_are_independent() -> None:
    """Separate instances maintain independent limits."""
    v1 = build_validator(ONE_MB_LIMIT)
    v2 = build_validator(10.1)
    # ~2 MB payload: json.dumps({"x": "y"*2_000_000}) is ~2_000_009 bytes
    large = json.dumps({"x": "y" * 2_000_000}).encode()
    assert v1.is_valid_size(large) is False, "v1 (1 MB) should reject ~2 MB payload"
    assert v2.is_valid_size(large) is True, "v2 (10.1 MB) should accept ~2 MB payload"


def test_json_payload_size_validator_validate_json_fields_success() -> None:
    """validate_json_fields passes for valid mixed payloads."""
    v = build_validator()
    v.validate_json_fields(
        {
            "system_facts": {"os": "RHEL"},
            "package_facts": {"glibc": "2.36"},
            "local_facts": {},
        }
    )


def test_json_payload_size_validator_validate_json_fields_rejects_empty() -> None:
    """validate_json_fields raises when all facts are empty."""
    v = build_validator()
    with pytest.raises(FactValidationError):
        v.validate_json_fields(
            {"system_facts": {}, "package_facts": {}, "local_facts": {}}
        )


def test_json_payload_size_validator_validate_json_fields_accepts_any_non_empty() -> (
    None
):
    """validate_json_fields accepts any non-empty fact type."""
    v = build_validator()
    v.validate_json_fields(
        {"system_facts": {}, "package_facts": {"glibc": "2.36"}, "local_facts": {}}
    )


def test_json_payload_size_validator_validate_json_fields_rejects_oversized() -> None:
    """validate_json_fields raises when any field exceeds limit."""
    v = build_validator(SMALL_LIMIT_MB)
    with pytest.raises(ValueError):
        v.validate_json_fields(
            {
                "system_facts": {"os": "RHEL"},
                "package_facts": {"x": "y" * 2000},
                "local_facts": {},
            }
        )


def test_fact_validation_error_is_subclass_of_value_error() -> None:
    """FactValidationError inherits from ValueError."""
    assert issubclass(FactValidationError, ValueError)
