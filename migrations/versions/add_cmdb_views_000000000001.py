"""Add CMDB views over the fact_inventory JSONB columns.

Revision ID: 000000000001
Revises: 000000000000

PostgreSQL-only. Creates decomposed read-only views for convenient access to
host, network, hardware, and OS data from fact_inventory JSONB columns:

Network views:
  1. cmdb_host_interfaces_private: Private. Join keys and raw interface
     JSONB only. Not for direct use; see cmdb_host_interfaces.
  2. cmdb_host_interfaces: Public. All interfaces with basic metadata.
  3. cmdb_host_interface_ipv4_addresses: IPv4 addresses per interface
  4. cmdb_host_interface_ipv6_addresses: IPv6 addresses per interface

Hardware views:
  5. cmdb_host_hardware: CPU, architecture, chassis, and hardware metadata
  6. cmdb_host_storage_devices: Storage device metadata with partition info

OS view:
  7. cmdb_host_os_info: Operating system metadata (kernel, distribution)

All extracted columns are documented with JSON path references in the
per-view docstrings below.

Views hold no data, so this revision is non-destructive in both directions.
Because every view is created with CREATE OR REPLACE, re-running the upgrade
over an existing set is also safe.
"""

from alembic import op

__all__ = [
    "downgrade",
    "upgrade",
]

# Revision identifiers, used by Alembic.
revision = "000000000001"
down_revision = "000000000000"
branch_labels = None
depends_on = None

# Views in dependency order: cmdb_host_interfaces (public) and the ipv4/ipv6
# address views all select from cmdb_host_interfaces_private, so it must be
# created first and dropped last.
CMDB_VIEWS = (
    "cmdb_host_interfaces_private",
    "cmdb_host_interfaces",
    "cmdb_host_interface_ipv4_addresses",
    "cmdb_host_interface_ipv6_addresses",
    "cmdb_host_hardware",
    "cmdb_host_storage_devices",
    "cmdb_host_os_info",
)


def _is_postgresql() -> bool:
    """Return True when running against PostgreSQL."""
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    """Create all CMDB views. No-op on non-PostgreSQL dialects."""
    if not _is_postgresql():
        return

    _create_cmdb_host_interfaces_private()
    _create_cmdb_host_interfaces()
    _create_cmdb_host_interface_ipv4_addresses()
    _create_cmdb_host_interface_ipv6_addresses()
    _create_cmdb_host_hardware()
    _create_cmdb_host_storage_devices()
    _create_cmdb_host_os_info()


def downgrade() -> None:
    """Drop all CMDB views. No-op on non-PostgreSQL dialects.

    Dropped in reverse creation order so dependent views are removed before
    the cmdb_host_interfaces_private view they select from.
    """
    if not _is_postgresql():
        return

    for view in reversed(CMDB_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")


# --- View definitions -------------------------------------------------------


def _create_cmdb_host_interfaces_private() -> None:
    """Create cmdb_host_interfaces_private view.

    Private. Not for direct use - see cmdb_host_interfaces for the
    public-facing view over this data.

    Extracts all non-loopback interfaces from system_facts JSONB.
    One row per interface per stored record. Carries only join/identifying
    keys and the raw per-interface JSONB; field decoding is left to the
    views that select from this one, so this view has nothing to keep in
    sync with them.

    Filtering:
      - Only extracts actual network interfaces (those with 'device' field)
      - Excludes metadata objects like default_ipv4, python, selinux, etc.
      - Excludes loopback interface (lo)

    ``interface_data`` is exposed raw so cmdb_host_interfaces and the
    dependent address views can each decode what they need directly,
    instead of joining back to fact_inventory, which under hash
    partitioning would scan every partition.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_interfaces_private AS
SELECT
      fi.id AS inventory_id
    , fi.client_address
    , fi.updated_at
    , iface.iface_key AS interface_name
    , iface.iface_data AS interface_data
    , (fi.system_facts ->> 'machine_id') AS machine_id
    , (fi.system_facts ->> 'fqdn') AS fqdn
FROM fact_inventory AS fi
CROSS JOIN LATERAL jsonb_each(fi.system_facts) AS iface(iface_key, iface_data)
WHERE
    iface.iface_data ? 'device'
    AND iface.iface_key IS DISTINCT FROM 'lo'
""")


