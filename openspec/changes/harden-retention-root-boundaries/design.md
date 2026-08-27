## Context

#1318 added default-off `runs/` retention for configured workspace and copyback roots. Its root hygiene applies only to additional roots; `OBJECT_STORE_ROOT=""` still resolves to the process CWD, and relative primary values still resolve there. Its equality-only deduplication is insufficient when one resolved root lies inside another root's `runs/<run_id>` subtree. Its additional-root deletion uses `rmtree_no_follow`, which refuses every descendant symlink after potentially deleting earlier entries, so the same run is selected and fails on every pass.

Fixture triage: issue type `bugfix`; project profile `NHMS`; blast radius `high`; fixture level `expanded`; upstream suggested level `absent`; repair intensity `high`. Mandatory triggers are path, delete, shared helper, and receipt behavior. Minimal mergeable slice: the three issue contracts together at the shared root-admission/deletion seam; splitting would duplicate edits and review of the same helper and regression matrix.

## Goals / Non-Goals

**Goals:**

- A blank, whitespace, or relative primary root never reaches `Path.resolve()` or a scan, and yields a readable no-op receipt record.
- No pair among the resolved primary/additional roots has an ancestor/descendant relationship in the accepted set.
- An aged additional-root run containing top-level and nested symlinks is reclaimed in one pass without touching link targets and is not selected on a second pass.
- Existing cutoff, frontier, protected-path, runs-only, gate, and per-entry failure behavior remains intact.

**Non-Goals:**

- Replacing the primary-root `shutil.rmtree` path with a containment primitive.
- Following symlinks, accepting a symlinked `runs/` root, changing cycle prefixes/windows/frontier, or changing receipt schema version.
- Treating production's currently absent internal symlinks as an urgent operational event; this closes a defensive liveness gap.

## Decisions

### D1: One parser, lane-specific reasons, raw pass input

Introduce one root-candidate sanitizer used before constructing/resolving both primary and additional roots. Freeze the new reason tokens as `primary_root_blank`, `primary_root_not_absolute`, and `root_overlap`; retain `extra_root_not_absolute` for additional-root compatibility. `None` stays an ordinary unset no-op; an explicitly empty/blank primary is recorded before it can collapse into `None`.

`ProductionSchedulerConfig` SHALL capture its constructor-time `object_store_root` value in a private raw field before `__post_init__` normalizes it. `scheduler_runtime._run_retention` SHALL pass that raw field to retention. The default factory therefore preserves the raw environment value, while programmatic callers preserve their explicit value. All non-retention scheduler paths continue to consume the existing normalized `object_store_root` field.

Alternatives rejected: validating only inside the normalized config is too late because empty becomes `None` and relative values become workspace-absolute; reading `os.getenv` directly in the pass would cover the default factory but silently ignore programmatic config construction. The cleanup CLI already passes the raw environment value directly.

### D2: Reject all resolved root overlap

Treat equality and ancestor/descendant relationships as overlap. The primary root wins over every additional root. Among additional roots, the first configured accepted root wins; each later conflicting root is omitted from `extra_roots.roots` and contributes one receipt skip with a stable `root_overlap` reason and the conflicting accepted root. Equality remains a silent dedup to preserve #1318 behavior; unequal overlap is recorded because it is a misconfiguration.

Alternative rejected: compute target-set intersections. A root nested below `A/runs/<run_id>` can be deleted as part of A's selected tree, so enumeration-order bookkeeping cannot make execution safe or freed-byte accounting trustworthy.

### D3: Selected additional-root run trees are disposable residue

Use `remove_tree_allow_symlinks(path.parent, path.name, containment_root=root, missing_ok=False)` for selected additional-root run trees. It opens directories through no-follow descriptors, unlinks symlink entries themselves, and never traverses their targets. A symlinked `runs/` root remains rejected before planning. This classifies contents of an aged, selected run workspace as removable residue, not immutable tamper evidence; otherwise one link permanently locks capacity recovery. Both workspace and copyback roots use the same rule because retention already selected the whole canonical run as expired and neither has a consumer contract requiring preservation of an internal link as evidence.

Alternative rejected: retain refusal and add a `partial` receipt marker. It still leaves an infinite retry/capacity leak and cannot restore entries deleted before the refusal.

### D4: Preserve receipt v2 and fail-per-entry semantics

Root-admission skips use the existing `skipped` list; no v3 schema is needed. Deletion exceptions remain `failed`, the sweep continues, and freed bytes count only complete removals. Tests assert physical state, link targets, and the second pass rather than trusting receipt shape alone.

## Risk Packs Considered

