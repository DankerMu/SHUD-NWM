## Design — retire-post-commit-publishers (#3204)

Change surface:
- the model-registry lifecycle modules (contracts, registry, lifecycle, lifecycle_support);
- deleting `state_clone_index_publisher.py` and its suite;
- `state_manager.get_latest_clone_row_for_model_source`;
- the variant-activation-cutover suites and helper, the legacy-reactivation-guard suite, the scheduler-lineage and
  real-DB clone-reader tests;
- the selector tables and pins, the durations, the large-file guard, `pyproject` per-file-ignores;
- two runbooks.

Must preserve:
- the activation transaction semantics and its result dict;
- the audit-failure rollback;
- the supersede-of-active preflight block;
- already-current idempotency;
- the earliest clone-row reader and the lineage resolver.

Must change:
- no post-commit publisher seam, context, predicate or default remains;
- the specs and runbooks stop claiming that a lifecycle transition publishes the node-22 file plane.

Governing invariant: production behaviour is unchanged, because both seams always ran their no-op defaults. Only dead
code and stale promises go.

Sibling surfaces:
- #3189 (already merged; `atomic-cutover-transaction` untouched here);
- the node-22 operator tooling;
- the API lifecycle routes (`apps/api/routes/models.py`, no publisher-derived field);
- `scripts/basin_retirement`.

Seats:
- correctness: deletion completeness, the lifecycle tail returns `result` unchanged, the orphan-reader deletion is safe;
- test evidence + spec compliance: retained assertions moved intact, E2/E3, the delta faithful to the node-22 runbook;
- invariant: transaction semantics, the lineage resolver untouched, the selector meta-guards and partition corpora.
