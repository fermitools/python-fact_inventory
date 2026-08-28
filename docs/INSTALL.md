# Installation

This guide covers installing and configuring fact_inventory for standalone operation.
For deployment patterns (bare metal, Kubernetes, embedding), see [DEPLOYMENT.md](DEPLOYMENT.md).
For development setup, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Requirements

- **Python 3.12+**
- **PostgreSQL 16+** (recommended for JSONB and GIN index performance)
- **uv** (Python package installer; see https://docs.astral.sh/uv/)

**pyproject.toml** keeps a record of the required python modules.

## Quick Start

1. **Clone the repository:**

```bash
git clone <repository-url>
cd python-fact_inventory
```

2. **Install dependencies:**

**NOTE**: For a fully isolated python:

```bash
export UV_PYTHON_INSTALL_DIR="$(pwd)/.python3"
uv python install 3
uv venv --python 3 --seed --link-mode copy --prompt fact_inventory
echo "installing dependencies"
source .venv/bin/activate
uv sync --link-mode=copy
```

**NOTE**: For use of a homedir uv cache

```bash
uv sync
```

3. **Set up environment configuration:**

Create a `.env.production` file (or `.env.{DEPLOYMENT}` for your target environment):

```bash
DATABASE_URI=postgresql+asyncpg://user:password@localhost/dbname
```

4. **Start the application:**

```bash
export WEB_CONCURRENCY=8
DEPLOYMENT=XYZ uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000 --workers "$WEB_CONCURRENCY"
```

The application is now listening on `http://0.0.0.0:8000/api/v1/facts` with 8 workers.

## Configuration

Configuration is managed through environment variables. The `DEPLOYMENT` variable selects which `.env` file to load and sets the `deployment_environment` field:

```bash
export DEPLOYMENT=production  # loads .env.production, sets deployment_environment="production"
export DEPLOYMENT=staging     # loads .env.staging, sets deployment_environment="staging"
export DEPLOYMENT=testing     # loads .env.testing, sets deployment_environment="testing"
```

### Environment Variables

All available configuration options are defined in [../fact_inventory/lib/settings.py](../fact_inventory/lib/settings.py).

Common settings:

| Variable              | Description                                                            |
| --------------------- | ---------------------------------------------------------------------- |
| `DEPLOYMENT`          | Environment name (e.g., `production`, `staging`, `testing`)            |
| `DATABASE_URI`        | PostgreSQL connection string: `postgresql+asyncpg://user:pass@host/db` |
| `DEBUG`               | Enable debug mode and OpenAPI documentation                            |
| `RETENTION_DAYS`      | Keep facts for this many days before auto-purge                        |
| `HISTORY_MAX_ENTRIES` | Keep this many newest facts per client after deduplication             |

### Database Configuration

PostgreSQL 16+ is recommended for JSONB and GIN index performance. For development, SQLite is supported but not recommended for production.

**PostgreSQL connection string format:**

```
postgresql+asyncpg://username:password@hostname:port/database_name
```

Example with local PostgreSQL:

```bash
export DATABASE_URI="postgresql+asyncpg://fact_user:secure_password@localhost:5432/fact_inventory"
```

**Connection pool and statement timeout** (PostgreSQL only; ignored for SQLite):

`DB_POOL_RECYCLE_SECONDS` recycles pooled connections after N seconds (lower
this if a firewall or load balancer silently drops idle connections sooner).
`DB_STATEMENT_TIMEOUT_MS` aborts any single statement that runs longer than N
milliseconds, applied to every query on the connection (API requests and
background cleanup jobs alike); 0 disables the timeout. See
[../fact_inventory/lib/settings.py](../fact_inventory/lib/settings.py) for
current defaults and valid ranges.

### Rate Limiting

Rate limiting is configured with two environment variables. The default is 2 requests per hour per IP address. Rate limiting is based on the client's IP address and can be bypassed with IP rotation.

```bash
export API_RATE_LIMIT_MAX_REQUESTS=10
export API_RATE_LIMIT_UNIT=hour
```

### Payload Size Limits

If clients submit large payloads:

```bash
# Increase total request body limit to 50 MB
export MAX_REQUEST_BODY_MB=50

# Increase per-field limit to 10 MB
export MAX_JSON_FIELD_MB=10

# Ensure total > 4 x per-field (there are 4 JSON fact fields)
# Example: 50 > 4 * 10 (40) -- OK
```

### Data Retention

Purge old facts automatically:

```bash
# Keep facts for 180 days (6 months)
export RETENTION_DAYS=180

# Check for expired facts every 24 hours
export RETENTION_CHECK_INTERVAL_HOURS=24

# Add up to 30 minutes of random jitter to prevent thundering-herd
export RETENTION_CHECK_JITTER_MINUTES=30
```

### Deduplication

Keep only the newest facts per client:

```bash
# Keep the 5 newest facts per client
export HISTORY_MAX_ENTRIES=5

# Check for duplicates every 12 hours
export HISTORY_CHECK_INTERVAL_HOURS=12

# Add up to 15 minutes of random jitter
export HISTORY_CHECK_JITTER_MINUTES=15
```

## Database Setup

For production, use Litestar's Alembic migrations to manage schema changes safely across deployments.

**Why use Alembic?**

- Version control for database schema
- Safe rollback if migrations fail
- Audit trail of schema changes
- Can be scripted into CI/CD pipelines
- Works across multiple instances without race conditions

**Setup:**

1. Run migrations against your production database:

```bash
DEPLOYMENT=production uv run litestar --app fact_inventory:app database upgrade
```

2. Start the application (tables will already exist):

```bash
DEPLOYMENT=production uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

## Running the Application

### Standalone with Uvicorn

Start the ASGI server:

```bash
DEPLOYMENT=production uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

The application listens on `http://0.0.0.0:8000`.

**Important deployment note**: By default, `APP_PREFIX=/` because a reverse proxy (nginx, Apache, or Kubernetes Ingress) strips the external `/fact_inventory` prefix before forwarding. The application internally sees clean paths like `/api/v1/facts`.

For direct development access without a reverse proxy, override the prefix:

```bash
APP_PREFIX=/fact_inventory uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

Then access the API at `http://localhost:8000/fact_inventory/api/v1/facts`.

### With systemd (Bare Metal)

Create `/etc/systemd/system/fact-inventory.service`:

```ini
[Unit]
Description=Fact Inventory Service
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=notify
# Note: Uvicorn sends SYS_READY on startup when Type=notify is used.
# Monitor with: systemctl is-active fact-inventory
# or enable health endpoint with: ENABLE_HEALTH_ENDPOINT=true
User=fact_inventory
Group=fact_inventory
WorkingDirectory=/opt/fact_inventory
Environment="DEPLOYMENT=production"
ExecStart=/opt/fact_inventory/.venv/bin/uvicorn fact_inventory:app_factory --factory --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fact-inventory
sudo systemctl start fact-inventory
```

View logs:

```bash
sudo journalctl -u fact-inventory -f
```

### Debug Mode

Enable OpenAPI documentation:

```bash
DEPLOYMENT=testing DEBUG=true uvicorn fact_inventory:app_factory --factory
```

Then visit:

- OpenAPI spec: `http://localhost:8000/fact_inventory/schema`
- Swagger UI: `http://localhost:8000/fact_inventory/schema/swagger`

(Replace `/fact_inventory` with your configured `APP_PREFIX` if different.)

## Health Checks

The application provides health and readiness endpoints for monitoring. Both are
disabled by default; enable them with environment variables:

```bash
export ENABLE_HEALTH_ENDPOINT=true
export ENABLE_READY_ENDPOINT=true
```

- **Health**: `GET /health` (or `GET /fact_inventory/health` if using `APP_PREFIX=/fact_inventory`)
- **Ready**: `GET /ready` (or `GET /fact_inventory/ready` if using `APP_PREFIX=/fact_inventory`)

Example with curl:

```bash
DEPLOYMENT=testing ENABLE_HEALTH_ENDPOINT=true uvicorn fact_inventory:app_factory --factory &
curl http://localhost:8000/health
```

These are useful for container orchestration probes and load balancer health checks.

## Troubleshooting

### Database Connection Failed

Verify PostgreSQL is running and the connection string is correct:

```bash
psql "postgresql://fact_user@localhost/fact_inventory"
```

If that works, verify the async connection string with the correct driver:

```bash
export DATABASE_URI="postgresql+asyncpg://fact_user@localhost/fact_inventory"
```

### HTTP 413 Request Entity Too Large

Two possible causes:

1. **Total request body exceeds `MAX_REQUEST_BODY_MB`**
   - Increase `MAX_REQUEST_BODY_MB`
   - Ensure `MAX_REQUEST_BODY_MB > 4 x MAX_JSON_FIELD_MB`

2. **Single JSON field exceeds `MAX_JSON_FIELD_MB`**
   - Increase `MAX_JSON_FIELD_MB`
   - Check logs for the specific field name

Example fix:

```bash
export MAX_REQUEST_BODY_MB=50
export MAX_JSON_FIELD_MB=10
```

### Rate Limited (HTTP 429)

Clients are rate-limited by IP by default.

- Adjust `API_RATE_LIMIT_MAX_REQUESTS` and `API_RATE_LIMIT_UNIT`
- Note: Rate limits can be bypassed with IP rotation

### Application Won't Start

Check environment variables:

```bash
echo $DEPLOYMENT
echo $DATABASE_URI
```

Ensure `.env.{DEPLOYMENT}` exists in the application directory and contains `DATABASE_URI`.

### High Database Load

The background cleanup tasks (retention and history pruning) run after startup on
their configured intervals with random jitter. They do not run during startup.
If you see periodic spikes in database load that impact performance, increase the jitter:

```bash
export RETENTION_CHECK_JITTER_MINUTES=60
export HISTORY_CHECK_JITTER_MINUTES=60
```

Or increase the check intervals to run less frequently:

```bash
export RETENTION_CHECK_INTERVAL_HOURS=48
export HISTORY_CHECK_INTERVAL_HOURS=24
```

## Next Steps

- **Deployment**: See [DEPLOYMENT.md](DEPLOYMENT.md) for bare metal, Kubernetes, and embedding patterns
- **Querying**: See [VIEWS.md](VIEWS.md) for SQL examples and pre-built views
