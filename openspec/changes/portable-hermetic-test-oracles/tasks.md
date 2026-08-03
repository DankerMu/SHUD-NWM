# Tasks — portable-hermetic-test-oracles (#1274)

Cross-file anchors verified at master b2a39d36; anchors into the two
edited test files are named (test id / function), never numbered.

Risk triage: fixture level **compact** (issue suggests none; the
workflow's fixture mandate overrides, recorded as divergence. S-size,
tests-only, frozen production surfaces — the risks that matter are
oracle discrimination on the tightened/re-expressed assertions and
cross-platform equivalence of the stat-dialect shim). Risk packs
selected: **oracle-discrimination** (tightened assertions must red on
the wrong branch; the shim must not change what the guard judges) and
**cross-platform-equivalence** (macOS and Linux must express the same
intent; the Linux side is proven by CI ubuntu-latest — node-27 is NOT
used, per this run's standing constraint that node-27 stays read-only
and CI is the issue-sanctioned alternative oracle). Not selected:
forensic-verbatim posture (no recorded forensic values), performance /
UI / migration (n/a).

Must-preserve behavior:

- The neighbouring runbook-equality pure-text assertions (the
  `docs/runbooks/two-node-production-e2e-plan.md` /
  `infra/README.two-node-docker.md` / `infra/env/README.md`
  comparisons in tests/test_readonly_db_validation.py) keep comparing
  the CANONICAL GNU snippet text byte-identically — the shim rewrites
  only the executed copy.
- Every currently-green test in both files stays green, on both
  platforms.
- The guards' control flow as executed is byte-identical outside the
  substituted `stat` invocations.
- No production file, doc, or infra file changes.

Seams under test (upstream-declared, consumed not renegotiated): the
guard snippets as published in the runbooks (Linux-only `stat -c`
dialect is their design); `_safe_merge_source_dir`'s check order
(symlink refusal before root approval — consumed as-is, the fixture
adapts to it); `load_cutover_declaration`'s never-raises error-dict
contract with its distinct `declaration_malformed_json` /
`declaration_not_object` codes.

