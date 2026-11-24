# One facade-seam lookup for scheduler state and lease owners

## Why

After the F2 facade cleanup two owner-side lookups read a monkeypatch off the
scheduler facade at call time (#3335 items 1-4):

- `scheduler_state_common._facade_seam` (two call sites) decides "patched" by
  identity against the aggregate module `scheduler_state`. That reference can
  itself be patched: with the aggregate and the owner patched and the facade
  untouched, the lookup returns the facade's original function and both
  patches are ignored. No test covers it.
- `scheduler_lease._scheduler_facade_function` (five call sites) has no
  patched/unpatched decision at all: any callable on the facade wins, so a
  patch on the lease module is ignored whenever the facade is loaded.
- The facade name-resolution guard does not see the literal names passed to
  either lookup, because they reach the facade through `sys.modules`, not
  through a facade binding. Deleting one of the facade's explicit imports
  would silently turn a seam into a no-op.
- Nothing states which names propagate. A patch on any other facade name is
  not seen inside these owners.

Late binding through the facade stays (umbrella design D3, an owner
decision). This change only makes the two lookups one and gives it a
reference that does not move with a patch on the facade or on any
re-exporting module.

## What Changes

- New leaf module `services/orchestrator/scheduler_facade_seam.py` (standard
  library only) with one lookup and one registration decorator. The original
  function of each seam is recorded where it is defined.
- Decision rule: a facade binding that is callable and is not the recorded
  original is a patch and wins; otherwise the binding of the module that
  holds the call site (`scheduler_state_decision`, `scheduler_state_failure`,
  `scheduler_lease`) is used, so a patch on that module is observed. A lookup
  for a name that was never registered raises.
- The two state call sites and the five lease call sites use the one lookup;
  `_facade_seam` and `_scheduler_facade_function` are deleted.
- The facade name-resolution guard collects the literal names owner sources
  pass to the lookup as facade reads, and rejects a call site whose fallback
  is not the seam's own name.
- Spec: one added requirement naming the seam boundary.

## Behaviour change

None in production: without a patch every lookup returns the function it
returned before. Under test: a patch on the lease module is now observed
while the facade is unpatched (it was ignored), and a patch on a call-site
module is observed even when the aggregate module is also patched (both were
ignored). A patch on the aggregate module alone, or on a defining module that
is not the call-site module, is still not observed; the spec now says so.

## Impact

- Code: `scheduler_facade_seam.py` (new), `scheduler_state_common.py`,
  `scheduler_state_rows.py`, `scheduler_state_decision.py`,
  `scheduler_state_failure.py`, `scheduler_lease.py`.
- Tests: `tests/test_scheduler_state_facade_seam.py` (retargeted and
  extended), `tests/test_production_scheduler.py` (the lease lookup test),
  `tests/test_facade_name_resolution.py` (new read form and bite rows).
- Spec: `scheduler-facade-deepening` (added requirement only; the umbrella
  change `redesign-orchestration-core` still carries MODIFIED deltas for the
  three existing requirements, so none of them is touched here).
- Operations: production scheduler code changes, so compute-node evidence
  (idle pass and submitting pass) is taken after the next deploy.
