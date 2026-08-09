# Tasks — tmpdir-normalization-meta-guard (#1211)

Anchors verified at master 97dfc681 (post-#1210 8/8 state, AST scan
re-run this session): the 8 `run_docker_smoke(` call sites live in
functions at `tests/test_two_node_docker_runtime.py:4272/:4320/:4357
/:4377/:4423/:4458/:4486/:4508` (call expressions at
`:4294/:4342/:4363/:4383/:4431/:4467/:4492/:4514`), ALL 8 with
in-function `monkeypatch.setenv("TMPDIR", ...)`; the delenv
counter-example tests (deliberately UNSET `TMPDIR`, must never be
covered by the invariant) at `:3783/:3857/:3885` inside
`test_preflight_defaults_tmpdir_to_repo_artifacts` (`:3779`) and two
siblings; spec invariant wording at
`openspec/specs/two-node-docker-runtime/spec.md:109` under
requirement "Docker-runtime self-tests MUST yield deterministic
verdicts across platforms ..."; production fallback (frozen) at
`scripts/validate_two_node_docker_runtime.py:5286-5289`.

Risk triage: fixture level **compact** (S-size; one ~15-line pure
test addition, zero production code, zero edits to existing tests).
Risk pack selected: **oracle-discrimination** (the meta-guard must
red when any one setenv is removed — hermetic and Class C alike —
and must red rather than green when its own AST matching collects
nothing; it must not depend on host state). Not selected:
concurrency-lifecycle, record-forensic, performance/UI/migration
(n/a — static analysis of one file). Node-22/node-27 untouched.

Must-preserve behavior:

- The 8 `run_docker_smoke` tests keep their assertion sets, verdict
  semantics, skipif guards, and skip/pass distribution byte-for-byte
  (zero edits to any existing test).
- The delenv counter-example tests (`:3779` family) stay untouched
  and OUTSIDE the guard's coverage — the guard keys on
  `run_docker_smoke` call sites only, and none of those three call
  it.
- `scripts/validate_two_node_docker_runtime.py` and
  `.github/workflows/ci.yml`: zero diff.
- `uv run pytest -q tests/test_two_node_docker_runtime.py` on macOS:
  no new failures; the only delta is +1 passed (the guard).

Seams under test (upstream-declared, consumed not renegotiated): the
spec.md:109 invariant wording (consumed as the guard's contract —
this change adds enforcement, it does not reword the invariant); the
in-file convention that normalization is expressed as a direct
`monkeypatch.setenv("TMPDIR", ...)` call in the test body (the
guard's detection grammar matches exactly this convention; adopting
a different normalization idiom later is a spec-level change that
should red the guard and force a deliberate update).

