# Fix the four macOS-vs-Linux environment assumptions in hermetic test oracles (#1274)

## Why

Four tests fail deterministically on the macOS dev machine while green
on Linux CI (3.11) — all four adjudicated in the issue as test-side
environment assumptions, with production behavior verified correct
per-case (anchors verified at master b2a39d36; issue reproduction at
`5aafaa5f`, CPython 3.12.12, two consecutive identical runs):

1. `tests/test_readonly_db_validation.py`
   `test_readonly_secret_source_guard_preserves_existing_secret_file`
   and 2. `test_systemd_source_path_guard_rejects_group_world_writable_sources`
   embed shell snippets using GNU-coreutils-only `stat -c` (`'%a'` /
   `'%U'` / `'%A'`); BSD stat rejects `-c`, so the guards take their
   "cannot stat" fail-closed branch and the tests red on returncode or
   on the wrong BLOCKED message. The snippets are verbatim copies of
   the node-27 runbook guards (string-compared to
   `docs/runbooks/two-node-production-e2e-plan.md`,
   `infra/README.two-node-docker.md`, `infra/env/README.md` by
   neighbouring pure-text tests) — the production snippets are
   Linux-only by design and correct; the tests wrongly EXECUTE them on
   whatever platform the suite runs on.
3. The `outside_root` parametrize row of
   `test_merge_readonly_db_source_evidence_rejects_untrusted_sources`
   hardcodes `Path("/tmp/nhms-readonly-db-forged")`; macOS's `/tmp` is
   a symlink to `private/tmp`, and
   `services/production_closure/readonly_db_validation.py`'s
   `_safe_merge_source_dir` checks `_refuse_symlink_components` BEFORE
   `_safe_resolved_evidence_root`, so the test gets
   `READONLY_DB_EVIDENCE_PATH_UNSAFE` instead of the
   `READONLY_DB_EVIDENCE_ROOT_UNAPPROVED` it asserts. Both paths are
   refusals — no security hole — but the fixture makes the test assert
   the wrong gate on macOS.
4. `tests/test_scheduler_generation.py`
   `test_load_cutover_declaration_handles_recursion_error_on_deeply_nested_json`
   pins a depth (2000) that triggers `RecursionError` on CPython 3.11
   but parses fine on 3.12, where the payload becomes a list and the
   error code is `declaration_not_object`, not the asserted
   `declaration_malformed_json`. Its docstring's prediction about what
   3.12 would yield is itself wrong, and — the real coverage loss —
   `scheduler_generation.py`'s `RecursionError` except branch has ZERO
   coverage on 3.12 today. Measured: depth 20000 (~40 KB, well under
   `MAX_CUTOVER_DECLARATION_BYTES` = 256 KiB) raises deterministically
   on both 3.11 and 3.12.

Two sibling tests are green-for-the-wrong-reason on macOS and must be
handled in the same change (issue's 受影响面):
`test_readonly_secret_source_guard_blocks_readable_file_before_source_or_validator`
and `test_operator_auth_source_guard_blocks_readable_file_before_source_or_header`
assert only `returncode == 1` + `"BLOCKED:" in stderr`, which the
"cannot stat" branch satisfies — the 0644-refusal path they name never
executes locally.

The noise is the whole cost: every macOS full-suite evidence run
carries these as standing reds (PR #1271 and #1275 both paid the
manual triage), and CI (all ubuntu-latest, with a selector blind spot
for both files) can structurally never expose the assumption.

## What Changes

The issue's recommended route, adopted in full — per-case intent
preservation, never blanket skips:

1. **stat-dialect portability shim (tests only)**: a probe run once
   per test module (or session fixture) detects whether `stat -c` is
   available; when it is not, the executed copy of each guard snippet
   has its `stat -c '%a'` / `stat -c '%U'` / `stat -c '%A'`
   invocations rewritten to the BSD equivalents (`stat -f '%Lp'` /
   `'%Su'` / `'%Sp'`) before execution — a textual substitution of
   exactly those tool invocations, leaving the guard's control flow
   byte-identical. The canonical GNU snippets stay untouched as
   strings: the neighbouring runbook-equality assertions keep
   comparing the canonical text, never the substituted copy. The
   substituted formats are guard-equivalent within the guards' input
   domain — verified at fixture review, with one recorded boundary:
   BSD `%Lp` drops setuid/setgid/sticky bits, immaterial for the
   high-bit-free modes these guards chmod themselves and pinned
   explicitly by a unit test so the boundary stays a recorded fact.
   The mapping follows the repo's existing production convention
   (`stat -c … 2>/dev/null || stat -f …` in seven scripts under
   `scripts/`, pinned by tests/test_scheduler_file_provider_refresh.py).
2. **Sibling assertions tightened**: both green-for-wrong-reason
   siblings assert the specific BLOCKED message of the refusal branch
   they name, so a wrong-branch pass becomes impossible on any
   platform.
3. **`outside_root` fixture de-symlinked**: the forged source dir
   becomes `Path(tempfile.gettempdir()).resolve() / "nhms-readonly-db-forged"`
   (resolve() flattens tempdir symlinks; on macOS the tempdir
   resolves under `/private/var/folders/…/T/…`), so the row genuinely
   exercises `READONLY_DB_EVIDENCE_ROOT_UNAPPROVED` on both
   platforms; a NEW explicit parametrize row plants a symlink
   component on purpose and asserts
   `READONLY_DB_EVIDENCE_PATH_UNSAFE`, giving each refusal gate its
   own row instead of two intents sharing one fixture.
4. **Recursion depth re-pinned for cross-version determinism**: depth
   20000, asserted `declaration_malformed_json`, docstring rewritten
   to state the cross-interpreter determinism rationale (and the
   falsified 3.11-only prediction removed); a NEW independent case
   pins the top-level-list payload to `declaration_not_object`, so
   both error codes are separately locked. The payload stays under
   `MAX_CUTOVER_DECLARATION_BYTES`, asserted in-test.

Explicitly not adopted (per the issue): `skipif(darwin)` /
`skipif(3.12+)` (abandons the named coverage and would silently stop
running if CI ever upgrades); deleting cases (each guards a real
fail-closed contract); any production-code change. The optional
`select_ci_tests.py` PathTestRule additions are LEFT to #1254 — this
change is tests-only.

## Impact

- Affected code (tests only): `tests/test_readonly_db_validation.py`,
  `tests/test_scheduler_generation.py`. The final file set is checked
  against `git diff --name-only` at evidence time (E5), not against
  this sentence.
- Frozen surfaces (zero diff):
  `services/production_closure/readonly_db_validation.py`,
  `services/orchestrator/scheduler_generation.py`,
  `docs/runbooks/**`, `infra/**`, `scripts/select_ci_tests.py`.
- Affected specs: `real-integration-test-matrix` (1 ADDED requirement:
  hermetic test oracles express their intent platform-portably).
