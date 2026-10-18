# Scripts Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for `scripts/`.
Shared vocabulary lives in `openspec/glossary.md`; use its terms (active
entrypoint, current authority, budget-counted finding) exactly as defined
there.

## Required Reading

- `docs/runbooks/ci-test-routing.md`
- `openspec/specs/ci-test-selector-routing/spec.md`
- `openspec/specs/python-environment-truth/spec.md`
- `scripts/diagnostic/qhh/README.md`

Python tooling rules (uv only, and the node-22 interpreter exception) are
defined once in root `CLAUDE.md` and `openspec/specs/python-environment-truth/spec.md`.
Follow them there; do not restate or reinterpret them in a script header.

## Ownership Boundaries

- Scripts are operator, CI, and governance entrypoints. Reusable logic belongs
  in `packages/`, `services/`, or `workers/`; a script parses arguments and
  calls it. Example: `scripts/node27_pgdata_migrate.py` is a thin wrapper over
  `packages/common/node27_pgdata_migrate.py`. Library code must never import
  `scripts`; `tests/test_import_layering_guard.py` enforces that for
  `packages/`.
- The `node27_*` and `node22_*` filename prefixes name the host a script
  targets. Host roles come from the three-node table in root `CLAUDE.md`;
  check it before adding or renaming a host-scoped script.
- `scripts/select_ci_tests.py` is the PR test selector. Adding a test file,
  a new importer of a guarded owner, or a new owner module needs a matching
  rule there; `tests/test_select_ci_tests.py` fails when a guarded owner gains
  an unrouted importer. Marker routing (`e2e`, `grib`) is described in
  `docs/runbooks/ci-test-routing.md`.
- `scripts/ci/` holds CI-only tooling (`scripts/ci/shard_tests.py` partitions
  the full regression, `scripts/ci/full_regression_watch.py` watches it).
  `scripts/governance/` holds the entropy audit
  (`scripts/governance/audit_repo_entropy.py` and its
  `scripts/governance/entropy_audit/` checks), the baseline writer, and the
  loop-log audit. `scripts/review_gate.py` is the only writer of the cross-PR
  review gate memory file.
- `scripts/diagnostic/` is diagnostic-only. The root-level QHH scripts listed
  in `scripts/diagnostic/qhh/README.md` are reproduction tools, not production
  entrypoints; never call them from `services/` or `workers/` code, and keep
  their refusal to run from the active compute checkout.

## State And Mutation Fences

- A new script that changes rows, files, units, or receipts must be dry-run by
  default and act only with an explicit `--apply`, as in
  `scripts/ops/node27_oneshot_sql.py`, `scripts/node27_retire_basin.py`, and
  `scripts/node22_model_succession.py`. Its dry run changes nothing and says
  so in its output.
- Existing scripts gate inconsistently: some use `--apply`, some `--execute`
  (`scripts/node22_backfill_forcing_for_model_ids.py`), some `--enforce`
  (`scripts/node27_pgdata_migrate.py`), some act by default with an opt-in
  `--dry-run` (`scripts/scheduler_file_provider_refresh.py`), and
  `scripts/register_grid_snapshot_drift.py` has no gate at all. Read a
  script's `--help` before running it.
- One-shot SQL in `scripts/ops/` ships with a paired `*_rollback.sql` file and
  runs through `scripts/ops/node27_oneshot_sql.py` in one transaction; copy-out
  backups are fsynced before the transaction commits. Credentials stay in the
  sourced private environment and are never placed in argv, output, or files
  under `scripts/`.
- Operator receipts are exclusive-create writes: the model succession, basin
  retirement, and merged-registry publish tools share
  `packages/common/succession_receipt.py` for that. Reuse it or the
  `packages/common/safe_fs.py` helpers instead of overwriting a receipt in
  place.
- `scripts/` (including Markdown) is a production-topology scan root of the
  entropy audit hard gate. Do not write host, role, or storage-topology claims
  here; point at root `CLAUDE.md` and `docs/governance/ROLE_BOUNDARY.md`.

## Focused Verification

Always run the issue-required governance checks after changing this file or
scripts scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

When the selector changes, run its meta-suite:

```bash
uv run pytest -q tests/test_select_ci_tests.py
```

For any other script, ask the selector which suites own the changed path and
run them:

```bash
printf '%s\n' scripts/node27_raw_retention.py | uv run python scripts/select_ci_tests.py
```
