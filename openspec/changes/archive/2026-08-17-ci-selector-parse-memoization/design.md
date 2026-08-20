# Design: ci-selector-parse-memoization

Fixture level: compact (S-M; single private helper in one test module;
risk is cache-correctness subtlety, not blast radius — production
selector does no AST parsing and `_parse_tracked` has no sibling copy).

## Risk triage

- Primary risk: a cache that changes guard semantics silently — wrong
  key (cwd aliasing), stale entries (rewrite unobserved), or shared
  mutable ASTs. All three get explicit treatment below; the first two
  get guard tests + red evidence, the third a mechanical audit.
- Secondary risk: measurement theater — claiming a speedup without
  isolating parse counts. Evidence floor requires instrumented counts,
  not just wall-clock.

## Decisions

1. **Cache shape**: module-level
   `_PARSE_CACHE: dict[tuple[str, int, int], ast.Module]`, key
   `(str(Path(path).resolve()), st.st_mtime_ns, st.st_size)` from one
   `os.stat` per call. `_parse_tracked(path)` signature unchanged; all
   call sites untouched. No `functools.lru_cache` on the raw argument
   (relative-path key is exactly the unsafe design the issue rules
   out). Unbounded dict is fine: the population is bounded by distinct
   files parsed in one pytest process — fixture review measured 510
   resolved identities (188 top-level suites + 313 non-test modules
   reached via `_one_hop_importer_modules` + support modules + tmp
   fixtures; Note-1) — and the process is short-lived. AMENDED after
   implementation measurement (implementer deviation 1, ruled
   in-scope): retaining ~512 ASTs to interpreter exit adds an ~8.4 s
   CPython finalization tail (end-to-end ~25 s vs pytest-reported
   ~12 s; baseline gap was ~0.8 s). SECOND AMENDMENT (after measuring
   that an in-module session-scoped autouse teardown fixes the run
   lane but can never fire under `--collect-only`, leaving the
   collect-only lanes regressed — suite-only ~3 s→~9 s, full-tree
   ~5.9 s→~11.3-12 s end-to-end — and that an atexit hook recovers
   only ~2 s of the ~4.5 s pre-atexit finalization): the ONE
   mechanism that covers both lanes is a guarded
   `pytest_unconfigure` hook in `tests/conftest.py` —
   `sys.modules.get("tests.test_select_ci_tests")`, and only if
   present clear `_PARSE_CACHE` + `gc.collect()` (measured 0.11 s at
   188 entries / 0.23 s full-tree; restores collect-only to
   baseline). The in-module autouse fixture is NOT kept (single
   mechanism, no redundancy). Sanctioned diff surface grows by this
   conftest hook; recorded consequences: `tests/conftest.py` is a
   #1487 selector carve-out (routing unchanged — meta-guard
   fallback) AND sits in ci.yml's `database` paths-filter, so this
   PR triggers one `real-db-integration` run when marked ready.
2. **cwd safety** (issue acceptance bullet 4): resolve() makes
   tmp_path spellings distinct keys by construction. Guard test:
   parse a real tracked file via its repo-relative spelling from repo
   root (priming the cache), `monkeypatch.chdir(tmp_path)`, create the
   SAME relative spelling under tmp_path with different content, parse
   again → must reflect the tmp_path content. This is the alias the
   two existing chdir tests (:637, :917) would hit only if a future
   fixture reuses a real tracked name — the guard makes the hazard a
   red today instead of a latent false-green. Third non-tracked entry
   point into the parse layer (fixture-review Note-3, complete
   enumeration): the literal-index seam at :2915 passes a tmp_path
   ABSOLUTE path via `_literal_path_consumer_index(suites=[...])` —
   cwd-independent, safe under any key; listed for completeness.
