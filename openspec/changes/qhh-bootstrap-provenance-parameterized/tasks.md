# Tasks: qhh-bootstrap-provenance-parameterized

## 1. Implementation

- [x] 1.1 `workers/model_registry/qhh_production_bootstrap.py` :1441 and
      :1452: `"qhh.tsd.forc"` → `f"{project_name}.tsd.forc"`
      (`project_name` is already a keyword-only param at :1427 — the
      change is exactly the two literals, no threading).
- [x] 1.2 Record in the PR body the fixture-review-completed
      never-break-userspace result (zero production consumers of the
      literal; API/frontend passthrough; no SQL predicate).

## 2. Tests (tests/test_qhh_production_bootstrap.py, additive only)

- [x] 2.1 ONE parametrized test over ("qhh", "qhh.tsd.forc") and
      ("heihe", "heihe.tsd.forc") per the design test plan: four-field
      consistency incl. nested `elevation_metadata["source"]` on its
      own assertion line; heihe leg's qhh-absence check scoped to the
      four field values (NOT a blanket dict check — harness ids contain
      qhh). The qhh leg is the FIRST real default-output pin (:1557 is
      an input fixture in an integration-gated test, not an oracle).
- [x] 2.2 The new test EXECUTES locally (not skipped) — no integration
      marker.

## 3. Spec delta

- [x] 3.1 ADDED requirement in
      `specs/fixed-station-forcing-production/spec.md` (seed-lane
      provenance follows parameterized project identity; #1359 handoff
      requirement untouched).

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_qhh_production_bootstrap.py` —
      non-skipped tests pass AND the new test's both legs executed
      (report passed/skipped counts; integration tests skip locally by
      design, a skip does not satisfy this item for the new test).
- [x] 4.2 Red evidence: heihe leg fails against unmodified source
      (source stays "qhh.tsd.forc"); qhh leg naturally green (pins
      existing behavior) — labeled honestly.
- [x] 4.3 `uv run ruff check .` passes (per issue Verification field).
- [x] 4.4 `openspec validate qhh-bootstrap-provenance-parameterized
      --strict --no-interactive` passes.
- [x] 4.5 Zero changes to `workers/forcing_producer/file_store.py`
      (diff inspection — #1359 lane).
- [x] 4.6 Zero modifications to existing test assertions.
- [ ] 4.7 Backfill follow-up issue filed (node-27 label): size the
      already-persisted mislabeled non-default-project rows
      (`met.met_station` GROUP BY over seed-lane rows) + remediation
      decision; referenced in the PR body.
