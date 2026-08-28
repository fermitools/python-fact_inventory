# Deployment

## Core Concept: The `/fact_inventory` Prefix

The fact_inventory application is built with `/fact_inventory` as a URL prefix. This design allows the same app code to work in three different environments:

1. **Bare metal** (single app on a webserver): Put nginx/Apache in front to handle the prefix
2. **Kubernetes** (containerized, shared ingress): Kubernetes Ingress routing handles the prefix
3. **Embedded** (part of a larger app): Mount it as a sub-router in a parent Litestar app

All three scenarios _strip_ the `/fact_inventory` prefix before the request reaches the application code. This means the application only sees clean paths like `/api/v1/facts`, not `/fact_inventory/api/v1/facts`.

### TL;DR for deployers

- **Bare metal**: Run Uvicorn, put nginx/Apache in front with prefix stripping
- **Kubernetes**: Use an Ingress controller with prefix stripping annotation
- **Embedded**: Use `create_router(path="/fact_inventory")` in your parent app

Pick the section that matches your environment.

## Uvicorn (Standalone ASGI Server)

Start the ASGI server on all interfaces:

```bash
export WEB_CONCURRENCY=8
uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

> **Note**: `litestar` CLI is an alternative but `uvicorn` is recommended for production.

## Gunicorn (ASGI Process Manager)

Gunicorn can manage uvicorn workers for production deployments. This provides process management, automatic restarts, and worker lifecycle control.

### Basic Usage

```bash
export WEB_CONCURRENCY=8
gunicorn fact_inventory:app_factory \
  --factory \
  --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers ${WEB_CONCURRENCY:-4}
```

### With Systemd

Create `/etc/systemd/system/fact-inventory.service`:

```ini
[Unit]
Description=Fact Inventory API
After=network.target

[Service]
Type=simple
User=fact_inventory
Group=fact_inventory
WorkingDirectory=/opt/fact-inventory
Environment="DEPLOYMENT=production"
Environment="DATABASE_URI=postgresql+asyncpg://user:pass@localhost/fact_inventory"
ExecStart=/opt/fact-inventory/.venv/bin/gunicorn \
  fact_inventory:app_factory \
  --factory \
  --worker-class uvicorn_worker.UvicornWorker \
  --bind 127.0.0.1:8000 \
  --workers ${WEB_CONCURRENCY:-4}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fact-inventory
sudo systemctl start fact-inventory
```

### Worker Tuning

For high-concurrency I/O workloads, consider async workers (requires gthread worker class):

```bash
# High concurrency with fewer processes
WEB_CONCURRENCY=2 gunicorn \
  fact_inventory:app_factory \
  --factory \
  --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers ${WEB_CONCURRENCY} \
  --threads 4 \
  --timeout 30
```

> **Note**: The `uvicorn_worker.UvicornWorker` class allows gunicorn to manage uvicorn's ASGI server as subprocesses.

### Gunicorn Configuration File

Create `gunicorn.conf.py`:

```python
import multiprocessing

workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "uvicorn_worker.UvicornWorker"
bind = "0.0.0.0:8000"
timeout = 30
keep_alive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
```

Run with:

```bash
DEPLOYMENT=production gunicorn fact_inventory:app_factory --factory -c gunicorn.conf.py
```

### Expected request paths when Uvicorn runs standalone

When accessing Uvicorn directly (no proxy in front), use these paths. The `APP_PREFIX` setting (default `/`) prepends to all paths:

| Request Path    | Handler                   | Enabled by default | Feature Flag             |
| --------------- | ------------------------- | ------------------ | ------------------------ |
| `/api/v1/facts` | `FactInventoryController` | Yes                | N/A                      |
| `/health`       | `health_check()`          | No                 | `ENABLE_HEALTH_ENDPOINT` |
| `/ready`        | `ready_check()`           | No                 | `ENABLE_READY_ENDPOINT`  |
| `/metrics`      | Prometheus metrics        | No                 | `ENABLE_METRICS`         |

> **Note**: Health, readiness, and metrics endpoints are disabled by default. Enable via environment variables if needed.

In production, you will **not** access Uvicorn directly. Instead, put a reverse proxy (nginx, Apache, or Kubernetes Ingress) in front of it.

### Controlling the URL Prefix

By default, `APP_PREFIX=/` because the reverse proxy strips the external `/fact_inventory` prefix and forwards clean paths to the application.

For development or testing direct access to Uvicorn, use `APP_PREFIX` to expose the application at a specific path:

```bash
# Development: access the app directly at /fact_inventory
APP_PREFIX=/fact_inventory uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

In development (direct access), you would use:

