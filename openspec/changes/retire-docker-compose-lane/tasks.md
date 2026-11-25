# Tasks

Fixture level: `standard`. Risk packs: deletion blast radius (a file that
something live still reads; a test of live behaviour deleted with the
lane's tests); governance gate drift (a check weakened beyond the deleted
subject, or a finding count that moves without an explanation);
spec-to-code fidelity (the rewritten requirements must describe what is
left).

Must preserve: no change to `apps/`, `packages/`, `services/`, `workers/`
or to any live script or unit, with two exceptions that the tasks name:
the governance audit scripts of 3.x, and one docstring in
`scripts/node27_timeseries_compression_prearm.py` (6.2); `scripts/ops/start-display-api.sh`,
`scripts/ops/display-c1-receipt.sh`, `infra/systemd/nhms-display-api.service`
and every other installed unit are not edited; `infra/docker-compose.dev.yml`
stays; the `.gitignore` half of the artifact ignore check; every test
outside the three deleted files still runs, apart from those listed as
changed in the report; the entropy hard gate ends at 0 failing.

## 1. Spec (orchestrator)

- [ ] 1.1 The nine deltas of this change; `two-node-docker-runtime` is
  retired.

## 2. Deletion

- [ ] 2.1 `git rm` the files of the proposal's "Deleted" list (the
  capability's spec is removed by the archive, not by hand). Remove the
  ruff per-file ignore of the runtime validator from `pyproject.toml` and
  the deleted test files from `scripts/ci/full_test_durations.json`.
- [ ] 2.2 After the deletion, `git grep` for each deleted path and for
  `validate_two_node`, `test_two_node_docker`, `compose.compute`,
  `compose.display`, `Dockerfile.app`, `infra/docker/`, `dockerignore`,
  `compute.example`, `nhms-compute-compose`, `nhms-display-compose`,
  `README.two-node-docker` outside `openspec/changes/archive/`,
  `docs/plans/`, `docs/runbooks/receipts/`, `docs/review-loop-log.jsonl`
  and this change: the report lists every remaining hit with the reason it
  stays (history, or a statement that the lane is retired).

## 3. Governance checks

- [ ] 3.1 `scripts/governance/entropy_audit/check_topology.py`: the scan
  list drops `infra/README.two-node-docker.md` and gains the directory
  `infra/systemd`. The audit yields only files whose name it accepts as
  text (`repo_files.py`, `_has_scannable_text_name`); the suffixes
  `.service`, `.timer` and `.conf` become accepted names, otherwise the
  new root would contribute one JSON file and no unit. `topology_display_env.py`: the allow token
  `compose.display` goes if nothing else needs it. Report the hard-gate
  numbers before and after and every finding that appears or disappears.
- [ ] 3.2 `check_paths_and_api.py`: the ownership check no longer requires
  the term `.dockerignore` in `DOC_STATUS.md`; the ignore check no longer
  reads `.dockerignore` (its three `.gitignore` expectations stay).
  `write_entropy_baseline.py`, `constants.py`, `repo_files.py`: the
  `.dockerignore` entries go. The tests and helpers that build a
  `.dockerignore` fixture or a synthetic lane README
  (`tests/entropy_audit_helpers.py`, `tests/test_entropy_audit_retired_paths.py`,
  `tests/test_entropy_audit_topology_authority.py`,
  `tests/test_entropy_audit_scan_boundaries.py`) follow: a test of the
  scan-list mechanism keeps proving the mechanism on a path that is still
  scanned; a test whose only subject was the deleted file is deleted and
  listed.
- [ ] 3.3 Mutations: a `.service` file under `infra/systemd` with a line
  the topology check must flag is flagged; it is not once `infra/systemd`
  is taken out of the list again, and not once the `.service` suffix is
  taken out of the accepted names again (the two halves of 3.1 are each
  needed); a `.gitignore` without one of the three expected
  entries still yields the ignore finding.

## 4. Tests that named the lane

- [ ] 4.1 `tests/test_production_scheduler.py::test_compute_env_example_enables_strict_forecast_warm_start`
  reads `infra/env/compute.scheduler-dbfree.env.example` (the key is set
  there); the two assertions on comment wording are kept only if that
  template carries the wording. `tests/test_slurm_gateway_deployment_contract.py`
  drops `infra/env/compute.example` from its two path tuples.
  `tests/test_gitignore_infra_env.py` drops it too and gains what makes
  the "examples are trackable" half bite (for a tracked file `git
  check-ignore` answers 1 whatever the rules say; assert on an untracked
  path that the negation rule must cover, or with `--no-index`).
