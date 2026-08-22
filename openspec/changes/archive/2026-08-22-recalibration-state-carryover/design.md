## Context

`fingerprint_gated_state_clone` gates every state clone on the G10 ten-surface
`hydrologic_core_fingerprint`. That predicate answers "is this the same model
package?" It is used to answer "is this state transferable?", and those two
questions differ on exactly two surfaces: `calibration` and `solver_config`.
Changing a calibration parameter changes how the model steps forward from a
state; it does not change what the state vector *is* or the mesh/river/soil
geometry it lives on.

This change adds a second, narrower, explicitly declared predicate for the
transferability question. The default path is untouched.

## Goals / Non-Goals

Goals:

- One additional, opt-in gate mode. Existing callers' behavior byte-identical.
- One additional provenance column so an auditor can tell the two gates apart.
- A DB-free tool that can execute the M1→M1′ clone on node-22 and leave both
  state indexes consistent in a single invocation.

Non-Goals: scheduler/admission changes; widening the allowed-to-differ set;
a DB-backed recalibration caller; the #1698/#1699 rollouts.

## Decisions

### D1 — Surface set, not a second hash format

`STATE_COMPATIBILITY_SURFACES = tuple(sorted(set(HYDROLOGIC_CORE_FINGERPRINT_LABELS)
- {"calibration", "solver_config"}))` = `("geol", "lake", "land", "mesh",
"river", "soil", "sp_att_non_forc", "state_schema")`.

`compute_hydrologic_core_fingerprint` and `verify_hydrologic_core_fingerprint_equal`
take a keyword-only `surfaces: tuple[str, ...] = HYDROLOGIC_CORE_FINGERPRINT_LABELS`.
The hashing algorithm is unchanged — per-surface hash, one `f"{label}\t{hash}\n"`
line per surface in alphabetical label order, SHA-256 of the joined buffer. An
8-surface fingerprint is a different value from a 10-surface one by construction
(different line set); the two are never compared to each other.

Consequences inside `compute_hydrologic_core_fingerprint`:

- `required_categories` for `_validate_category_files` derives from the surface
  set: the file categories are `tuple(s for s in surfaces if s in NON_SP_ATT_CATEGORIES)`.
  In 8-surface mode `calibration` is neither required nor accepted, so
  `CALIB/*` and `cfg.calib` are never enumerated.
- The `assert set(per_surface_hash) == set(HYDROLOGIC_CORE_FINGERPRINT_LABELS)`
  becomes `== set(surfaces)`.
- `state_schema` / `solver_config` / `sp_att_non_forc` surfaces are computed
  only when present in the surface set. `sp_att_path` is still required
  (it is in both sets).
- `surfaces` is validated: must be a non-empty subset of
  `HYDROLOGIC_CORE_FINGERPRINT_LABELS` with no duplicates; violations raise
  `SpAttRewriteError`.

Default-path proof obligation: `tests/test_mapping_builder_rewrite.py:1342`
asserts ten-label coverage and must stay green untouched.

### D2 — `transfer_mode` on the clone, default `fix_forward`

```python
transfer_mode: Literal["fix_forward", "recalibration"] = "fix_forward"
```

- `fix_forward` (default): every existing check, in existing order, with the
  ten-surface gate. No observable change.
- `recalibration`: the equality gate runs over `STATE_COMPATIBILITY_SURFACES`.
  On `HydrologicCoreFingerprintMismatchError` **or** `MissingPackageFileError`
  the clone refuses fail-closed with the new
  `refusal_scope = "state_compatibility_unequal"`. `MissingPackageFileError`
  maps into that scope because a hydrologic-core file present on one side and
  absent on the other *is* surface inequality; the audit record carries the
  missing side and relative path so the operator can locate it. Fix-forward
  keeps its current propagation of `MissingPackageFileError` byte-identical.
  `_refuse` today takes only a `scope`; it is extended with an optional
  `extra: Mapping[str, Any] | None = None` merged into the audit record, so the
  missing side/path ride along without changing any existing refusal's record
  shape.

All pre-gate checks are shared and unconditional in both modes: the
no-reverse-clone classifier guard, the degenerate-inputs refusal, and the
qualified-source lookup. In particular the degenerate check still requires
**both** `state_schema_bytes` and `solver_config_bytes` non-empty in
recalibration mode even though the solver bytes do not enter the 8-surface
hash — one unconditional check, no mode-conditional branch, no drift.

