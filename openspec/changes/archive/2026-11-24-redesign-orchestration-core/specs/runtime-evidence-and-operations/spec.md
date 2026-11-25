## MODIFIED Requirements

### Requirement: Slurm preflight tilde expansion never raises

The Slurm preflight storage-root checks SHALL expand a leading tilde in
received root values (in both the allowed-roots walk and the per-root
storage check) without ever letting the expansion escape the preflight as
an exception. This is a defence-in-depth guarantee at the helper level:
today's configuration layer cannot deliver a leading-tilde value to these
helpers (configuration construction, which is always the DB-free
construction, anchors the value at the working directory first), so
the requirement hardens the preflight against future callers and against
changes in that layer rather than closing a currently live escape.
Specifically: when the home directory cannot be determined (an unknown `~user`
prefix, or a plain `~` with no usable home-directory source), the unexpanded
value SHALL flow on as an ordinary path into the existing arms — the
allowed-roots walk's ENOENT tolerance arm (which admits a cwd-anchored
root with no blocker) and the per-root storage check's structured
containment/visibility verdicts — so the preflight always returns its
structured result instead of aborting the scheduling pass. Values whose tilde does expand, and values
without a tilde, keep their existing verdicts byte-for-byte — except the
recorded `./~/x` acceptance (a `.`-prefixed value whose first surviving
component is a tilde now takes the fail-closed cwd-anchored verdict at the
str-input storage check instead of expanding) — and no new blocker reason
is introduced.

#### Scenario: unexpandable tilde in allowed storage roots is tolerated without crashing the preflight

WHEN the allowed-roots walk receives a root value of `~nosuchuser/roots`
(or a plain `~/…` with no determinable home directory)
THEN `_slurm_preflight` returns its structured status/blockers result — no
RuntimeError escapes — and the affected root flows through the existing
ENOENT tolerance arm and is admitted as a cwd-anchored containment root
with no blocker (the existing arm never produces a blocker for
not-yet-existing roots; the resulting phantom-root geometry on this preflight
leg is adjudicated by `docs/adr/0009-path-canonicalization-dereference-doctrine.md`,
which admits it under clause 3 — the admitted root serves only as a containment
base, carries no assertion that it exists, and the path judged against it is
dereferenced before the verdict is committed, which is what makes the admission
safe; that geometry is documented, not changed, here)

#### Scenario: unexpandable tilde in a storage root field yields the existing check verdict

WHEN the per-root storage check receives an unexpandable tilde value for a
storage root field (workspace/object-store/log/runtime)
THEN the per-root storage check produces its existing structured
configured/contained/visible verdict without raising

#### Scenario: expandable and tilde-free roots keep their behavior

WHEN a configured root has no tilde or its tilde expands normally
THEN the preflight verdict is byte-for-byte identical to the pre-change
behavior, except the recorded `./~/x` acceptance at the str-input storage
check (fail-closed, pinned in the byte-compatibility carve-out)

### Requirement: DB-free scheduler config path adjudication survives symlink loops

The db-free scheduler configuration lane SHALL normalize the required-path
values it adjudicates, and the config values it canonicalizes at construction
time, without relying on symlink-loop-unsafe resolution, and SHALL produce the
same canonical form on every supported CPython version. `Path.resolve()` is
not a usable loop predicate on the supported interpreter range — the
non-strict form stopped raising on symlink loops in CPython 3.13+, and the
strict form raises an errno-less `RuntimeError` on 3.12 and earlier — so this
lane SHALL normalize through `os.path.realpath`, whose strict form raises
`OSError` carrying an errno on every supported version and whose non-strict
form raises neither `OSError` nor `RuntimeError` for the input classes this
lane adjudicates — symlink loops and missing components — on any of them.
That bound is deliberate: the non-strict form is not total either. An
unrepresentable path string (one carrying an embedded NUL) raises `ValueError`
from every resolution primitive, and a *relative* value raises `OSError` from
the non-strict form when the process working directory is unavailable
(measured on a deleted cwd: `FileNotFoundError`, errno `ENOENT`). Each helper
below states whether it folds those cases or leaves them as a pre-existing
escape.

