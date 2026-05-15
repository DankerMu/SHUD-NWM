# Validation Matrix

This repository keeps fast checks separate from real-service integration checks.

## Backend Fast

No Docker, PostgreSQL, MinIO, Slurm, or external network is required.

```bash
uv run pytest -q
uv run ruff check .
```

## Backend Integration

Requires a reachable PostgreSQL database with PostGIS and TimescaleDB available. The pytest fixture creates and drops a temporary database from the configured URL, applies migrations from zero, and seeds deterministic issue-126 data.

```bash
docker compose -f infra/docker-compose.dev.yml up -d db
export NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
uv run pytest -q -m integration
```

`DATABASE_URL` is also accepted for CI compatibility. Without either URL, integration tests are intentionally skipped and the fast command remains self-contained.

## Frontend

Run from `apps/frontend/` with pnpm through Corepack.

```bash
cd apps/frontend
corepack prepare pnpm@10.11.0 --activate
corepack pnpm install --frozen-lockfile
corepack pnpm test
corepack pnpm build
```

## Frontend E2E

Use the existing Playwright scripts with the frontend preview server and any required API service for the target scenario.

```bash
cd apps/frontend
corepack pnpm test:e2e
```

## OpenSpec

```bash
openspec validate issue-126-real-integration-test-matrix --strict --no-interactive
```
