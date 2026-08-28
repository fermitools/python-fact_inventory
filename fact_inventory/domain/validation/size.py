"""JSON field size validation constraint domain object.

Domain object expressing payload size rules independently of the HTTP
layer. No framework imports.

Notes
-----
    JsonPayloadSizeValidator is a domain rule, not an HTTP concern. Raising
    a domain exception here keeps the domain free of litestar/pydantic deps;
    the service layer or presentation layer is responsible for converting
    the error to the appropriate HTTP response.
"""

import json
from typing import Any

from fact_inventory.domain.validation.requirements import (
    JsonPayloadRequirementsValidator,
)
from fact_inventory.lib.exceptions import FactPayloadTooLargeError

__all__ = ["JsonPayloadSizeValidator"]


class JsonPayloadSizeValidator:
    """Encapsulates JSON field size validation rules.

    Validates that serialized JSON fields do not exceed configured size limit.
    """

    JSON_FIELD_NAMES = ("system_facts", "package_facts", "local_facts", "client_facts")

    def __init__(self, max_size_mb: float) -> None:
        """Initialize validator.

        Parameters
        ----------
        max_size_mb : float
            Maximum field size in megabytes. Must be positive.

        Raises
        ------
        ValueError
            If max_size_mb is not positive.
        """
        if max_size_mb <= 0:
            raise ValueError("max_size_mb must be positive")  # noqa: TRY003
        self._max_bytes = int(max_size_mb * 1024 * 1024)
        self._max_mb = max_size_mb

    def is_valid_size(self, serialized_json: bytes) -> bool:
        """Check if serialized JSON fits within configured size limit.

        Parameters
        ----------
        serialized_json : bytes
            UTF-8 encoded JSON field.

        Returns
        -------
        bool
            True if size is within limit, False otherwise.
        """
        return len(serialized_json) <= self._max_bytes

    def validate_size(self, field_name: str, value: dict[str, Any]) -> None:
        """Validate that a JSON field fits within the configured size limit.

        Serializes the value to JSON and compares against the configured size
        limit. Raises FactPayloadTooLargeError if the serialized size exceeds
        the limit.

        Parameters
        ----------
        field_name : str
            Field name (for error messages).
        value : dict[str, Any]
            The JSON-serializable value to validate.

        Raises
        ------
        FactPayloadTooLargeError
            If field exceeds size limit.
        """
        serialized = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if not self.is_valid_size(serialized):
            size_mb = len(serialized) / (1024 * 1024)
            raise FactPayloadTooLargeError(  # noqa: TRY003
                f"{field_name} exceeds maximum size "
                f"({size_mb:.1f}MB > {self._max_mb:.1f}MB)"
            )

    def validate_json_fields(self, data: dict[str, Any]) -> None:
        """Validate all JSON fields against size constraints.

        Checks that at least one fact category contains data and that no
        individual field exceeds the configured size limit.

        Parameters
        ----------
        data : dict[str, Any]
            Dictionary containing facts

        Raises
        ------
        FactValidationError
            If all fact categories are empty.
        FactPayloadTooLargeError
            If any JSON field exceeds the configured size limit.
        """
        validator = JsonPayloadRequirementsValidator()
        validator.has_required_facts(data)
        for field_name in self.JSON_FIELD_NAMES:
            self.validate_size(field_name, data.get(field_name, {}))

    def __repr__(self) -> str:  # pragma: no cover
        """Return string representation of the JsonPayloadSizeValidator instance.

        Returns
        -------
        str
            Formatted string showing the maximum size configuration.
        """
        return f"JsonPayloadSizeValidator(max_size_mb={self._max_mb})"
