## 1. Design gate

- [ ] 1.1 Fixture review of design D1–D7 against the six #2374 acceptance bullets; record
      `receipts/fixture-review-1.json`.

## 2. Executor and oracle

- [ ] 2.1 Implement `reprepare`/`reforward`, re-admission, reattach, reforward validation, retained read and the
      recovery narrow-OID pin in `tools/window_execute.py`; initial `prepare`/`window`/`recover` behavior unchanged.
- [ ] 2.2 Extend `tools/window_smoke.py` with `reforward`, `reforward-rename`, `reforward-restart` and
      `reforward-readiness` cases (design D7).

Scope: this change's `tools/` only, plus the parent expand-contract rollback wording and runbook §4.10.3. No migration,
parser, reader, lifecycle or service change.

Minimal mergeable slice: atomic — admission, reattach and recovery pin are one transition; a partial slice would admit
a state that cannot be recovered or recover a state that cannot be admitted.

Evidence Floor:

- Disposable real PG15.2+Timescale: expand → NEW parse → D12 → OLD-window run created with default route and parsed by
  the real OLD parser → fresh re-admission → real `reforward` reaches `WINDOW_VALIDATED` with original OLD/narrow
  OIDs, retained facts byte-equal, OLD facts intact, ledger unchanged, routes as derived.
- Typed refusals with no catalog/ledger/route/service change: extra table, missing rollback, foreign rollback OID,
  owner change, divergent ledger, provenance hash change, retained run changed since D12, foreign `run_key`, source
  drift before T0, and both phase/mode gates.
- SIGKILL after reattach commit, SIGKILL at display start, and readiness failure each recover OLD twice with both OIDs,
  all narrow and legacy facts and the ledger preserved; a later re-admission from the new state succeeds.
- Existing eight-case initial matrix, unit-config and display-ready oracles still PASS on the same tool commit.
- No ledger DELETE, rollback DROP, `000059` rerun or prior-state write exists in the executor (source audit).

Verification: publish exact bytes through GitHub; on node-27 run the guarded disposable launcher with a fresh DB and
state per case for the eight initial cases plus four reforward cases, and the two DB-free oracles; record argv/hashes
without DSNs in `receipts/`.

Hygiene: `uv run ruff check openspec/changes/node27-post-d12-reforward/tools`;
`openspec validate node27-post-d12-reforward --strict --no-interactive`.

## 3. Documentation

- [ ] 3.1 Amend `timeseries-narrow-store-expand-contract` rollback requirement/design D12 and runbook §4.10.3 to name
      the reattach transition; DROP stays required only before re-running `000059` from scratch.
