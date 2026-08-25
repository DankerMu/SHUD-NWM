## Context

Issue #1561 documents a missing selector edge: ordinary changed suites self-select, but their module-scope importer suites do not. The current tree contains many such edges and has already changed since the issue snapshot, so a frozen filename table would immediately become stale. The selector must also keep working with `--repo-root` outside a Git checkout.

Fixture level: expanded. Project profile: NHMS. Repair intensity: medium. Upstream suggested level and minimal mergeable slice: absent.

## Goals / Non-Goals

**Goals:**
- Add one-hop, module-scope suite-importer selection at the ordinary changed-suite branch.
- Derive the edge set from the supplied repository tree and prove the derivation is non-vacuous.
- Exclude file-level `integration`/`e2e` suites from the pull-request lane.
- Preserve suite self-selection, selector meta-guards, redirects, support-module routing, and output shape.

**Non-Goals:**
- Function-local imports, recursive importer closure, support-module routing, or CI job restructuring.
- Restoring deleted importer filenames from the issue's historical snapshot.

## Decisions

1. Build a repository-local reverse index from Python ASTs only when an ordinary changed suite reaches the self-selection branch, limited to test-suite files and `tree.body` imports. The matcher supports `import tests.test_owner`, `from tests.test_owner import helper`, and `from tests import test_owner`; malformed suite source propagates a parse failure whenever that closure is required instead of silently dropping an edge. Discovery is rooted at `repo_root`, not the process CWD or Git. Each build walks and stats the suite tree so additions/deletions remain visible, while immutable per-file derivations are reused across calls only when absolute path, repository-relative path, nanosecond mtime, byte size, and ctime identity all match.
2. Map a changed suite path to its dotted module and add only direct, non-self importers. Do not recurse through importer-of-importer edges.
3. Reuse the repository's file-level gating semantics: suites with file-level `integration` or `e2e` markers are excluded; function-level marks do not hide an otherwise runnable suite.
4. Apply the closure only in the existing ordinary self-selection branch. A shared pure activation predicate defines whether each `CHANGED_TEST_FILE_RULES` entry is active for the actual changed set; production routing and the live-tree guard both use it so conditional owners remain ordinary when their activation surface is absent. Active redirects retain their focused behavior, and the disjoint support-module branch remains untouched.
5. Keep `meta_guard_only` as the strict final-selection shape check. A changed suite remains non-collapsed because the owner itself is selected, regardless of whether importers exist.

## Risk Packs Considered

- Public API / CLI / script entry: selected — `select_tests` and `--repo-root` are shared CI entry seams.
- Config / project setup: not selected — no configuration or workflow topology changes.
- File IO / path safety / overwrite: not selected — read-only traversal is confined to the trusted repository test root; no publish, delete, overwrite, or external trust boundary.
- Schema / columns / units / field names: not selected — no data contract changes.
- Auth / permissions / secrets: not selected — no security boundary.
- Concurrency / shared state / ordering: not selected — selection is synchronous and stateless.
- Resource limits / large input / discovery: selected — scan the test tree once per selection call and avoid recursive closure.
- Legacy compatibility / examples: selected — redirects, support modules, nested suites, and exact GitHub-output semantics must remain compatible.
- Error handling / rollback / partial outputs: selected — malformed source must fail selection rather than silently omit an importer; no partial publication exists.
- Release / packaging / dependency compatibility: not selected — standard-library AST only, no dependency or package change.
- Documentation / migration notes: selected — update the CI contract through OpenSpec; no operator migration.
- NHMS domain packs (geospatial, hydro-met, SHUD numerical, PostGIS/TimescaleDB, Slurm lifecycle, external providers, run provenance, published artifacts): not selected — this changes only CI test selection, not those runtime domains.

## Seams Under Test

- `select_tests(changed_paths, repo_root=...)`: real-tree and synthetic-tree selections, including lazy-build and parse-reuse seams.
- Selector CLI GitHub output: `meta_guard_only` remains false for a changed suite.
- Mechanically derived recursive live-tree invariant: every non-gated module-scope importer of each ordinary owner suite is selected, including future nested and `*_test.py` suites; only redirects active for `changed=[owner]` are excluded.
- Synthetic tree mutation: added and rewritten importer files invalidate discovery/parse state; unchanged files reuse parse work.

## Risks / Trade-offs

- [AST scanning adds selector latency] → build lazily only for ordinary changed suites, parse each changed file identity once across calls, and keep closure one hop; the final full selector suite runs in about three minutes rather than the eager-scan prototype's thirty-three minutes.
- [A new import syntax or marker form is missed] → synthetic tests cover `import tests.test_owner`, `from tests.test_owner import helper`, `from tests import test_owner`, file-level gates, and function-local exclusion.
- [Malformed suite source could silently shrink the reverse index] → propagate the AST parse error and pin that public failure behavior with a synthetic repository.
- [Historical acceptance filename was deleted] → assert the current derived closure and retain stable owner anchors instead of reviving or freezing old names.
- [Redirected suites unexpectedly broaden or conditional owners fall out of evidence] → share rule activation between production and guard, run closure only after no active redirect matched, and pin standalone/activated/unconditional cases.