def _create_cmdb_host_interfaces() -> None:
    """Create cmdb_host_interfaces view.

    Public. Decodes basic per-interface metadata from the raw
    ``interface_data`` JSONB carried by cmdb_host_interfaces_private.
    ``SELECT *`` and schema introspection (psql \\d, ORM reflection, etc.)
    show only these decoded columns; the raw JSONB stays in the private view.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_interfaces AS
SELECT
      inventory_id
    , client_address
    , updated_at
    , machine_id
    , fqdn
    , interface_name
    , (interface_data ->> 'type') AS device_type
    , CASE
        WHEN NULLIF(interface_data ->> 'macaddress', '') IS NULL
            THEN NULL
        ELSE NULLIF(interface_data ->> 'macaddress', '')::macaddr
    END AS mac_address
    , CASE
        WHEN interface_data ->> 'active' IS NULL
            THEN NULL
        WHEN LOWER(interface_data ->> 'active') IN ('true', 't', 'yes', 'y', '1')
            THEN TRUE
        WHEN LOWER(interface_data ->> 'active') IN ('false', 'f', 'no', 'n', '0')
            THEN FALSE
    END AS is_active
    , NULLIF(NULLIF(interface_data ->> 'mtu', ''), 'NULL')::integer AS mtu
    , NULLIF(NULLIF(interface_data ->> 'speed', ''), 'NULL')::integer AS link_speed
    , (interface_data ->> 'module') AS module
    , (interface_data ->> 'pciid') AS pci_id
FROM cmdb_host_interfaces_private
""")


