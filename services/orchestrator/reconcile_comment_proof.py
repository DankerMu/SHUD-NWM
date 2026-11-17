"""Comment-accounting proof classification for reserved-unbound reconcile."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.orchestrator.reconcile_records import (
    FALLBACK_BASIS_NAME_WINDOW_COUNT,
    MAX_EXACT_COMMENT_MATCHES,
    MAX_FALLBACK_MASTERS,
    CommentAccountingResult,
    CommentSacctQuerier,
    ReconcileQueryUnavailable,
    SacctRecord,
    _callable_accepts_keyword,
    _strict_utc_datetime,
)


@dataclass(frozen=True)
class _CommentAccountingProof:
    kind: str
    records: tuple[SacctRecord, ...] = ()
    coverage_complete: bool = False
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    # #2655: name-window fallback match basis (pass evidence only).
    fallback_match_basis: str | None = None


def _comment_query_records(
    value: CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None,
    *,
    expected_user: str = "",
    expected_account: str = "",
) -> tuple[SacctRecord, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, SacctRecord) else value
    owned = (
        item
        for item in values
        if isinstance(item, SacctRecord)
        and (not expected_user or item.user == expected_user)
        and (not expected_account or item.account == expected_account)
    )
    return tuple(item for _, item in zip(range(MAX_EXACT_COMMENT_MATCHES), owned, strict=False))


def _bounded_comment_records(
    value: CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None,
) -> tuple[SacctRecord, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, SacctRecord) else value
    return tuple(
        item
        for _, item in zip(
            range(MAX_EXACT_COMMENT_MATCHES),
            (entry for entry in values if isinstance(entry, SacctRecord)),
            strict=False,
        )
    )


def _classify_global_comment_records(
    records: tuple[SacctRecord, ...],
    *,
    expected_user: str,
    expected_account: str,
    coverage_complete: bool = False,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
) -> _CommentAccountingProof:
    if not records:
        return _CommentAccountingProof(
            "global_absence",
            coverage_complete=coverage_complete,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
    owned = tuple(
        record
        for record in records
        if (not expected_user or record.user == expected_user)
        and (not expected_account or record.account == expected_account)
    )
    foreign = tuple(record for record in records if record not in owned)
    if foreign:
        return _CommentAccountingProof("foreign_collision", records)
    if len(owned) > 1:
        return _CommentAccountingProof("ambiguous", owned)
    if owned:
        return _CommentAccountingProof("owned_match", owned)
    return _CommentAccountingProof("global_absence")


def _effective_versioned_absence_coverage(
    proof: _CommentAccountingProof,
    *,
    attempt_anchor: datetime | None,
) -> bool:
    """Recompute absence authority from durable anchor and adapter bounds."""

    anchor = _strict_utc_datetime(attempt_anchor)
    coverage_start = _strict_utc_datetime(proof.coverage_start)
    coverage_end = _strict_utc_datetime(proof.coverage_end)
    return bool(
        proof.coverage_complete is True
        and anchor is not None
        and coverage_start is not None
        and coverage_end is not None
        and coverage_start <= anchor <= coverage_end
    )


def _query_comment_accounting_proof(
    comment_query: CommentSacctQuerier,
    idempotency_key: str,
    *,
    expected_user: str,
    expected_account: str,
    accepted_submit_contract_version: str | None = None,
    submission_attempt_started_at: datetime | None = None,
    forcing_controller_identity: bool = False,
    forcing_stage: str | None = None,
    forcing_task_count: int | None = None,
    forcing_exact_comment: str | None = None,
    forcing_current_attempt_comment: bool = False,
) -> _CommentAccountingProof:
    """Obtain an owner match or an authoritative global exact-comment proof."""
    supports_scope = _comment_query_accepts_scope(comment_query)
    if forcing_current_attempt_comment and (
        not forcing_exact_comment
        or not supports_scope
        or not _callable_accepts_keyword(comment_query, "forcing_exact_comment")
        or not _callable_accepts_keyword(comment_query, "forcing_current_attempt_comment")
    ):
        raise ReconcileQueryUnavailable(
            "forcing exact-comment query adapter unavailable",
            reason_class="comment_accounting_unproven",
        )
    if not supports_scope:
        # A legacy one-argument fake represents a global collection. Never
        # owner-filter it into a false zero-match proof.
        global_value = comment_query(idempotency_key)
        return _classify_global_comment_records(
            _bounded_comment_records(global_value),
            expected_user=expected_user,
            expected_account=expected_account,
            coverage_complete=accepted_submit_contract_version is None,
        )

    scoped_kwargs: dict[str, Any] = {
        "expected_user": expected_user or None,
        "expected_account": expected_account or None,
    }
    if accepted_submit_contract_version is not None and _comment_query_accepts_contract_version(comment_query):
        scoped_kwargs["accepted_submit_contract_version"] = accepted_submit_contract_version
    if submission_attempt_started_at is not None and _comment_query_accepts_attempt_anchor(comment_query):
        scoped_kwargs["submission_attempt_started_at"] = submission_attempt_started_at
    if forcing_exact_comment and _callable_accepts_keyword(comment_query, "forcing_exact_comment"):
        scoped_kwargs["forcing_exact_comment"] = forcing_exact_comment
    if (
        forcing_current_attempt_comment
        and _callable_accepts_keyword(comment_query, "forcing_current_attempt_comment")
    ):
        scoped_kwargs["forcing_current_attempt_comment"] = True
    if (
        forcing_controller_identity
        and _comment_query_accepts_forcing_controller_identity(comment_query)
    ):
        scoped_kwargs.update(
            {
                "forcing_controller_identity": True,
                "forcing_stage": forcing_stage,
                "forcing_task_count": forcing_task_count,
            }
        )
    owner_value = comment_query(idempotency_key, **scoped_kwargs)
    owner_records = _bounded_comment_records(owner_value)
    if isinstance(owner_value, CommentAccountingResult) and owner_value.scope == "global":
        return _classify_global_comment_records(
            owner_records,
            expected_user=expected_user,
            expected_account=expected_account,
            coverage_complete=owner_value.coverage_complete,
            coverage_start=owner_value.coverage_start,
            coverage_end=owner_value.coverage_end,
        )
    if isinstance(owner_value, CommentAccountingResult) and owner_value.controller_exact_comment:
        if owner_value.scope != "owner":
            raise ReconcileQueryUnavailable("controller adapter did not provide owner identity")
        owned = _comment_query_records(
            owner_records,
            expected_user=expected_user,
            expected_account=expected_account,
        )
        if len(owned) > 1:
            return _CommentAccountingProof("ambiguous", owned)
        if len(owner_records) != 1 or len(owned) != 1:
            raise ReconcileQueryUnavailable(
                "controller did not prove one owned forcing master",
                reason_class="comment_accounting_unproven",
            )
        return _CommentAccountingProof("controller_match", owned)
    if isinstance(owner_value, CommentAccountingResult) and owner_value.fallback_name_window:
        # #1565: the name-window fallback result is a complete owner-exact
        # proof by itself — the query was name-scoped and already filtered by
        # exact user/account and submit window, so no exact-comment global
        # visibility round follows it.
        if owner_value.fallback_submit_unparsable:
            # An otherwise-eligible name-window row carried missing or
            # unparsable Submit evidence. Transient denial: the caller reports
            # ``query_unavailable`` with the pass-only
            # ``fallback_submit_unparsable`` reason.
            return _CommentAccountingProof(
                "fallback_submit_unparsable",
                owner_records,
                coverage_complete=False,
                coverage_start=owner_value.coverage_start,
                coverage_end=owner_value.coverage_end,
            )
        if not owner_records:
            return _CommentAccountingProof(
                "fallback_no_match",
                coverage_start=owner_value.coverage_start,
                coverage_end=owner_value.coverage_end,
            )
        # A result without a basis (an adapter that never read SubmitLine)
        # is count-only by construction.
        match_basis = owner_value.fallback_match_basis or FALLBACK_BASIS_NAME_WINDOW_COUNT
        if len(owner_records) >= MAX_FALLBACK_MASTERS:
            return _CommentAccountingProof(
                "fallback_ambiguous",
                owner_records,
                coverage_start=owner_value.coverage_start,
                coverage_end=owner_value.coverage_end,
                fallback_match_basis=match_basis,
            )
        return _CommentAccountingProof(
            "fallback_unique",
            owner_records,
            coverage_start=owner_value.coverage_start,
            coverage_end=owner_value.coverage_end,
            fallback_match_basis=match_basis,
        )
    owned = _comment_query_records(
        owner_records,
        expected_user=expected_user,
        expected_account=expected_account,
    )
    if len(owned) > 1:
        return _CommentAccountingProof("ambiguous", owned)
    if owner_records:
        if not owned:
            return _CommentAccountingProof("foreign_collision", owner_records)
        owner_candidate = owned[0]
    else:
        owner_candidate = None

    # Neither owner-scoped zero nor one owner candidate proves global identity.
    # A bounded all-ownership view must prove either one identical owned master
    # or zero exact-comment rows before bind/retry can proceed.
    global_kwargs = dict(scoped_kwargs)
    global_kwargs.update({"expected_user": None, "expected_account": None})
    global_value = comment_query(idempotency_key, **global_kwargs)
    if isinstance(global_value, CommentAccountingResult) and global_value.scope != "global":
        raise ReconcileQueryUnavailable("exact-comment adapter did not provide global proof")
    global_proof = _classify_global_comment_records(
        _bounded_comment_records(global_value),
        expected_user=expected_user,
        expected_account=expected_account,
        coverage_complete=(
            global_value.coverage_complete
            if isinstance(global_value, CommentAccountingResult)
            else accepted_submit_contract_version is None
        ),
        coverage_start=(
            global_value.coverage_start if isinstance(global_value, CommentAccountingResult) else None
        ),
        coverage_end=(
            global_value.coverage_end if isinstance(global_value, CommentAccountingResult) else None
        ),
    )
    if owner_candidate is not None and global_proof.kind == "owned_match":
        global_candidate = global_proof.records[0]
        if (
            global_candidate.slurm_job_id != owner_candidate.slurm_job_id
            or global_candidate.user != owner_candidate.user
            or global_candidate.account != owner_candidate.account
            or global_candidate.comment != owner_candidate.comment
        ):
            return _CommentAccountingProof(
                "foreign_collision", (owner_candidate, global_candidate)
            )
    elif owner_candidate is not None and global_proof.kind == "global_absence":
        return _CommentAccountingProof("foreign_collision", (owner_candidate,))
    return global_proof


def _comment_query_accepts_scope(comment_query: CommentSacctQuerier) -> bool:
    try:
        parameters = tuple(inspect.signature(comment_query).parameters.values())
    except (TypeError, ValueError):
        return False
    parameter_names = {parameter.name for parameter in parameters}
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or {
        "expected_user",
        "expected_account",
    }.issubset(parameter_names)


def _comment_query_accepts_contract_version(comment_query: CommentSacctQuerier) -> bool:
    try:
        parameters = tuple(inspect.signature(comment_query).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
        parameter.name == "accepted_submit_contract_version" for parameter in parameters
    )


def _comment_query_accepts_attempt_anchor(comment_query: CommentSacctQuerier) -> bool:
    try:
        parameters = tuple(inspect.signature(comment_query).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
        parameter.name == "submission_attempt_started_at" for parameter in parameters
    )


def _comment_query_accepts_forcing_controller_identity(
    comment_query: CommentSacctQuerier,
) -> bool:
    return all(
        _callable_accepts_keyword(comment_query, name)
        for name in (
            "forcing_controller_identity",
            "forcing_stage",
            "forcing_task_count",
        )
    )


def _comment_query_capability(comment_query: Any) -> bool | None:
    """Read the querier's typed tri-state comment-storage capability verdict.

    The default querier exposes ``capability()`` returning ``True`` /
    ``False`` / ``None`` (storing / explicitly comment-less / unknown), cached
    after exactly one probe per querier instance. A querier without the typed
    accessor is treated as unknown (None), which preserves the exact-comment
    pre-PR runtime semantics for injected/legacy fakes.
    """

    capability = getattr(comment_query, "capability", None)
    if not callable(capability):
        return None
    try:
        return capability()
    except Exception:  # noqa: BLE001 - a probe fault stays unknown, never raises.
        return None