### D3 — Per-side gate bytes (the false-pass this change must not ship)

`fingerprint_gated_state_clone` today takes one `state_schema_bytes` and one
`solver_config_bytes` and passes each to **both** sides of
`verify_hydrologic_core_fingerprint_equal`. That was correct for July's
baseline→variant cutover (the variant copies the baseline's `cfg.ic` verbatim,
so the bytes are genuinely shared platform-level bytes). Under M1→M1′ it is a
false pass: if M1′ ships a new `cfg.ic`, feeding M1's bytes to both sides makes
the `state_schema` surface compare equal and admits a clone that this change
explicitly must refuse.

Signature extension (additive, backward compatible):

```python
m0_state_schema_bytes: bytes | None = None   # defaults to state_schema_bytes
m0_solver_config_bytes: bytes | None = None  # defaults to solver_config_bytes
```

`state_schema_bytes` / `solver_config_bytes` remain the M1 (target) side and
remain required. When the `m0_*` overrides are `None` the function behaves
exactly as today (same bytes both sides). The recalibration tool always
supplies both overrides, read per package root.

The degenerate-inputs refusal covers the overrides too: an empty override is
refused with `degenerate_gate_inputs`.

### D4 — Category enumeration in recalibration mode: union of both roots

`scripts/provision_direct_grid_scheduler_registry.py::_category_files` derives
relative paths from **one** root, and its `lake` category falls back to the
package's `*.cfg.para` when the basin has no `*.lake.*` file. Under
recalibration `cfg.para` is exactly the file that changes, so a single-root
enumeration would hash `cfg.para` into the `lake` surface and refuse the very
case this change exists to permit.

The recalibration tool therefore uses its own builder,
`_state_compatibility_category_files(m0_root, m1_root)`:

- Six categories (`mesh`, `river`, `lake`, `soil`, `geol`, `land`) — no
  `calibration`, because 8-surface mode does not accept it.
- Each category's relative-path set is the **union** of the paths enumerated
  under both roots. A file added on either side, or removed on either side, is
  therefore enumerated and raises `MissingPackageFileError` on the side lacking
  it → refuse `state_compatibility_unequal`. A single-root enumeration would
  silently false-pass a *removed* lake file, since the M1′-derived list would
  never name it.
- Only when the union for `lake` is empty on **both** roots does the builder
  substitute the package's `*.sp.mesh` relative path as a non-empty placeholder
  (`_validate_category_files` rejects an empty sequence). `sp.mesh` is
  invariant under recalibration and already covered by the `mesh` surface, so
  the substitution adds no signal and removes none — mesh drift still refuses
  via the `mesh` face. The substitution is visible in `covered_paths` as
  `lake:<basin>.sp.mesh`; that is intended and auditable, not hidden.

`_category_files` in the provision script is **not** modified; the fix-forward
path keeps using it verbatim.

### D5 — Evidence cross-check is explicitly waived in recalibration mode

Step 4 of the clone cross-checks the recomputed M1 fingerprint against
`m1_recorded_hydrologic_core_fingerprint`, "the value recorded in the M1
mapping evidence package". Two facts decide this:

1. `scripts/provision_direct_grid_scheduler_registry.py` — the producer of the
   dg variants in production — records **no** `hydrologic_core_fingerprint`
   anywhere in the variant manifest or the registry row (verified by grep).
   Only `workers/mapping_builder/evidence.py` records one, and the dg variants
   this change targets are not built through it.
2. `scripts/node22_clone_direct_grid_cutover_states.py` already passes
   `m1_recorded_hydrologic_core_fingerprint=fingerprint.hash` — the value it
   just computed. The cross-check is therefore **already vacuous** on the tool
   path.

Rather than keep a vacuous self-supply, recalibration mode makes the waiver
explicit: `m1_recorded_hydrologic_core_fingerprint` becomes optional
(`str | None`) and, when `None`, the cross-check is skipped and the skip is
recorded. In `fix_forward` mode the parameter stays **required and non-empty**
— an absent or empty value there is refused with the existing
`evidence_fingerprint_mismatch` scope, so the fix-forward contract does not
weaken. When a recalibration caller *does* supply a recorded value, the
cross-check runs against the mode's own (8-surface) recompute.

