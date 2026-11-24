# Tasks

Fixture level: `compact`. Risk packs: facade name surface read by owners and
tests through late binding; module-load side effects of an import.

Scope: #3335 items 6 and 7. No function body changes anywhere.

Must preserve:

- Every name read through the chain facade keeps resolving:
  `tests/test_facade_name_resolution.py` passes unchanged, and the tests that
  read facade names through name lists (`tests/test_orchestration_chain.py`,
  the `..._compat_...` family) pass with bodies unchanged.
- `tests/test_orchestrator_first_import.py` passes unchanged.
- The set of modules loaded by `import services.orchestrator.chain`, and by
  `import services.orchestrator.scheduler`, each in a fresh interpreter, is
  identical before and after, except for modules recorded in the log as
  dropped with no consumer (task 1.1).
- `sorted(vars(services.orchestrator.scheduler))` is identical before and
  after; `sorted(vars(services.orchestrator.chain))` differs exactly by the
  removed names.
- The file-level `# ruff: noqa` line of `chain.py` is not edited in this
  change.
- Test renames change only the `def` line: same body, same parametrization,
  same position in the file.

## 1. Code (`services/orchestrator/chain.py`)

- [x] 1.1 For each candidate in the proposal, decide by evidence:
  - readers inside `chain.py` (AST loads of the name, excluding the import
    statement itself);
  - readers through the facade anywhere in `services apps workers scripts
    packages tests`: attribute access on any alias of the facade
    (`chain.X`, `_chain.X`, `legacy_chain.X`, ...), `from
    services.orchestrator.chain import X`, string targets
    `"services.orchestrator.chain.X"`, and name lists fed to `getattr` on
    the facade (the scanner in `tests/test_facade_name_resolution.py`
    reports non-literal reads; read those call sites and their name lists);
  - the loaded-module set with and without the import.
  Remove the import when there is no reader. If the removal drops modules
  from the loaded set, list them and search for consumers of their being
  loaded: `sys.modules` lookups of them, attribute reads on a parent package
  without an import, and import-time registration inside them. With no
  consumer, the import still goes and the dropped modules are recorded. Keep
  an import only when a consumer exists, with a comment on its line naming
  that consumer. Record the per-name verdict in
  `.workplans/3335/chain-imports-c.log`.
- [x] 1.2 Remove a now-unneeded line-level `# noqa: F401` only on a line
  that is deleted. Do not touch other `noqa` markers.

## 2. Tests

- [x] 2.1 New small test file `tests/test_chain_facade_import_readers.py`:
  collect from the AST of `chain.py` every name bound by an `import` /
  `from ... import` statement (the bound name, i.e. the alias when there is
  one; skip `from __future__`). Each name must be one of:
  - loaded somewhere in `chain.py`'s own AST;
  - a chain-facade read reported by the scan in
    `tests/test_facade_name_resolution.py` (reuse its `scan_tree()` result,
    do not reimplement it);
  - in a literal tuple in the test of names that tests read only through
    name lists, each entry with the test that reads it;
  - in a literal mapping of imports kept for module loading, each with the
    consumer named (expected to be empty).
  The failure message lists the unread names. Red on master, where it names
  the candidates. Also assert the two literal collections contain no name
  that the first two rules already cover (no stale entries).
- [x] 2.3 `tests/test_node27_connection_attribution_delegated.py`: the
  delegated-connect closure registry lists, per registered component, the
  connect-capable modules its static import closure reaches. Dropping the
  unread `BestAvailableManager` import takes `packages/common/best_available.py`
  out of the closure of the components that reached it only through
  `chain.py`. Remove exactly the rows the test reports as stale and correct
  the count in the comment above them; no other row changes. In
  `tests/test_node27_connection_attribution.py`, drop the stale line number
  from the reason text that cites the deleted `chain.py` import line (the
  path through `chain.py` to the canonical converter still exists through
  the remaining from-import).
- [x] 2.2 Rename the eighteen tests whose names end in `_and_inventory`
  (eleven in `tests/test_orchestration_chain.py`, seven in
  `tests/test_production_scheduler.py`) by dropping that suffix. If a
  shortened name collides with an existing test name in the same file, keep
  the old name for that one and report it. Update any docstring sentence in
  those tests that still says an inventory is checked.

## 3. Evidence Floor

- [x] 3.1 `.workplans/3335/chain-imports-c.log`: per-name verdict with the
  reader evidence; loaded-module sets (count and digest) for both facades
  before and after; `vars()` digests and the exact name difference for the
  chain facade.
- [x] 3.2 Red run of 2.1 on the pre-change tree.
- [x] 3.3 Mutations, each killed and restored:
  - M1 remove one import that an owner reads through the facade (pick one
    from the guard's read list) -> `tests/test_facade_name_resolution.py`
    red.
  - M2 remove one name that is read only through a name list in
    `tests/test_orchestration_chain.py` -> that test red.
  - M3 put one removed import back -> 2.1 red, naming it.
  - M4 add a stale entry to the name-list tuple (a name `chain.py` uses
    itself) -> 2.1 red.
- [x] 3.4 Collected test ids: the eighteen old ids are gone and eighteen new
  ids exist (`pytest --collect-only -q` on the two files, before and after,
  same total count).
- [x] 3.5 Local: `uv run pytest -q tests/test_chain_facade_import_readers.py
  tests/test_node27_connection_attribution.py
  tests/test_node27_connection_attribution_delegated.py
  tests/test_facade_name_resolution.py
  tests/test_orchestrator_first_import.py tests/test_orchestration_chain.py
  tests/test_production_scheduler.py`; `uv run ruff check --no-cache .`;
  `openspec validate drop-unread-chain-facade-imports --strict --no-interactive`.
- [x] 3.6 node-27: the files of 3.5, zero unexpected skips.