For the **required-path check**, which owns a structured blocker channel: a
value whose strict resolution fails with an errno other than `ENOENT` — a
symlink loop foremost — SHALL produce the existing
`db_free_required_path_unsafe` blocker carrying the errno-derived reason,
rather than being folded lexically and then attributed downstream as
`db_free_required_path_not_found`. Attribution SHALL therefore distinguish
"this path is a symlink loop" from "this path has not been created", which the
lexical fold previously conflated on CPython 3.13+ and which sends operators
to the wrong remedy. A value whose strict resolution fails with `ENOENT`
keeps its existing admitted semantics through a loop-filtered non-strict
fallback — a required path whose final components do not exist yet is
adjudicated by the existing downstream missing-parent and not-found blockers,
not by the resolution step — and a fallback that still carries a loop is
rejected as unsafe. A value that cannot be resolved because it is not a valid
path string SHALL take the same unsafe blocker rather than escaping. The
blocker record shape and its `error_type` evidence field are unchanged, and
**no new blocker code is introduced**. The containment comparison that follows
is itself unchanged but is no longer reached by values that fail resolution:
a value that is both unresolvable and outside the configured boundary SHALL
report the unsafe blocker rather than
`db_free_required_path_outside_boundary`, while a cleanly resolving
out-of-boundary value keeps the boundary blocker unchanged. The reason *values* carried by `db_free_required_path_unsafe`
do widen: today that code carries only `unsafe`, `traversal` and
`credential_component`, and adopting the errno-derived classification adds the
shared mapping's values (`unsafe_path` for a symlink loop or a non-directory
component, `not_writable` for a permission fault, `unavailable` otherwise).
That widening is the point of the requirement rather than a side effect, and
it SHALL include re-classifying the already-blocked under-a-loop case from
`unsafe` to the errno-derived value.