This is a deliberate contract narrowing recorded in the spec delta and in
ADR 0005, not an implementation accident.

### D6 — `clone_gate_kind` provenance column

New nullable `TEXT` column, migration `000053_state_snapshot_clone_gate_kind.sql`,
same NULL-default column-only house style as `000046`. Values: `"hydrologic_core"`
on a `fix_forward` clone, `"state_compatibility"` on a `recalibration` clone,
`NULL` on every pre-existing and non-clone row.

Surfaces that must all carry it (see Invariant Matrix): `StateSnapshot`
dataclass; **two independent copies** of the `hydro.state_snapshot` upsert SQL —
`packages/common/state_manager.py::PsycopgStateSnapshotRepository.upsert_state_snapshot`
and `packages/common/state_clone_hook.py::_CursorBoundStateSnapshotRepository.upsert_state_snapshot`
— each needing the `INSERT` column list + `VALUES` placeholders +
`ON CONFLICT DO UPDATE SET` + parameter tuple; `_snapshot_from_row`,
`_snapshot_to_dict`, `_state_index_entry_from_snapshot`,
`_state_snapshot_from_index_entry`.

The hook's copy is not optional bookkeeping: `state_clone_hook.py` is the
pre-activation hook that runs **inside** the cutover lifecycle transaction and
is one of the only two production callers of `fingerprint_gated_state_clone`.
If its SQL is not extended, every hook-driven clone row stores `clone_gate_kind`
as `NULL` while the in-memory row says otherwise — the exact silent drop that
migration `000046` already had to be threaded through both copies to avoid
(`state_clone_hook.py` carries `cloned_from_state_id` / `cloned_from_model_id` /
`clone_gate_fingerprint` today for that reason). Deduplicating the two SQL copies
is **out of scope** here; both are updated in lockstep.

Backward compatibility: the PG read path is `SELECT *` + `row.get(...)`, and
the file-index reader is `entry.get(...)` — both lenient, so an old reader
ignores the new key and a new reader tolerates its absence. There is no JSON
Schema with `additionalProperties: false` over the state index (verified: no
state-index schema exists under `schemas/`).

### D7 — Dual-index write

The tool takes `--state-index` (NFS canonical) and `--mirror-state-index`
(node-22-local scratch mirror). Both repositories are constructed with
`create_missing=False`.

The gate runs **once**: source lookups and the clone execute against the
canonical repository, then the *same* `StateSnapshot` object returned in
`StateCloneResult.cloned_row` is upserted into the mirror repository. Running
the gate twice would risk two differing rows.

This matters for the copyback merge in
`packages/common/state_manager.py`: its collision resolution treats
`current == source_entry` (dict equality) as a no-conflict replay, and raises
`state_snapshot_index_copyback_conflict` when two differing entries share an
equal `created_at`. Writing the identical row object to both indexes keeps the
two serialized entries byte-identical, so a later copyback replays cleanly.

The receipt records both index paths and a per-index write outcome. If the
mirror write fails after the canonical write succeeded, the tool exits
non-zero and the receipt names the canonical row as written and the mirror as
not written — the operator repairs the mirror before `t*` (a stalled cycle is
the fail-safe outcome under `NHMS_REQUIRE_FORECAST_WARM_START=true`).

### D8 — `--pairs` resolves by `model_id`, never through `_variant_map`

`_variant_map` keys variants on `resource_profile.baseline_model_id`. For an
M1′ produced by re-running the provision script, that field still points at the
**original baseline**, not at M1 — so the existing map cannot express an
M1→M1′ pair. `--transfer-mode recalibration` therefore takes explicit
`--pairs <M1_model_id>:<M1prime_model_id>,...` and resolves each side by
`model_id` directly out of the supplied registry payload(s).

Per-pair validation, all fail-closed:

- Both model rows exist and their package roots resolve to directories.
- Both classify direct-grid through
  `load_forcing_mapping_contract_from_manifest` (the target's classification is
  re-checked inside the clone; the source's is checked by the tool so an
  operator typo cannot silently point the tool at a legacy model).
- Both declare the same normalized `direct_grid_source_id`; the clone runs once
  per pair for that one source.
- `M1 != M1′`.

