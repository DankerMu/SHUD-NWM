"""#2442: bind ``EVALUATING_PASS_STATUSES`` to the WRITERS, by source text + ``ast``.

``operator_action_listing.EVALUATING_PASS_STATUSES`` decides whether a scanned
pass is allowed to answer "nothing waits" (``exit 0``).  Until this module the
only pin on it was a self-copy of the same 24 literals inside
``tests/test_operator_action_listing.py``: it froze the module constant against
a careless edit and said NOTHING about the writers.  Changing a writer could not
redden any test.

This pin reads the writer sources as TEXT and parses them with ``ast``.  It
never imports them -- the listing surface is db-free and importer-free on
purpose (``operator_action_listing`` imports two names from the scheduler
package and nothing else), and importing the writers from here would also add
importer pairs to the CI selector's directory rules.  Same shape as the A1
decision closure in ``tests/test_operator_action_listing.py``.

WHAT THIS PIN COVERS (set membership, both directions):

* a writer literal that no longer appears in the whitelist, or a whitelist
  literal no writer produces any more -- the stale-whitelist direction;
* a NEW pass-status literal in any scanned writer (it lands outside the
  whitelist and reddens :func:`test_the_writers_pass_status_literals_close_over_the_evaluating_set`);
* a write site whose value can not be resolved statically: it must be enumerated
  in :data:`_DECLARED_DYNAMIC_SITES` with a reason, so a new unjudged spelling
  is loud rather than silently dropped;
* a status helper that moves OUT of the scanned files: the delegation closure
  (:func:`test_every_delegated_status_helper_is_scanned_and_contributes`)
  requires every delegated call target to be a scanned function that itself
  contributed at least one literal.

WHAT IT DOES NOT COVER -- the asymmetry that is the reason #2442 exists.  A set
pin sees MEMBERSHIP, not CONTROL FLOW.  If a status ALREADY in the whitelist is
later written at a point BEFORE ``_build_candidates`` runs, the literal set does
not move one bit, this pin stays green, and the reader keeps treating such a
pass as "evaluated everything, nothing waits" -- a SILENT false ``exit 0`` for
candidates that were never enumerated.  That failure is not hypothetical: the
listing module documents ``preflight_blocked`` as exactly such a status (written
both before candidate construction with empty lists and after it with the full
lists), which is why it is excluded from the whitelist in the first place.  The
same positional drift inside the whitelist needs a different mechanism (for
example asserting, per evaluating status path, that the evidence the pass wrote
carries the candidate lists).  Adding this pin does NOT close that half; do not
read a green run here as "the whole of #2442's risk is handled".

THE PASSTHROUGH (the declared dynamic source).  ``_scheduler_pass_status_from_execution``
ends with ``str(execution_evidence[-1].get("status") or "planned")``: the pass
status is whatever the last execution-evidence item carries, and that vocabulary
comes from pipeline results at runtime, not from any resolvable literal set.
There is no repo-side authority for it either -- ``production_contract``'s
``PRODUCTION_STATUS_TAXONOMY`` is the coarse pending/ready/running/succeeded
output classification plus an alias map INTO it, not the raw pass-status
vocabulary.  So it is registered here as a judged dynamic site
(:data:`_DECLARED_DYNAMIC_SITES`) and the statuses that can only arrive that way
are enumerated in :data:`_PASSTHROUGH_ONLY_STATUSES` with that reason.  The
statically resolvable part of that same vocabulary IS pinned: the
execution-evidence item builders in ``scheduler_candidate_execution_evidence.py``
are scanned, so a new item status literal there is loud.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

#: The orchestrator package on disk.  Read as text, never imported.
_ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1] / "services" / "orchestrator"

#: The pass writer.  Every pass artifact is written from ``run_once`` (proved by
#: :func:`test_every_pass_evidence_write_site_is_inside_run_once`), so the scan
#: is scoped to that function: the module's other ``status`` literals belong to
#: sub-blocks (restart-reconcile evidence, retention receipts, reservation
#: records) that are not the pass status and must not pollute the set.
_PASS_WRITER = "scheduler_runtime.py"
#: The two calls that put a pass artifact on disk.  Anti-vacuity: a new pass
#: write site OUTSIDE ``run_once`` would sit outside the scan, so it reddens.
_PASS_WRITE_CALLS = ("_write_evidence", "_write_prelock_blocked_evidence")
#: ``run_once`` finalises the status through this helper; its second argument is
#: the fallback used when the payload carries none.
_EVIDENCE_STATUS_CALLS = ("_evidence_status", "evidence_status")
#: The local ``run_once`` binds before writing ``{"status": pass_status}``.
_PASS_STATUS_NAME = "pass_status"
#: The pass-evidence dict ``run_once`` builds from ``_base_evidence`` and updates.
_PASS_EVIDENCE_NAME = "evidence"
#: Any function whose name carries this token computes a pass status and is
#: scanned return-by-return, in every scanned file (the thin facades in
#: ``scheduler_runtime`` / ``scheduler_candidate_runtime`` included -- they
#: resolve by delegation).
_STATUS_HELPER_TOKEN = "pass_status"
#: The size fallback is the one place in the payload module that rewrites the
#: PASS's own status; the module's other ``status`` keys are sub-block markers
#: (``evidence_compaction``, ``limit.source_cycles``, retained-field summaries).
_BOUNDED_PAYLOAD_WRITER = "scheduler_evidence_payload.py"
_BOUNDED_PAYLOAD_FUNCTION = "bounded_evidence_payload"
#: Execution-evidence item builders.  Their ``status`` is what the passthrough
#: above hands back verbatim as the pass status, so the whole module is scanned
#: for ``"status"`` dict entries: every one of them is an evidence row.
_EXECUTION_EVIDENCE_WRITER = "scheduler_candidate_execution_evidence.py"
#: Status helpers live here (and are reached from ``run_once`` by delegation).
_STATUS_HELPER_WRITERS = (
    "scheduler_candidate_runtime.py",
    "scheduler_evidence_proofs.py",
    _PASS_WRITER,
)
#: Every file this pin reads.
_SCANNED_WRITERS = (
    _PASS_WRITER,
    "scheduler_candidate_runtime.py",
    "scheduler_evidence_proofs.py",
    _EXECUTION_EVIDENCE_WRITER,
    _BOUNDED_PAYLOAD_WRITER,
)

#: Write sites whose value can NOT be resolved statically, each with the ruling
#: that keeps it out of the set.  Identified by file + enclosing function +
#: ``ast.unparse`` of the expression, never by line number: a line number would
#: redden on any unrelated edit above it.  A new unjudged spelling fails loud.
_DECLARED_DYNAMIC_SITES = (
    # THE passthrough (#2442's central obstacle, see the module docstring): the
    # last execution-evidence item's own status becomes the pass status.  Its
    # vocabulary is produced by pipeline results at runtime; the statuses that
    # reach the whitelist only this way are enumerated in
    # _PASSTHROUGH_ONLY_STATUSES.
    "scheduler_candidate_runtime.py::_scheduler_pass_status_from_execution: "
    "str(execution_evidence[-1].get('status') or 'planned')",
    # The forcing-ready item copies the pipeline result's status (default
    # 'forcing_ready', itself a passthrough-only whitelist member below).
    "scheduler_candidate_execution_evidence.py::_candidate_forcing_ready_evidence: status",
    # The main execution item copies the result/outcome status through
    # _candidate_status_from_outcome -- same runtime vocabulary.
    "scheduler_candidate_execution_evidence.py::_candidate_execution_evidence_item: status",
    # A STAGE row inside stage_statuses, not an execution-evidence item: it is
    # never the last item _scheduler_pass_status_from_execution reads, so it can
    # not become a pass status at all.  Enumerated because the scan is
    # file-wide on purpose (a hand-listed set of item builders would be the same
    # unclosed-set defect one level up).
    "scheduler_candidate_execution_evidence.py::_stage_run_evidence: getattr(stage, 'status', None)",
)

#: Whitelist members the writers CAN NOT be shown to produce statically: every
#: one of them arrives through the declared passthrough above, as the status of
#: the last execution-evidence item.  Enumerated rather than waved at, so that
#: moving one onto a literal write site (or dropping it) has to be judged here.
_PASSTHROUGH_ONLY_STATUSES = frozenset(
    (
        "submission_failed",
        "skipped_duplicate_submission",
        "reconciling",
        "submit_result_ambiguous",
        "reconcile_unverified",
        "cancelled",
        "complete",
        "succeeded",
        "parsed_partial",
        "forcing_ready_partial",
        "forcing_ready",
        "already_done",
    )
)
#: Written as a pass status by the scanned writers AND non-evaluating on
#: purpose, next to the two transparent ones: ``lease_lost`` and the
#: exception-path ``resource_limit_blocked`` both run AFTER candidate
#: construction and empty the candidate lists, and the size fallback keeps the
#: same status for its bounded product.
_DECLARED_NON_EVALUATING_WRITTEN = frozenset(("lease_lost", "resource_limit_blocked"))


@functools.cache
def _parsed_writer(name: str) -> ast.Module:
    return ast.parse((_ORCHESTRATOR_DIR / name).read_text(encoding="utf-8"))


@functools.cache
def _function_owners(name: str) -> tuple[tuple[int, int, str], ...]:
    """``(start, end, function name)`` for every function in one writer file."""

    return tuple(
        sorted(
            (node.lineno, node.end_lineno or node.lineno, node.name)
            for node in ast.walk(_parsed_writer(name))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        )
    )


def _owner(name: str, lineno: int) -> str:
    """The innermost function containing ``lineno`` (``<module>`` when none)."""

    innermost = "<module>"
    for start, end, function in _function_owners(name):
        if start <= lineno <= end:
            innermost = function
    return innermost


def _site(name: str, value: ast.expr) -> str:
    return f"{name}::{_owner(name, value.lineno)}: {ast.unparse(value)}"


def _called_name(node: ast.Call) -> str | None:
    """The callee's own name, through an attribute or a ``getattr`` indirection.

    The scheduler facades call ``getattr(_scheduler, "_blocked_pass_status")(...)``
    rather than the function directly, so the name has to be read out of the
    ``getattr`` literal for those sites to resolve by delegation instead of
    landing in the unresolved list.
    """

    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
        and isinstance(func.args[1], ast.Constant)
        and isinstance(func.args[1].value, str)
    ):
        return func.args[1].value
    return None


def _normalised_helper(called: str) -> str:
    """``_blocked_pass_status`` and ``blocked_pass_status`` are one helper."""

    return called.lstrip("_")


def _resolve(
    name: str,
    value: ast.expr,
    *,
    statuses: set[str],
    delegated: set[str],
    unresolved: list[str],
) -> None:
    """Classify one write site: literal, bound name, delegation, or unresolved."""

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        statuses.add(value.value)
        return
    if isinstance(value, ast.Name) and value.id == _PASS_STATUS_NAME:
        # ``{"status": pass_status}`` / ``_evidence_status(evidence, pass_status)``:
        # the local's own assignments are collected in the same pass.
        return
    if isinstance(value, ast.Call):
        called = _called_name(value)
        if called is not None and _STATUS_HELPER_TOKEN in called:
            delegated.add(_normalised_helper(called))
            return
    unresolved.append(_site(name, value))


def _dict_status_values(node: ast.Dict) -> list[ast.expr]:
    return [
        item
        for key, item in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "status"
    ]


def _scan_run_once(*, statuses: set[str], delegated: set[str], unresolved: list[str]) -> None:
    """The pass writer: every status that can reach a pass artifact from ``run_once``."""

    for node in ast.walk(_run_once()):
        if isinstance(node, ast.Call):
            callee = node.func
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr == "update"
                and isinstance(callee.value, ast.Name)
                and callee.value.id == _PASS_EVIDENCE_NAME
            ):
                for argument in node.args:
                    if isinstance(argument, ast.Dict):
                        for value in _dict_status_values(argument):
                            _resolve(
                                _PASS_WRITER, value, statuses=statuses, delegated=delegated, unresolved=unresolved
                            )
            if isinstance(callee, ast.Name) and callee.id in _EVIDENCE_STATUS_CALLS and len(node.args) >= 2:
                _resolve(_PASS_WRITER, node.args[1], statuses=statuses, delegated=delegated, unresolved=unresolved)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bound_status_local = isinstance(target, ast.Name) and target.id == _PASS_STATUS_NAME
                bound_evidence_status = (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == _PASS_EVIDENCE_NAME
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "status"
                )
                if bound_status_local or bound_evidence_status:
                    _resolve(
                        _PASS_WRITER, node.value, statuses=statuses, delegated=delegated, unresolved=unresolved
                    )


def _run_once() -> ast.FunctionDef:
    functions = [
        node
        for node in ast.walk(_parsed_writer(_PASS_WRITER))
        if isinstance(node, ast.FunctionDef) and node.name == "run_once"
    ]
    assert len(functions) == 1, [node.lineno for node in functions]
    return functions[0]


def _scan_status_helpers(
    name: str, *, statuses: set[str], delegated: set[str], unresolved: list[str]
) -> dict[str, set[str]]:
    """Every ``return`` of every ``*pass_status*`` function in one writer file."""

    contributed: dict[str, set[str]] = {}
    for node in ast.walk(_parsed_writer(name)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if _STATUS_HELPER_TOKEN not in node.name:
            continue
        own: set[str] = set()
        for statement in ast.walk(node):
            if isinstance(statement, ast.Return) and statement.value is not None:
                _resolve(name, statement.value, statuses=own, delegated=delegated, unresolved=unresolved)
        statuses.update(own)
        contributed.setdefault(_normalised_helper(node.name), set()).update(own)
    return contributed


def _scan_bounded_payload(*, statuses: set[str], delegated: set[str], unresolved: list[str]) -> None:
    """The size fallback's own pass status (``bounded_evidence_payload``)."""

    functions = [
        node
        for node in ast.walk(_parsed_writer(_BOUNDED_PAYLOAD_WRITER))
        if isinstance(node, ast.FunctionDef) and node.name == _BOUNDED_PAYLOAD_FUNCTION
    ]
    assert len(functions) == 1, _BOUNDED_PAYLOAD_FUNCTION
    for node in ast.walk(functions[0]):
        if isinstance(node, ast.Dict):
            for value in _dict_status_values(node):
                _resolve(
                    _BOUNDED_PAYLOAD_WRITER, value, statuses=statuses, delegated=delegated, unresolved=unresolved
                )


