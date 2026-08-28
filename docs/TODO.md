# TODO

Outstanding work items. Not prioritized; items should be evaluated against
use-case requirements before implementation.

## Application

- [ ] Add distributed rate-limit state (Redis or similar) for multi-instance
      deployments that require rate-limit persistence across restarts
- [ ] Implement authentication/authorization if clients require identity
      (currently network-level security only)

## Observability

- [ ] Add custom Prometheus metrics (cleanup duration, fact sizes, rate-limit
      violations)

## Repository

- [ ] Set up CI to verify compatibility with latest `advanced-alchemy`,
      `litestar`, and `sqlalchemy` releases
