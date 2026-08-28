"""Tests for FactInventoryService business logic."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fact_inventory.application.services.fact import FactInventoryService
from fact_inventory.lib.exceptions import FactValidationError
from fact_inventory.lib.settings import get_settings
from tests.factories import (
    FACT_FIELDS,
    build_fact_model,
    create_at_limit_field,
    create_oversized_field,
    create_partial,
    create_record,
)
from tests.fixtures import parametrize_ipv4_ipv6
from tests.support.db import (
    count_records_for_client,
    get_records_for_client,
    persist_fact_inventory,
)

SINGLE_FACT_CASES = [
    pytest.param(("system_facts", {"os": "RHEL"}), id="system_facts"),
    pytest.param(("package_facts", {"glibc": "2.36"}), id="package_facts"),
    pytest.param(("local_facts", {"env": "prod"}), id="local_facts"),
    pytest.param(("client_facts", {"target_url": "/example/path"}), id="client_facts"),
]


@parametrize_ipv4_ipv6()
async def test_insert_record_creates_new_row(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Insert appends a new record."""
    client_address = test_ips["first"]
    record = await service.insert_record(
        create_record(
            client_address,
            system_facts={"os": "RHEL"},
        )
    )

    assert record.client_address == client_address
    assert record.system_facts == {"os": "RHEL"}
    assert record.id is not None


