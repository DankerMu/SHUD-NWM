# Archive min-age guard must compare against the live DB retention window (#1227)

## Why

The invariant "archive minimum age >= DB retention window" is real and
load-bearing: the mover retires hot object-store sources after
`NHMS_ARCHIVE_MIN_AGE_DAYS`, and the ADR 0001 station-forcing display route
is disk-only with no DB/archive fallback, so a hot window shorter than the
DB hot window produces a user-visible display contradiction (DB timeseries
served, station CSV stably NOT_FOUND for the same cycles). All three
existing guards compare against the compile-time constant
`DEFAULT_DB_RETENTION_DAYS = 14` (`packages/common/storage.py:111,203`;
`scripts/node27_product_archive.py:4339`;
`scripts/node27_storage_inventory_audit.py:1058`), and the one seam that
could carry the real value — `validate_archive_configuration(retention_days=)`
— has zero production callers passing it. Raising the live retention window
to 21 d (node-27, verified read-only 2026-08-01: `WINDOW_DAYS=21` vs
`NHMS_ARCHIVE_MIN_AGE_DAYS=14`) silently unloaded the invariant: the live
(14, 21) pair passes every guard while the mover deletes hourly under
`--enforce`. The coupling lives in a comment, not in code.

## What Changes

Make the live retention window an explicit, fail-closed input to the guard:

- `packages/common/storage.py`:
  - new `read_retention_window_days(env_path)` — extracts
    `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` from the retention env file
    (shell-source lexical semantics per design D1: last assignment wins,
    `export`/quotes/trailing comments handled); a MISSING or EMPTY
    assignment resolves to the shared `DEFAULT_RETENTION_WINDOW_DAYS = 14`
    — the retention runner's own live-effective semantics, drift-locked by
    importing the same constant into the runner — while a missing file,
    unset/empty/relative path variable, or a PRESENT non-integer /
    non-positive value raises `ArchiveConfigurationError` (fail closed —
    no constant fallback for unreadable sources).
  - `retention_days` becomes a REQUIRED keyword argument of both
    `validate_archive_configuration` and `resolve_archive_storage_config`
    (defaults removed) so no caller can silently fall back to 14.
  - `DEFAULT_DB_RETENTION_DAYS` is deleted; the new
    `DEFAULT_RETENTION_WINDOW_DAYS` exists ONLY as the runner-equivalent
    missing-assignment default shared with the retention runner (design
    D1), never as a comparison fallback.
- `scripts/node27_product_archive.py`: `_validate_config` drops the
  duplicated hardcoded comparison; the mover config resolves the retention
  env path from new REQUIRED env `NODE27_TIMESERIES_RETENTION_ENV`
  (absolute path), reads the window via the shared helper, and passes
  `retention_days=` to `validate_archive_configuration` — the single
  comparison site.
- `scripts/node27_storage_inventory_audit.py`: same wiring (REQUIRED
  `NODE27_TIMESERIES_RETENTION_ENV`, shared helper via
  `validate_archive_configuration` with
  `cleanup_roots={"object_store_root": object_root}`, drop the local
  hardcoded comparison; `ArchiveConfigurationError` re-wrapped as
  `AuditConfigError` so the refusal publishes blocked/CONFIG_INVALID;
  the refusal message names both live numbers).
- `scripts/node27_timeseries_retention.py`: one line — its
  `_DEFAULT_WINDOW_DAYS` becomes an import of the shared
  `DEFAULT_RETENTION_WINDOW_DAYS` (drift-lock; consumption unchanged).
- `infra/env/node27-product-archive.example` and
  `infra/env/node27-storage-inventory-audit.example`: comment no longer
  hardcodes "14-day"; both gain the `NODE27_TIMESERIES_RETENTION_ENV`
  line pointing at the deployed retention env path.
- `infra/env/node27-timeseries-retention.example`: header gains one
  sentence — archive-side guards now depend on this file's PATH (a
  move/rename breaks them fail-closed).
- Spec sync: ADDED requirement in `timeseries-product-archive` (main spec)
  stating the validation compares against the live window and fails closed
  when the window source is unreadable; the pending
  `tier-node27-timeseries-storage` delta's "(14 days)" wording updated in
  place to "the live DB retention window", and the same pending change's
  `design.md` line asserting "config-validated >= the 14-day DB retention
  window" (~:80) updated to the live-window wording in the same commit.
- Runbook `docs/runbooks/tier-node27-timeseries-storage.md`: the
  product-archive section documents the new env line, the live-window
  coupling, and the deployment consequence below.

Deployment consequence (documented, NOT performed by this change): with the
live pair (min_age=14, window=21), the first deployment of this code makes
the mover and the audit refuse at startup — fail closed. Full cascade,
stated honestly: the mover's refusal is journal/stderr + non-zero exit
only (no receipt is written; monitoring must not watch the mover receipt
for this); the AUDIT's refusal DOES publish a terminal blocked/
CONFIG_INVALID receipt over its production receipt path, which is the
retention gate's completeness-receipt input — so every audit tick also
starves the #855 retention gate (fail-closed in the safe direction:
nothing is dropped). Hot-source deletion stops (protective: the display
gap stops growing). Clearing it is an operator decision with a real
capacity trade-off (raise `NHMS_ARCHIVE_MIN_AGE_DAYS` to >= 21 — hot
volume grows — or lower the retention window back), tracked by a
follow-up ops issue filed before merge (tasks task 8) together with the
retention-timer question. This change does not edit any live env file.

## Non-goals

- Editing live node-27 env files or deciding the min-age-vs-capacity
  trade-off (operator decision, tracked by the task-8 follow-up ops
  issue).
- Installing/enabling the missing `nhms-node27-timeseries-retention.timer`
  (same follow-up ops issue).
- #1222 (runbook query literal, merged), #1156 (compression walls),
  #1153 (archive_root path drift).
- Changing how the retention runner CONSUMES the window for dropping
  (the one-line default-constant import is a drift-lock, not a behavior
  change — pinned by test row (j)).
- Cross-checking `NHMS_ARCHIVE_MIN_AGE_DAYS` between the two archive-side
  env templates (they are copies of the same variable for two services;
  both now validate against the same live source at startup).
