# Proposal: runtime-allowed-roots-errno-split (#1348)

## Why

PR #1346 fixed `_preflight_allowed_roots` (slurm storage preflight) to the
strict-realpath + errno-split paradigm, but the same dead predicate — "non-
strict `Path.resolve()` raises on a symlink loop", false since CPython 3.13
(GH-113838) — survives verbatim at three allowed-roots-level sites:

1. `services/orchestrator/scheduler_runtime_roots.py:448-463`
   `_scheduler_allowed_roots` — feeds `_scheduler_allowed_roots_policy_check`
   and every `_scheduler_root_check` containment decision on both the
   lock/evidence arm and the full runtime arm, plus the preflight payload's
   `allowed_roots` evidence field.
2. `services/orchestrator/scheduler_config.py:1058-1071`
   `_db_free_allowed_roots` — the db-free containment base consumed by
   `_db_free_path_check`.
3. `services/orchestrator/retry.py:1529-1535` (db-free selector allowed-roots
   lane) — its `db_free_allowed_root_unresolvable` rejection is dead code on
   BOTH interpreter arms (≤3.12: RuntimeError passes through `except OSError`;
   3.13+: nothing raises).

Consequences on 3.13+: a symlink-loop root is silently admitted as an
**approved containment base** (phantom root) at all three sites — the same
fail-open class as #1332/#1344/#1345 — and, worse, the SAME run's evidence
now self-contradicts: `slurm_preflight.checks.allowed_roots == []` with an
UNSAFE_PATH blocker while
`runtime_root_preflight.checks.allowed_roots_policy.allowed_roots ==
['<loop>']` with no blocker (issue #1348 证据 3, reproduced on 3.14.2). On
≤3.12: site 1's bare `raise` (:457) escapes as an errno-less RuntimeError on
database-backed runtimes instead of a structured blocker — reachable now
that #1347 (PR #1349) unblocked config construction.

## What Changes

- All three sites adopt the merged family paradigm verbatim (#1344/#1346/
  #1349): `os.path.realpath(expanded, strict=True)` + `except OSError` +
  errno split; both forms of `Path.resolve()` are banned for allowed-roots
  normalization (non-strict never raises on 3.13+, strict raises errno-less
  RuntimeError on ≤3.12).
- Site 1 (`_scheduler_allowed_roots`): ENOENT → non-strict `os.path.realpath`
  fallback (historical "admitted missing root" semantics); other errno on
  db-free → existing #831 lexical-tolerance arm, no blocker; other errno on
  database-backed runtimes → the root is DROPPED from the effective allowed
  roots and a structured `SCHEDULER_ROOT_ALLOWED_ROOTS_<REASON>` blocker
  (native `_scheduler_root_blocker` family, reason via
  `_scheduler_root_os_error_reason`) is surfaced through both preflight arms
  — never a bare raise. Blocker paths obey the existing
  `evidence_safe_paths` masking discipline (database-backed +
  `repair_missing_forcing` runs mask as `[local-path]`).
- Site 2 (`_db_free_allowed_roots` — NOT db-free-only: `db_free_runtime_
  preflight` also runs on database-backed `repair_missing_forcing` runs and
  adjudicates real containment there): becomes a pair function; ENOENT →
  non-strict realpath fallback; non-ENOENT on db-free → #831 lexical arm
  verbatim; non-ENOENT on the database-backed repair-authority lane → root
  dropped + a `db_free_allowed_root_unsafe` blocker through that preflight's
  existing `_db_free_blocker` channel. The 3.13+ collapsed-arms
  discriminator is restored.
- Site 3 (retry.py db-free selector allowed-roots lane): ENOENT → non-strict
  realpath, admitted; non-ENOENT → `db_free_allowed_root_unresolvable`
  rejection — the dead lane becomes reachable and test-anchored.
- Evidence-plane consistency (the issue's primary acceptance anchor): for the
  same config and the same loop root, `slurm_preflight.checks.allowed_roots`
  and `runtime_root_preflight.checks.allowed_roots_policy.allowed_roots`
  reach the same verdict (root dropped + blocker), asserted by a regression
  test that exercises both planes in one process.
- `require_runtime_roots=False` (db-backed default) is explicitly ruled
  (design D6): the not-required payload shares the adjudication — unsafe root
  dropped from the displayed allowed roots, no blocker (the payload has no
  blocker channel and declares no containment adjudication), never an
  unhandled exception; the slurm storage preflight stays the blocker-bearing
  plane. The #1346 B8 tripwire test — whose docstring mandates it goes red
  when #1348 lands — is flipped to this structured assertion (its `skipif` on
  3.13+ removed).
- Behavior-change disclosure: EACCES/EPERM (untraversable ancestor) roots,
  silently admitted today on every version, are now dropped + blocked
  (`..._NOT_WRITABLE`) on database-backed runtimes and rejected by the
  db-free selector lane — the split is "not ENOENT", not "only ELOOP", and
  this tightening applies to production interpreters (3.11/3.12) too.
- The new pair function is registered through the scheduler facade per the
  module's convention (forwarder/EXPORTS lists +
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` entry).

## Impact

- Affected specs: `slurm-array-runner-integration` (MODIFIED requirement —
  the unresolvable-allowed-storage-root scenario extends to the runtime-root
  preflight planes and the db-free selector lane).
- Affected code: `services/orchestrator/scheduler_runtime_roots.py`
  (site 1 + its two preflight-arm callers), `services/orchestrator/
  scheduler_config.py` (site 2), `services/orchestrator/retry.py` (site 3),
  `tests/test_production_scheduler.py`.
- Facade surfaces (`scheduler_candidate_runtime.py` forwarders) re-export by
  name; consumer closure re-verified by grep during implementation.

## Non-Goals

- `_optional_config_path` (scheduler_runtime_roots.py:502) — fixed by #1347
  / PR #1349, untouched here.
- `_preflight_allowed_roots` / `_storage_root_check` — fixed by PR #1346,
  referenced as paradigm only.
- Path-level (non-root) same-family candidates flagged by the issue as
  evaluate-only: `_db_free_path_identity` (scheduler_config.py:1074-1081),
  `_db_free_selector_path_rejection` (retry.py:1555-1558), the path-level
  `resolve(strict=False)` inside `_db_free_path_check`
  (scheduler_config.py:1131), and the parent-level `resolve()` trio
  (`_confined_path` scheduler_runtime_roots.py:491,
  `_config_path_preserve_final_component` :530,
  `_config_path_relative_to_preserve_final` :537).
  Evaluated in design D5; deferred with recorded reasons, not silently.
- No evidence-schema changes; no blocker-code renames beyond adding the
  allowed_roots instances of the existing `SCHEDULER_ROOT_*` family.
