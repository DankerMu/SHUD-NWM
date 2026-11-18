## Design — retire-state-clone-activation-hook (#3189)

Change surface:
- the model-registry lifecycle modules (contracts, registry, lifecycle, lifecycle_support);
- deleting `state_clone_hook.py` and its suite;
- the variant-activation-cutover tests and helper, `test_model_registration.py` and the basin-retirement helper;
- the selector tables and pins, the durations, the large-file guard;
- an ADR note;
- comment rewording (part 2).

Must preserve:
- activation's single transaction, scope lock, preflight, transition, the audit insert and its rollback, and the
  already-current idempotency;
- the post-commit `publish_context` and its tail (both publisher seams untouched);
- `_extract_source_scope`;
- the core clone and CLI.

Must change:
- no hook chain, approval input or refusal-audit path remains;
- the specs stop promising an activation-time clone.

Governing invariant: activation behaviour is byte-for-byte what production already did (the hook was never registered).
Only dead code and the stale spec promises go.

Sibling surfaces:
- the post-commit publishers (filed separately);
- `state_clone_index_publisher.py`;
- the node-22 clone CLI;
- the API lifecycle routes (`apps/api/routes/models.py:563,:622`, which never pass `cold_start_approval`);
- `scripts/basin_retirement/steps.py:210`;
- the AGENTS.md hit, which is about `state_clone.py` and needs no change.

Seats:
- correctness: deletion completeness, the load-bearing block preserved, test rewrites keeping the atomicity assertions;
- test evidence + spec compliance: E2 grep-zero, E3 mutation reds, spec delta faithfulness to code;
- invariant: the activation transaction, the already-current path, the post-commit tail untouched, the selector
  meta-guards.
