# Tasks: retire-dead-branches-for-retired-surfaces (#3149)

## Risk packs

- [x] Legacy compatibility — verdicts 1-4 keep the live contract and the fail-closed guards. → tasks 1.1, 2.3, 2.4.
- [x] Documentation — container-contract comments, the selector comments and pins, and the entropy-report example.
  → tasks 1.2, 1.3, 1.4.
- [ ] Not selected — Public API (no OpenAPI change), Schema, Auth, Concurrency, File IO, Resource limits, Release,
  Config.

## 1. Implementation

- [x] 1.1 Re-verify verdicts 1, 3 and 4 against the code, and record a one-line reason for each in the PR:
  - verdict 1: the detail route is live, and the frontend consumes `GeoJsonLineString`;
  - verdict 3: there is no sbatch fallback;
  - verdict 4: `DEFAULTS` is pinned by `tests/test_node27_pgdata_relocation_retired.py`, and the
    `node27_resource_governance` default is kept on purpose.

  Change no code for these items.
- [x] 1.2 Update both comments in `packages/common/node27_container_contract.py`, the mount comment (~:250-262) and the
  docstring (~:311-318), to the 2026-11-16 live measurement in `proposal.md` item 4, keeping their conclusion.
  **Do not add any line that starts with `# MEASURED`:** `tests/test_node27_external_contract_snapshot.py` pins exactly
  three such lines.
- [x] 1.3 Remove the hand carry of `DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS` from the `services/tiles/mvt.py` and
  `apps/api/routes/hydro_display*.py` selector rules in `scripts/select_ci_tests_rules.py`.
  - Keep the `station_set_flip.py` rule.
  - Update the comments that describe the carry or the derived importers: `select_ci_tests_rules.py` ~:2435-2454 and
    ~:2600-2603, and `select_ci_tests_tables.py` ~:1638-1653.
  - Update the pins in `tests/test_select_ci_tests.py`: the whole-corpus carry assertion (~:3733-3737) and the exact
    `mvt.py` output pin (~:2371-2372).
  - Keep the tracked-tree guard that pins the two flip suites plus the helper.
- [x] 1.4 Delete the two archived-change prefix branches from `_placeholder_path_allowlist_reason`. Delete with them:
  - the reason-to-key mappings that no longer have a producer, `findings.py` ~:108-111 (first confirm that nothing
    else produces those reasons);
  - the two test functions that exist only for these branches, `tests/test_entropy_audit_retired_paths.py` ~:103-180;
  - the stale sample reason in `docs/governance/entropy-report.example.md`.
- [x] 1.5 Handle the conditional `"met-stations"` member of `apps/api/routes/hydro_display_catalog.py` (~:151).
  Trace `source_version` for the met-stations layer through `services/tiles/mvt.py`, the layers response models and
  serialization.
  - If it never reaches a response or SQL, delete the member.
  - Otherwise keep it and record the path that it reaches.

## 2. Tests / verification (each expected to pass)

- [x] 2.1 `uv run ruff check .` is clean.
- [x] 2.2 Run `uv run python scripts/governance/audit_repo_entropy.py --format json` before and after the change.
  - `metadata.budget_counted_count` and `summary_counts.by_allowlist_state` are unchanged.
  - The `allowlist_state` counts of the `findings[]` entries with `check_id == "placeholder-path-token"` are
    unchanged.
  - `--mode hard-gate` exits 0 with `hard_gate_status == "pass"` and `hard_gate_failing_count == 0`.
- [x] 2.3 The selector-chosen set for the diff passes, plus these suites:
  - `tests/test_select_ci_tests.py`;
  - `tests/test_entropy_audit*.py`;
  - `tests/test_openapi*.py`;
  - `tests/test_node27_external_contract_snapshot.py`;
  - `tests/test_node27_pgdata_relocation_retired.py`;
  - the hydro-display and layers suites, if 1.5 deletes the member.

  Run them locally and in a node-27 temp worktree.
- [x] 2.4 `git diff --stat -- openapi/ apps/frontend/` is empty.
- [x] 2.5 1.5 deleted the member, so API code changed. Verified live on node-27 without a production deploy: a
  side display API runs from a temp worktree of the branch, on port 18149, with the production `display.env` (role
  `display_readonly`) and a private MVT cache dir. `/health` returns 200 on both that instance and production :8080,
  and the `/api/v1/layers` `data` is identical on both (all four layers, `met-stations` field for field). The side
  instance, its cache and the worktree were removed afterwards. Deploying production :8080 is a separate decision,
  because the node-27 checkout lags `origin/master` by many commits.