# Database Views

The `fact_inventory` table stores arbitrary JSON in `system_facts`,
`package_facts`, and `local_facts`. PostgreSQL views project frequently
needed fields as queryable columns, eliminating repeated JSONB operator
syntax in SQL.

## Built-in CMDB Views

The following views are automatically created by database migrations. Refer to
migration `migrations/versions/add_cmdb_views_000000000001.py` for full column
documentation and JSON path references. Schema details are maintained there;
this document is not updated when view definitions change.

Views are PostgreSQL-only, read-only projections with no storage overhead and reflect
current data at query time.

**`fact_inventory` is append-only**, so a host that has submitted facts
repeatedly has one row per submission, not one row per host. Every view
inherits that: a "one row per host" reading is only correct when a single
record exists per `client_address`. To select a single point in time, filter
on `updated_at`:

```sql
-- Most recent record per host
SELECT DISTINCT ON (client_address) *
  FROM cmdb_host_os_info
 ORDER BY client_address, updated_at DESC;
```

### cmdb_host_interfaces

All non-loopback network interfaces per host. One row per interface.

**Key columns:** `inventory_id`, `client_address`, `updated_at`, `machine_id`,
`fqdn`, `interface_name`, `device_type`, `mac_address`, `is_active`, `mtu`,
`link_speed`, `module`, `pci_id`

```sql
-- All active interfaces across hosts
SELECT client_address, interface_name, device_type, mac_address
  FROM cmdb_host_interfaces
 WHERE is_active;

-- Find hosts with a specific interface type
SELECT DISTINCT client_address
  FROM cmdb_host_interfaces
 WHERE device_type = 'ether';
```

### cmdb_host_interface_ipv4_addresses

IPv4 addresses and derived netmask/network info per interface. One row per
address per interface per stored record. Excludes loopback (127.0.0.0/8).

**Key columns:** `inventory_id`, `client_address`, `machine_id`, `updated_at`,
`fqdn`, `interface_name`, `device_type`, `mac_address`, `is_active`,
`ipv4_cidr`, `ipv4_address`, `ipv4_netmask`, `ipv4_prefix`, `ipv4_network`,
`ipv4_broadcast`

`ipv4_cidr` is the native PostgreSQL `inet` value carrying both address and
prefix (`131.225.220.34/24`). Every other address column is derived from it,
so the full range of `inet` operators works directly:

```sql
-- Find all hosts on a specific subnet.
-- Use <<= rather than <<: ipv4_cidr carries the interface prefix, so an
-- address like 10.0.0.5/24 is not *strictly* inside 10.0.0.0/24.
SELECT DISTINCT client_address, interface_name
  FROM cmdb_host_interface_ipv4_addresses
 WHERE ipv4_cidr <<= '10.0.0.0/24'::inet;

-- Hosts sharing a subnet with a given address
SELECT client_address, ipv4_cidr
  FROM cmdb_host_interface_ipv4_addresses
 WHERE ipv4_network = network('131.225.220.34/24'::inet);

-- List all IPv4 addresses by host
SELECT client_address, interface_name, ipv4_address, ipv4_netmask
  FROM cmdb_host_interface_ipv4_addresses
 WHERE ipv4_address IS NOT NULL;
```

### cmdb_host_interface_ipv6_addresses

IPv6 addresses and prefix info per interface. One row per address per
interface per stored record. Excludes loopback (::1).

**Key columns:** `inventory_id`, `client_address`, `updated_at`, `machine_id`,
`fqdn`, `interface_name`, `device_type`, `mac_address`, `is_active`,
`ipv6_cidr`, `ipv6_address`, `ipv6_prefix`, `ipv6_network`, `ipv6_scope`

As with IPv4, `ipv6_cidr` is the native `inet` value (`fe80::.../64`) and the
other columns are derived from it.

```sql
-- Find all link-local IPv6 addresses
SELECT client_address, interface_name, ipv6_address, ipv6_scope
  FROM cmdb_host_interface_ipv6_addresses
 WHERE ipv6_scope = 'link';

-- IPv6 addresses by host
SELECT client_address, interface_name, ipv6_address, ipv6_prefix
  FROM cmdb_host_interface_ipv6_addresses
 WHERE ipv6_address IS NOT NULL;
```

### cmdb_host_hardware

CPU, chassis, and hardware identification metadata. One row per stored record.

**Key columns:** `inventory_id`, `client_address`, `updated_at`, `machine_id`,
`board_vendor`, `board_model`, `product_name`, `product_version`,
`product_uuid`, `product_serial`, `chassis_form_factor`, `system_arch`,
`cpu_manufacturer`, `cpu_model_name`, `cpu_socket_count`, `cpu_core_count`,
`cpu_thread_count`, `ram_bytes`

