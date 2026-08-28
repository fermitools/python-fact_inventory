"""Tests for JsonPayloadRequirementsValidator domain object.

Tests verify that payloads meet minimum requirements (at least one fact
category must contain data). This domain object enforces that submitted
facts are meaningful and not completely empty.

Design Notes:
- Validation ensures at least one of: system_facts, package_facts,
  local_facts, or client_facts contains data
- Empty payloads are rejected with FactValidationError
- All fact categories can be empty dicts as long as one has content
"""

import pytest

from fact_inventory.domain.validation.requirements import (
    JsonPayloadRequirementsValidator,
)
from fact_inventory.lib.exceptions import FactValidationError


def test_json_payload_requirements_validator_rejects_empty() -> None:
    """JsonPayloadRequirementsValidator raises when all fact types are empty."""
    v = JsonPayloadRequirementsValidator()
    with pytest.raises(FactValidationError):
        v.has_required_facts(
            {"system_facts": {}, "package_facts": {}, "local_facts": {}}
        )


def test_json_payload_requirements_validator_accepts_any_non_empty() -> None:
    """JsonPayloadRequirementsValidator accepts any non-empty fact type."""
    v = JsonPayloadRequirementsValidator()
    v.has_required_facts(
        {"system_facts": {}, "package_facts": {"glibc": "2.36"}, "local_facts": {}}
    )


def test_json_payload_requirements_validator_has_required_facts_with_data() -> None:
    """has_required_facts passes when at least one fact type has data."""
    v = JsonPayloadRequirementsValidator()
    v.has_required_facts({"system_facts": {"os": "RHEL"}})