Argparse: `--warm-basins` / `--cold-basins` / `--baseline-registry` /
`--variant-registry` / `--expected-*-count` are `required=True` today. Make them
optional at the parser level and enforce them **after** parsing, per mode, so
`--transfer-mode baseline_cutover` still refuses a missing flag with a clear
error and `recalibration` refuses a missing `--pairs` / `--mirror-state-index`.
Do not restructure into subparsers — the existing invocation must keep working
verbatim.

Naming: `fingerprint_gated_state_clone`'s `m0_*` / `m1_*` parameters originally
meant "baseline" / "variant". Under `recalibration` they mean
"transfer source (`M1`)" / "transfer target (`M1′`)". The parameters are not
renamed; the docstring states both readings so a caller cannot misread which
package each side takes.

### D9 — Where the tool runs

node-22. It is the only host that can reach both the shared NFS canonical index
and the node-22-local `/scratch/frd_muziyao` mirror. node-22 is DB-free, so the
recalibration mode never constructs a DB repository. Recorded in
`docs/runbooks/current-production-ops.md`.

## Risks / Trade-offs

- **Widening a fail-closed gate.** Mitigated by: opt-in mode with an
  unchanged default; the eight remaining surfaces still refuse any
  mesh/river/soil/geol/land/lake/`sp.att`/IC drift; the union enumeration
  catching adds *and* removes; and the receipt being the declared carry-over
  evidence.
- **A new `cfg.ic` alongside a calibration update refuses.** Intentional — a
  new IC means the modeling side declared a new starting point.
- **Spin-up distortion.** Warm carry-over across a recalibration is lower
  distortion than a cold start but not zero. The announcement obligation is
  retained, unchanged, in the spec delta.
- **Two indexes, one non-atomic write.** Accepted: the failure mode is a
  stalled cycle (fail-safe), the receipt localizes it, and the alternative
  (a distributed transaction across two filesystems) is disproportionate.

## Invariant Matrix

Governing invariant: a `hydro.state_snapshot` / state-index row that carries a
model's identity may only exist if a recorded, named gate proved the state is
transferable to that model under that gate's declared surface set — and the row
records which gate.

Source-of-truth identity/contract: `(model_id, source_id, valid_time)` plus
`model_package_version` / `model_package_checksum`, with
`clone_gate_kind` + `clone_gate_fingerprint` naming the admitting gate.

Surfaces:

- Producers: `packages/common/state_clone.py::fingerprint_gated_state_clone`,
  `_build_clone_row`; the two production callers —
  `scripts/node22_clone_direct_grid_cutover_states.py` (file-index plane, node-22)
  and `packages/common/state_clone_hook.py` (DB plane, inside the cutover
  transaction; it keeps `transfer_mode` at its `fix_forward` default and is
  otherwise unchanged apart from the `clone_gate_kind` SQL threading).
- Validators/preflight:
  `workers/mapping_builder/rewrite.py::compute_hydrologic_core_fingerprint`,
  `verify_hydrologic_core_fingerprint_equal`, `_validate_category_files`.
- Storage/cache/query: `packages/common/state_manager.py` PG
  `upsert_state_snapshot` INSERT/DO-UPDATE/params;
  `packages/common/state_clone_hook.py::_CursorBoundStateSnapshotRepository.upsert_state_snapshot`
  — the second, independently maintained copy of that same SQL, used inside the
  cutover transaction; `_snapshot_from_row`, `_snapshot_to_dict`,
  `_state_index_entry_from_snapshot`, `_state_snapshot_from_index_entry`, the
  state-index copyback merge;
  `db/migrations/000053_state_snapshot_clone_gate_kind.sql`.
- Public routes/entrypoints: the tool CLI (`build_parser`) only. No HTTP route.
- Frontend/downstream consumers: `services/orchestrator/chain.py::_validate_state_lineage`
  and `chain_forecast_state.py::_validate_strict_forecast_state` — both
  **unchanged**, and both must still accept a recalibration clone row.
- Failure paths/rollback/stale state: every `_refuse` path (no row written);
  the mirror-write failure path; the `stale_latest_snapshot` /
  `missing_qualified_source` paths, unchanged.
- Evidence/audit: the refusal audit record (`refusal_scope`), the clone receipt
  (`schema_version`, pairs, `t*`, both fingerprints, `clone_gate_kind`, both
  index paths), `docs/adr/0005-recalibration-state-carryover.md`.

