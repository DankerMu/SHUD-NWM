# Tasks: fix-archive-min-age-live-window

Fixture level: expanded · Repair intensity: high · Issue #1227

Triage note: fail-closed configuration guard in front of an enforce-mode
hot-source retirement lane that is actively running hourly on node-27.
Both false directions are costly: false-accept keeps the live display gap
growing silently; false-refuse (over-eager refusal on a healthy pair)
stalls the archive lane and lets the hot volume fill — D1's
runner-equivalent missing-assignment semantics exist precisely to avoid
one such false-refuse. Expanded is mandatory; high intensity per the
live-impact context.

Change surface:
- packages/common/storage.py (new `read_retention_window_days`;
  `DEFAULT_RETENTION_WINDOW_DAYS = 14` shared default;
  `retention_days` REQUIRED in `validate_archive_configuration` and
  `resolve_archive_storage_config`; `DEFAULT_DB_RETENTION_DAYS` deleted)
- scripts/node27_timeseries_retention.py (ONE line:
  `_DEFAULT_WINDOW_DAYS` becomes an import of the shared default; window
  consumption semantics unchanged)
- scripts/node27_product_archive.py (`MoverConfig.retention_env_path`
  field resolved in `_config_from_args`; `_validate_config` stays pure,
  drops the hardcoded comparison :4339-4340, calls the helper and passes
  `retention_days=`)
- scripts/node27_storage_inventory_audit.py (same wiring;
  `cleanup_roots={"object_store_root": object_root}`; drops :1058-1061;
  catches `ArchiveConfigurationError` → `AuditConfigError` so refusal
  publishes blocked/CONFIG_INVALID; audit keeps its own `_absolute()`
  field values — shared normalization is validation-only)
- infra/env/node27-product-archive.example +
  infra/env/node27-storage-inventory-audit.example (comment
  de-hardcoded; new `NODE27_TIMESERIES_RETENTION_ENV` line)