- [ ] 4.2 `tests/test_role_boundary_static.py`: the template test also
  pins `NHMS_DISPLAY_API_PORT=8080` (the requirement's seventh key) and
  the two storage roots; a test pins that
  `infra/systemd/node22/nhms-compute-api.service` and
  `infra/systemd/node22/nhms-slurm-gateway.service` set their
  `NHMS_SERVICE_ROLE` (if a test already does, name it instead); the
  lines that expect `compute.example` in `ROLE_BOUNDARY.md` follow 6.x.
- [ ] 4.3 `tests/test_readonly_db_validation_routes.py` (pins on the lane
  README and on `two-node-production-e2e-plan.md`),
  `tests/test_node22_runbook_section_markers.py` (the §5.2 marker): a pin
  whose subject is deleted is deleted; a pin on a sentence that 6.x
  rewrites follows the sentence. The report lists each.
- [ ] 4.4 A new test pins D5: the forbidden-key list of
  `infra/env/README.md` equals `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS`
  (`tests/test_role_boundary_static.py`, built from the runtime guard).
  The list is the bullet that 6.1 rewrites to begin "Keys the display
  role refuses at startup (owner: `apps/api/runtime_mode.py`):" followed
  by the keys in backticks; the test parses that bullet. A name in today's
  bullet that the guard does not refuse, if there is one, leaves the
  bullet and is listed in the report. Red
  runs: a key removed from the bullet; a key added to it.

## 5. The display template

- [ ] 5.1 `infra/env/display.example`: the header says what the file is
  (template of `infra/env/display.env`, sourced by the unit of
  `scripts/ops/start-display-api.sh`); compose-only keys and comments go
  by the rule of D3; `NHMS_PUBLISHED_ARTIFACT_ROOT` takes the host path
  of D3 and its comment loses the compose sentence. The report lists each
  removed key with the search that showed it unread, each kept key that
  looked compose-only with its reader, and each test that pinned a removed
  key or the old value.

## 6. Docs

- [ ] 6.1 `infra/env/README.md`: title and introduction (the directory
  holds the env templates of the systemd units, not of a compose stack);
  the paragraphs on `compute.env` / `compute.example` shrink to the
  leftover statement of the requirement; the "compose-lane role contract"
  section keeps what is true of the live units (the DB-free scheduler,
  which file owns which key) and loses what described compose services;
  the display section keeps the forbidden-key list (owner: the runtime
  guard) and loses mounts, HostConfig and compose interpolation; the
  validation commands go.
- [ ] 6.2 `docs/runbooks/two-node-deployment-overview.md` (§5.2 list and
  its superseded marker, the links to the README and the compose files),
  `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/runbooks/display-readonly-live-mvt.md`,
  `docs/runbooks/object-store-forcing-series-read.md`,
  `docs/runbooks/production-ops/known-issues-state-index.md`,
  `docs/runbooks/node-27-bringup-checklist.md` (lines outside C1),
  `scripts/diagnostic/qhh/README.md`,
  `scripts/node27_timeseries_compression_prearm.py` (a docstring): each
  statement that sends the reader to a deleted file, or describes compose
  as a way to deploy, is corrected to the systemd reality or cut.
- [ ] 6.3 `docs/governance/ROLE_BOUNDARY.md`, `DOC_STATUS.md`,
  `API_LAYER_INVERSION_INVENTORY.md`, `LEGACY_DEAD_CODE_INVENTORY.md`
  (the lane gets an entry as deleted, with this issue),
  `entropy-burndown-triage.md`.
- [ ] 6.4 Rule for 6.x: no new claim about production that was not
  already in the doc or in the proposal; a sentence is cut rather than
  rephrased when its only content was the lane. The report lists every
  edited passage (file:line, cut / rewritten).

## 7. Evidence

- [ ] 7.1 Local: every test file touched, every test that reads a file
  edited by 5.x or 6.x (find them with `git grep -l` for the file's
  name under `tests/`; known: the `tests/test_node22_*` files that parse
  `infra/env/README.md`, `tests/test_precip_overlay.py`,
  `tests/test_node27_mvt_cache_retention_wrapper.py`), every `tests/test_entropy_audit_*.py`,
  `tests/test_role_boundary_static.py`, `tests/test_env_templates.py`,
  `tests/test_start_display_api.py`, `tests/test_display_c1_receipt.py`,
  `tests/test_runtime_mode.py`; `ruff check --no-cache .`;
  `openspec validate retire-docker-compose-lane --strict --no-interactive`;
  the hard gate (numbers before and after, each moved finding explained);
  `tests/test_entropy_audit_baseline_writer_summary.py` and whatever
  compares against `.entropy-baseline/latest.json` (if a comparison fails
  only because the baseline's counts are old, the test id and assertion
  are reported and the orchestrator rewrites the baseline).
- [ ] 7.2 node-27: the touched test files in a detached worktree.
- [ ] 7.3 Both hosts, read-only, after the merge: no installed `nhms*`
  user unit references a deleted path (the issue's third check).
- [ ] 7.4 Archive the change in the PR's last push; then, in the main
  specs: the Purpose of `node22-env-hygiene` without the compose lane, the
  Purpose of `readonly-db-and-e2e-evidence` without the Docker evidence
  bundles, and the scenario "display env includes compute-only env" of
  `entropy-automation` without "or compose file".