@parametrize_ipv4_ipv6()
async def test_insert_record_allows_multiple_rows_per_ip(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Insert preserves history by creating multiple rows per client."""
    client_address = test_ips["second"]
    payload = create_record(
        client_address,
        system_facts={"os": "RHEL"},
    )

    await service.insert_record(payload)
    await service.insert_record(payload)
    await service.insert_record(payload)

    assert await count_records_for_client(test_db_session, client_address) == 3


@parametrize_ipv4_ipv6()
async def test_insert_record_creates_unique_ids(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Insert generates a unique row id for each append."""
    client_address = test_ips["third"]
    payload = create_record(
        client_address,
        system_facts={"os": "RHEL"},
    )

    first = await service.insert_record(payload)
    second = await service.insert_record(payload)

    assert second.id != first.id


@parametrize_ipv4_ipv6()
async def test_service_write_rejects_all_empty_facts(
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Insert rejects payloads with no facts."""
    with pytest.raises(FactValidationError):
        await service.insert_record(
            create_record(test_ips["first"]),
        )


@pytest.mark.parametrize("fact_field", FACT_FIELDS)
@parametrize_ipv4_ipv6()
async def test_service_write_rejects_oversized_facts(
    service: FactInventoryService,
    fact_field: str,
    test_ips: dict[str, str],
) -> None:
    """Insert enforces per-field JSON size limits."""
    payload = create_record(test_ips["first"])
    payload[fact_field] = {
        "data": create_oversized_field(get_settings().max_json_field_mb)
    }

    with pytest.raises(ValueError):
        await service.insert_record(payload)


@parametrize_ipv4_ipv6()
async def test_service_write_accepts_field_at_limit(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Insert accepts fields exactly at the configured limit."""
    at_limit = create_at_limit_field(get_settings().max_json_field_mb)
    client_address = test_ips["second"]

    record = await service.insert_record(
        create_record(
            client_address,
            system_facts={"data": at_limit},
        ),
    )

    assert record.system_facts["data"] == at_limit


@pytest.mark.parametrize("single_fact_case", SINGLE_FACT_CASES)
@parametrize_ipv4_ipv6()
async def test_service_write_accepts_single_fact_type(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    single_fact_case: tuple[str, dict[str, Any]],
    test_ips: dict[str, str],
) -> None:
    """Insert accepts payloads with exactly one populated category."""
    fact_field, fact_data = single_fact_case
    client_address = test_ips["first"]
    payload = create_partial(fact_field, fact_data)
    payload["client_address"] = client_address

    record = await service.insert_record(payload)
    assert getattr(record, fact_field) == fact_data


@parametrize_ipv4_ipv6()
async def test_purge_facts_older_than_removes_old_records(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Time-based purge deletes only records older than the cutoff."""
    old_client = test_ips["history"]
    recent_client = test_ips["second"]
    await persist_fact_inventory(
        test_db_session,
        build_fact_model(
            old_client,
            days_offset=30,
            system_facts={"age": "old"},
        ),
        build_fact_model(
            recent_client,
            days_offset=2,
            system_facts={"age": "new"},
        ),
    )

    assert await service.purge_facts_older_than(retention_days=10) == 1
    assert await count_records_for_client(test_db_session, old_client) == 0
    assert await count_records_for_client(test_db_session, recent_client) == 1


@parametrize_ipv4_ipv6()
async def test_purge_facts_older_than_keeps_recent_records(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Time-based purge leaves recent records untouched."""
    client_address = test_ips["second"]
    await persist_fact_inventory(
        test_db_session,
        build_fact_model(client_address, days_offset=2),
    )

    assert await service.purge_facts_older_than(retention_days=10) == 0
    assert await count_records_for_client(test_db_session, client_address) == 1


@parametrize_ipv4_ipv6()
async def test_purge_facts_older_than_returns_deleted_count(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """Time-based purge reports how many rows it removed."""
    old_clients = [test_ips["first"], test_ips["second"], test_ips["third"]]
    models = []
    for ip in old_clients:
        models.append(build_fact_model(ip, days_offset=30))
    await persist_fact_inventory(test_db_session, *models)

    assert await service.purge_facts_older_than(retention_days=10) == 3


@parametrize_ipv4_ipv6()
async def test_purge_facts_over_limit_keeps_newest_per_ip(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """History purge keeps only the most recently active rows for a client."""
    client_address = test_ips["first"]
    models = []
    for index in range(5):
        models.append(
            build_fact_model(
                client_address,
                days_offset=5 - index,
                system_facts={"i": index},
            )
        )
    await persist_fact_inventory(test_db_session, *models)

    assert await service.purge_fact_history_more_than(max_entries=3) == 2

    remaining = await get_records_for_client(test_db_session, client_address)
    values = []
    for record in remaining:
        values.append(record.system_facts["i"])
    assert values == [4, 3, 2]


@parametrize_ipv4_ipv6()
async def test_purge_facts_over_limit_keeps_all_when_under_limit(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """History purge keeps all rows when the client is under the limit."""
    client_address = test_ips["third"]
    await persist_fact_inventory(
        test_db_session,
        build_fact_model(client_address),
    )

    assert await service.purge_fact_history_more_than(max_entries=5) == 0
    assert await count_records_for_client(test_db_session, client_address) == 1


@parametrize_ipv4_ipv6()
async def test_purge_facts_over_limit_returns_deleted_count(
    test_db_session: AsyncSession,
    service: FactInventoryService,
    test_ips: dict[str, str],
) -> None:
    """History purge reports how many rows were removed."""
    client_address = test_ips["history"]
    models = []
    for index in range(7):
        models.append(
            build_fact_model(
                client_address,
                days_offset=7 - index,
                system_facts={"i": index},
            )
        )
    await persist_fact_inventory(test_db_session, *models)

    assert await service.purge_fact_history_more_than(max_entries=5) == 2


@pytest.mark.parametrize("invalid_days", [0, -5, 3651])
async def test_purge_facts_older_than_rejects_invalid_retention_days(
    service: FactInventoryService,
    invalid_days: int,
) -> None:
    """Time-based purge rejects invalid retention windows."""
    with pytest.raises(ValueError):
        await service.purge_facts_older_than(retention_days=invalid_days)


@pytest.mark.parametrize("invalid_max_entries", [0, -5, 1001])
async def test_purge_facts_over_limit_rejects_invalid_max_entries(
    service: FactInventoryService,
    invalid_max_entries: int,
) -> None:
    """History purge rejects invalid retention counts."""
    with pytest.raises(ValueError):
        await service.purge_fact_history_more_than(max_entries=invalid_max_entries)


def test_fact_payload_factory_create_partial_rejects_invalid_fact_type() -> None:
    """Fact payload factory rejects unknown fact categories."""
    with pytest.raises(ValueError):
        create_partial("bad_field", {"k": "v"})
