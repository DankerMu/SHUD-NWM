"""Bounded comment and fallback sacct queriers for restart reconcile."""

from __future__ import annotations

import inspect
import logging
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    is_forecast_cohort_stage_name,
)
from services.orchestrator.forcing_submit_identity import FORCING_ATTEMPT_COMMENT_PREFIX, is_forcing_stage_name
from services.orchestrator.reconcile_records import (
    FALLBACK_BASIS_NAME_WINDOW_COUNT,
    FALLBACK_BASIS_SUBMITLINE_EXACT,
    MAX_EXACT_COMMENT_MATCHES,
    MAX_FALLBACK_MASTERS,
    CommentAccountingResult,
    CommentSacctQuerier,
    ReconcileQuerySaturated,
    ReconcileQueryUnavailable,
    SacctQuerier,
    SacctRecord,
    _as_utc_datetime,
    _fallback_master_id,
    _parse_fallback_submit_instant,
    _parse_master_sacct_row,
    _ReconcileFallbackQuerySaturated,
    _ReconcileFallbackQueryUnavailable,
    _strict_utc_datetime,
)
from services.orchestrator.reconcile_sacct_probe import (
    _query_forcing_controller_identity,
    _terminate_and_reap,
    default_comment_storage_probe,
    default_global_accounting_visibility_probe,
)
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

LOGGER = logging.getLogger("services.orchestrator.reconcile")

# Conservative lookback for comment-based sacct recovery. The crash window
# between sbatch acceptance and durable bind can outlive sacct's default
# accounting horizon; a fixed 7-day floor keeps a just-submitted master job
# visible to reconcile-by-comment instead of being misjudged reservation_lost.
COMMENT_SACCT_LOOKBACK_DAYS = 7
COMMENT_SACCT_PAGE_HOURS = 12

# #1565: the bounded name-window fallback. One query per reservation from the
# immutable attempt anchor through the querier's frozen now, scoped to the
# ``nhms_forecast`` family and the reservation's exact owner/account. Only
# zero / unique / ambiguous classification is needed, so at most two distinct
# bare masters are retained.
FALLBACK_JOB_NAME = "nhms_forecast"

# #2655: the comment-less fallback additionally reads ``SubmitLine`` (last, so a
# command line containing ``|`` is the rejoined remainder of the row). Only this
# fallback query requests it; the visibility probe and exact-comment lane keep
# their own formats byte-identical.
FALLBACK_SACCT_FORMAT = "JobID,JobName,State,ExitCode,Comment,User,Account,Submit,SubmitLine"
_SUBMITLINE_COMMENT_FLAG = "--comment="

MAX_COMMENT_SACCT_BYTES = 2 * 1024 * 1024
MAX_COMMENT_SACCT_ROWS = 20_000
COMMENT_SACCT_TIMEOUT_SECONDS = 30.0


def default_sacct_querier(slurm_bin_path: str = "") -> SacctQuerier:
    """Build a sacct querier that shells out to the real ``sacct`` binary.

    Uses ``--parsable2 --noheader`` with the same field shape the gateway uses
    elsewhere (``JobID|JobName|State|ExitCode``). It returns the exact target
    row when present; a bare array master with no parent row is reconstructed
    from its exact ``<master>_<task>`` rows.
    """

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    def _query(slurm_job_id: str) -> SacctRecord | None:
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
            f"--jobs={slurm_job_id}",
        ]
        try:
            stdout = _bounded_sacct_stdout(command)
        except ReconcileQueryUnavailable as error:
            LOGGER.warning("sacct query failed for %s: %s", slurm_job_id, error)
            raise
        # Query succeeded: a None here means accounting has no such row
        # (confirmed-absent), not that we failed to ask.
        return _parse_master_sacct_row(stdout, slurm_job_id)

    return _query


def _fallback_candidate_in_window(submit: datetime | None, *, anchor: datetime, end: datetime) -> bool:
    """A fallback master is eligible only inside the closed attempt window."""

    if submit is None:
        return False
    return anchor <= submit <= end


def _submitline_comment_key(submit_line: str) -> str | None:
    """The single distinct ``--comment=`` value in a Slurm ``SubmitLine``.

    Tokens are whitespace-delimited (the gateway passes sbatch an argv list, so
    ``--comment=nhms_idem:<key>`` is one token and keys never contain
    whitespace). A missing/empty line, no such token, an empty value, or two or
    more distinct values yield ``None`` (no key).
    """

    values = {
        token[len(_SUBMITLINE_COMMENT_FLAG) :]
        for token in submit_line.split()
        if token.startswith(_SUBMITLINE_COMMENT_FLAG)
    }
    if len(values) != 1:
        return None
    (value,) = values
    return value or None


