## Context

Issue #1646 owns two orthogonal decisions. Current repository config has no `filterwarnings`, `addopts`, or `pytest-timeout` dependency. An exact subprocess probe under `-c pyproject.toml` exits 0 with `1 passed, 1 warning` when a worker raises; adding `-W error::pytest.PytestUnhandledThreadExceptionWarning` exits 1 and retains the unique cause. The current direct-thread census is 21 test files; the master full lane excludes `e2e`, `grib`, and `integration`, while two direct-thread suites contain 23 integration tests.

The timeout premise has drifted. #1632 concerns marker-lane umask coverage, not durations. #1671 proves a 13k-test job is slow-but-finite at 45 minutes; it does not provide per-test setup/body/teardown distributions. `pytest-timeout` is per-test: its normal signal method interrupts with `pytest.fail()` but does not force process exit, while its portable thread method hard-exits and can lose fixture teardown/JUnit output. Timers are canceled when a test finishes, so a passing test that leaves a non-daemon thread to strand interpreter shutdown is not a complete fit for either per-test method. A universal value/method would therefore claim coverage the evidence does not establish.

Fixture level: **expanded**. Repair intensity: **high** because shared pytest config changes every local/CI/marker entrypoint and timeout method selection changes cleanup/report semantics. Upstream suggested level and minimal mergeable slice are absent.

## Goals / Non-Goals

**Goals:**

- Make every pytest-observed unhandled worker exception fail the owning test/session with its original cause visible.
- Escalate the exact category only; unrelated warnings retain existing behavior.
- Prove the shipping config semantically and make every config/dependency change run that proof in targeted CI.
- Verify current default-lane and node-27 integration suites contain no warning-only thread exception.
- Record an explicit no-global-`pytest-timeout` decision while preserving local protocol bounds and CI job-level backstops.

**Non-Goals:**

- Do not replace issue-owned worker capture, joins, barriers, deadlines, subprocess bounds, daemon choices, or cleanup assertions with global policy.
- Do not add `pytest-timeout`, set `timeout`/`timeout_method`, change CI `timeout-minutes`, solve #1671 full-job duration, or execute #1632's umask marker campaign.
- Do not escalate all warnings, hide expected thread warnings with local filters, or weaken tests to make the warning census pass.
- Do not claim a per-test plugin catches a hang that begins after its timer is canceled or safely preserves teardown/reporting under hard exit.

## Decisions

### D1 — Escalate the exact built-in pytest category

Add `error::pytest.PytestUnhandledThreadExceptionWarning` under `[tool.pytest.ini_options].filterwarnings`. This uses pytest's built-in thread-exception collector and adds no dependency. Broad `error`, generic `RuntimeWarning`, custom `threading.excepthook`, or a conftest hook are rejected: they either change unrelated warning policy or duplicate pytest internals.

### D2 — Test shipping configuration as behavior

`tests/test_pytest_thread_exception_policy.py` invokes `sys.executable -m pytest` on throwaway tests with explicit repository `-c pyproject.toml` and a controlled environment. It proves: shipping config fails with the unique worker cause; a source-derived config with only the exact filter removed passes with `PytestUnhandledThreadExceptionWarning`; an unrelated `UserWarning` still passes. String presence alone is not evidence.

The test also parses configuration/dependencies to pin the exact filter, absence of broad warning escalation, absence of `pytest-timeout` and global timeout keys, and current policy ownership. Temporary configs/tests live below `tmp_path`; no external `/tmp/pyproject.toml` can be discovered.

An explicit per-test warning marker or an executed `warnings.filterwarnings` call can intentionally override any ini warning rule. That is ordinary Python/pytest behavior, not a security boundary this issue can make immutable. A static analyzer for arbitrary aliases, control flow, shadowing, dynamic category values, and regular-expression overlap would be an incomplete Python interpreter and would create a false guarantee larger than the one-line policy it protects. This change therefore performs a final-head tracked-source audit for existing explicit overrides, but adds no permanent source-language guard. Future local warning overrides remain reviewable test-policy changes; the repository default and its semantic subprocess proof stay authoritative when no test explicitly opts out.

### D3 — Make config/dependency ownership executable in PR CI