- Request path: `/fact_inventory/health`
- App receives: `/fact_inventory/health`
- Routes at: `/fact_inventory/health`

For production, don't override `APP_PREFIX`. Let it default to `/` and put a reverse proxy in front to add the external prefix:

```bash
# Production: app runs at root, proxy adds the prefix
uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

In production (behind reverse proxy):

- Client request: `https://inventory.example.com/fact_inventory/health`
- Proxy strips prefix and forwards: `/health`
- App receives and routes at: `/health`
- Logs have: `"service.name": "fact_inventory"`

If you need to change the service name in logs (separate from routing), use `APP_NAME`:

```bash
# Change both service name and default prefix (for non-proxy deployments)
APP_NAME=my_facts APP_PREFIX=/my_facts uvicorn fact_inventory:app_factory --factory --host 0.0.0.0 --port 8000
```

> **Warning**: Changing `APP_NAME` affects the logging service name in all logs. Set it carefully and consistently across deployments.

---

## Bare Metal Deployment (Single Webserver)

### Setup steps

1. Start Uvicorn on localhost, port 8000 (or your chosen port)
2. Configure your webserver (nginx or Apache) to:
   - Listen on port 443 (HTTPS)
   - Accept requests at `https://inventory.example.com/fact_inventory/...`
   - **Strip the `/fact_inventory` prefix** before forwarding to Uvicorn
   - Forward to `http://localhost:8000/...` (no prefix)

### Why strip the prefix?

The application code internally expects clean paths like `/api/v1/facts`. If you forward the full path `/fact_inventory/api/v1/facts` to Uvicorn, the routing fails. The webserver must remove the prefix so Uvicorn sees only `/api/v1/facts`.

### nginx (Recommended)

nginx receives external requests at `https://inventory.example.com/fact_inventory/...`, strips the prefix, and forwards to Uvicorn at `http://localhost:8000/...`.

**Request flow:**

```
Client
  |
  | POST https://inventory.example.com/fact_inventory/api/v1/facts
  |
  v
nginx (listens on 443)
  | strips /fact_inventory prefix
  v
Uvicorn (listens on 127.0.0.1:8000)
  | receives: POST /api/v1/facts
  |
  v
Application router
```

**nginx configuration:**

```nginx
upstream fact_inventory {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl;
    server_name inventory.example.com;

    # Redirect bare prefix to trailing slash for consistency
    location = /fact_inventory {
        return 301 /fact_inventory/;
    }

    # Strip /fact_inventory prefix, proxy to Uvicorn at /
    location /fact_inventory/ {
        proxy_pass         http://fact_inventory/;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Forwarded-Host  $host;
    }
}
```

#### Request flow example

| External URL                                                | What Uvicorn receives | Notes                               |
| ----------------------------------------------------------- | --------------------- | ----------------------------------- |
| `https://inventory.example.com/fact_inventory/api/v1/facts` | `POST /api/v1/facts`  | Always available                    |
| `https://inventory.example.com/fact_inventory/health`       | `GET /health`         | Only if ENABLE_HEALTH_ENDPOINT=true |
| `https://inventory.example.com/fact_inventory/ready`        | `GET /ready`          | Only if ENABLE_READY_ENDPOINT=true  |
| `https://inventory.example.com/fact_inventory/metrics`      | `GET /metrics`        | Only if ENABLE_METRICS=true         |

> **Key detail**: The trailing slash in `proxy_pass http://fact_inventory/` (note the `/` at the end) tells nginx to _replace_ the matched prefix with the target path. Without it, the prefix would be _appended_.

### Apache (mod_proxy)

```apache
<VirtualHost *:443>
    ServerName inventory.example.com

    SSLEngine on
    SSLCertificateFile    /etc/pki/tls/certs/inventory.example.com.crt
    SSLCertificateKeyFile /etc/pki/tls/private/inventory.example.com.key

    # Enable required modules:
    #   a2enmod proxy proxy_http headers rewrite

    # Strip /fact_inventory prefix, proxy to Uvicorn at /
    ProxyPreserveHost On
    ProxyPass        /fact_inventory/ http://127.0.0.1:8000/
    ProxyPassReverse /fact_inventory/ http://127.0.0.1:8000/

    # Rewrite redirect responses from Uvicorn
    ProxyPassReverseCookiePath / /fact_inventory/

    # Forward real client IP for rate limiting
    RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}e"
    RequestHeader set X-Forwarded-Proto "https"

    # Redirect bare prefix to trailing slash
    RedirectMatch ^/fact_inventory$ /fact_inventory/
</VirtualHost>
```

---

## Kubernetes Deployment (Containerized)

For Kubernetes, use the same **prefix-stripping** pattern via an Ingress controller (nginx-ingress, HAProxy, Traefik, etc.).