def _scan_execution_evidence_items(*, statuses: set[str], delegated: set[str], unresolved: list[str]) -> None:
    """Every evidence-row ``status`` of the execution-evidence module (file-wide)."""

    for node in ast.walk(_parsed_writer(_EXECUTION_EVIDENCE_WRITER)):
        if isinstance(node, ast.Dict):
            for value in _dict_status_values(node):
                _resolve(
                    _EXECUTION_EVIDENCE_WRITER, value, statuses=statuses, delegated=delegated, unresolved=unresolved
                )


def _written_pass_statuses() -> tuple[set[str], list[str], set[str], dict[str, set[str]]]:
    """``(literals, unresolved sites, delegated helper names, literals per helper)``."""

    statuses: set[str] = set()
    delegated: set[str] = set()
    unresolved: list[str] = []
    helpers: dict[str, set[str]] = {}
    _scan_run_once(statuses=statuses, delegated=delegated, unresolved=unresolved)
    for name in _STATUS_HELPER_WRITERS:
        for helper, literals in _scan_status_helpers(
            name, statuses=statuses, delegated=delegated, unresolved=unresolved
        ).items():
            helpers.setdefault(helper, set()).update(literals)
    _scan_bounded_payload(statuses=statuses, delegated=delegated, unresolved=unresolved)
    _scan_execution_evidence_items(statuses=statuses, delegated=delegated, unresolved=unresolved)
    return statuses, sorted(unresolved), delegated, helpers