Introduce a dedicated selector tuple containing the policy suite and selector meta-guard. Both `pyproject.toml` and `uv.lock` rules append it to their existing core-smoke ownership, and selector tests prove removing either policy/meta-guard route reds. This future-proofs both pytest collection/config changes and a later dependency decision: a lock-only `pytest-timeout` addition cannot bypass the policy test.

### D4 — Keep local capture and bounds mandatory

The global warning filter is a last-resort pytest boundary. It does not guarantee helper semantics when direct-called, cause-before-result ordering, cleanup, peer release, or termination of a blocked/non-daemon worker. Existing #1633/#1645/#1648 harness contracts remain stronger and unchanged. The one stale #1633 sentence is updated to describe the new defense-in-depth relationship.

### D5 — Reject a universal per-test timeout on current evidence

No dependency or timeout config is added. #1671's whole-job data cannot calibrate a per-test value. The 23 directly threaded integration tests require node-27 warning-strict validation, but their existence supplies no safe timeout distribution; direct-thread `e2e`/`grib` collection is empty today.

The issue's marker-only middle path (`@pytest.mark.timeout(N)` on selected concurrency tests) is also rejected in this change. It still adds the same plugin dependency and requires one defensible bound/method per annotated test; the current issue supplies neither. Its known target shapes already have more precise local Barrier/spin/poll/join/subprocess bounds from #1633/#1645/#1648, while hand-annotating an incomplete subset would create a misleading coverage claim. A future marker-only proposal is legitimate only when it identifies uncovered concrete tests, calibrates their setup/body/teardown bounds, and proves the chosen method/lifecycle contract rather than duplicating existing local bounds.

Any future global or marker-only timeout proposal must separately prove per-test bounds for affected marker lanes, select a method with explicit process-termination versus teardown/report trade-offs, and cover post-test non-daemon/child-process limits. Until then, local bounded harnesses and existing 35/45-minute CI job bounds are the honest backstops.

### D6 — Treat current warning census failures as real defects

Run the master marker expression under exact warning-as-error before implementation. Any unhandled-thread failure is diagnosed and repaired at its owning harness without filtering/xfail. The pre-change census reached a natural terminal result after 43:40 with 13,382 passed, 12 skipped, 182 deselected, zero thread-warning failures, and one unrelated `UserWarning`. Its only test failure — four `production-topology-node22-local-postgres` entropy findings — reproduces unchanged in a detached clean `origin/master` tree and is already tracked by #1662/#1707; this change SHALL report that baseline rather than fix, suppress, or claim it green.

After implementation, run the same full lane, all directly threaded files, and node-27 `-m integration` warning-strict. Success for the policy census means zero unhandled-thread failures and no new non-policy failure versus the same-clock baseline configuration. The post-change full lane met that policy criterion: it had zero thread-warning failures, while its entropy failure remains linked to #1662/#1707 and twelve state-clone failures reproduce unchanged when the same tree runs under the original pytest config. Those twelve are a date-driven baseline defect — a fixed index timestamp crossed its 168-hour freshness window during this issue — and are owned by #1743 rather than suppressed or fixed here. E2E/GRIB remote execution is not required because current collection finds no directly threaded tests under those markers; the static collection result is recorded, not generalized forever.

## Risk Packs Considered

- Public API / CLI / script entry: not selected — no shipped runtime entrypoint.
- Config / project setup: selected — shared pytest config and dev dependency/lock ownership.
- File IO / path safety / overwrite: not selected — only pytest `tmp_path` policy fixtures; no trusted path boundary.
- Schema / columns / units / field names: not selected — no data schema.
- Auth / permissions / secrets: not selected — no auth surface.
- Concurrency / shared state / ordering: selected — worker exception collection and local harness ordering.
- Resource limits / large input / discovery: selected narrowly — timeout/backstop decision and full-suite/marker census; no large input.
- Legacy compatibility / examples: selected — existing harness semantics, marker expressions and unrelated warnings remain.
- Error handling / rollback / partial outputs: selected — warning-to-failure conversion, cause visibility, teardown/report trade-off.
- Release / packaging / dependency compatibility: selected — explicit no-new-dependency decision and both pyproject/lock selector ownership.
- Documentation / migration notes: selected narrowly — stale contract/docstring wording must match new policy.
- All NHMS domain packs: not selected — no geospatial, hydro-met, numerical, PostGIS/Timescale semantics, Slurm lifecycle, provider snapshot, manifest/QC, or published artifact behavior. Node-27 integration is a test-policy compatibility oracle, not a domain change.

