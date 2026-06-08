## Context

The audit found tracked placeholder and legacy paths:

- `apps/web` is explicitly a deprecated frontend placeholder.
- Hyphenated worker directories such as `workers/forcing-producer` and `workers/shud-runtime` are placeholders; canonical packages use underscores.
- `workers/sbatch_templates` contains legacy single-run templates and should be treated more carefully than empty placeholders because `services/slurm_gateway/config.py` references its legacy status.
- `services/tile-publisher` exists while active tile publisher code is elsewhere.
- QHH scripts are diagnostic-only and still useful for bring-up/debugging.
- Playwright specs use `page.route('**/api/v1/**')` and should not be called live e2e.
- `.github/workflows/ci.yml` contains a paused job with `&& false`.

## Decisions

### D0. Issue #362 slice is inventory-only

This change fixture spans the whole Governance-2 epic, but issue #362 only
creates the persistent legacy/dead-code inventory. It MUST NOT delete, move,
rename, archive, or wrap any governed path. Later issues #363-#366 perform the
actual cleanup, diagnostic isolation, e2e split, and paused CI retirement after
the inventory exists.

Fixture level for #362: `expanded` because the work classifies legacy paths and
future cleanup evidence across docs, CI, frontend tests, workers, Slurm, and
diagnostic scripts.

Repair intensity for #362: `medium`. The PR is documentation/governance-only,
but incorrect classification could misdirect later destructive cleanup.

Risk packs considered for #362:

- Public API / CLI / script entry: selected - inventory must identify scripts
  and test commands that remain active entrypoints or diagnostic commands.
- Config / project setup: selected - inventory must classify workflow and
  deployment/config references that make a path active.
- File IO / path safety / overwrite: not selected - #362 does not add runtime
  file reads/writes or move/delete/archive paths.
- Schema / columns / units / field names: selected - the persistent inventory
  has a governed row schema used by downstream #363-#366 cleanup issues.
- Auth / permissions / secrets: not selected - no auth policy, secret handling,
  or role permission behavior changes.
- Concurrency / shared state / ordering: not selected - no scheduler/runtime
  state changes.
- Resource limits / large input / discovery: selected - inventory evidence uses
  repository-wide discovery and should avoid unbounded or misleading claims.
- Legacy compatibility / examples: selected - primary risk; every governed path
  must have evidence before later retirement.
- Error handling / rollback / partial outputs: not selected - no runtime side
  effects in this PR.
- Release / packaging / dependency compatibility: selected - inventory must
  record active build/import/deploy evidence before later cleanup.
- Documentation / migration notes: selected - the inventory is the migration
  source for later G2 issues.
- Geospatial / CRS / basin geometry: not selected - no GIS behavior change.
- Hydro-met time series / forcing windows: not selected - no forcing/time-series
  behavior change.
- SHUD numerical runtime / conservation / NaN: not selected - no solver/runtime
  behavior change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  change.
- Slurm production lifecycle / mock-vs-real parity: selected - inventory covers
  `workers/sbatch_templates`, `infra/sbatch`, and Slurm gateway references.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior change.
- Run manifest / QC provenance: not selected - no production manifest/QC
  behavior change.
- Published NHMS artifacts / display identity: selected - inventory covers tile
  publisher and display evidence boundaries.

Must preserve for #362:

- No governed path is deleted, moved, renamed, archived, or wrapped.
- No CI workflow, Playwright runtime behavior, QHH script behavior, Slurm
  gateway default, frontend app behavior, API behavior, or deployment behavior
  changes.
- Follow-up tasks #363-#366 must be able to use the inventory without treating
  #362 as cleanup approval.

### D1. Use four path statuses

Each governed path should be classified as:

- `production`: active runtime/build/deploy path.
- `diagnostic`: supported debugging or bring-up path, not production.
- `test-only`: regression fixtures or mocked tests.
- `archived`: historical reference outside active build/test/deploy.

The inventory must be a persistent artifact, not just PR body prose. Each row should include exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, and final action.

### D2. Do not delete diagnostic QHH scripts first

The QHH scripts already have `DIAGNOSTIC-ONLY` headers and static tests that keep them out of production orchestrator code. First create a diagnostic README/manifest and optionally move them behind short compatibility wrappers later.

### D3. Delete or archive placeholders only with import/CI/runbook evidence

Empty or README-only placeholders can be deleted or archived after grep/import/CI evidence proves no active path depends on them. `workers/sbatch_templates` needs a stronger migration note because it contains actual sbatch templates.

### D4. Separate mocked regression from live proof

Mocked Playwright tests remain useful, but filenames/config/docs must say mocked regression. Live e2e profiles must prohibit `page.route('**/api/v1/**')`.

The live display-readonly profile is a deliverable, not only a plan. If live credentials or node access are unavailable, the issue may mark runtime execution blocked, but the config/script/static no-mock guard still needs to land.

## Risks / Mitigations

- **Risk: removing historical placeholders breaks old docs.** Mitigation: update docs or archive historical docs in the same PR.
- **Risk: moving diagnostic scripts breaks runbooks.** Mitigation: first add README/manifest; move only with wrappers and deprecation messaging.
- **Risk: live e2e cannot run locally.** Mitigation: separate config/profile and make live receipt explicit, not part of fast local tests.

## Verification

For issue #362, verification proves inventory completeness and formatting only:

- `rg` inventory for candidate paths records discovered references and active
  counterparts; the expected result is a completed inventory, not deletion.
- `find` inventory records governed candidate directories and active
  counterparts.
- `uv run ruff check .` exits 0.
- `npx --yes markdownlint-cli2 --config .markdownlint.yaml 'docs/**/*.md'`
  exits 0.
- Focused workflow grep records any `&& false` paused job in the inventory; #362
  does not remove it.

Follow-up #363-#366 verification adds runtime/static checks such as
`tests/test_qhh_scripts_static.py`, frontend build/tests, and workflow cleanup
proof when those issues change the relevant files.