def _create_cmdb_host_interface_ipv4_addresses() -> None:
    """Create cmdb_host_interface_ipv4_addresses view.

    Extracts IPv4 addresses from interfaces. Handles both:
      - Structured objects, as emitted by Ansible setup facts:
        {"address": "10.0.0.1", "prefix": "24", "netmask": "255.255.255.0",
         "network": "10.0.0.0", "broadcast": "10.0.0.255"}
      - Array of strings: ["10.0.0.1", "10.0.0.0/24", ...]

    One row per address per interface per stored record.

    In the object form ``address`` is a bare host address carrying no prefix;
    the mask is supplied in a sibling ``prefix`` field. Casting ``address``
    alone would yield /32 with a self-referential network and broadcast, so
    the two are recombined into a single native ``inet`` value:

        set_masklen(address::inet, prefix::int)

    Every other column is then derived from that one value using the built-in
    inet accessors, rather than reading the redundant netmask/network/
    broadcast fields the facts also provide. When prefix is missing or empty,
    the address is cast directly to inet without set_masklen.

    This view is robust against malformed JSON: missing fields, empty strings,
    and invalid IP addresses are handled gracefully with NULL values instead
    of throwing SQL errors.

    Filtering: Excludes 127.0.0.0/8 addresses (loopback already filtered at
    interface level). Preserves NULL addresses for auditing downstream filtering.

    Reads from cmdb_host_interfaces_private (not the public cmdb_host_interfaces
    view) since it needs the raw interface_data JSONB the public view doesn't
    carry, decoding device_type/mac_address/is_active from it directly here.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_interface_ipv4_addresses AS
WITH addr_subquery AS (
    SELECT
          ci.inventory_id
        , ci.client_address
        , ci.machine_id
        , ci.updated_at
        , ci.fqdn
        , ci.interface_name
        , ci.interface_data
        , addr_inet
    FROM cmdb_host_interfaces_private AS ci
    LEFT JOIN LATERAL (
        SELECT
            CASE
                WHEN ci.interface_data -> 'ipv4' ->> 'address' IS NULL
                    THEN NULL
                WHEN ci.interface_data -> 'ipv4' ->> 'address' = ''
                    THEN NULL
                WHEN
                    ci.interface_data -> 'ipv4' ->> 'prefix' IS NULL
                    OR ci.interface_data -> 'ipv4' ->> 'prefix' = ''
                    THEN (ci.interface_data -> 'ipv4' ->> 'address')::inet
                ELSE SET_MASKLEN(
                    (ci.interface_data -> 'ipv4' ->> 'address')::inet
                    , NULLIF(ci.interface_data -> 'ipv4' ->> 'prefix', '')::int
                )
            END AS addr_inet
        WHERE JSONB_TYPEOF(ci.interface_data -> 'ipv4') = 'object'
        UNION ALL
        SELECT
            CASE
                WHEN JSONB_ARRAY_ELEMENTS_TEXT(ci.interface_data -> 'ipv4') = ''
                    THEN NULL
                ELSE JSONB_ARRAY_ELEMENTS_TEXT(ci.interface_data -> 'ipv4')::inet
            END AS addr_inet
        WHERE JSONB_TYPEOF(ci.interface_data -> 'ipv4') = 'array'
    ) AS addr ON TRUE
    WHERE
        addr.addr_inet IS NULL
        OR NOT (addr.addr_inet << inet '127.0.0.0/8')
)
SELECT
      inventory_id
    , client_address
    , machine_id
    , updated_at
    , fqdn
    , interface_name
    , addr_inet AS ipv4_cidr
    , (interface_data ->> 'type') AS device_type
    , CASE
        WHEN NULLIF(interface_data ->> 'macaddress', '') IS NULL
            THEN NULL
        ELSE NULLIF(interface_data ->> 'macaddress', '')::macaddr
    END AS mac_address
    , CASE
        WHEN interface_data ->> 'active' IS NULL
            THEN NULL
        WHEN LOWER(interface_data ->> 'active') IN ('true', 't', 'yes', 'y', '1')
            THEN TRUE
        WHEN LOWER(interface_data ->> 'active') IN ('false', 'f', 'no', 'n', '0')
            THEN FALSE
    END AS is_active
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE HOST(addr_inet)::inet
    END AS ipv4_address
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE NETMASK(addr_inet)
    END AS ipv4_netmask
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE MASKLEN(addr_inet)::smallint
    END AS ipv4_prefix
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE NETWORK(addr_inet)
    END AS ipv4_network
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE BROADCAST(addr_inet)::inet
    END AS ipv4_broadcast
FROM addr_subquery
""")