def test_the_writers_pass_status_literals_close_over_the_evaluating_set() -> None:
    """The whitelist must be what the writers actually produce, partitioned by reason.

    This replaces the 24-literal self-copy that used to live in
    ``tests/test_operator_action_listing.py``: the literals are still written
    down, but now each one is bound either to a writer site (the first two
    assertions) or to the declared passthrough vocabulary (the third), and a
    writer edit moves one of the three.

    Read the module docstring before trusting a green run: this is SET
    membership.  Moving an already-listed status to a write site BEFORE
    candidate construction is a control-flow change that leaves all three
    partitions untouched.
    """

    from services.orchestrator import operator_action_listing

    statuses, unresolved, _delegated, _helpers = _written_pass_statuses()

    assert unresolved == sorted(_DECLARED_DYNAMIC_SITES)

    evaluating = operator_action_listing.EVALUATING_PASS_STATUSES
    transparent = operator_action_listing.TRANSPARENT_PASS_STATUSES

    # 1. Every statically resolvable EVALUATING status, read off the writers.
    assert statuses & evaluating == {
        "planned",
        "blocked",
        "unavailable",
        "submitted",
        "submitted_partial",
        "slurm_status_synced",
        "slurm_status_sync_failed",
        "slurm_cancelled",
        "slurm_partially_cancelled",
        "slurm_cancellation_blocked",
        "restart_reconciled",
        "restart_reconcile_unknown",
    }
    # 2. Everything else the writers write is non-evaluating BY RULING: the two
    #    transparent statuses plus the two that empty the candidate lists.  A new
    #    writer literal nobody judged lands here and reddens.
    assert statuses - evaluating == transparent | _DECLARED_NON_EVALUATING_WRITTEN
    # 3. The rest of the whitelist can only arrive through the declared
    #    passthrough.  A status dropped from it without being added to a writer
    #    site reddens here, not silently.
    assert evaluating - statuses == _PASSTHROUGH_ONLY_STATUSES
    assert transparent <= statuses
    assert not (evaluating & transparent)


