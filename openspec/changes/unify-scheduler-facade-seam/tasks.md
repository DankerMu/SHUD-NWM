# Tasks

Fixture level: `standard`. Risk packs: production scheduler lease path (lock
acquire/release runs through four of the seams), monkeypatch surface relied
on by existing tests, import layering (a new leaf module).

Scope: #3335 items 1-4. Items 5 (candidate-runtime namespace copy), 6
(chain.py dead imports) and 7 (test renames) are separate PRs.

Must preserve:

- With nothing patched, each call site calls exactly the function it calls on
  master (the lookup returns the fallback).
- Existing facade-patch behaviour: `tests/test_production_scheduler.py`
  `test_candidate_state_decision_scheduler_monkeypatch_active_jobs_compat`,
  `..._raw_manifest_repair_compat` and the test that patches
  `services.orchestrator.scheduler._open_regular_guard_file` (around line
  24575) pass unchanged.
- Every case the current `tests/test_scheduler_state_facade_seam.py` covers
  still holds for the new lookup: facade not loaded, non-callable facade
  attribute, a registered name removed from the facade
  (`monkeypatch.delattr`) returns the fallback, facade patch returned as is, a
  `functools.wraps` spy counts as a patch, a direct re-export counts as
  unpatched, owner patch wins while the facade is unpatched, facade patch
  wins when both are patched.
- `scheduler_lease.py` keeps importing nothing from `services.orchestrator`
  except the new leaf module; the leaf module imports only the standard
  library. `tests/test_import_layering_guard.py` and
  `tests/test_orchestrator_first_import.py` stay green.
- Tests that patch a call-site module today (`scheduler_state_failure` in
  `tests/test_production_scheduler.py`) and the one test that patches a lease
  seam on the facade keep passing unchanged.
- No comment added by this change carries a `path:line` reference (the
  line-reference baseline of the entropy audit counts them).
- `scheduler_lease.__all__` and the facade's explicit imports of the six seam
  names are unchanged; the facade and the aggregate keep re-exporting the
  same function objects.
- No change to lock semantics, guard-mode resolution, or any state decision.

## 1. Code

- [x] 1.1 New `services/orchestrator/scheduler_facade_seam.py`, standard
  library only:
  - `facade_seam_original(function)`: decorator, records `function` under
    `function.__name__` as the seam's unpatched reference and returns it
    unchanged. A later registration of the same name replaces the earlier
    one (module reload).
  - `facade_seam(name, fallback)`: raises `LookupError` naming the seam when
    `name` was never registered; otherwise reads `name` off
    `sys.modules.get("services.orchestrator.scheduler")` (never imports the
    facade) and returns that value when it is callable and is not the
    registered original, else `fallback`.
  - Docstring states the rule in the spec's words (precedence, what is not
    observed).
- [x] 1.2 Decorate the six definitions: `_state_active_jobs`
  (`scheduler_state_rows.py`), `_object_manifest_is_missing`
  (`scheduler_state_common.py`), `_default_owner_liveness_probe`,
  `_unlink_lock_file`, `_open_lock_parent_directory`,
  `_open_regular_guard_file` (`scheduler_lease.py`).
- [x] 1.3 Call sites: `scheduler_state_decision.py`,
  `scheduler_state_failure.py` (one each) and `scheduler_lease.py` (five)
  call `facade_seam(...)` with the same literal name and fallback as today.
- [x] 1.4 Delete `_facade_seam`, `_FACADE_MODULE`, `_FACADE_OWNER_MODULE`
  from `scheduler_state_common.py` and `_scheduler_facade_function` from
  `scheduler_lease.py`, with every import or re-export of them. `sys`
  imports that become unused go too.
- [x] 1.5 If a governance fixture has to know the new module (import
  layering allowlist, structural ownership/budget, module inventories), make
  the minimal registration and report it.

## 2. Tests