def _create_cmdb_host_interface_ipv6_addresses() -> None:
    """Create cmdb_host_interface_ipv6_addresses view.

    Extracts IPv6 addresses from interfaces. Handles both:
      - Array of objects, as emitted by Ansible setup facts:
        [{"address": "fe80::1", "prefix": "64", "scope": "link"}, ...]
      - Array of strings: ["::1", "fe80::1", ...]

    One row per address per interface per stored record.

    As with IPv4, ``address`` in the object form carries no prefix, so it is
    recombined with the sibling ``prefix`` field into a single native ``inet``
    value via set_masklen() and every other column is derived from that.
    When prefix is missing or empty, the address is cast directly to inet.

    This view is robust against malformed JSON: missing fields, empty strings,
    and invalid IP addresses are handled gracefully with NULL values instead
    of throwing SQL errors.

    Both shapes are arrays, so the two branches are distinguished by the
    element type rather than the container type.

    Filtering: Excludes ::1 address (loopback interface already filtered at
    interface level). Preserves NULL addresses for auditing downstream filtering.

    Reads from cmdb_host_interfaces_private (not the public cmdb_host_interfaces
    view) since it needs the raw interface_data JSONB the public view doesn't
    carry, decoding device_type/mac_address/is_active from it directly here.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_interface_ipv6_addresses AS
SELECT
      ci.inventory_id
    , ci.client_address
    , ci.updated_at
    , ci.machine_id
    , ci.fqdn
    , ci.interface_name
    , addr_inet AS ipv6_cidr
    , scope AS ipv6_scope
    , (ci.interface_data ->> 'type') AS device_type
    , CASE
        WHEN NULLIF(ci.interface_data ->> 'macaddress', '') IS NULL
            THEN NULL
        ELSE NULLIF(ci.interface_data ->> 'macaddress', '')::macaddr
    END AS mac_address
    , CASE
        WHEN ci.interface_data ->> 'active' IS NULL
            THEN NULL
        WHEN LOWER(ci.interface_data ->> 'active') IN ('true', 't', 'yes', 'y', '1')
            THEN TRUE
        WHEN LOWER(ci.interface_data ->> 'active') IN ('false', 'f', 'no', 'n', '0')
            THEN FALSE
    END AS is_active
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE HOST(addr_inet)::inet
    END AS ipv6_address
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE MASKLEN(addr_inet)::smallint
    END AS ipv6_prefix
    , CASE
        WHEN addr_inet IS NULL
            THEN NULL
        ELSE NETWORK(addr_inet)
    END AS ipv6_network
FROM cmdb_host_interfaces_private AS ci
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN
                elem ->> 'address' IS NULL
                OR elem ->> 'address' = ''
                THEN NULL
            WHEN
                elem ->> 'prefix' IS NULL
                OR elem ->> 'prefix' = ''
                THEN (elem ->> 'address')::inet
            ELSE SET_MASKLEN(
                (elem ->> 'address')::inet
                , NULLIF(elem ->> 'prefix', '')::int
            )
        END AS addr_inet
        , CASE
            WHEN
                elem ->> 'scope' IS NULL
                OR elem ->> 'scope' = ''
                THEN NULL
            ELSE elem ->> 'scope'
        END AS scope
    FROM JSONB_ARRAY_ELEMENTS(ci.interface_data -> 'ipv6') AS elem
    WHERE
        JSONB_TYPEOF(ci.interface_data -> 'ipv6') = 'array'
        AND JSONB_TYPEOF(elem) = 'object'
    UNION ALL
    SELECT
        CASE
            WHEN (elem #>> '{}') = ''
                THEN NULL
            ELSE (elem #>> '{}')::inet
        END AS addr_inet
        , NULL AS scope
    FROM JSONB_ARRAY_ELEMENTS(ci.interface_data -> 'ipv6') AS elem
    WHERE
        JSONB_TYPEOF(ci.interface_data -> 'ipv6') = 'array'
        AND JSONB_TYPEOF(elem) = 'string'
) AS addr ON TRUE
WHERE
    addr.addr_inet IS NULL
    OR HOST(addr.addr_inet)::inet IS DISTINCT FROM inet '::1'
""")


def _create_cmdb_host_hardware() -> None:
    """Create cmdb_host_hardware view.

    Extracts CPU, architecture, chassis, and hardware identification metadata.
    One row per stored record; because fact_inventory is append-only, a host
    with history contributes one row per submission. Filter on updated_at or
    inventory_id to select a single point in time.

    This view is robust against malformed JSON: missing numeric fields are
    returned as NULL instead of throwing SQL errors.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_hardware AS
SELECT
      fi.id AS inventory_id
    , fi.client_address
    , fi.updated_at
    , (fi.system_facts ->> 'machine_id') AS machine_id
    , (fi.system_facts ->> 'board_vendor') AS board_vendor
    , (fi.system_facts ->> 'board_name') AS board_model
    , (fi.system_facts ->> 'product_name') AS product_name
    , (fi.system_facts ->> 'product_version') AS product_version
    , (fi.system_facts ->> 'product_uuid') AS product_uuid
    , (fi.system_facts ->> 'product_serial') AS product_serial
    , (fi.system_facts ->> 'form_factor') AS chassis_form_factor
    , (fi.system_facts ->> 'machine') AS system_arch
    , (fi.system_facts -> 'processor' ->> 1) AS cpu_manufacturer
    , (fi.system_facts -> 'processor' ->> 2) AS cpu_model_name
    , NULLIF(NULLIF(fi.system_facts ->> 'processor_count', ''), 'NULL')::integer
        AS cpu_socket_count
    , NULLIF(NULLIF(fi.system_facts ->> 'processor_cores', ''), 'NULL')::integer
        AS cpu_core_count
    , NULLIF(NULLIF(fi.system_facts ->> 'processor_vcpus', ''), 'NULL')::integer
        AS cpu_thread_count
    , NULLIF(NULLIF(fi.system_facts ->> 'memtotal_mb', ''), 'NULL')::bigint * 1048576
        AS ram_bytes
FROM fact_inventory AS fi
""")


