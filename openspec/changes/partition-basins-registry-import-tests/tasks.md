## 1. Baseline Contract and Fixture

- [x] 1.1 Freeze source commit `3c29698f…`, the 3,931-line / 156,249-byte source blob,
  94 test definitions, 19 support functions, one class, four constants, and their exact
  source/AST/marker/monkeypatch fingerprints in an ignored deterministic contract.
- [x] 1.2 Capture 96 unique suffixes, 17 integration suffixes (auth 5 / DB 5 / QHH 7),
  baseline default `78 passed, 18 skipped`, non-integration
  `78 passed, 1 skipped, 17 deselected`, integration-only
  `17 skipped, 79 deselected`, and BUG-008 `2 passed, 94 deselected` from an isolated
  Git-archive snapshot with database/real-Basins opt-ins scrubbed.
- [x] 1.3 Rebuild the contract twice byte-identically; require contract SHA-256
  `42803dd59276621d559bf6719b4c31cccc64ad751ed0f46105c373ba7b17c60c` and preserve
  issue-start guard provenance separately from current guard shape.
- [x] 1.4 Complete one read-only expanded/high fixture review, repair at most twice if
  required, and pass `openspec validate partition-basins-registry-import-tests --strict
  --no-interactive` before implementation.

## 2. Seven-Suite Physical Partition

- [x] 2.1 Mechanically retain the 18-case core and move parser 13, CLI 5, security 20,
  auth 11, DB 5, and QHH 22 definitions into their frozen owners without changing any
  definition source, AST, decorator, parameter ID, marker, skip, fixture, assertion, or
  monkeypatch target.
- [x] 2.2 Move all 19 support functions, `_FakeRiverSegmentCursor`, and the four private
  constants to non-collectible `tests/basins_registry_import_helpers.py`; keep same-named
  package-publication helpers separate and collect zero nodes.
- [x] 2.3 Keep every registry test/helper output strictly below 1,000 lines without changing
  `.large-file-guard.json`, adding an exclusion, or retaining a collectible shim.
- [x] 2.4 Generate a tracked self-digested registry oracle from the immutable contract and
  compare all 96 suffixes, 94 definitions, seven owner counts, and helper members one-to-one.

## 3. Direct Consumers and QHH Bridge

- [x] 3.1 Retarget all seven registry owners and lift every registry-helper use in
  `tests/test_publish_scheduler_file_registry.py` to module scope; derive exactly eight
  direct collectible importers from tracked ASTs.
- [x] 3.2 Retarget only D (`tests/qhh_production_bootstrap_helpers.py`) from the registry
  monolith to the new helper; require A/B/C to remain free of direct registry-helper imports
  and retain their imports from D.
- [x] 3.3 Record and test the controlled D transition: only the two import-module references
  may change; update the affected helper row, helper aggregate, frozen literal, and QHH
  oracle self-digest while all other QHH rows and its 66-node behavior remain unchanged.
- [x] 3.4 Prove no function-local or module-level import of the retained collectible registry
  core remains in any consumer.

## 4. Selector, Database Filter, and Current Docs

- [x] 4.1 Add a sorted seven-owner `BASINS_REGISTRY_IMPORT_TESTS` authority and replace only
  the monolith literal under `workers/model_registry/**`, preserving package-publication,
  QHH-bootstrap, and every other baseline target while allowing unrelated future additions.
- [x] 4.2 Route registry-helper-only changes to exactly eleven collectible suites (eight
  direct consumers plus QHH A/B/C), with the existing selector meta rider additive; prove
  each route edge independently RED when deleted and restore the table GREEN.
- [x] 4.3 Expand only the relevant `database:` authority to the exact eight-path union:
  registry helper/core/auth/DB/QHH/reingest plus QHH D/C; keep parser, CLI, security, QHH A/B,
  and broad registry globs absent.
- [x] 4.4 Add block-scoped per-edge deletion proofs for all eight database literals so the
  deleted path is unrescued, the other seven remain matched, and unrelated future patterns
  remain legal.
- [x] 4.5 Update active full-registry and real-Basins smoke commands to the new owners while
  keeping BUG-008 commands and historical/archived evidence byte-identical.

## 5. Evidence Floor

- [x] 5.1 Run the seven-file collection/comparator and pytest: require 96 unique baseline
  suffixes, 94 exact definitions, `78 passed, 18 skipped` default,
  `78 passed, 1 skipped, 17 deselected` non-integration, helper zero collection, and retained
  core BUG-008 `2 passed, 16 deselected`.
- [x] 5.2 Run the historical three-owner BUG-008 command and ledger validator; require eight
  passes with QHH-bootstrap / registry / production-scheduler ownership 5 / 2 / 1.
- [x] 5.3 Run `tests/test_select_ci_tests.py`, all seven owner RED rows, all eleven helper-route
  RED rows, all eight database RED rows, AST importer closure, and QHH controlled-transition
  guards; require every restored table GREEN.
- [x] 5.4 Run QHH A/B/C, scheduler-file-registry, reingest, affected Basins consumers, and
  full `uv run pytest -q`; focused suites pass, and full pytest reports 16,975 passed / 225
  skipped with only the two pure-master failures routed to #2029 and #2043, so #1913 adds no
  SQL/schema/geometry/auth/oracle regression.
- [x] 5.5 Run Ruff for changed Python, strict single/all OpenSpec validation, Markdown lint,
  entropy report/differential, `git diff --check`, scope/guard audits, and a non-vacuous
  23-path ordinary-hook proof; require zero #1913 findings while recording master hard-gate
  issue #2029 rather than misreporting the absolute gate as green.
- [x] 5.6 On node-27 at the frozen final SHA, run the 17 registry integration nodes plus 11
  QHH-bootstrap scheduler nodes against one isolated temporary database on local PostgreSQL
  `:55432`; require 28 collected, 27 passed, only the disabled real-Basins smoke skipped,
  all registry-QHH/bootstrap-QHH nodes passed, owned DB/role cleanup, and unchanged production
  DB/display identity. Do not access node-22 or expose credentials.
- [x] 5.7 Confirm the final diff is limited to registry test layout/helper/oracle, the two
  cross-test consumer modules, selector/meta-tests, exact CI database paths, active docs, and
  this OpenSpec change; exclude production code, fixtures, `.large-file-guard.json`, #1903,
  and archived evidence.