- [x] 2.1 `tests/test_scheduler_state_facade_seam.py`: retarget the existing
  cases at `scheduler_facade_seam.facade_seam` and add:
  - (a) through the owner path, so it is a real assertion failure on master:
    patch `_object_manifest_is_missing` on both the call-site module
    `scheduler_state_failure` and the aggregate `scheduler_state`, leave the
    facade untouched, call the function that holds the call site
    (`_artifact_uri_missing_status` or its nearest public caller) and assert
    the call-site patch ran (red on master: the facade original runs);
  - (b) aggregate patched only -> the lookup returns the fallback (the
    call-site module's own binding);
  - (c) unregistered name -> `LookupError`. Pass the name through a variable,
    not a literal (see 2.3);
  - (d) every registered seam name resolves on the facade and the facade's
    binding is the registered original (the six names, listed literally in
    the test as plain strings, not as `facade_seam(...)` arguments);
  - (e) lease, through the owner path: with
    `scheduler_lease._default_owner_liveness_probe` patched and the facade
    untouched, `scheduler_lease._compat_owner_liveness_probe(payload)`
    returns the patch's value (red on master: the facade original runs);
  - (f) lease end to end without touching the facade: with
    `NHMS_SCHEDULER_LEASE_GUARD_MODE` and
    `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` removed from the environment
    (`monkeypatch.delenv(..., raising=False)`), patch
    `scheduler_lease._unlink_lock_file` with a recorder that delegates to
    the real function, acquire and release a `FileSchedulerLease` in a
    temporary directory, and assert the recorder was called by `release()`
    (red on master);
  - (g) patching the defining module when it is not the call-site module
    (`scheduler_state_rows._state_active_jobs`) is not observed: the lookup
    at the call site still returns the call-site module's binding.
- [x] 2.2 `tests/test_production_scheduler.py`
  `test_scheduler_lease_compat_lookup_names_resolve_scheduler_monkeypatches`:
  call the new lookup instead of `_scheduler_facade_function`; assertions
  otherwise unchanged.
- [x] 2.3 `tests/test_facade_name_resolution.py`, owner sources only (as
  `owner-dispatch` is today; calls in test sources are not collected and are
  not violations):
  - a call to `facade_seam` (bare name or attribute access ending in
    `facade_seam`) with a literal first argument is collected as a read of
    the scheduler facade under a new form `seam-literal`;
  - a non-literal first argument is a violation;
  - a second argument that is not a bare name equal to the literal is a
    violation (the fallback must be the seam's own name);
  - add the form to `ALL_FORMS` but NOT to `REQUIRED_FORMS_PER_FACADE` (the
    chain facade has no seam);
  - bite rows: one flag row for an unresolvable literal, one flag row each
    for the two violations, one no-flag row;
  - coverage: assert that the `seam-literal` reads at HEAD are exactly the
    seven `(path, name)` call sites (two state, five lease; six distinct
    names), in the style of the existing `owner-dispatch` assertion;
  - update the module docstring sentence that says `sys.modules[...]`
    lookups are not scanned.

## 3. Evidence Floor

- [x] 3.1 Red run on the pre-change tree for 2.1 (a), (e), (f) - the three
  cases that go through an owner path and therefore fail on an assertion,
  not on an import - with the failing assertion recorded.
- [x] 3.2 Mutations, each killed by the named test and restored:
  - M1 the lookup compares against the aggregate module's attribute instead
    of the registered original -> 2.1 (a) red.
  - M2 the lookup drops the identity check (any callable facade value wins)
    -> 2.1 (e) and (f) red, and the existing "owner patch wins" case red.
  - M3 the lookup returns the fallback for an unregistered name -> 2.1 (c)
    red.
  - M4 remove the facade's explicit import of `_unlink_lock_file` ->
    2.3 guard red and 2.1 (d) red.
  - M5 remove the decorator from `_state_active_jobs` -> 2.1 (d) or the
    existing facade-patch test red (LookupError).
  - M6 a call site passes the wrong fallback for its literal
    (`facade_seam("_unlink_lock_file", _open_regular_guard_file)` at the
    stale-takeover site, which 2.1 (f) does not reach) -> 2.3 guard red
    (fallback-name violation).
- [x] 3.3 Local: `uv run pytest -q tests/test_scheduler_state_facade_seam.py
  tests/test_facade_name_resolution.py tests/test_import_layering_guard.py
  tests/test_orchestrator_first_import.py tests/test_safe_fs_lock.py
  tests/test_scheduler_lease_loop_root.py tests/test_production_scheduler.py`;
  `uv run ruff check --no-cache .`;
  `openspec validate unify-scheduler-facade-seam --strict --no-interactive`.
- [ ] 3.4 node-27: the files of 3.3 plus `tests/test_orchestration_chain.py`,
  zero unexpected skips.

## Post-merge (not a task of this change)

Compute-node evidence after the next deploy: idle pass and submitting pass on
the new code, lease acquired and released normally. Recorded on #3335, which
stays open until then.