@dataclass
class _FallbackMaster:
    """One eligible bare master seen by the name-window fallback parser."""

    record: SacctRecord
    # The common submit-line key of every eligible row, or ``None`` (unknown)
    # once any eligible row lacks a key or disagrees.
    key: str | None


def _parse_fallback_sacct_rows(
    stdout: str,
    *,
    expected_user: str,
    expected_account: str,
    window_start: datetime,
    window_end: datetime,
    reservation_comment: str | None = None,
) -> tuple[list[SacctRecord], bool, str | None]:
    """Classify the name-window fallback output into eligible masters.

    Returns ``(records, submit_parsable, match_basis)``. A row is eligible only
    when its normalized bare master id is well-shaped, its submit instant
    parses and falls inside the closed attempt window, its owner matches
    exactly, and its job name belongs to the FORECAST family (``nhms_forecast``
    or a forecast stage alias; forcing/batch/extern/unrelated names are
    ineligible). An ineligible row never turns into
    ``fallback_submit_unparsable``: only an otherwise-eligible forecast/owner
    row with missing/unparsable Submit triggers the transient denial.
    ``submit_parsable`` is False when at least one candidate row carried
    missing/unparsable Submit evidence (transient denial per the pass-evidence
    contract).

    #2655 submit-line key: each eligible row's ``SubmitLine`` (the rejoined
    remainder after ``Submit``; an eight-field row has none) yields a key, and
    a master's key is known only when all its eligible rows agree. Basis
    ``submitline_exact`` applies only when every eligible master's key is
    known: then masters whose key differs from ``reservation_comment`` are
    excluded BEFORE the ``MAX_FALLBACK_MASTERS`` cap. Any unknown key excludes
    nothing and keeps the pre-change count-only classification
    (``name_window_count``). Every row inside the ``MAX_COMMENT_SACCT_ROWS``
    slice is read (no early stop), so the basis is decided over ALL eligible
    masters: an unknown-key master late in the output still forces
    ``name_window_count``. ``MAX_FALLBACK_MASTERS`` caps only the returned
    records. ``match_basis`` is ``None`` when no master remains.
    """

    masters: dict[str, _FallbackMaster] = {}
    submit_parsable = True
    for line in stdout.splitlines()[:MAX_COMMENT_SACCT_ROWS]:
        fields = line.strip().split("|")
        master_id = _fallback_master_id(fields[0]) if fields else None
        if master_id is None:
            continue
        user = fields[5].strip() if len(fields) > 5 else ""
        account = fields[6].strip() if len(fields) > 6 else ""
        if expected_user and user != expected_user:
            continue
        if expected_account and account != expected_account:
            continue
        job_name = fields[1].strip() if len(fields) > 1 else ""
        # Phase 6j: the fallback is the FORECAST-family name window. A row is
        # eligible only when its JobName is the canonical fallback name
        # (``nhms_forecast``) or a forecast stage alias recognized by
        # ``is_forecast_cohort_stage_name``. Deliberately NOT the broader
        # inflight ``_GENERIC_ARRAY_JOB_NAMES`` (which includes ``nhms_forcing``):
        # a non-forecast row must be ineligible, so a query with only such rows
        # is ``fallback_no_match`` (count 0) rather than a wrong
        # ``identity_mismatch_blocked``. Forcing aliases, batch/extern step
        # names, and unrelated names never enter the candidate list.
        if not is_forecast_cohort_stage_name(job_name.removeprefix("nhms_")):
            continue
        if len(fields) < 8:
            # Structurally sufficient owner/name/master row whose Submit field
            # is missing entirely: malformed evidence, transient denial (#1565).
            submit_parsable = False
            continue
        submit = _parse_fallback_submit_instant(fields[7])
        if submit is None:
            submit_parsable = False
            continue
        if not _fallback_candidate_in_window(submit, anchor=window_start, end=window_end):
            continue
        row_key = _submitline_comment_key("|".join(fields[8:])) if len(fields) > 8 else None
        master = masters.get(master_id)
        if master is None:
            master = _FallbackMaster(
                record=SacctRecord(
                    slurm_job_id=master_id,
                    job_name=job_name,
                    raw_state=fields[2].strip(),
                    exit_code=fields[3].strip() or None,
                    comment=fields[4].strip() or None,
                    user=user or None,
                    account=account or None,
                    submitted_at=submit,
                ),
                key=row_key,
            )
            masters[master_id] = master
        elif master.key != row_key:
            master.key = None
    eligible = list(masters.values())
    if eligible and reservation_comment and all(master.key is not None for master in eligible):
        basis = FALLBACK_BASIS_SUBMITLINE_EXACT
        remaining = [master for master in eligible if master.key == reservation_comment]
    else:
        basis = FALLBACK_BASIS_NAME_WINDOW_COUNT
        remaining = eligible
    records = [
        replace(master.record, submitline_key=master.key)
        for master in remaining[:MAX_FALLBACK_MASTERS]
    ]
    return records, submit_parsable, (basis if records else None)