3. **Staleness**: stat identity (mtime_ns + size) invalidates
   rewrites. Guard test: write tmp file, parse, rewrite with
   different content AND explicitly bump `os.utime` (determinism — do
   not rely on filesystem timestamp granularity), parse → new content.
   Recorded boundary (code comment + spec parenthetical): a rewrite
   preserving resolved path, mtime_ns AND size aliases; unreachable
   under the suite's probe discipline (tracked files are never mutated
   mid-run) and nanosecond mtimes.
4. **Shared AST objects**: cache hits return the SAME `ast.Module`
   instance. Safe only if no consumer mutates parsed trees. Implementer
   MUST audit (mechanically: no attribute assignment / transform on
   nodes from `_parse_tracked` anywhere in the suite) and record the
   audit result in the PR body. If any mutation exists, stop and
   report — do not silently switch to deepcopy (that would eat the
   win and needs a recorded decision).
5. **`filename=` is parse-time-only** (fixture-review P2-1 corrected
   an earlier false claim here): `ast.Module` carries no filename
   attribute — the `filename=` argument affects only SyntaxError
   messages raised DURING parse and leaves no trace on the returned
   tree (consumers like `_module_names_from_nodes` take the path as a
   separate parameter, never off the tree). Cache reuse therefore
   cannot alter any derivation through filename spelling; no code
   needed.
6. **New tests are additive**: issue acceptance bullet 1 ("用例数与
   用例名集合不变") is read as "the existing 121 keep passing, none
   skipped, none renamed" — bullet 4 itself demands a NEW cwd guard
   test, so the honest final count is 121 + 2 = 123. This
   interpretation is recorded here and in the PR body (deviation from
   the issue's literal first checkbox, internally contradictory
   otherwise).

## Must preserve

- All 121 existing tests pass unmodified (no edits to any existing
  test, no renames, no new skips).
- Guard predicate functions and derivation helpers: zero diff outside
  `_parse_tracked` + the new cache + the two new tests + the
  session-end cache clear (decision 1 amendment) + a small shared
  `_assigned_names` helper used only by the two new guard tests
  (implementer deviation 3, sanctioned).
- `scripts/select_ci_tests.py` and whole-tree selection output:
  untouched (not in the diff at all).
- Suite must not require repo-root cwd any more than it already does
  (resolve() at parse time keeps relative spellings working exactly
  as before from any cwd).

## Seams under test

Real filesystem via tmp_path + monkeypatch.chdir (the cache is
exercised through its public behavior, no injectable seam added). Red
evidence via out-of-tree scratchpad copies/variants of the cache
(relative-path-keyed variant → cwd guard test reds; stat-free-keyed
variant → staleness test reds). Zero tracked mutation, no git stash.

## Test plan (maps to acceptance)

1. Suite green: existing 121 + 2 new guard tests = 123 passed.
2. Wall-clock before/after recorded (target: back under ~40 s; honest
   number either way).
3. Instrumented `ast.parse` count: ~10^4 → order 10^2 (out-of-tree
   pytest plugin, both counts recorded).
4. cwd guard + staleness guard red evidence against broken cache
   variants (out-of-tree).
5. ruff clean on tracked files; openspec validate strict.

## Risks to watch

- Do NOT let the cache leak into `scripts/select_ci_tests.py` — the
  production selector parses nothing; keep the diff inside the test
  module.
- collect-only lane: module-level `_zero_consumer_collapse_params()`
  derivation benefits from the cache automatically (same helper);
  measure collect-only before/after too, but do not restructure the
  parametrize (out of scope). Expect a MODEST collect-only gain
  (fixture review simulated ~2.6 s → ~2.0 s; only 375 parses / 188
  misses happen at collection) — do not promise a large one.
- `_tracked_python_files` (:432) shells out to `git ls-files` on
  every call — measured 138 subprocess calls / 1.47 s per run
  (fixture-review Note-4): noise relative to the parse cost,
  deliberately OUT of scope; recorded here so the next hardening
  pass does not re-investigate it from scratch.