Regression rows:

1. recalibration + only `cfg.calib`/`CALIB/*`/`cfg.para` differ → clone written,
   `clone_gate_kind="state_compatibility"`, `clone_gate_fingerprint` = the
   8-surface hash, `model_package_version`/`checksum` = M1′'s,
   `cloned_from_model_id` = M1.
2. recalibration + `cfg.ic` differs between M1 and M1′ (per-side bytes) →
   refused, `refusal_scope="state_compatibility_unequal"`, no row written.
3. recalibration + any of `mesh`/`river`/`soil`/`geol`/`land`/`sp.att` non-FORC
   differs → refused with the same scope.
4. recalibration + a `*.lake.*` file present on exactly one side (added **or**
   removed) → refused with the same scope (union enumeration).
5. recalibration + no lake file on either side → `lake` surface uses the
   `sp.mesh` placeholder, clone proceeds, `covered_paths` names it.
6. `fix_forward` (default, no `transfer_mode` argument) with the existing
   fixtures → every existing `tests/test_state_clone*.py` assertion holds
   unchanged, including all six existing refusal scopes.
7. `fix_forward` with an absent/empty `m1_recorded_hydrologic_core_fingerprint`
   → refused `evidence_fingerprint_mismatch` (contract does not weaken).
8. Unchanged sibling consumer: a state-index entry carrying `clone_gate_kind`
   parses through `_state_snapshot_from_index_entry`; an entry **without** it
   parses to `clone_gate_kind=None`.
9. Dual write: the entries written to the canonical and mirror indexes are
   byte-identical, so the copyback merge's `current == source_entry` branch
   treats a replay as a no-conflict carry-through.
10. `--pairs` with an M1′ whose `baseline_model_id` points at the original
    baseline resolves correctly by `model_id`; a pair whose two sides declare
    different `direct_grid_source_id`, or whose source model classifies
    legacy, is refused before any write.
11. Ten-surface default coverage: `HYDROLOGIC_CORE_FINGERPRINT_LABELS` still
    has 10 members and the existing coverage assertion at
    `tests/test_mapping_builder_rewrite.py:1342` still passes.
12. Hook write path: a clone driven through
    `packages/common/state_clone_hook.py` persists
    `clone_gate_kind='hydrologic_core'` on the DB row — asserted through
    `tests/test_state_clone_cutover_hook.py`'s `FakeCursor`, which reads the
    provenance columns positionally out of the params tuple, so a column added
    to the SQL without the matching param (or vice versa) fails the test.
13. Both PG copies stay in lockstep: the `INSERT` column list, the `VALUES`
    placeholder count, the `ON CONFLICT DO UPDATE SET` list and the params
    tuple in `state_manager.py` and in `state_clone_hook.py` each contain
    `clone_gate_kind` exactly once, with matching arity.

## Boundary-Surface Checklist

- Shared helper roots: `workers/mapping_builder/rewrite.py` (fingerprint),
  `packages/common/state_manager.py` (`StateSnapshot` + its two repositories),
  and `packages/common/state_clone_hook.py` (a third, hook-local repository
  adapter over the same table).
  Every signature extension is keyword-only with a behavior-preserving default.
- Public entrypoints: the clone tool CLI. New flags only; every existing flag
  and the existing baseline→variant mode keep their meaning.
- Read surfaces: PG `SELECT *`, file-index `entry.get`. Both lenient — verified.
- Write/overwrite surfaces: `upsert_state_snapshot` on two *file* index
  repositories (canonical + mirror) and on **two distinct PG implementations**
  (`state_manager.py` and `state_clone_hook.py`); each PG copy's
  `ON CONFLICT DO UPDATE` column list must gain `clone_gate_kind` or a re-upsert
  silently drops it, and the hook copy additionally must gain it in the INSERT
  column list/params or it is never written at all.
- Staging/publish/rollback: refusals write nothing; a mirror-write failure is
  reported, not swallowed.
- Producer/consumer evidence boundary: receipt written `O_EXCL`, as today.
- Stale-state/idempotency: the copyback merge's equality and `created_at`
  conflict rules (regression row 9).
- Unchanged downstream consumers: `_validate_state_lineage`,
  `_validate_strict_forecast_state`, `strict_warm_start_evidence` — inspected,
  not modified.