## Invariant Matrix

- Governing invariant: every unhandled worker exception collected by repository pytest SHALL fail with its cause visible, without broadening unrelated warning policy or pretending a per-test timeout supplies unproven process/lifecycle coverage; local harness contracts remain authoritative.
- Source of truth: `pyproject.toml` exact filter, absence of timeout dependency/config, policy subprocess suite, selector routes, and existing harness specs.
- Producers: pytest's built-in `threading.excepthook`/thread-exception plugin from test-owned or library worker threads.
- Validators/preflight: policy subprocess tests, removed-filter mutant, unrelated-warning control, TOML/lock assertions, selector tests, and a final-head audit for explicit tracked warning-filter overrides.
- Storage/cache/query: none — config/test policy only.
- Public routes/entrypoints: `uv run pytest`, CI `pip install -e ".[dev]"`, targeted/full/real-DB pytest lanes.
- Frontend/downstream consumers: developers, PR targeted CI, master full lane, node-27 integration lane; no frontend runtime.
- Failure/cleanup/stale state: call/teardown thread exception, expected locally captured exception, blocked/non-daemon worker, plugin hard-exit cleanup/report loss, stale selector ownership.
- Evidence/audit/readiness: pre-fix probe, full warning-strict census, directly threaded files, node-27 integration, policy/selector tests, Ruff/strict OpenSpec, exact-SHA CI/Governance.
- Regression rows:
  - shipping config + uncaught worker RuntimeError -> nonzero pytest result naming unique cause and warning category;
  - same config with only exact filter removed -> zero result with `1 passed` plus thread warning (semantic mutant RED for the policy test);
  - shipping config + unrelated UserWarning -> pass with warning, proving category precision;
  - tracked test or conftest with an explicit local warning override -> final-head audit reports it for removal or deliberate policy review;
  - worker exception captured/re-raised by a local harness -> existing cause-first/cleanup oracle remains unchanged;
  - blocked or post-test live worker -> no new global claim; local harness/process/job bound remains required;
  - `pyproject.toml` or `uv.lock` changes -> targeted selector includes policy suite and selector meta-guard/core ownership;
  - default lane and node-27 integration under exact warning escalation -> zero unhandled-thread warning failures.

## Boundary Surface Checklist

- Shared helper roots: pytest built-in threadexception plugin only; no custom hook.
- Public entrypoints: repository pytest config across local/CI lanes.
- Producer/consumer evidence: worker exception -> pytest warning category -> exact error filter -> nonzero test outcome/log cause.
- Resource/lifecycle boundary: per-test signal vs hard-exit semantics explicitly rejected as universal policy; existing local/job bounds unchanged.
- Stale-state boundary: pyproject and lock routing cannot drift from the policy suite.
- Unchanged consumers: unrelated warnings, expected harness-captured failures, marker expressions, production and deployment.

## Risks / Trade-offs

- [Stored warning debt breaks the suite] -> run pre-change full warning-strict census; repair real owners, never suppress.
- [Overbroad warning policy breaks unrelated tests] -> exact category plus UserWarning control.
- [Policy test passes on string presence] -> execute shipping config and a removed-filter semantic mutant.
- [A local marker overrides the repository default] -> audit final-head tracked tests/conftest for explicit overrides; do not claim an incomplete Python source analyzer makes ini policy immutable.
- [Config PR skips its owner] -> route both pyproject and lock to the dedicated suite, with selector mutation tests.
- [Global timeout creates false coverage or loses cleanup] -> no dependency/config until method/lane evidence exists.
- [External config contaminates subprocess] -> explicit `-c`, controlled env and tmp_path-owned files.

## Migration Plan

1. Capture current false-green and run warning-strict census.
2. Add policy/selector contract tests RED against current source.
3. Add exact filter and selector ownership; update stale wording.
4. Run local full/default/direct-thread evidence and node-27 integration; verify exact-SHA PR CI/Governance.
5. Roll back by reverting filter, selector ownership, tests and spec together; do not leave a config rule without its semantic proof.

## Open Questions

None.
