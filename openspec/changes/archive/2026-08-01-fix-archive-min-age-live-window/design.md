# Design: fix-archive-min-age-live-window (#1227)

## D1 — Window source: read the retention env file, single variable, runner-equivalent semantics

The guard must compare against the ACTUAL live window, so the only sound
source is the same file the retention runner reads:
`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` in the deployed retention env.

- REJECTED: a new manually-synced shared variable (e.g.
  `NHMS_DB_RETENTION_DAYS` in the archive envs). It reproduces the exact
  failure mode being fixed — ops raises the retention window and forgets
  the sibling variable; the coupling stays in a comment.
- REJECTED: keeping the compile-time constant (the bug).
- The retention env header's "do not sync this env against
  node27-product-archive.env" note forbids value SYNCING between files,
  not read-only extraction of one variable; the merged §7.3 runbook
  pattern (PR #1226) already established single-variable extraction from
  this file as the operator-facing idiom. The retention example header
  gains one sentence noting that the archive-side guards now depend on
  this file's PATH (a move/rename breaks them fail-closed).
- **Runner-equivalent resolution (fixture-review P1-4)**: the retention
  runner treats the variable as OPTIONAL — `_optional_positive_int`
  returns the default 14 for BOTH a missing assignment and an empty value
  (`scripts/node27_timeseries_retention.py:272-275,308-312`,
  `_DEFAULT_WINDOW_DAYS = 14` at `:92`). The guard MUST mirror that,
  otherwise a healthy live pair (assignment removed ⇒ runner window 14,
  min age 14) would false-refuse — the costly direction. Therefore:
  - missing assignment or empty value ⇒ effective window = the shared
    default (this is the runner's live effective value, NOT a blind
    fallback);
  - the default constant is lifted to `packages/common/storage.py`
    (`DEFAULT_RETENTION_WINDOW_DAYS = 14`) and the retention runner's
    `_DEFAULT_WINDOW_DAYS` becomes an import of it, so the two sides
    cannot drift (one-line runner change; window CONSUMPTION semantics
    unchanged);
  - non-integer or non-positive PRESENT values ⇒ fail closed
    (`ArchiveConfigurationError`), matching the runner, which also
    refuses such values rather than defaulting;
  - MISSING FILE or unset/empty/relative path variable ⇒ fail closed —
    unlike a missing assignment, a missing file means the guard is
    pointed at nothing and no runner-equivalent value exists.
- Extraction lexical forms (fixture-review P2-3) — accepted and pinned by
  test: optional `export ` prefix; single- or double-quoted values;
  trailing ` # comment` stripped; surrounding whitespace stripped
  OUTSIDE quotes only (quoted content is taken verbatim, so
  `VAR=" 21 "` refuses exactly like the runner, whose strict parser
  rejects whitespace-padded values — the runner-equivalence claim is
  scoped to unquoted shapes); `#`
  full-line comments ignored; last assignment of the EXACT variable name
  wins (shell source semantics); near-name decoys
  (e.g. `..._WINDOW_DAYS_OLD`) ignored.
- **Round-1 review amendments (PR #1229, verified findings A1/A2)** — the
  set above is tightened so every divergence from the runner's actual
  oracle (`set -a; . file`) refuses instead of mis-parsing:
  - an UNQUOTED value with leading whitespace (`VAR= 21`) REFUSES: bash
    parses that line as assignment-prefix-plus-command, so the runner
    sees the variable unset; accepting 21 would refuse a pair against a
    window the runner never uses. Refusing with an honest
    malformed-assignment message is a recorded fail-closed narrowing of
    runner equivalence for a malformed hand-edit;
  - a value whose FIRST character is `#` (`VAR=#21`) is a PRESENT
    non-integer value → refuses (bash exports `#21` and the runner
    refuses to start on it; `#` opens a comment only after whitespace);
  - CRLF (any non-`\n` line-break character reaching a candidate line)
    refuses — bash keeps the `\r` in the value and the runner refuses
    it; extraction splits on `\n` only;
  - ANY non-comment line that CONTAINS
    `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=` without being accepted as
    that variable's assignment (`readonly VAR=21`, `declare -i VAR=21`,
    a truncated edit) refuses — PER LINE, regardless of whether a plain
    assignment was accepted elsewhere in the file. Round-2 verified C2:
    the round-1 form gated this on "no assignment accepted", so a mixed
    `VAR=14` + `readonly VAR=30` file mis-parsed to the stale 14 while
    `set -a; . file` yields 30 — a fail-open the per-line rule closes;
  - the runner-equivalent default applies ONLY when the file is
    recognizably the deployed retention env: at least one OTHER
    `NODE27_TIMESERIES_RETENTION_*` assignment is accepted while the
    window assignment is absent or empty — EXCLUDING the archive-side
    pointer variable `NODE27_TIMESERIES_RETENTION_ENV` itself, which
    shares the prefix but is never consumed by the runner and lives in
    the ARCHIVE env files (round-2 verified C1: counting it accepted the
    guard's own env file — a self-referencing pointer silently
    defaulted to 14). The real bytes of both shipped archive templates
    are pinned REFUSED by test, so any future retention-prefixed
    addition to them that would re-open the hole turns red. A readable
    file with no recognized retention-family assignment at all (wrong
    file, `/dev/null`, a stale copy) refuses — the guard is pointed at
    something that is not the runner's config, and defaulting there
    re-admits the exact #1227 silent pass.

## D2 — Path plumbing: explicit REQUIRED env var, no derived default

Both consumers gain REQUIRED `NODE27_TIMESERIES_RETENTION_ENV` (absolute
path to the deployed retention env). Missing/empty/relative → refuse, and
the relative-path refusal must name absoluteness (not be masked by a
missing-file error).

- REJECTED: deriving a default from `NODE27_PRODUCT_ARCHIVE_REPO_ROOT`
  (the audit has no equivalent root var; two derivation rules would
  diverge) or hardcoding `/home/nwm/NWM/...` (host-layout assumption in
  shared code).
- Deployment already requires an env edit to clear the (14, 21) refusal;
  adding the line is part of the same operator step.

## D3 — API shape: required kwarg, single comparison site, pinned seams

`retention_days` loses its default in BOTH `validate_archive_configuration`
and `resolve_archive_storage_config`; `DEFAULT_DB_RETENTION_DAYS` is
deleted (replaced by `DEFAULT_RETENTION_WINDOW_DAYS` whose only role is
the runner-equivalent missing-assignment default, D1). The duplicated
comparisons in the mover (`:4339-4340`) and audit (`:1058-1061`) are
removed; `validate_archive_configuration` is the single comparison site
both route through. Refusal messages carry both live numbers.

- **Mover seam (fixture-review P2-1)**: `MoverConfig` gains a
  `retention_env_path` field resolved in `_config_from_args` (where env
  reading already lives); `_validate_config(config)` stays PURE over the
  dataclass — it calls the helper with `config.retention_env_path` and
  passes `retention_days=` to the shared validation. No `os.environ`
  reads inside `_validate_config`; existing `archive.run(...)` tests stay
  env-independent by setting the field in the `_config` helper (pointing
  at a per-test tmp retention env). No autouse env fixture.
- **Audit wiring (fixture-review P1-3)**: the audit starts calling
  `validate_archive_configuration` with
  `cleanup_roots={"object_store_root": object_root}`. The newly imported
  checks (non-empty cleanup_roots, archive/object-root overlap
  rejection, path normalization) are INTENDED hardening; the audit keeps
  its own `_absolute()` values for the `AuditConfig` it constructs (the
  shared function's normalized result is used for validation only, not
  to replace the audit's fields — no #1153-style root drift). A healthy
  node-27-shaped config (non-overlapping roots) must still parse; an
  overlapping pair now refuses deliberately (pinned by test).
- **Audit error class (fixture-review P1-2)**: `config_from_args` catches
  `ArchiveConfigurationError` and re-raises as `AuditConfigError` so a
  config refusal publishes `outcome=blocked` /
  `refusal_reason=CONFIG_INVALID`, not `indeterminate`/UNEXPECTED_ERROR.
- REJECTED: keeping a defaulted kwarg for "compatibility" — a defaulted
  guard input is precisely the never-wired seam this issue documents.
- Known test fallout (sized in-fixture, round-2 review completed the
  list):
  - `tests/test_storage.py:873-892` (two `resolve_archive_storage_config`
    constant-14 rows — rewritten to explicit `retention_days=`) PLUS the
    six direct `validate_archive_configuration(...)` call sites at
    `:801,824,832,840,847,870` (unrelated overlap/normalization rows —
    mechanically gain `retention_days=14`; their asserted semantics stay
    byte-identical);
  - `tests/test_node27_product_archive.py:3833`
    (`test_invalid_minimum_age_never_falls_back`, message text changes),
    `:3840` (`test_minimum_age_equal_to_fourteen_day_policy_is_accepted`,
    becomes equality-against-live-window row), the `_config` helper
    (`:74-88`) and the additional `MoverConfig(...)` constructions at
    `:101` (`_live_shape_config`), `:861`, `:4244` — all gain
    `retention_env_path`;
  - `tests/test_node27_storage_inventory_audit.py`: `_args` helper
    (`:3132-3144`) sets the path var; rows `:3150,3157,3159,3168,3335,
    3345` re-based (`:3157` asserts a successful parse and must keep
    passing once the helper provides the var); `MoverConfig` use at
    `:288`.
  - `MoverConfig` field-order trap: `retention_env_path` (no default)
    must be declared BEFORE the first defaulted field in the dataclass
    (`scripts/node27_product_archive.py:257-268`), or the class fails to
    import.

## D4 — Deployment sequencing documented honestly; no receipt/schema changes

With live (14, 21) the new guard refuses mover and audit at startup. This
is the invariant working as specified. Honest surface inventory
(fixture-review P2-4 / P1-1):

- MOVER: config refusal surfaces as journal/stderr
  `{"status":"failed",...}` + non-zero exit ONLY — `_validate_config`
  runs before `_publish_refusal_receipt` is reachable, so NO receipt is
  written and none is added by this change (mover receipt schema
  untouched; the last on-disk receipt stays the previous success — noted
  in the runbook so monitoring is not pointed at a receipt that will not
  change).
- AUDIT: the audit DOES publish a terminal receipt on config refusal —
  over its production receipt path, which is the retention gate's
  completeness-receipt input. Post-deploy on the drifted pair, every
  audit tick therefore replaces the completeness receipt with
  `blocked/CONFIG_INVALID` and the #855 retention gate is starved in
  addition to the mover stopping. This cascade is documented in the
  runbook and the PR; it is still fail-closed in the safe direction
  (retention refuses; nothing is dropped).
- No grace mode, no warn-only flag (a warn-only guard is the
  comment-coupling again, one level up). The runbook documents the two
  operator exits (raise min age after capacity assessment / lower the
  window) — the decision itself is an operator action tracked by a
  follow-up ops issue (see tasks; fixture-review P1-5), not performed
  here.

## D5 — Residual risks (recorded)

- (a) The guard reads the retention env FILE, not the runner's in-memory
  value; a shell-exported override bypassing the file still
  desynchronizes. Accepted: the systemd units source the env files;
  file-level truth is the deployment contract.
- (b) If the retention timer is genuinely not installed (observed
  2026-08-01, unconfirmed), the DB hot window is effectively unbounded
  and NO finite min age satisfies the underlying display invariant. Out
  of scope; tracked in the same follow-up ops issue as the min-age
  decision (P1-5).
- (c) TOCTOU between config validation and the tick body — same exposure
  as every other env-read; per-tick revalidation at startup is the
  existing model and unchanged.
- (d) Lexical parser fidelity is bounded to the DETECTABLE set (round-3
  honest narrowing): unsupported-shape refusal is keyed on the literal
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=` substring on non-comment
  lines (catches `readonly`/`declare` prefixes and truncated edits)
  plus the round-1 value-shape refusals (leading whitespace, `#`-value,
  CRLF). Shell forms that set the window variable WITHOUT that
  substring — `VAR+=21`, `: ${VAR:=21}`, nested `source`/`.`,
  `printf -v`, `read`, `eval` — bypass detection and resolve to the
  runner-equivalent default while the runner may export a different
  value: a verified round-3 residual (fail-open direction, low
  likelihood — requires non-plain hand edits of the deployed env),
  deferred to issue #1230, whose fix is a closed-world file grammar
  (every line must be blank, a full-line comment, or a plain accepted
  assignment; pre-audited zero false-refusals across all 15 shipped
  `infra/env/*.example` templates). The DIFFERENTIAL oracle test
  (added when the parser-fail-direction class repeated across two
  review rounds, triggering the 6.2 invariant audit) compares the
  helper against `bash -c 'set -a; . file'` + the runner's strict
  parse over the enumerated corpus — the helper MUST either refuse or
  return exactly the runner's effective window. It is a regression
  pin, not a search: it cannot discover shapes outside its corpus.
  Multi-line quoted values remain the other recorded exception
  (strict xfail). The remaining honest residual is FILE IDENTITY: a
  wrong path whose target is lexically indistinguishable from the
  deployed retention env (e.g. the shipped `.example`, which carries a
  valid `WINDOW_DAYS=14` assignment) cannot be detected lexically; the
  runbook's dual-pointer note (`NODE27_TIMESERIES_RETENTION_ENV` and
  the runner wrapper's `NODE27_TIMESERIES_RETENTION_ENV_FILE` must
  reference the same file) is the operational mitigation.