- infra/env/node27-timeseries-retention.example (header gains one
  sentence: archive-side guards depend on this file's PATH)
- openspec main spec `timeseries-product-archive` (ADDED requirement) +
  pending `tier-node27-timeseries-storage` delta "(14 days)" wording AND
  its design.md "config-validated >= the 14-day DB retention window"
  line (~:80) updated in place
- docs/runbooks/tier-node27-timeseries-storage.md product-archive
  section (new env line; live-window coupling; deployment consequence
  incl. the audit→completeness-receipt→retention-gate starvation cascade
  and the mover's no-receipt refusal surface; the two operator exits;
  no-live-edit note)
- tests/test_storage.py, tests/test_node27_product_archive.py,
  tests/test_node27_storage_inventory_audit.py (fallout sized in design
  D3 + new rows)

Must preserve:
- All other validation semantics in `validate_archive_configuration`
  (root overlap, cleanup-roots, positive bounds) byte-identical
- Mover default-dry-run / enforce-flag behavior untouched; NO new mover
  receipt outcome; mover receipt schema untouched
- Retention runner window CONSUMPTION unchanged (only the default
  constant's home moves); runner behavior for missing/empty/invalid
  values byte-equivalent before/after
- No live env file edits; templates keep `NHMS_ARCHIVE_MIN_AGE_DAYS=14`
  as shipped default
- Audit receipt schema unchanged (blocked/CONFIG_INVALID already exists)

Must add (per design D1-D4):
- `read_retention_window_days(env_path)` with D1 lexical forms and
  runner-equivalent missing/empty-assignment default; fail closed on
  missing file, unset/empty/relative path var, non-integer or
  non-positive present values
- REQUIRED `NODE27_TIMESERIES_RETENTION_ENV` (absolute) in both
  consumers; relative-path refusal names absoluteness
- Single comparison site; refusal message with both numbers

## Implementation tasks

- [x] 1. storage.py: helper + shared default + required kwargs +
  constant deletion; retention runner one-line import.
- [x] 2. Mover wiring per D3 (config field, pure `_validate_config`,
  hardcoded comparison removed, `retention_days=` passed).
- [x] 3. Audit wiring per D3 (cleanup_roots, error-class re-wrap,
  validation-only normalization, local comparison removed).
- [x] 4. Env templates: comment rewrite + new line (both archive-side
  templates); retention example header sentence.
- [x] 5. Spec sync: ADDED requirement (this change) + pending #855 delta
  wording update in place; runbook update per change surface.
- [x] 6. Tests — rows (a)-(j) minimum:
  (a) live replay: temp retention env `WINDOW_DAYS=21`, min age 14 →
  mover `_validate_config` refuses AND audit `config_from_args` raises
  `AuditConfigError`; messages carry 14 and 21; audit `main()`-level row
  publishes `outcome=blocked` + `refusal_reason=CONFIG_INVALID` to a
  test-scoped receipt path; mover row asserts NO receipt file is
  created/modified by the refusal;
  (b) boundary: (21,21) and (30,21) PASS for both consumers; (20,21)
  REFUSES (kills near-boundary weakening);
  (c) fail-closed matrix (parametrized at the helper seam + at least one
  consumer-level row per consumer): path var unset; empty; RELATIVE PATH
  POINTING AT AN EXISTING FILE with a valid window (refusal must name
  absoluteness — not be masked by missing-file); missing file;
  non-integer value; zero; negative;
  (d) runner-equivalent defaults + lexical forms (helper seam,
  parametrized): missing assignment → shared default 14; empty value →
  14; `export VAR=21` → 21; `VAR="21"` and `VAR='21'` → 21;
  `VAR=21   # trailing comment` → 21; surrounding whitespace → 21;
  full-line comment `# VAR=99` ignored; decoy `..._WINDOW_DAYS_OLD=99`
  ignored; last assignment wins (14 then 21 → 21);
  (e) no-fallback pin: missing FILE refuses even when
  `NHMS_ARCHIVE_MIN_AGE_DAYS` alone would satisfy the old constant-14
  rule (kills any "fall back on unreadable source" mutant); paired with
  (d)'s missing-ASSIGNMENT row so the two shapes are distinguished;
  (f) single-site pin: with a valid window source, the (14,21) refusal
  originates from `validate_archive_configuration` for BOTH consumers,
  WITHOUT any autouse env fixture (e.g. altering the shared comparison
  on a scratch copy flips both consumer rows — see mutation (i));
  (g) template lint: both archive-side example files contain the new env
  line and no longer state the invariant bound as a "14-day" literal;
  (h) drift-lock: a test asserts the retention runner's default equals
  `DEFAULT_RETENTION_WINDOW_DAYS` (import identity or value equality
  with a comment pinning why);
  (i) audit hardening rows: healthy node-27-shaped config
  (non-overlapping roots, window 14, min age 14) PASSES config parse;
  overlapping object/archive roots now REFUSE deliberately;
  (j) runner regression guard: retention runner window resolution for
  missing/empty/invalid env values behaves byte-identically (reuse or
  extend its existing config tests; zero behavior diff).
- [x] 7. Mutation proof on a scratch copy (never the working tree):
  (i) comparison weakened to constant 14 (ignore `retention_days`) →
  row (a) fails for both consumers;
  (ii) missing-FILE fallback to 14 introduced in the helper → rows
  (c)/(e) fail;
  (iii) mover's `retention_days=` wiring dropped → TypeError in mover
  rows (required-kwarg enforcement is load-bearing);
  (iv) extraction takes FIRST assignment instead of last → row (d)
  fails;
  (v) comparison off-by-one (`< retention_days - 1`) → row (b)'s
  (20,21) fails.
  Capture outputs for the PR body.
- [x] 8. Follow-up ops issue (fixture-review P1-5) filed BEFORE merge,
  covering: the live min-age-vs-capacity decision (issue #1227
  acceptance item 7 — raise `NHMS_ARCHIVE_MIN_AGE_DAYS` ≥ live window
  or lower the window, decision + receipt) AND the
  retention-timer-not-installed question (D5-b). PR body links it; #1227
  closes with an explicit hand-off reference to it.

## Round-1 fix pass (PR #1229 cross-review, verified findings)

- [x] 9. Parser fail-direction hardening (A1/A2 CONFIRMED; design D1
  round-1 amendments are the contract):
  (a) `VAR= 21` (unquoted leading-whitespace value) refuses;
  (b) `VAR=#21` refuses as a present non-integer value (drop the
  value-index-0 comment arm from `_strip_env_trailing_comment`);
  (c) CRLF / non-`\n` line-break content refuses (split on `\n` only);
  (d) no accepted window assignment + a non-comment line containing
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=` → refuse (kills
  `readonly`/`declare`/truncated-edit silent default);
  (e) recognizability rule: readable file with zero accepted
  `NODE27_TIMESERIES_RETENTION_*` assignments refuses; the existing
  `missing-assignment-uses-runner-default` and
  `only-commented-assignment-is-unassigned` rows are re-based onto
  bodies carrying a sibling retention assignment;
  (f) new refusal tests for every shape above (incl. `readonly` /
  `declare -i` forms, `/dev/null`, and an archive-env wrong-file row)
  plus sibling-line default rows; `storage.py` docstring corrected to
  match.
- [x] 10. Docs/test-honesty rides (B3 PLAUSIBLE, B4 CONFIRMED):
  drift-lock test docstring states value-equality (small-int interning
  defeats `is` identity — no source-inspection assertion needed, row (h)
  floor allows value equality); runbook min-age-guard section + both
  archive template comments gain the dual-pointer sentence
  (`NODE27_TIMESERIES_RETENTION_ENV` must name the same file the runner
  wrapper's `NODE27_TIMESERIES_RETENTION_ENV_FILE` selects — repointing
  one without the other leaves the guard reading a stale window).

## Required evidence

- `uv run pytest -q tests/test_storage.py
  tests/test_node27_product_archive.py
  tests/test_node27_storage_inventory_audit.py
  tests/test_node27_timeseries_retention.py` all green (retention file
  included for row (j) / task 1's one-line change).
- `uv run ruff check .` clean; markdownlint on the runbook 0 issues.
- `openspec validate fix-archive-min-age-live-window --strict
  --no-interactive` valid; `tier-node27-timeseries-storage` still valid.
- Mutation outputs (i)-(v).
- `git diff --stat` limited to the change surface; no live env files.
- node-27 read-only live receipt (fixture-review P1-1/P2-5 hardened):
  scratch worktree with the PR branch; production env pair as-is
  (min age 14, window 21).
  - MOVER: run WITHOUT `--enforce`, receipt AND LOCK redirected to
    scratch (the lock is acquired before validation — production hourly
    tick must never see lock-contended), with the read-only watermark
    DSN sourced OR `--reference-time` supplied so the recorded failure
    is the MIN-AGE refusal naming 14 and 21, not a watermark error;
    non-zero exit; production receipt untouched.
  - AUDIT: run with `--receipt-path <scratch>` (CLI seam resolves before
    validation); expect blocked/CONFIG_INVALID in the SCRATCH receipt;
    assert the production completeness receipt
    (`/home/nwm/node27-storage-inventory-audit-logs/completeness-receipt.json`)
    mtime/sha unchanged — it is the retention gate's input and MUST NOT
    be overwritten by evidence gathering.
  - Record which guard fired and the verbatim messages.

## Non-goals

- Live env edits / performing the min-age capacity decision (operator;
  tracked by the task-8 follow-up issue).
- Retention timer installation (same follow-up issue).
- #1222 / #1156 / #1153 scopes; ADR 0002 narrative drift (governance
  lane).
- Mover/audit receipt schema changes.