def test_every_pass_evidence_write_site_is_inside_run_once() -> None:
    """Anti-vacuity for the scan scope: no pass artifact is written anywhere else.

    The status scan of the pass writer is scoped to ``run_once`` so the module's
    unrelated ``status`` literals (restart-reconcile evidence, retention
    receipts, the reservation record) stay out of the set.  That scoping is only
    sound while ``run_once`` is the sole writer of pass artifacts -- a second one
    elsewhere in the module could write a status this pin never sees, which is
    the silent false ``exit 0`` again.
    """

    write_sites = [
        (node.lineno, _owner(_PASS_WRITER, node.lineno))
        for node in ast.walk(_parsed_writer(_PASS_WRITER))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in _PASS_WRITE_CALLS)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in _PASS_WRITE_CALLS)
        )
    ]

    assert write_sites, _PASS_WRITE_CALLS
    assert {owner for _lineno, owner in write_sites} == {"run_once"}, write_sites


def test_every_delegated_status_helper_is_scanned_and_contributes() -> None:
    """A delegated call resolves only while its target is a SCANNED helper with literals.

    ``pass_status = _blocked_pass_status(...)`` is treated as resolved because the
    helper itself is read.  Move that helper to a sixth module and the delegation
    would silently resolve to nothing -- the R2-05 failure the A1 decision pin
    had to fix one level up -- so every delegated name must be a scanned function
    that contributed at least one literal.
    """

    _statuses, _unresolved, delegated, helpers = _written_pass_statuses()

    assert delegated == {
        "scheduler_pass_status_from_execution",
        "scheduler_pass_status_from_cancellation",
        "blocked_pass_status",
    }
    for name in sorted(delegated):
        assert helpers.get(name), name


