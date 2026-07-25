# Tasks: allow-cutover-declaration-env-in-refresh-wrapper

Fixture level: compact (one-regex wrapper edit + harness-based regression
tests + example/runbook docs; issue is implementation-ready)
Repair intensity: normal

Risk packs considered (core):
- Auth / permissions / secrets: selected - the wrapper allowlist IS a
  security boundary (0600 env file, DB-selector refusal); the new key must
  not weaken any existing constraint
- Config / project setup: selected - EnvironmentFile contract + example file
  must stay in sync with the wrapper's accepted set
- Error handling / rollback / partial outputs: selected - absence of the key
  must keep the current safe-refuse behavior byte-identical
- Public API / CLI / script entry: not selected - no CLI surface change;
  wrapper arg passthrough untouched
- Documentation / migration notes: selected - runbook must tell operators
  when to set/remove the key (issue acceptance criterion 4)

## 1. Wrapper + tests + docs

- [ ] 1.1 `scripts/scheduler_file_provider_refresh_once.sh:17`: append
  `|NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` inside the `allowed_keys`
  alternation (before the closing `)$`). Do NOT add it to the `required`
  loop (`:45-54`), do NOT touch the `REQUIRE_DIRECT_GRID` value assertion
  (`:55`), db_selectors, mode check, symlink check, newline/duplicate
  checks.
  Evidence floor: `git diff` of the wrapper shows exactly the one-line regex
  change.
- [ ] 1.2 Wrapper regression test (new), using
  `_write_wrapper_execution_fixture` (`tests/test_scheduler_file_provider_refresh.py:2512`
  — extend the fixture helper minimally to (i) accept a keyword-only
  `declaration_path: str | None = None` appended to the generated env file
  and (ii) printf `CUTOVER=${NHMS_REGISTRY_CUTOVER_DECLARATION_PATH-<unset>}`
  in the fake interpreter, inserted BEFORE the `ARGS=` printf — the
  clean-environment test at `:2607` asserts stdout ENDS with the args
  line): WITH the key → wrapper exit 0, child sees the exact value, and
  `refresh.CUTOVER_DECLARATION_ENV ==
  "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"` asserted in the same test (name
  identity between wrapper allowlist and runner reader).
  BASH VERSION GUARD (critical, fixture-review verified): the wrapper's
  `[[ ]]` guards do NOT abort under `set -e` on bash 3.2 (macOS
  `/bin/bash`) — the pre-change wrapper exits 0 WITH the key there, so the
  red-proof silently inverts. The new wrapper tests (1.2 and the rejection
  tests in 1.3) MUST resolve a bash >= 4 interpreter (check `/bin/bash`
  major version, fall back to `bash` on PATH; `pytest.skip` with an
  explicit reason when none is available) instead of hardcoding
  `/bin/bash`. Existing wrapper tests stay untouched on `/bin/bash` (their
  reject path uses explicit `exit 2`, version-independent).
  Evidence floor: RED-PROOF mandatory ON A BASH >= 4 HOST — capture red
  (pre-change wrapper, WITH-key → non-zero exit at the allowlist assert)
  and green (post-change → exit 0 + value propagated) on a bash >= 4 host,
  recording `bash --version` alongside both outputs verbatim. A local
  bash >= 4 resolved by the test's own fallback (e.g. homebrew
  `/opt/homebrew/bin/bash`) satisfies this; node-27 remains the oracle
  when no local bash >= 4 exists. The tests SKIP only where no bash >= 4
  is resolvable.
- [ ] 1.3 Absence + closed-set regressions:
  (i) the existing
  `test_wrapper_clean_environment_loads_fixed_config_and_strips_inherited_db_selectors`
  (`:2572`) runs withOUT the key — extend it with one assertion pinning
  `CUTOVER=<unset>` in the child env (this test stays on `/bin/bash`; the
  `-` default expansion works on any bash), proving absence keeps the
  safe-refuse default.
  (ii) NEW unknown-key rejection test (bash >= 4 guarded, per 1.2): env
  file carrying an out-of-allowlist key (e.g. `NHMS_SOMETHING_ELSE=x`) →
  non-zero exit, marker absent — pins spec Scenario 3's closed-set claim
  and doubles as the permanent regression form of the red-proof.
  (iii) NEW empty-value rejection test (bash >= 4 guarded):
  `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=` (blank value) → non-zero exit
  — pins the `-n "$value"` conjunct so the docs' "delete the line, don't
  blank it" instruction stays true.
  The only other existing wrapper execution test is
  `test_wrapper_rejects_forbidden_selector_in_mode_0600_env_before_exec`
  (`:2610`) — it passes unmodified (its reject path uses explicit
  `exit 2`).
  Evidence floor: full wrapper-test subset green (on node-27 for the
  bash >= 4 tests; macOS shows them as skipped).
- [ ] 1.4 `infra/env/compute.scheduler-provider-refresh.env.example`: add a
  COMMENTED block for `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` — purpose
  (declared package cutover), default-absent semantics (gate refuses
  undeclared cutovers), operator lifecycle (set absolute path during a
  declared cutover, then DELETE THE LINE after — an empty value
  `KEY=` is rejected by the wrapper's `-n "$value"` check at parse time,
  it is NOT equivalent to absent), and pointer to the cutover schema
  (`schemas/scheduler_registry_package_cutover.schema.json`). Follow the
  commented-key style precedent in
  `infra/env/compute.scheduler-dbfree.env.example:122`.
  Evidence floor: the existing contract test asserting example content
  (`:2237` family) stays green; the commented key must NOT be an active
  line.
- [ ] 1.5 `docs/runbooks/current-production-ops.md` cutover gate section
  (`:479-534`): add the systemd-path procedure (edit EnvironmentFile with
  the key → run/await the timer or trigger the service → verify
  `declared_cutovers`/accepted receipt → DELETE the key line, never blank
  it), noting the wrapper admits the key as of #1095. Scope the existing
  "env 未设置或空值等同于'无 declaration'" sentence (`:503-504`) to the
  manual-CLI path — on the systemd path an empty value aborts the wrapper
  at parse time with a bare exit 1.
  Evidence floor: section renders as valid markdown; no other runbook
  content changed.

## 2. Change-level verification floor

- [ ] 2.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
  green (full suite).
- [ ] 2.2 `uv run ruff check .` clean.
- [ ] 2.3 `openspec validate allow-cutover-declaration-env-in-refresh-wrapper
  --strict --no-interactive` PASS.
- [ ] 2.4 Scope check: changed files exactly = wrapper script, tests file,
  env example, runbook (+ this fixture). `git diff --name-only
  origin/master...HEAD` confirms; no schema/, no other scripts/.