**Request flow:**

```
External client
  |
  | POST https://inventory.example.com/fact_inventory/api/v1/facts
  |
  v
Kubernetes Ingress controller (nginx-ingress)
  | strips /fact_inventory prefix
  v
Service (fact-inventory-svc on port 8000)
  | routes to Pod
  v
Pod running Uvicorn
  | receives: POST /api/v1/facts
  |
  v
Application router
```

### Ingress configuration (nginx-ingress controller)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fact-inventory
  annotations:
    nginx.ingress.kubernetes.io/strip-prefix: "/fact_inventory"
spec:
  ingressClassName: nginx
  rules:
    - host: inventory.example.com
      http:
        paths:
          - path: /fact_inventory
            pathType: Prefix
            backend:
              service:
                name: fact-inventory-svc
                port:
                  number: 8000
          - path: /fact_inventory/
            pathType: Prefix
            backend:
              service:
                name: fact-inventory-svc
                port:
                  number: 8000
```

The ingress controller strips `/fact_inventory` and forwards to your pod. Your pod runs Uvicorn on port 8000.

### Service and Deployment manifests

````yaml
apiVersion: v1
kind: Service
metadata:
  name: fact-inventory-svc
spec:
  selector:
    app: fact-inventory
  ports:
    - port: 8000
      targetPort: 8000
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fact-inventory
spec:
  replicas: 2
  selector:
    matchLabels:
      app: fact-inventory
  template:
    metadata:
      labels:
        app: fact-inventory
    spec:
      containers:
        - name: app
          image: fact-inventory:latest
          ports:
            - containerPort: 8000
          env:
            - name: DEPLOYMENT
              value: "production"
            - name: ENABLE_METRICS
              value: "true"
            - name: ENABLE_HEALTH_ENDPOINT
              value: "true"
            - name: ENABLE_READY_ENDPOINT
              value: "true"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /ready
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 5

The container exposes its health and readiness endpoints directly at `/health`
and `/ready` because APP_PREFIX defaults to `/`. Remote clients use the
external path `/fact_inventory/health` and `/fact_inventory/ready`; the ingress
controller or load balancer strips the `/fact_inventory` prefix before forwarding.

### Metrics Endpoint

Prometheus metrics are available at `/metrics` when `ENABLE_METRICS=true` is set. In the Kubernetes example above, the metrics endpoint would be accessible at `/fact_inventory/metrics` externally.

---

## Nested in a Larger Application

When embedding fact_inventory in a parent Litestar app, the parent app **owns** the routing and prefix configuration. The parent app calls `create_router(path="/fact_inventory")` to mount fact_inventory at a specific path.

### Configuration

1. The parent app imports `create_router` from `fact_inventory.presentation.router`
2. The parent app calls `create_router(path="/your_prefix")` to set the mount point
3. Optionally enable endpoints via environment variables (disabled by default)
4. Optionally enable Prometheus metrics via environment variable (disabled by default)

```python
import os

# Enable endpoints if needed (default: all disabled)
os.environ["ENABLE_METRICS"] = "true"
os.environ["ENABLE_HEALTH_ENDPOINT"] = "true"
os.environ["ENABLE_READY_ENDPOINT"] = "true"

from litestar import Litestar
from litestar.plugins.prometheus import PrometheusConfig, PrometheusController
from fact_inventory.presentation.router import create_router

# Mount fact_inventory at /fact_inventory
# Note: this is the path argument, not APP_NAME
fact_inventory_router = create_router(path="/fact_inventory")

prometheus_config = PrometheusConfig(app_name="my_app")

app = Litestar(
    route_handlers=[fact_inventory_router, PrometheusController],
    middleware=[prometheus_config.middleware],
)
````

> **Important**: When embedding, you pass the prefix directly to `create_router(path="...")`. You do **not** set `APP_NAME`. The `APP_NAME` environment variable only affects standalone deployments.

---

## Ansible Client Configuration

Update Ansible playbooks to include the prefix. This works the same regardless of deployment method—the prefix is part of your public API contract.

```yaml
- name: Make POST request with ansible_facts
  ansible.builtin.uri:
    url: http://127.0.0.1:8000/fact_inventory/api/v1/facts
    method: POST
    body_format: json
    body:
      system_facts: >-
        {{
          ansible_facts
          | dict2items
          | rejectattr('key', 'in', ['ansible_local', 'packages'])
          | items2dict
          | combine({'ansible_version': ansible_version})
        }}
      package_facts: >-
        {{ ansible_facts.packages | default({}) }}
      local_facts: >-
        {{ ansible_facts.ansible_local | default({}) }}
      client_facts: {}
    status_code: 201
```