def test_every_scanned_writer_contributes_at_least_one_literal() -> None:
    """Anti-vacuity per file: a scan that stopped matching anything would pass silently.

    Each scanned file is read with its own site rule (``run_once`` for the pass
    writer, ``*pass_status*`` returns for the helpers, ``bounded_evidence_payload``
    for the size fallback, every evidence row for the execution-evidence module).
    A rename that makes one of those rules match nothing would shrink the set
    quietly, so each file is asserted to contribute.
    """

    contributions: dict[str, set[str]] = {}
    for name in _SCANNED_WRITERS:
        assert (_ORCHESTRATOR_DIR / name).is_file(), name
        statuses: set[str] = set()
        delegated: set[str] = set()
        unresolved: list[str] = []
        if name == _PASS_WRITER:
            _scan_run_once(statuses=statuses, delegated=delegated, unresolved=unresolved)
        if name in _STATUS_HELPER_WRITERS:
            _scan_status_helpers(name, statuses=statuses, delegated=delegated, unresolved=unresolved)
        if name == _BOUNDED_PAYLOAD_WRITER:
            _scan_bounded_payload(statuses=statuses, delegated=delegated, unresolved=unresolved)
        if name == _EXECUTION_EVIDENCE_WRITER:
            _scan_execution_evidence_items(statuses=statuses, delegated=delegated, unresolved=unresolved)
        contributions[name] = statuses

    assert {name: bool(statuses) for name, statuses in contributions.items()} == dict.fromkeys(
        _SCANNED_WRITERS, True
    ), contributions
    # The size fallback is the ONE pass-status rewrite in the payload module; if
    # this ever grows, the new literal has to be judged in the main pin above.
    assert contributions[_BOUNDED_PAYLOAD_WRITER] == {"resource_limit_blocked"}