```sql
-- Hosts grouped by CPU model
SELECT cpu_model_name, count(*) AS host_count
  FROM cmdb_host_hardware
 GROUP BY cpu_model_name
 ORDER BY host_count DESC;

-- Find hosts with insufficient cores (< 4)
SELECT client_address, cpu_model_name, cpu_core_count, ram_bytes
  FROM cmdb_host_hardware
 WHERE cpu_core_count < 4;
```

### cmdb_host_storage_devices

Physical and virtual storage devices. One row per device per host.

**Key columns:** `inventory_id`, `client_address`, `updated_at`, `machine_id`,
`device_name`, `device_model`, `device_vendor`, `device_serial`, `device_wwn`,
`device_size_bytes`, `is_rotational`, `is_virtual`, `is_removable`, `is_fibre`

```sql
-- Total storage per host
SELECT client_address,
       count(*) AS device_count,
       sum(device_size_bytes) AS total_bytes
  FROM cmdb_host_storage_devices
 WHERE NOT is_virtual
 GROUP BY client_address;

-- Physical spinning disks (HDDs)
SELECT client_address, device_name, device_model, device_size_bytes
  FROM cmdb_host_storage_devices
 WHERE is_rotational AND NOT is_virtual;
```

### cmdb_host_os_info

Operating system and machine metadata. One row per stored record.

**Key columns:** `inventory_id`, `client_address`, `updated_at`, `machine_id`,
`fqdn`, `os_name`, `os_version`, `os_family`, `kernel`

```sql
-- Count hosts by OS distribution and version
SELECT os_version, count(*) AS host_count
  FROM cmdb_host_os_info
 GROUP BY os_version
 ORDER BY host_count DESC;

-- Hosts running a specific kernel version
SELECT client_address, fqdn, os_version, kernel
  FROM cmdb_host_os_info
 WHERE kernel ~ '^6\.1';
```

## Example Custom Views

The following are template patterns for creating additional views tailored
to your fact schema. Adjust JSON paths and field names to match the facts
your clients submit.

## Stale Hosts

Hosts that have not checked in within a given window. Useful for monitoring and alerting. Adjust the interval based on your retention policy (see `RETENTION_DAYS` setting).

```sql
CREATE OR REPLACE VIEW stale_hosts AS
SELECT client_address
     , system_facts->>'hostname'  AS hostname
     , updated_at
     , now() - updated_at         AS time_since_update
FROM fact_inventory
WHERE updated_at < now() - interval '7 days';
```

**Note**: This example uses a 7-day threshold. Modify the interval to match your operational requirements.

```sql
SELECT * FROM stale_hosts ORDER BY time_since_update DESC;
```

## Package Inventory

Unnest the `package_facts` JSONB object so each package name becomes its own row. This makes it straightforward to search for a specific package across all hosts:

```sql
CREATE OR REPLACE VIEW package_inventory AS
SELECT hf.client_address
     , hf.system_facts->>'hostname'  AS hostname
     , pkg.key                       AS package_name
     , pkg.value                     AS package_versions
FROM fact_inventory hf,
     LATERAL jsonb_each(hf.package_facts) AS pkg(key, value);
```

```sql
-- Find every host with openssl installed
SELECT client_address
     , hostname
     , package_versions
  FROM package_inventory
 WHERE package_name = 'openssl';

-- Count how many hosts have a given package
SELECT package_name, count(*) AS host_count
  FROM package_inventory
 GROUP BY package_name
 ORDER BY host_count DESC
 LIMIT 20;
```

## Distribution Summary

Aggregate view showing how many hosts run each OS distribution and version:

```sql
CREATE OR REPLACE VIEW distribution_summary AS
SELECT system_facts->>'distribution'                AS distribution
     , system_facts->>'distribution_version'        AS version
     , system_facts->>'distribution_major_version'  AS major_version
     , count(*)                                     AS host_count
FROM fact_inventory
GROUP BY system_facts->>'distribution'
       , system_facts->>'distribution_version'
       , system_facts->>'distribution_major_version';
```

```sql
SELECT * FROM distribution_summary ORDER BY host_count DESC;
```

## Network Addresses

If the Ansible `setup` module collects network facts, you can extract interface details:

```sql
CREATE OR REPLACE VIEW host_network AS
SELECT client_address
     , system_facts->>'hostname'                   AS hostname
     , system_facts->'default_ipv4'->>'address'    AS default_ipv4
     , system_facts->'default_ipv6'->>'address'    AS default_ipv6
     , system_facts->'default_ipv4'->>'interface'  AS ipv4_interface
     , system_facts->'default_ipv6'->>'interface'  AS ipv6_interface
FROM fact_inventory;
```

```sql
-- Find hosts on a specific subnet
SELECT client_address
     , hostname
     , default_ipv4
  FROM host_network
 WHERE default_ipv4 IS NOT NULL
   AND default_ipv4::inet <<= '10.0.0.0/8'::inet;
```