For the **config construction** helpers, which have no rejection channel and
must return a path, the construction SHALL normalize through strict
`os.path.realpath`, falling back to the non-strict form on any `OSError` — and
on `OSError` only, because the
fallback call would raise `ValueError` again for an unrepresentable path
string and turn a pre-existing escape into an escape from inside the handler.
This is the only construction arm; the database-backed arm it was aligned with
was deleted (#3311). That `ValueError` escape is therefore retained here exactly as it stands
today, neither introduced nor removed. Classification remains the
storage preflight's responsibility rather than construction's, so this arm
SHALL NOT introduce an errno split or a rejection of its own. This makes the
two interpreter arms agree on one canonical form: a loop-bearing value that
CPython 3.12 and earlier previously returned unresolved is now returned in the
same folded form CPython 3.13+ produces, and downstream classification acts on
one shape instead of two.

The path-identity comparison helper in the same lane SHALL likewise normalize
through non-strict `os.path.realpath` so that its comparison verdicts do not
depend on the interpreter version. Its `ValueError` escape on an
unrepresentable path string is pre-existing and explicitly retained, neither
introduced nor removed by this requirement: the helper has no rejection channel
and its callers compare only its own products, so folding that case would
require a sentinel value this lane does not define. Dropping this helper's
`try`/`except (OSError, RuntimeError)` also un-catches the second escape class
registered above — an `OSError` from a *relative* value normalized while the
working directory is unavailable, which the old handler folded by returning the
input path. That class SHALL be recorded here rather than re-guarded: unlike
the config-construction arm, this helper has no database-backed twin to be
aligned with, so the change is a plain deletion of a guard whose reachability
is nil at every current call site (all of them pass absolute values). Values that resolve cleanly keep
their existing normalized values byte-for-byte in all of these helpers.

#### Scenario: A symlink-loop required path is attributed as unsafe, not as missing

- **GIVEN** a db-free required-path value that is a symlink loop, or lies
  under one, within the configured containment bases
- **WHEN** the db-free required-path check adjudicates it
- **THEN** it produces the existing `db_free_required_path_unsafe` blocker
  code carrying the errno-derived reason value `unsafe_path` on every
  supported CPython version, and not the `db_free_required_path_not_found`
  attribution the lexical fold previously produced — and a path lying *under*
  a loop, already blocked with that same code, likewise reports the
  errno-derived value in place of the generic `unsafe`

#### Scenario: A not-yet-created required path keeps its existing adjudication

- **GIVEN** a db-free required-path value under a cleanly resolving
  containment base whose final components do not exist yet
- **WHEN** the required-path check adjudicates it
- **THEN** the resolution step admits it with a value byte-identical to the
  pre-change normalized value, and the verdict is produced by the existing
  downstream missing-parent / not-found blockers exactly as before

#### Scenario: Config construction yields one canonical form on both interpreter arms

- **GIVEN** a db-free config path value that is a symlink loop, or lies under
  one
- **WHEN** the config construction helper canonicalizes it
- **THEN** it returns the folded canonical form — the same value on CPython
  3.12 and earlier as on 3.13+, where 3.12 and earlier previously returned the
  unresolved value — and no exception escapes construction for this input
  class, the pre-existing `ValueError` escape on an unrepresentable path string
  being retained unchanged (a relative value under an unavailable working
  directory is not reached by this helper at all: the caller absolutizes with
  `Path.cwd()` first, which already raised there before this change)

#### Scenario: Path identity comparison is version-independent

- **GIVEN** two db-free config path values whose canonicalization involves a
  symlink loop
- **WHEN** the lane's path-identity helper normalizes each for comparison
- **THEN** each normalization returns the folded form, so the resulting
  identity verdict is the same on every supported CPython version — where
  previously CPython 3.12 and earlier returned the unresolved value and 3.13+
  the folded one, making the verdict interpreter-dependent

#### Scenario: Clean values keep their behavior

- **GIVEN** db-free config or required-path values with no symlink loop
- **WHEN** any helper in this lane normalizes them
- **THEN** the resulting values and verdicts are byte-for-byte identical to
  the pre-change behavior

### Requirement: The scheduler root check SHALL attribute a symlink-loop root identically on every supported interpreter

`_scheduler_root_check` SHALL NOT use `Path.resolve()` to canonicalise the root under check or the workspace anchor, and SHALL NOT answer a symlink-loop root with an early return on an interpreter-specific exception and a separately assembled `check`. A root whose path is a symlink loop, lies below one, or folds onto one through `..` SHALL flow through the same assembly as any other root, so that the kernel `lstat` of the configured path decides the blocker (`SYMLINK`, `UNSAFE_PATH` on `ELOOP`, `NOT_FOUND` on `ENOENT`) and the `check` carries the same key set on CPython 3.11 and on CPython 3.13+. The verdict (`blocked`) for such roots is unchanged. A symlink CHAIN (no loop) deeper than the interpreter's recursion limit, which up to CPython 3.12 makes `os.path.realpath` raise `RecursionError`, SHALL yield a `blocked` check with a typed `UNSAFE_PATH` blocker instead of an exception; on 3.13+ the chain resolves and the `lstat` decides. The pass that renders a blocked root into `resolved_runtime_roots` SHALL NOT abort on a loop root: that path string is render-only and SHALL NOT be produced with `Path.resolve()`.

#### Scenario: A self-loop lock root yields the same code and keys on 3.11 and 3.14

- **GIVEN** a `ProductionSchedulerConfig` whose `lock_root` is a symlink pointing at itself
- **WHEN** the scheduler runtime-root preflight (`_scheduler_runtime_root_preflight`) runs on CPython 3.11 and on CPython 3.14
- **THEN** both runs report `blocked` with blocker code `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` and the same `checks.lock_root` key set, including `symlink`, `allow_create` and `under_workspace` (#3311 observed-behaviour row 21)

#### Scenario: A full scheduler pass with a loop lock root writes its blocked evidence on 3.11 and 3.14

- **GIVEN** runtime roots are required and the configured `lock_root` is a symlink pointing at itself (or lies below one)
- **WHEN** `ProductionScheduler.from_env(config).run_once()` runs on CPython 3.11 and on CPython 3.14
- **THEN** neither run raises; both return `preflight_blocked` and write a pass artifact under the evidence root whose `root_preflight.blockers` carry `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` (or `..._UNSAFE_PATH` for the below-loop root) and whose `resolved_runtime_roots.lock_root.path` is the configured spelling

#### Scenario: A symlink chain deeper than the recursion limit is blocked, not raised

- **GIVEN** a root that is the head of a finite symlink chain longer than `sys.getrecursionlimit()`
- **WHEN** `_scheduler_root_check` runs on it
- **THEN** it returns a `blocked` check on every interpreter: `UNSAFE_PATH` with the canonicaliser-failure key set on CPython 3.11, `SYMLINK` with the ordinary-symlink key set on CPython 3.13+
