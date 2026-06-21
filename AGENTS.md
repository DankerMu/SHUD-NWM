## Local Agent Instructions

### Python virtual environment

- Use the repository-managed virtual environment for all Python work.
- Prefer `uv run ...` from the repository root so commands use `.venv` and `uv.lock`.
- If running commands manually in a shell, activate the environment first:

```bash
source .venv/bin/activate
```

- Install or refresh dependencies with:

```bash
uv sync --all-extras --dev
```

- Run backend checks through the virtual environment, for example:

```bash
uv run pytest -q
uv run ruff check .
uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload
```

- Do not use the system Python for this repository unless explicitly requested.

### Frontend environment

- Frontend commands run from `apps/frontend/` with pnpm:

```bash
cd apps/frontend
pnpm install
pnpm test
pnpm build
```

### Linux / Production Environment Migration

- Do NOT reuse macOS `.venv` or `node_modules` on Linux — delete and recreate.
- Required initialization sequence:
  1. `uv sync --all-extras --dev` (creates fresh .venv with all dev dependencies)
  2. `corepack prepare pnpm@10.11.0 --activate` (enable pnpm via Corepack)
  3. `CI=true corepack pnpm install --frozen-lockfile` (install frontend deps)
- Common post-migration checks:
  - `uv run ruff check .` must pass
  - `uv run pytest -q tests/test_api.py tests/test_gateway.py` must pass
  - `cd apps/frontend && corepack pnpm test` must pass
  - `cd apps/frontend && corepack pnpm build` must pass

## Three-Node Topology

This repo deploys across three machines: local Mac (dev), node-22 (pure compute, no DB), node-27 (primary PG + ingest + display API + frontend). Public service entry: `https://test.nwm.ac.cn`.

**Required reading before any non-trivial change:**

- `CLAUDE.md` — dev workflow + verification oracle routing + current host/DB topology
- `docs/runbooks/two-node-deployment-overview.md` — deployment topology (note: M22 design-intent doc; see banner for current-vs-design delta)
- `docs/governance/ROLE_BOUNDARY.md` — service role boundaries (note: "node-22 owns database mutation" is design-intent wording; current physical deployment differs — see banner)
