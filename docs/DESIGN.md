# Design

The application follows a layered architecture within the `fact_inventory` package:

- **lib/**: Application infrastructure (settings, logging, exceptions)
- **domain/**: Business rules and domain constraints
- **application/**: Service layer (business logic orchestration)
- **presentation/**: HTTP layer (endpoints, routes, DTOs)
- **server/**: Application assembly and plugins
- **infrastructure/**: Technical infrastructure (database, middleware)

## API

### Routing architecture

All route handlers are defined within the `presentation/` subpackage.
The standalone application factory (`create_app` in `fact_inventory.server.app`) serves them at the root path `/`
by default (suitable for production behind a reverse proxy that strips the prefix).

`create_app` in `fact_inventory.server.app` is the actual application factory function.
`fact_inventory.app_factory` is an alias for it, intended for use with an ASGI
server's factory mode (e.g. `uvicorn fact_inventory:app_factory --factory`);
using factory mode defers configuration loading until the server calls it.
`fact_inventory.app` is an eagerly-constructed instance (`create_app()` called
at import time) and therefore requires configuration to already be set
before the module is imported.

When embedding in a larger Litestar application, pass a custom prefix to `create_router` in `fact_inventory.presentation.router`:

```python
from fact_inventory.presentation.router import create_router

router = create_router(path="/fact_inventory")
```

For development direct access without a reverse proxy, use the `APP_PREFIX` setting.
Note that `APP_PREFIX` must include the leading slash:

```bash
APP_PREFIX=/fact_inventory uvicorn fact_inventory:app_factory --factory
```

In production behind a reverse proxy that strips the prefix, set `APP_PREFIX=/`.

### Unversioned non-api routes

These operational endpoints are not tied to any API version and have no
database-layer dependencies beyond the readiness probe.
All unversioned routes are excluded from rate limiting.

| Method | Path       | Description                                                                                 | Enabled By               |
| ------ | ---------- | ------------------------------------------------------------------------------------------- | ------------------------ |
| `GET`  | `/`        | Root handler -- HTTP 200 with service name and version                                      | Always enabled           |
| `GET`  | `/health`  | Liveness probe -- HTTP 200 while the process is alive                                       | `enable_health_endpoint` |
| `GET`  | `/ready`   | Readiness probe -- HTTP 200 when the database is reachable (`SELECT 1`), HTTP 503 otherwise | `enable_ready_endpoint`  |
| `GET`  | `/metrics` | Prometheus metrics endpoint for monitoring                                                  | `enable_metrics`         |

All optional endpoints are disabled by default. Enable them via environment variables.

### /api/v1

The API router (`fact_inventory/presentation/api/router.py`) applies rate limiting to all `/api/v1/*` routes.
The unversioned non-API routes (`/`, `/health`, `/ready`, `/metrics`) are excluded from rate limiting.

- **Controller Layer** (`fact_inventory/presentation/api/v1/factcontroller.py`): HTTP endpoint handlers with request validation. The controller is responsible only for validation and persistence.
- **Service Layer** (`fact_inventory/application/services/fact.py`): Business logic without database-specific behavior. Perform JSON field size validation to enforce policy constraints.
- **Response Models** (`fact_inventory/presentation/api/v1/schemas/responses.py`): Pydantic response envelopes for the v1 API.

#### /api/v1/facts

**Endpoint**: `POST /api/v1/facts`

**Content-Type**: `application/json`

**Request Body**:

```json
{
  "system_facts": {},
  "package_facts": {},
  "local_facts": {},
  "client_facts": {}
}
```

**Response Codes**:

- `201 CREATED`: Facts stored successfully
- `400 BAD REQUEST`: Client address could not be determined
- `409 CONFLICT`: Database error during storage (constraint violation, connection error, etc.)
- `413 PAYLOAD TOO LARGE`: Request payload exceeds configured size limits
- `429 TOO MANY REQUESTS`: Client IP has exceeded rate limit
- `500 INTERNAL SERVER ERROR`: Unexpected application error

**Rate Limiting**:

Rate limiting is handled by Litestar's `RateLimitConfig.middleware` in `fact_inventory/presentation/api/router.py`. Health, readiness, and metrics probes are excluded from rate limiting via path patterns. The middleware uses an in-memory store; rate-limit state resets on server restart. Default configuration is 2 requests per hour per IP address (configurable via `API_RATE_LIMIT_MAX_REQUESTS` and `API_RATE_LIMIT_UNIT` environment variables).

```
HTTP/1.1 429 Too Many Requests
RateLimit-Limit: 2
RateLimit-Remaining: 0
RateLimit-Reset: <seconds>
```

Standard `RateLimit-*` response headers are included on every response.

##### Example with curl

The following examples assume `APP_PREFIX=/fact_inventory`.
For a standalone deployment with `APP_PREFIX=/`, omit the prefix from the path.

```bash
curl -X POST http://localhost:8000/fact_inventory/api/v1/facts \
  -H "Content-Type: application/json" \
  -d '{
    "system_facts": {},
    "package_facts": {},
    "local_facts": {}
  }'
```

## Database

### FactInventory

Clients are identified by the IP address they use to connect to the endpoint, not by any data they submit.

- **Repository Layer** (`fact_inventory.infrastructure.db.repositories`): Database-specific query logic.
- **Model Layer** (`fact_inventory.infrastructure.db.models`): SQLAlchemy ORM models.
- **API Layer** (`fact_inventory.presentation.api.v1.schemas`): Pydantic request/response models that define the HTTP contract independently of the database schema.

#### `fact_inventory` Table

| Column           | Type             | Description                                         |
| ---------------- | ---------------- | --------------------------------------------------- |
| `id`             | UUID             | Surrogate primary key (auto-generated)              |
| `created_at`     | TIMESTAMP        | Record creation time (auto-generated)               |
| `updated_at`     | TIMESTAMP        | Record last-update time (auto-generated)            |
| `client_address` | VARCHAR(45)/INET | Client IP address (IPv4/IPv6), part of composite PK |
| `system_facts`   | JSONB            | System facts submitted by the client                |
| `package_facts`  | JSONB            | Package facts submitted by the client               |
| `local_facts`    | JSONB            | Local facts submitted by the client                 |
| `client_facts`   | JSONB            | Agent facts submitted by the client                 |

The primary key is composite, `(id, client_address)`. PostgreSQL requires the
partition key to be part of any primary key on a partitioned table, and this
table is HASH partitioned on `client_address` across 64 partitions. Practical
uniqueness is carried by `id` alone.

#### Storage Methods

The service layer (`fact_inventory.application.services`) provides one method for storing facts:

| Method            | Behavior                                                          | Use Case                                                |
| ----------------- | ----------------------------------------------------------------- | ------------------------------------------------------- |
| `insert_record()` | Always creates a new row, even if `client_address` already exists | Store historical records; multiple submissions per host |

It accepts this data structure:

```python
{
    "client_address": "192.0.2.1",
    "system_facts": {...},
    "package_facts": {...},
    "local_facts": {...},
    "client_facts": {...},
}
```

It returns the created record and raises `RepositoryError` or `SQLAlchemyError` on failure.

#### Indexes

- `ix_fact_inventory_client_address`: Index on row client_address.
- `ix_fact_inventory_created_at`: BRIN index on row creation timestamp (PostgreSQL); plain index on other backends.
- `ix_fact_inventory_updated_at`: Index on row last-update timestamp.
- `ix_fact_inventory_client_address_updated_at`: Composite index on `(client_address, updated_at)`. Serves the history-retention window function, which partitions by `client_address` and orders by `updated_at`. Two single-column indexes cannot substitute here: PostgreSQL can intersect separate indexes for filtering, but never for ordering.
- `ix_fact_inventory_system_facts`: GIN index for efficient JSONB queries (GIN is PostgreSQL only).
- `ix_fact_inventory_package_facts`: GIN index for efficient JSONB queries (GIN is PostgreSQL only).
- `ix_fact_inventory_local_facts`: GIN index for efficient JSONB queries (GIN is PostgreSQL only).
- `ix_fact_inventory_client_facts`: GIN index for efficient JSONB queries (GIN is PostgreSQL only).

#### Connection Pool & Statement Timeout

Pool sizing (`DB_POOL_SIZE`, `DB_POOL_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`) and
`DB_POOL_RECYCLE_SECONDS` are applied to the SQLAlchemy engine's connection
pool. `DB_POOL_RECYCLE_SECONDS` (default 3600) should be lowered if an
intermediate firewall or load balancer silently drops idle TCP connections
before that interval elapses, to avoid intermittent `OperationalError`s from
stale pooled connections.

`DB_STATEMENT_TIMEOUT_MS` (default 60000, 0 disables) sets PostgreSQL's
`statement_timeout` via asyncpg's `server_settings`, applied once at
connection startup rather than per-query or per-transaction. PostgreSQL then
enforces the cap on every statement issued over that connection -- API
request inserts and background-job deletes alike -- and the setting is
automatically reapplied whenever the pool opens a fresh connection (e.g.
after `DB_POOL_RECYCLE_SECONDS` elapses). Both settings are PostgreSQL-only;
SQLite (development/test only) ignores them.

## Plugins

### AsyncBackgroundJobPlugin

The `AsyncBackgroundJobPlugin` (`fact_inventory.server.background_job.plugin`) implements
Litestar's `InitPluginProtocol` to manage periodic background tasks. Multiple
plugins can run concurrently, each with its own job callback, schedule, and logging.

**Data Retention Cleanup**

- Runs `purge_facts_older_than()` to delete records with `updated_at` older
  than `RETENTION_DAYS`
- Interval: `RETENTION_CHECK_INTERVAL_HOURS`
- Jitter: `RETENTION_CHECK_JITTER_MINUTES`
- First run deferred until after one full interval plus jitter; cleanup does not run during startup

**Duplicate Fact Pruning**

- Runs `purge_fact_history_more_than()` to delete oldest records per `client_address`
  exceeding `HISTORY_MAX_ENTRIES`
  - Interval: `HISTORY_CHECK_INTERVAL_HOURS`
  - Jitter: `HISTORY_CHECK_JITTER_MINUTES`
  - First run deferred until after one full interval plus jitter; cleanup does not run during startup

Common behaviors:

- Uses `lifespan` context managers so startup and shutdown are handled in a
  single, self-contained block
- First run deferred until after the first interval and jitter; cleanup does not run
  during application startup
- Exceptions are logged but do not crash the loop; retries on next interval
- Jitter prevents thundering-herd effects across multiple instances

### Distributed Job Locking

Jitter spreads scheduling but does not guarantee exclusivity. When several
workers or servers share a database, `run_exclusive_background_job()`
(`fact_inventory.server.background_job.lock`) ensures only one instance of
a named job runs at a time.

#### `background_job_lock` Table

| Column        | Type         | Description                                           |
| ------------- | ------------ | ----------------------------------------------------- |
| `id`          | UUID         | Surrogate key (auto-generated by `UUIDBase`)          |
| `acquired_at` | TIMESTAMP    | UTC time of the most recent acquisition/heartbeat     |
| `owner_token` | UUID         | Token identifying the current lock owner (not unique) |
| `name`        | VARCHAR(255) | Job name, used as the lock key (unique)               |

A held lock is one row. Releasing deletes the row.

#### Protocol

1. **Acquire.** Insert a row with a freshly generated `owner_token`. The
   unique index on `name` is what makes this atomic: concurrent inserts
   cannot both succeed. The loser catches the integrity error and falls
   through to the staleness check.
2. **Take over if stale.** A lock is stale when `acquired_at` is older than
   `2 * interval_seconds`. A conditional `UPDATE` claims it and **rotates
   `owner_token`**. If the row is not stale, acquisition returns nothing and
   the job is skipped.
3. **Heartbeat.** While work runs, a task refreshes `acquired_at` every
   `interval_seconds / 2`, so a long job is never mistaken for a dead one.
   The heartbeat shares an event with the work task; if it discovers the lock
   was taken over, or if it cannot refresh for longer than the stale-lock
   window, it signals lease loss.
4. **Work cancellation.** The work coroutine runs as a task. On lease loss the
   task is cancelled and `run_exclusive_background_job()` raises
   `BackgroundJobLeaseLostError`. This prevents the old worker from continuing
   cleanup after another worker has taken over.
5. **Release.** A `finally` block deletes the row owned by this worker's token.

#### Owner Token Fencing

Both refresh and release are conditional on `name` _and_ `owner_token`, so a
worker can only affect the lock it actually acquired.

Token rotation on takeover is the load-bearing detail. A worker that stalls
past the staleness window has its lock taken over by a replacement. If the
takeover reused the existing token, the stalled worker would resume holding a
still-valid credential and its heartbeat would keep refreshing a lock it no
longer owns - two workers running concurrently, which is what the lock exists
to prevent. Because takeover issues a new token, the stalled worker's next
refresh matches no row, returns `None`, and it signals lease loss. The active
work task is cancelled, the worker raises `BackgroundJobLeaseLostError`, and
its release becomes a no-op that cannot delete the replacement's lock.

#### Failure Handling

- Lock unavailable: job logs and returns `0`; no work is done.
- Heartbeat database error: logged, loop continues. If the heartbeat cannot
  refresh for longer than the stale-lock window, lease loss is signalled and
  the active work task is cancelled.
- Heartbeat reports lost ownership: lease loss is signalled, the active work
  task is cancelled, and `BackgroundJobLeaseLostError` is raised.
- Release failure: logged; the row is left to expire via the staleness path.

Because a lost or stale lock leads to takeover rather than deadlock, a crashed
worker cannot block a job permanently.