Non-goals: production/doc/infra edits; `select_ci_tests.py` rules
(#1254); the #1272 seven (already merged); #1276's thread flake;
node-27 execution of any kind.

Minimal mergeable slice: all four cases plus the two sibling
tightenings — landing the stat shim without tightening the siblings
would leave two green-for-wrong-reason tests standing, the exact
disease this issue names.

## 1. stat-dialect shim (cases #1, #2 + siblings)

- [ ] 1.1 One probe + one substitution helper, single definition in
  `tests/test_readonly_db_validation.py` (all consumers live there):
  probe `stat -c '%a' <some file>` once (module-scope or
  session-scope fixture); on failure, substitute in the EXECUTED
  snippet copy exactly: `stat -c '%a'` → `stat -f '%Lp'`,
  `stat -c '%U'` → `stat -f '%Su'`, `stat -c '%A'` → `stat -f '%Sp'`.
  No other text changes. SCOPE: substitute only in the executed
  `script` string of each guard test — the runbook doc-equality
  assertion literals contain the identical `stat -c '%a'` substring
  at runtime, so any file-wide or all-strings replacement breaks the
  must-preserve clause by construction. The four executing tests (two
  red cases + two siblings) run the substituted copy; the
  runbook-equality assertions keep the canonical text. Provenance:
  this is the repo's existing production mapping, not a novel one —
  seven scripts already ship `stat -c '%a' … 2>/dev/null || stat -f
  '%Lp' …` fallbacks, pinned by
  tests/test_scheduler_file_provider_refresh.py; cite that convention
  in the helper docstring.
- [ ] 1.2 Equivalence check, recorded in the helper's docstring and
  asserted by a small unit test: the `%a`↔`%Lp` pair is
  guard-equivalent ONLY on the permission bits — BSD `%Lp` DROPS
  setuid/setgid/sticky (measured: a 04600 file gives GNU `%a`=4600
  but `%Lp`=600, which would slip past a fail-closed `!= "600"`
  comparison in the unsafe direction) — so the claim is conditional:
  equivalence holds exactly on the high-bit-free modes these guards
  chmod themselves (0600/0644/0664), and the unit test PINS the
  boundary explicitly (chmod a scratch file 0o4600, assert the BSD
  side yields the string "600"), making the divergence a recorded
  fact rather than a wrong general claim. `%U`↔`%Su` (owner name) and
  `%A`↔`%Sp` (symbolic mode; the guard consumes positional characters
  whose indices agree in both dialects) are equivalent in the guards'
  input domain. The three pairs are spelled in one table the helper
  and the test share; on a platform where only one dialect exists,
  the unit test exercises the available one (the 04600 boundary pin
  runs only where BSD stat exists).
- [ ] 1.3 Sibling tightening:
  `test_readonly_secret_source_guard_blocks_readable_file_before_source_or_validator`
  and
  `test_operator_auth_source_guard_blocks_readable_file_before_source_or_header`
  assert the specific BLOCKED message of the refusal branch each
  names (read the guard snippet to quote the exact message), not just
  `"BLOCKED:" in stderr`.

## 2. outside_root fixture (case #3)

- [ ] 2.1 The `outside_root` row's forged dir becomes
  `Path(tempfile.gettempdir()).resolve() / "nhms-readonly-db-forged"`;
  the row asserts `READONLY_DB_EVIDENCE_ROOT_UNAPPROVED` and now
  genuinely reaches the root-approval gate on macOS too (resolve()
  flattens tempdir symlinks; measured fact: macOS's real tempdir
  resolves under `/private/var/folders/…/T/…`, NOT `/private/tmp`).
  MANDATORY in-row assertion, not an optional escape clause: the row
  asserts the constructed path is outside every entry of
  `APPROVED_EVIDENCE_ROOTS` (imported from
  `services.production_closure.readonly_db_validation`; its second
  entry is `/scratch/frd_muziyao`, which a Slurm-style Linux
  `$TMPDIR` could genuinely resolve under — exactly the
  platform-topology dependence the new requirement forbids), and
  when the tmpdir does resolve inside an approved root the row falls
  back to a constructed provably-outside path instead of failing —
  the row's oracle must never depend on where the host puts its
  tempdir.
- [ ] 2.2 NEW parametrize row `symlink_component`: constructs a
  source dir whose path contains a real symlink component (built
  under `tmp_path`) and asserts
  `READONLY_DB_EVIDENCE_PATH_UNSAFE` — the symlink-refusal gate gets
  its own row; the check-order seam (symlink before root) is thereby
  pinned by tests instead of accidentally relied on. Implementation
  facts (verified at fixture review): `_refuse_symlink_components`
  fires only on `component.exists() and component.is_symlink()` — the
  symlink must point at an EXISTING directory (a dangling link does
  not trigger the gate); the existing mutator dispatch ends in a bare
  `else:` that is the outside_root fallback — it must become an
  `elif` before this row is added, and the row's builder needs
  `tmp_path` threaded into the mutator signature.

## 3. Recursion determinism (case #4)

- [ ] 3.1 Depth 2000 → 20000 in
  `test_load_cutover_declaration_handles_recursion_error_on_deeply_nested_json`;
  assert `declaration_malformed_json`; assert the payload size is
  under `MAX_CUTOVER_DECLARATION_BYTES` (import the constant, no
  magic number); docstring rewritten: the depth is chosen for
  deterministic `RecursionError` on CPython 3.11 AND 3.12 (measured),
  the falsified "3.12 would yield declaration_wrong_schema"
  prediction removed. Keep the existing `sys.setrecursionlimit(1000)`
  wrapper: on 3.11 it is the determinism source; on 3.12 it is
  harmless (the interpreter's C-level guard threshold is independent
  of it — measured ~9998 here, so depth 20000 keeps a 2x margin).
- [ ] 3.2 NEW independent case: top-level JSON list payload (shallow)
  → `declaration_not_object`. Both error codes now have their own
  case; neither depends on interpreter version.

## 4. Spec + validation

- [ ] 4.1 Spec delta: ADDED requirement in
  `real-integration-test-matrix` — hermetic tests SHALL express
  platform-portable oracles (tool-dialect probing for embedded shell,
  no reliance on platform path topology like symlinked tempdirs,
  interpreter-version-deterministic triggers), with the canonical
  published snippet text staying the comparison target for
  doc-equality assertions.
- [ ] 4.2 `openspec validate portable-hermetic-test-oracles --strict
  --no-interactive` green.

## Evidence Floor

- [ ] E1 `uv run pytest -q tests/test_readonly_db_validation.py
  tests/test_scheduler_generation.py` green on macOS (baseline:
  4 failed / 173 passed), including the 4 issue ids, the 2 tightened
  siblings, and the 2 new cases.
- [ ] E2 `uv run ruff check .` green.
- [ ] E3 openspec strict validation green (4.2).
- [ ] E4 **Red proofs (discrimination)**, backup-copy mutation +
  `cmp` restore:
  (i) with the shim's `%Lp` mapping corrupted (e.g. to `%Sp`), the
  mode-consuming guard test reds on macOS — the equivalence table is
  load-bearing, not decorative;
  (ii) with a sibling's tightened expected BLOCKED message corrupted,
  that sibling reds — and with the sibling's fixture set up to take
  the "cannot stat" branch artificially (e.g. stat name corrupted in
  the executed copy), the tightened assertion reds where the old
  loose one passed — proving the tightening closed the
  green-for-wrong-reason hole;
  (iii) with the new `symlink_component` row's symlink replaced by a
  real directory, that row reds (expects PATH_UNSAFE, gets
  ROOT_UNAPPROVED or admission) — the row really pins the symlink
  gate;
  (iv) with depth 20000 reverted to 2000, the recursion case reds on
  3.12 (reproducing the original defect) — restore afterwards.
- [ ] E5 Surface check: `git diff --name-only` = the two test files
  plus this openspec change, nothing else; frozen surfaces zero
  diff. Check against the command's own output.
- [ ] E6 Cross-platform oracle: PR CI `Unit Tests` green on
  ubuntu-latest/3.11 with both files selected (they are changed test
  files, so the selector picks them directly); no node-27 execution.
- [ ] E7 Full-suite spot on macOS: the 4 ids absent from failures;
  expected residual reds: none from this family (#1276's flake may
  or may not appear; it is tracked and out of scope).