Non-goals: the `normalized_tmpdir` shared-fixture hygiene refactor
(issue's alternative — deferred by design); any autouse
normalization (forbidden by the `:3779` delenv contract);
`tests/test_two_node_docker_source_trust.py` (#1127/#1209); making
Class C cases hermetic; CI workflow env changes.

Minimal mergeable slice: the single meta-test IS the slice.

## 1. The meta-guard

- [x] 1.1 Add one test (suggested name
  `test_every_run_docker_smoke_call_site_normalizes_tmpdir`, placed
  adjacent to the `run_docker_smoke` test block) to
  `tests/test_two_node_docker_runtime.py`:
  - `ast.parse(Path(__file__).read_text(encoding="utf-8"))` — the
    file already imports `from pathlib import Path` (`:10`), there
    is NO `pathlib` module name bound; add `import ast` to the
    stdlib import block in alphabetical position (before
    `import json`) — ruff `I` is enabled, an out-of-order import
    reds E4. The explicit `encoding="utf-8"` is mandatory: the file
    contains non-ASCII bytes (`:4111`) and a locale-default read
    would break the host-independence claim (repo idiom: 50×
    `read_text(encoding="utf-8")` in this very file).
  - Walk `(ast.FunctionDef, ast.AsyncFunctionDef)` nodes
    (`asyncio_mode = "auto"` means a future `async def test_...`
    would be pytest-collected but invisible to a FunctionDef-only
    guard — the exact silent-green class this change closes); a
    function is a CALL SITE when any `ast.Call` in its body has a
    callee named `run_docker_smoke` (both `ast.Name` id and
    `ast.Attribute` attr forms). The guard references the name only
    as a string literal so it cannot match itself. Nested-def
    attribution is a known non-goal: `ast.walk` re-collects nested
    `def`s as their own functions; the file's three nested closures
    (`:3779`/`:3803`/`:5218` runners) call only preflight/probe
    helpers, never `run_docker_smoke`, so no false red today, and
    the J1-style up-attribution idiom
    (`tests/test_timescale_write_guard_wire_site_invariant.py`) is
    deliberately not imported here (KISS).
  - A call site is NORMALIZED when its body contains an `ast.Call`
    whose callee is an `ast.Attribute` with attr `setenv`, whose
    first positional argument is the string constant `"TMPDIR"`,
    AND whose second argument's `ast.unparse(...)` contains the
    BARE substrings `artifacts` and `tmp` (no quotes in the
    needles — `ast.unparse` renders string literals with SINGLE
    quotes, so a `"artifacts"` needle with double quotes matches
    zero of the 8 sites and reds the guard at first run; the
    discriminating needle is `artifacts` — any `tmp_path`-derived
    expression already contains `tmp`). The
    target-shape conjunct is load-bearing: the file already
    contains the counter-idiom `monkeypatch.setenv("TMPDIR",
    "/tmp")` at `:3809` (deliberately unapproved, in a preflight
    test) — a smoke test copying that line must RED the guard, not
    green it. All 8 current sites are byte-identical
    `str(tmp_path / "artifacts" / "tmp")`
    (`:4276/:4324/:4361/:4381/:4427/:4464/:4490/:4512`), so the
    conjunct greens today.
  - Assert the offending set (call sites minus normalized) is empty,
    with a failure message that lists the offending function names
    verbatim.
  - Assert the collected call-site set is non-empty (self-check: an
    AST-matching bug that collects zero sites must red, never
    green).
  - NO skipif, no filesystem/host dependencies beyond reading this
    file's own source; identical verdict on macOS/CI/node-22.
- [x] 1.2 Keep the guard's count assertion at "non-empty", NOT a
  pinned `== 8` (the issue's acceptance wording; legitimate removal
  or renaming of a smoke test must not red the guard).

## 2. Spec + validation

- [x] 2.1 Spec delta: ADDED requirement in `two-node-docker-runtime`
  — the TMPDIR-normalization invariant SHALL be machine-enforced by
  an in-file static meta-guard, 3 scenarios (missing setenv reds
  naming the function; host-independent verdict; empty-collection
  self-check reds).
- [x] 2.2 `openspec validate tmpdir-normalization-meta-guard
  --strict --no-interactive` green.

## Evidence Floor

- [x] E1 Discrimination red proofs (acceptance criterion 2;
  backup-copy + `cmp` restore, one hermetic + one Class C site):
  (a) temporarily delete the `monkeypatch.setenv("TMPDIR", ...)`
  line from ONE hermetic site (e.g. the `:4272` function) →
  `uv run pytest -q tests/test_two_node_docker_runtime.py -k
  <guard-name>` reds and the failure output CONTAINS that function's
  name; restore, `cmp` clean;
  (b) same for ONE Class C site (the `:4423` function — the
  historically-missed one; it is the ONLY smoke test that skips on
  macOS, skipif at `:4419`) → guard reds naming it; restore, `cmp`
  clean. Note the guard reds on macOS where the Class C test itself
  is SKIPPED — that asymmetry is the point (static guard vs runtime
  skip) and both outputs are pasted;
  (c) target-shape arm: change ONE site's setenv second argument to
  the in-file counter-idiom `"/tmp"` (shape of `:3809`) → guard
  reds naming the function (presence alone must not green);
  restore, `cmp` clean.
- [x] E2 Self-check red proof: temporarily break the collector (e.g.
  match name `run_docker_smoke_X`) → the guard reds via its
  non-empty assertion, NOT greens; restore, `cmp` clean.
- [x] E3 Suite parity on macOS: `uv run pytest -q
  tests/test_two_node_docker_runtime.py` before (at master) and
  after — measured baseline at 97dfc681 on this machine: `424
  passed, 2 skipped`; expected after: `425 passed, 2 skipped`; both
  tails pasted.
- [x] E4 `uv run ruff check .` green; `openspec validate
  tmpdir-normalization-meta-guard --strict --no-interactive` green.
- [x] E5 Surface check: `git diff master...HEAD --name-only` =
  `tests/test_two_node_docker_runtime.py` + this openspec change,
  nothing else; frozen surfaces
  (`scripts/validate_two_node_docker_runtime.py`,
  `.github/workflows/ci.yml`) zero diff via the branch-scoped form.
- [x] E6 CI `Unit Tests` green on the PR head (the target file is
  selected directly by `select_ci_tests` when changed; Linux
  oracle confirms host-independence).
