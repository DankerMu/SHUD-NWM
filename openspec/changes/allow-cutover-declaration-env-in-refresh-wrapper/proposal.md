# Allow NHMS_REGISTRY_CUTOVER_DECLARATION_PATH through the refresh wrapper allowlist (#1095)

## Why

The systemd refresh wrapper `scripts/scheduler_file_provider_refresh_once.sh`
parses `infra/env/compute.scheduler-provider-refresh.env` as data against a
fixed key allowlist (`allowed_keys`, `:17`, currently 13 keys) and fail-fasts
on any unknown key (`:39` under `set -euo pipefail`). PR #1080 added the
cutover gate env entry `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`
(`scripts/scheduler_file_provider_refresh.py:105`, read at `:586`) but never
extended the allowlist. Consequence: adding the declaration path to the
EnvironmentFile kills the wrapper at parse time; omitting it means the systemd
runner can never see a declaration and every existing-model package cutover is
refused (`registry_cutover_undeclared`). The operator-friendly declared-cutover
path is dead — only the manual CLI can accept a declaration. (p2, PR #1080
round-1 遗留.)

## What Changes

- `scripts/scheduler_file_provider_refresh_once.sh`: append
  `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` to the `allowed_keys` regex.
  OPTIONAL key: it is NOT added to the `required` loop and gets no
  DEDICATED value assertion (the shared non-empty `-n "$value"` parse check
  at `:39` still applies to every allowlisted key, this one included) — all
  other safety constraints stay (0600 mode, symlink refusal,
  DB-selector refusal, newline/CR refusal, duplicate-key refusal,
  `REQUIRE_DIRECT_GRID == true` assertion, final `unset` of db selectors).
- Wrapper regression tests in `tests/test_scheduler_file_provider_refresh.py`,
  using the existing execution harness `_write_wrapper_execution_fixture`
  (fake interpreter printf-dumps its env): (a) env file WITH the declaration
  path → wrapper exits 0 and the exec'd process sees the value; (b) env file
  WITHOUT it → behavior unchanged (exit 0, variable unset in child) — the
  existing clean-environment test already pins most of (b), extend rather
  than duplicate where sensible.
- Runner-side acceptance is ALREADY pinned: declared-cutover accept with
  `CUTOVER_DECLARATION_ENV` set is tested at `:3493` and `:4111` (monkeypatch
  setenv + declaration file → published receipt with `declared_cutovers`
  coverage). The wrapper test proves propagation; the composition (env file
  → wrapper → runner env → declared cutover accepted at the gate) is
  covered by the two halves meeting at the same env
  variable name — assert the name identity
  (`refresh.CUTOVER_DECLARATION_ENV == "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"`)
  in the new wrapper test so the two halves cannot silently diverge.
- `infra/env/compute.scheduler-provider-refresh.env.example`: add a commented
  entry documenting the key — optional, absent by default (gate then refuses
  undeclared cutovers, which is the safe default), set to an absolute
  declaration JSON path only while an operator is executing a declared
  cutover, then DELETE the line (an empty value fail-fasts the wrapper's
  `-n "$value"` parse check — blanking is not equivalent to absence).
- `docs/runbooks/current-production-ops.md` cutover section (`:479-534`):
  add the systemd-path instructions (put the key in the EnvironmentFile for
  the duration of the cutover; wrapper accepts it as of this change; delete
  the line afterwards), and scope the existing "空值等同于无 declaration"
  sentence to the manual-CLI path.

## Out of Scope

- Cutover gate semantics, runner classifier, schemas, declaration validation.
- Registry mirror atomicity; other wrappers (this is the only provider
  refresh wrapper in the repo — no sibling copies).
- Making the key required or defaulted — absence stays the safe default.

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement: wrapper
  admits the declaration path as an optional key).
- Affected code: `scripts/scheduler_file_provider_refresh_once.sh` (one
  regex edit), `tests/test_scheduler_file_provider_refresh.py`,
  `infra/env/compute.scheduler-provider-refresh.env.example`,
  `docs/runbooks/current-production-ops.md`.
- Existing wrapper contract test
  (`test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`
  `:2237`) and execution tests (`:2572+`) must stay green unmodified except
  for deliberate new assertions.
- Platform note: the new wrapper red-proof/rejection tests are bash >= 4
  gated (macOS `/bin/bash` 3.2 does not abort on failing `[[ ]]` under
  `set -e`); red/green evidence is captured on node-27.
- Deployment note: node-22 picks the change up via `git pull --ff-only` (the
  wrapper runs from the repo checkout); no service-unit change needed.