- Public API / CLI / script entry: selected — scheduler pass and cleanup CLI must share primary-root hygiene.
- Config / project setup: selected — environment root values drive a deletion surface.
- File IO / path safety / overwrite: selected — overlap, containment, symlink unlink, and physical no-delete assertions.
- Schema / columns / units / field names: selected — freeze `primary_root_blank`, `primary_root_not_absolute`, `root_overlap`, `conflicting_root`, and existing reason/v2 compatibility.
- Auth / permissions / secrets: not selected — no authorization or secret surface.
- Concurrency / shared state / ordering: selected — overlapping roots and deterministic precedence; no new concurrent writer.
- Resource limits / large input / discovery: selected — eliminate perpetual re-scan of an undeletable tree; no broader discovery.
- Legacy compatibility / examples: selected — equality dedup, primary prefix behavior, and v2 shape stay compatible.
- Error handling / rollback / partial outputs: selected — one-pass complete removal or explicit failed entry; no symlink-refusal half-destruction.
- Release / packaging / dependency compatibility: not selected — no dependency or package change.
- Documentation / migration notes: selected — archived D5/D6 semantics are superseded through this delta and proposal.
- Geospatial / CRS / basin geometry: not selected — no geometry.
- Hydro-met time series / forcing windows: selected — additional roots remain runs-only; forcing prefixes are untouched.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — no database.
- Slurm production lifecycle / mock-vs-real parity: not selected — no scheduler submission behavior.
- External hydro-met providers / snapshot reproducibility: not selected — no provider data.
- Run manifest / QC provenance: not selected — no manifest/QC content change.
- Published NHMS artifacts / display identity: selected — link targets and non-runs prefixes survive unchanged.

## Invariant Matrix

Governing invariant: retention deletes only a selected canonical target beneath exactly one admitted absolute root, never derives a root from CWD, never follows a symlink, and reports each completed deletion once.

Source-of-truth identity/contract: ordered configured root values normalized to an overlap-free set; receipt identity remains `(root, key)` under `nhms.production_scheduler.retention.v2`.

Surfaces:

- Producers: `ProductionSchedulerConfig` raw-value capture, `plan_retention`, `_resolve_runs_only_roots`, the new shared root sanitizer.
- Validators/preflight: raw primary/additional blank-relative checks, resolved overlap check, `runs/` symlink guard.
- Storage/cache/query: filesystem root tree only; no persisted cache/DB.
- Public routes/entrypoints: `run_retention`, `scheduler_runtime._run_retention`, `cli._run_cleanup`.
- Frontend/downstream consumers: node-27 forcing/display and ingest trees remain protected by runs-only/window semantics.
- Failure paths/rollback/stale state: per-entry `failed`; default-off extra-root gate; invalid primary becomes explicit no-op.
- Evidence/audit/readiness: v2 receipt `skipped`, `extra_roots.roots`, `deleted`, `failed`, and `freed_bytes`.

Regression rows:

- Direct/pass/CLI raw primary input `{None, "", "  ", "relative/store"}` + CWD/workspace-derived old cycle/run -> `None` is a quiet no-op; each explicit invalid value produces `primary_root_blank` or `primary_root_not_absolute`, no plan/delete, and physical bytes survive.
- Primary A plus additional B, and additional A plus additional B in both configuration orders, where B is beneath `A/runs/<canonical_run_id>/nested` -> primary or first accepted additional wins; loser records `root_overlap` with `conflicting_root=<winner>`; no loser plan or duplicate `freed_bytes`.
- Additional-root old run with top-level and nested links -> run removed; external targets byte-identical; second pass has no plan/failure for it.
- Symlinked additional `runs/` root -> still skipped and external target untouched.
- Unchanged primary absolute root / equal-root aliases / forcing prefix / frontier-protected run -> existing behavior and receipt v2 contract remain unchanged.

## Boundary-Surface Checklist

- Shared helper roots: `retention.py` root admission and `safe_fs.remove_tree_allow_symlinks`.
- Public entrypoints: direct API, scheduler pass using constructor-time raw primary input, cleanup CLI using raw env primary input.
- Read surfaces: root resolution, overlap comparison, `runs/` enumeration.
- Write/delete/overwrite: primary `shutil.rmtree` unchanged; additional run-tree deletion changes primitive.
- Staging/publish/rollback: none; default-off extra-root gate remains rollback.
- Producer/consumer evidence: receipt v2 root/reason/freed-byte attribution.
- Stale/idempotency: second pass proves removed trees are not perpetually reselected.
- Unchanged consumers: forcing/display prefixes, published-artifact protection, node-27 ingest window, scheduler frontier.

## Risks / Trade-offs

- [Path race remains on primary deletion] -> explicitly out of scope; no claim of primary containment hardening beyond configuration admission.
- [Unlinking an internal link removes potential forensic evidence] -> receipt still identifies the expired run; targets survive; the production sample had no links, and permanent retention is worse than unlinking disposable workspace residue.
- [Rejecting overlap may omit a previously scanned configured root] -> the receipt records the conflict and deterministic winner; this is fail-safe versus duplicate/ancestor deletion.
- [Two mandatory touched files predate the 1000-line guard threshold] -> add exact exclusions only for `services/orchestrator/scheduler_config.py` and `tests/test_retention.py`; keep the guard enabled at 1000 lines and track splitting both files plus removing the exclusions in #1872.

## Migration Plan

No configuration migration. Merge with existing default-off additional-root gate. Rollback is code rollback or disabling `NHMS_RETENTION_EXTRA_ROOTS_ENABLED`; invalid primary values deliberately remain no-op instead of restoring CWD scanning.

## Open Questions

None. The issue #1615 semantic decision is resolved by D3.