def _create_cmdb_host_storage_devices() -> None:
    """Create cmdb_host_storage_devices view.

    Extracts physical and virtual storage device metadata.
    One row per device per stored record. Includes partition metadata as JSONB.

    The fact data is inconsistently typed: ``virtual`` arrives as a JSON
    number while ``removable`` arrives as a string. Comparing the ``->>``
    text extraction handles both uniformly.

    ``links.ids`` is an array, so Fibre detection tests each element rather
    than regexing the rendered JSON, which would otherwise match text in
    adjacent list entries. The pattern is anchored so that 'wwn-0x6' matches
    only a leading NAA-6 identifier.

    No ORDER BY is defined; a view's ordering is not preserved by an outer
    query, so sorting here would only cost a sort on every access.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_storage_devices AS
SELECT
      fi.id AS inventory_id
    , fi.client_address
    , fi.updated_at
    , devices.device_name
    , (fi.system_facts ->> 'machine_id') AS machine_id
    , (devices.device_data ->> 'model') AS device_model
    , (devices.device_data ->> 'vendor') AS device_vendor
    , (devices.device_data ->> 'serial') AS device_serial
    , (devices.device_data ->> 'wwn') AS device_wwn
    , NULLIF(NULLIF(devices.device_data ->> 'sectors', ''), 'NULL')::bigint
        * NULLIF(NULLIF(devices.device_data ->> 'sectorsize', ''), 'NULL')::bigint
        AS device_size_bytes
    , COALESCE(NULLIF(devices.device_data ->> 'virtual', '') = '1', FALSE)
        AS is_virtual
    , COALESCE(NULLIF(devices.device_data ->> 'removable', '') = '1', FALSE)
        AS is_removable
    , COALESCE(NULLIF(devices.device_data ->> 'rotational', '') = '1', FALSE)
        AS is_rotational
    , COALESCE(
        EXISTS (
            SELECT 1
            FROM
                JSONB_ARRAY_ELEMENTS_TEXT(
                    devices.device_data -> 'links' -> 'ids'
                ) AS link_id
            WHERE link_id ~ '^(fc-|wwn-0x6)'
        )
        , FALSE
    ) AS is_fibre
FROM fact_inventory AS fi
CROSS JOIN LATERAL JSONB_EACH(fi.system_facts -> 'devices')
    AS devices (device_name, device_data)
""")


def _create_cmdb_host_os_info() -> None:
    """Create cmdb_host_os_info view.

    Extracts operating system and machine metadata.
    One row per stored record; because fact_inventory is append-only, a host
    with history contributes one row per submission. Filter on updated_at or
    inventory_id to select a single point in time.
    """
    op.execute("""
CREATE OR REPLACE VIEW cmdb_host_os_info AS
SELECT
      fi.id AS inventory_id
    , fi.client_address
    , fi.updated_at
    , (fi.system_facts ->> 'machine_id') AS machine_id
    , (fi.system_facts ->> 'fqdn') AS fqdn
    , (fi.system_facts ->> 'distribution') AS os_name
    , (fi.system_facts ->> 'distribution_version') AS os_version
    , (fi.system_facts ->> 'os_family') AS os_family
    , (fi.system_facts ->> 'kernel') AS kernel
FROM fact_inventory AS fi
""")