def _fallback_eligibility(
    *,
    expected_user: str,
    expected_account: str,
    submission_attempt_started_at: Any,
) -> bool:
    """A current accepted-submit cohort MAY enter fallback only with a strict
    UTC attempt anchor and non-empty expected user/account."""

    if not expected_user or not expected_account:
        return False
    return _strict_utc_datetime(submission_attempt_started_at) is not None


def _forcing_controller_identity_request(
    *,
    enabled: bool,
    expected_user: str,
    expected_account: str,
    forcing_stage: str | None,
    forcing_task_count: int | None,
    submission_attempt_started_at: datetime | None,
) -> datetime | None:
    """Return a strict forcing-attempt anchor eligible for controller proof."""

    if (
        not enabled
        or not expected_user
        or not expected_account
        or not is_forcing_stage_name(forcing_stage)
        or type(forcing_task_count) is not int
        or forcing_task_count < 1
    ):
        return None
    return _strict_utc_datetime(submission_attempt_started_at)


def default_comment_sacct_querier(
    slurm_bin_path: str = "",
    *,
    global_visibility_probe: Callable[[], bool] | None = None,
    comment_storage_probe: Callable[[], bool | None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CommentSacctQuerier:
    """Build the bounded comment/controller querier for a crashed reservation.

    ``sacct`` owns the normal exact-comment proof.  On an explicitly
    comment-less cluster, an explicitly identified forcing array MAY instead
    use the controller's still-retained exact comment to bind one original
    master; controller expiry never proves absence.
    """

    from services.orchestrator.reservation import slurm_comment_for

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"
    scontrol = f"{slurm_bin_path.rstrip('/')}/scontrol" if slurm_bin_path else "scontrol"

    indexed_matches_by_page: dict[
        tuple[str, str, str, str], dict[str, tuple[SacctRecord, ...]]
    ] = {}
    scan_budget = _SacctScanBudget()
    visibility_probe = global_visibility_probe or default_global_accounting_visibility_probe(slurm_bin_path)
    visibility_proven: bool | None = None
    storage_probe = comment_storage_probe or default_comment_storage_probe(slurm_bin_path)
    comment_storage_proven: bool | None = None
    # Distinguish "not yet probed" from "probed and unknown (None)" so the
    # tri-state verdict True/False/None is each cached after exactly one probe
    # call per querier instance (#1565 D1).
    comment_storage_probed = False

    def _pages() -> tuple[tuple[datetime, datetime], ...]:
        end = now().astimezone(UTC).replace(microsecond=0)
        floor = end - timedelta(days=COMMENT_SACCT_LOOKBACK_DAYS)
        width = timedelta(hours=COMMENT_SACCT_PAGE_HOURS)
        pages: list[tuple[datetime, datetime]] = []
        while end > floor:
            start = max(end - width, floor)
            pages.append((start, end))
            end = start
        return tuple(pages)

    def capability() -> bool | None:
        """Tri-state comment-storage capability, probed at most once per instance.

        ``True`` = accounting stores job comments (exact-comment lane),
        ``False`` = explicitly comment-less (fallback lane), ``None`` =
        unknown (no lane may issue). Cached after the first probe so the verdict
        is stable for the whole scheduler/reconcile session (#1850 D2).
        """

        nonlocal comment_storage_proven, comment_storage_probed
        if not comment_storage_probed:
            comment_storage_proven = storage_probe()
            comment_storage_probed = True
        return comment_storage_proven

    # One querier instance is one scheduler/reconcile session. Freeze its full
    # time window once so every key and owner/global scope shares identical page
    # cache keys even while wall clock time advances during the scan.
    pages = _pages()

    def _query(
        idempotency_key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
        accepted_submit_contract_version: str | None = None,
        submission_attempt_started_at: datetime | None = None,
        forcing_controller_identity: bool = False,
        forcing_stage: str | None = None,
        forcing_task_count: int | None = None,
        forcing_exact_comment: str | None = None,
        forcing_current_attempt_comment: bool = False,
    ) -> CommentAccountingResult:
        owner_scope = (str(expected_user or ""), str(expected_account or ""))
        target_comment = str(forcing_exact_comment or "") or slurm_comment_for(idempotency_key)
        nonlocal visibility_proven, comment_storage_proven, comment_storage_probed
        if accepted_submit_contract_version not in (None, ACCEPTED_SUBMIT_CONTRACT_VERSION):
            raise ReconcileQueryUnavailable("accepted-submit contract version is unsupported")
        # Ahead of the visibility gate so every scope — owner, global and legacy —
        # is covered: without stored comments no scope's empty answer proves absence.
        if not comment_storage_probed:
            comment_storage_proven = storage_probe()
            comment_storage_probed = True
        if comment_storage_proven is None:
            # Probe failed or omitted AccountingStoreFlags: capability unknown.
            # No comment query and no fallback may issue — a failure here must
            # not be mistaken for an explicit no-comment cluster (#1565 D1).
            raise ReconcileQueryUnavailable(
                "comment storage capability is unknown",
                reason_class="comment_accounting_unproven",
            )
        if comment_storage_proven is False:
            controller_anchor = _forcing_controller_identity_request(
                enabled=forcing_controller_identity,
                expected_user=owner_scope[0],
                expected_account=owner_scope[1],
                forcing_stage=forcing_stage,
                forcing_task_count=forcing_task_count,
                submission_attempt_started_at=submission_attempt_started_at,
            )
            if controller_anchor is not None:
                assert type(forcing_task_count) is int
                return _query_forcing_controller_identity(
                    scontrol=scontrol,
                    target_comment=target_comment,
                    expected_user=owner_scope[0],
                    expected_account=owner_scope[1],
                    expected_stage=str(forcing_stage or ""),
                    expected_task_count=forcing_task_count,
                )
            # Explicitly comment-less (including (null)): exact-comment search
            # is provably useless, so refuse it — but a current accepted-submit
            # forecast cohort with strict anchor and exact ownership MAY enter
            # the bounded name-window fallback (#1565).
            if accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION and _fallback_eligibility(
                expected_user=owner_scope[0],
                expected_account=owner_scope[1],
                submission_attempt_started_at=submission_attempt_started_at,
            ):
                return _query_name_window_fallback(
                    owner_scope,
                    submission_attempt_started_at=submission_attempt_started_at,
                    reservation_comment=target_comment,
                )
            raise ReconcileQueryUnavailable(
                "accounting does not store job comments",
                reason_class="comment_accounting_unproven",
            )
        # Comment-storing cluster: exact-comment owner/global queries page
        # ``sacct`` exactly as before.
        if not any(owner_scope) and accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION:
            if visibility_proven is None:
                visibility_proven = bool(visibility_probe())
            if not visibility_proven:
                raise ReconcileQueryUnavailable(
                    "global accounting visibility is unproven",
                    reason_class="accounting_authority_unproven",
                )
        budget = scan_budget
        matches: list[SacctRecord] = []
        seen_ids: set[str] = set()
        for page_start, page_end in pages:
            # sacct reads bare timestamps in the host's local timezone.
            start_time = page_start.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            end_time = page_end.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            page_key = (*owner_scope, start_time, end_time)
            if page_key not in indexed_matches_by_page:
                command = [
                    sacct,
                    "--parsable2",
                    "--noheader",
                    "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
                    f"--starttime={start_time}",
                    f"--endtime={end_time}",
                ]
                command.append(f"--user={owner_scope[0]}" if owner_scope[0] else "--allusers")
                if owner_scope[1]:
                    command.append(f"--accounts={owner_scope[1]}")
                stdout = _bounded_sacct_page(command, budget)
                indexed_matches_by_page[page_key] = _index_comment_sacct_matches(
                    stdout,
                    expected_user=owner_scope[0] or None,
                    expected_account=owner_scope[1] or None,
                )
            for record in indexed_matches_by_page[page_key].get(target_comment, ()):
                if record.slurm_job_id in seen_ids:
                    continue
                seen_ids.add(record.slurm_job_id)
                matches.append(record)
                if len(matches) >= MAX_EXACT_COMMENT_MATCHES:
                    break
            if len(matches) >= MAX_EXACT_COMMENT_MATCHES:
                break
        # Below: the query succeeded. A None here is the authoritative
        # "accounting has no job for this comment" answer (confirmed-absent),
        # which is the ONLY case allowed to mark reservation_lost. A transient
        # failure never reaches here.
        coverage_start = pages[-1][0]
        coverage_end = pages[0][1]
        attempt_anchor = _as_utc_datetime(submission_attempt_started_at)
        coverage_complete = bool(
            attempt_anchor is not None and coverage_start <= attempt_anchor <= coverage_end
        )
        return CommentAccountingResult(
            tuple(matches),
            scope="owner" if any(owner_scope) else "global",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            coverage_complete=coverage_complete,
        )

    def _query_name_window_fallback(
        owner_scope: tuple[str, str],
        *,
        submission_attempt_started_at: datetime | None,
        reservation_comment: str,
    ) -> CommentAccountingResult:
        """One bounded name-scoped query from the frozen attempt anchor to now.

        Returns the eligible masters as an ``owner``-scoped result whose
        ``records`` hold at most ``MAX_FALLBACK_MASTERS`` distinct bare master
        ids, carrying ``fallback_submit_unparsable`` as a pass-only reason when
        an otherwise-eligible row had missing/unparsable Submit evidence. The
        reservation's ``nhms_idem:<key>`` comment drives the #2655 SubmitLine
        exact-key exclusion; the result carries the pass-only match basis.
        """

        attempt_anchor = _strict_utc_datetime(submission_attempt_started_at)
        if attempt_anchor is None:
            raise ReconcileQueryUnavailable(
                "accounting does not store job comments",
                reason_class="comment_accounting_unproven",
            )
        query_end = pages[0][1]
        if attempt_anchor > query_end:
            raise ReconcileQueryUnavailable(
                "accounting does not store job comments",
                reason_class="comment_accounting_unproven",
            )
        start_time = attempt_anchor.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        end_time = query_end.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            f"--format={FALLBACK_SACCT_FORMAT}",
            f"--name={FALLBACK_JOB_NAME}",
            f"--user={owner_scope[0]}",
            f"--accounts={owner_scope[1]}",
            f"--starttime={start_time}",
            f"--endtime={end_time}",
        ]
        try:
            stdout = _bounded_sacct_page(command, scan_budget)
        except _ReconcileFallbackQueryUnavailable:
            raise
        except ReconcileQuerySaturated as error:
            # Re-raise as the fallback-origin saturated marker so the reconcile
            # writer keeps the durable held tuple reason ``comment_accounting_unproven``
            # while pass evidence carries the applicable bounded reason.
            raise _ReconcileFallbackQuerySaturated(error.boundary) from error
        except ReconcileQueryUnavailable as error:
            # Same for a process/timeout failure: pass reason ``process_unavailable``,
            # durable held tuple reason ``comment_accounting_unproven``.
            raise _ReconcileFallbackQueryUnavailable(
                str(error), reason_class=error.reason_class
            ) from error
        records, submit_parsable, match_basis = _parse_fallback_sacct_rows(
            stdout,
            expected_user=owner_scope[0],
            expected_account=owner_scope[1],
            window_start=attempt_anchor,
            window_end=query_end,
            reservation_comment=reservation_comment,
        )
        return CommentAccountingResult(
            tuple(records),
            scope="owner",
            coverage_start=attempt_anchor,
            coverage_end=query_end,
            coverage_complete=submit_parsable,
            fallback_name_window=True,
            fallback_submit_unparsable=not submit_parsable,
            fallback_match_basis=match_basis,
        )

    # #1850 D2: expose the cached tri-state capability verdict as a typed
    # attribute on the querier so the reconcile writer can choose the runtime
    # lane without a second probe.
    setattr(_query, "capability", capability)
    return _query


@dataclass
class _SacctScanBudget:
    """One scan deadline plus independent limits applied to every time page."""

    page_max_bytes: int = 0
    page_max_rows: int = 0
    deadline: float = 0.0

    def __post_init__(self) -> None:
        if not self.page_max_bytes:
            self.page_max_bytes = MAX_COMMENT_SACCT_BYTES
        if not self.page_max_rows:
            self.page_max_rows = MAX_COMMENT_SACCT_ROWS
        if not self.deadline:
            self.deadline = time.monotonic() + COMMENT_SACCT_TIMEOUT_SECONDS


def _bounded_sacct_page(command: Sequence[str], budget: _SacctScanBudget) -> str:
    """Call the bounded reader while retaining compatibility with injected fakes."""

    try:
        supports_budget = "budget" in inspect.signature(_bounded_sacct_stdout).parameters
    except (TypeError, ValueError):
        supports_budget = False
    if supports_budget:
        return _bounded_sacct_stdout(command, budget=budget)
    if time.monotonic() >= budget.deadline:
        raise ReconcileQueryUnavailable("sacct query timed out")
    stdout = _bounded_sacct_stdout(command)
    if time.monotonic() >= budget.deadline:
        raise ReconcileQueryUnavailable("sacct query timed out")
    encoded = stdout.encode("utf-8")
    rows = stdout.count("\n") + int(bool(stdout) and not stdout.endswith("\n"))
    if len(encoded) > budget.page_max_bytes:
        raise ReconcileQuerySaturated("bytes")
    if rows > budget.page_max_rows:
        raise ReconcileQuerySaturated("rows")
    return stdout


def _bounded_sacct_stdout(command: Sequence[str], *, budget: _SacctScanBudget | None = None) -> str:
    """Read sacct output with byte/row/time bounds and always reap the child."""

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell.
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ReconcileQueryUnavailable("sacct query could not start") from error
    stdout = process.stdout
    if stdout is None:
        _terminate_and_reap(process)
        raise ReconcileQueryUnavailable("sacct query has no stdout")
    output = bytearray()
    row_count = 0
    started = time.monotonic()
    deadline = budget.deadline if budget is not None else started + COMMENT_SACCT_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    try:
        selector.register(stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReconcileQueryUnavailable("sacct query timed out")
            events = selector.select(timeout=min(remaining, 0.25))
            if not events:
                continue
            chunk = os.read(stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            output.extend(chunk)
            row_count += chunk.count(b"\n")
            logical_row_count = row_count + int(bool(output) and not output.endswith(b"\n"))
            byte_limit = budget.page_max_bytes if budget is not None else MAX_COMMENT_SACCT_BYTES
            row_limit = budget.page_max_rows if budget is not None else MAX_COMMENT_SACCT_ROWS
            if len(output) > byte_limit:
                raise ReconcileQuerySaturated("bytes")
            if logical_row_count > row_limit:
                raise ReconcileQuerySaturated("rows")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReconcileQueryUnavailable("sacct query timed out")
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise ReconcileQueryUnavailable(f"sacct query returned {return_code}")
        return bytes(output).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        raise ReconcileQueryUnavailable("sacct query failed") from error
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_and_reap(process)
        else:
            process.wait()


def _index_comment_sacct_matches(
    stdout: str,
    *,
    expected_user: str | None = None,
    expected_account: str | None = None,
) -> dict[str, tuple[SacctRecord, ...]]:
    comments: dict[str, list[SacctRecord]] = {}
    seen: dict[str, set[str]] = {}
    for line in stdout.splitlines()[:MAX_COMMENT_SACCT_ROWS]:
        fields = line.strip().split("|")
        if len(fields) < 5:
            continue
        comment = fields[4].strip()
        if not comment.startswith(("nhms_idem:", FORCING_ATTEMPT_COMMENT_PREFIX)):
            continue
        user = fields[5].strip() if len(fields) > 5 and fields[5].strip() else None
        account = fields[6].strip() if len(fields) > 6 and fields[6].strip() else None
        if expected_user and user != expected_user:
            continue
        if expected_account and account != expected_account:
            continue
        master_id = fields[0].split(".", 1)[0].split("_", 1)[0]
        if not SLURM_JOB_ID_RE.fullmatch(master_id):
            continue
        comment_seen = seen.setdefault(comment, set())
        if master_id in comment_seen:
            continue
        comment_seen.add(master_id)
        matches = comments.setdefault(comment, [])
        if len(matches) >= MAX_EXACT_COMMENT_MATCHES:
            continue
        matches.append(
            SacctRecord(
                slurm_job_id=master_id,
                job_name=fields[1].strip(),
                raw_state=fields[2].strip(),
                exit_code=fields[3].strip() or None,
                comment=comment,
                user=user,
                account=account,
            )
        )
    return {comment: tuple(records) for comment, records in comments.items()}
