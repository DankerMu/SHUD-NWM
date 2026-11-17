"""Bounded sacct visibility, comment-storage and controller-identity probes for restart reconcile."""

from __future__ import annotations

import logging
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from services.orchestrator.forcing_submit_identity import is_forcing_stage_name
from services.orchestrator.reconcile_identity import _stages_match
from services.orchestrator.reconcile_records import (
    MAX_EXACT_COMMENT_MATCHES,
    CommentAccountingResult,
    ReconcileQuerySaturated,
    ReconcileQueryUnavailable,
    SacctRecord,
    _fallback_master_id,
    _parse_fallback_submit_instant,
)
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

LOGGER = logging.getLogger("services.orchestrator.reconcile")

COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS = 5.0
MAX_VISIBILITY_PROBE_BYTES = 256 * 1024
MAX_VISIBILITY_PROBE_ROWS = 5_000


def default_global_accounting_visibility_probe(slurm_bin_path: str = "") -> Callable[[], bool]:
    """Prove controller and SlurmDBD both expose jobs to ``--allusers``."""

    scontrol = f"{slurm_bin_path.rstrip('/')}/scontrol" if slurm_bin_path else "scontrol"
    sacctmgr = f"{slurm_bin_path.rstrip('/')}/sacctmgr" if slurm_bin_path else "sacctmgr"

    def _probe() -> bool:
        proven = True
        for command in ([scontrol, "show", "config"], [sacctmgr, "show", "config"]):
            try:
                stdout = _bounded_visibility_stdout(
                    command,
                )
            except ReconcileQueryUnavailable:
                proven = False
                continue
            if not _private_data_allows_global_jobs(stdout):
                proven = False
        return proven

    return _probe


def _bounded_visibility_stdout(command: Sequence[str]) -> str:
    """Drain visibility-probe stdout/stderr with hard byte, row, and time bounds."""

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell.
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ReconcileQueryUnavailable("visibility probe could not start") from error
    if process.stdout is None or process.stderr is None:
        _terminate_and_reap(process)
        raise ReconcileQueryUnavailable("visibility probe pipes are unavailable")
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    row_counts = {process.stdout: 0, process.stderr: 0}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReconcileQueryUnavailable("visibility probe timed out")
            events = selector.select(timeout=min(remaining, 0.25))
            if not events:
                continue
            for key, _mask in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    continue
                streams[stream].extend(chunk)
                row_counts[stream] += chunk.count(b"\n")
                logical_rows = row_counts[stream] + int(
                    bool(streams[stream]) and not streams[stream].endswith(b"\n")
                )
                if sum(len(output) for output in streams.values()) > MAX_VISIBILITY_PROBE_BYTES:
                    raise ReconcileQuerySaturated("bytes")
                if logical_rows > MAX_VISIBILITY_PROBE_ROWS:
                    raise ReconcileQuerySaturated("rows")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReconcileQueryUnavailable("visibility probe timed out")
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise ReconcileQueryUnavailable(f"visibility probe returned {return_code}")
        return bytes(streams[process.stdout]).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        raise ReconcileQueryUnavailable("visibility probe failed") from error
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_and_reap(process)
        else:
            process.wait()


def _private_data_allows_global_jobs(stdout: str) -> bool:
    found = False
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "privatedata":
            found = True
            private_data = {
                token.strip().lower()
                for token in value.replace(",", " ").split()
                if token.strip()
            }
            if "jobs" in private_data or "all" in private_data:
                return False
    return found


def default_comment_storage_probe(slurm_bin_path: str = "") -> Callable[[], bool | None]:
    """Classify one querier instance's comment-storage capability (tri-state).

    Returns ``True`` when ``AccountingStoreFlags`` contains ``job_comment``
    (comment-storing: exact-comment recovery stays authoritative), ``False`` when
    a present line explicitly lacks the token — including ``(null)`` —
    (explicitly comment-less: the name-window fallback MAY run), and ``None``
    when the probe could not execute or the output omits the
    ``AccountingStoreFlags`` line (unknown: no accounting query may issue).
    """

    scontrol = f"{slurm_bin_path.rstrip('/')}/scontrol" if slurm_bin_path else "scontrol"

    def _probe() -> bool | None:
        try:
            stdout = _bounded_visibility_stdout([scontrol, "show", "config"])
        except ReconcileQueryUnavailable as error:
            LOGGER.warning("comment storage probe could not execute: %s", error)
            return None
        verdict = _accounting_store_flags_classify(stdout)
        if verdict is False:
            LOGGER.warning(
                "accounting does not store job comments: AccountingStoreFlags lacks job_comment"
            )
        elif verdict is None:
            LOGGER.warning(
                "comment storage capability unknown: AccountingStoreFlags line is absent"
            )
        return verdict

    return _probe


def _accounting_store_flags_classify(stdout: str) -> bool | None:
    """Tri-state classification of ``AccountingStoreFlags`` in ``scontrol`` output.

    ``True``: a present line contains ``job_comment``. ``False``: a present line
    explicitly lacks it (including the literal ``(null)``). ``None``: no line at
    all (unknown — never a claim that comments are absent).
    """

    found = False
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip().lower() != "accountingstoreflags":
            continue
        found = True
        flags = {
            token.strip().lower()
            for token in value.replace(",", " ").split()
            if token.strip()
        }
        if "job_comment" in flags:
            return True
        return False
    if not found:
        return None
    return False


def _controller_job_records(stdout: str) -> tuple[dict[str, str], ...]:
    """Parse bounded ``scontrol show job -o`` records without trusting omissions."""

    records: list[dict[str, str]] = []
    lines: list[str] = []
    for line in stdout.splitlines():
        if line.lstrip().startswith("JobId="):
            if lines:
                records.append(_controller_job_fields(lines))
            lines = [line]
        elif lines:
            lines.append(line)
    if lines:
        records.append(_controller_job_fields(lines))
    return tuple(record for record in records if record)


_CONTROLLER_IDENTITY_FIELDS = frozenset(
    {
        "JobId",
        "ArrayJobId",
        "ArrayTaskId",
        "JobName",
        "UserId",
        "Account",
        "JobState",
        "ExitCode",
        "SubmitTime",
        "Comment",
    }
)


def _controller_job_fields(lines: Sequence[str]) -> dict[str, str]:
    """Read only fixed scontrol fields; duplicate identity fields fail closed."""

    fields: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("JobId="):
            allowed = _CONTROLLER_IDENTITY_FIELDS
        elif line.startswith("UserId="):
            allowed = ("UserId",)
        elif line.startswith("Priority="):
            allowed = ("Account",)
        elif line.startswith("JobState="):
            allowed = ("JobState",)
        elif line.startswith("Requeue="):
            allowed = ("ExitCode",)
        elif line.startswith("SubmitTime="):
            allowed = ("SubmitTime",)
        elif line.startswith("Comment="):
            allowed = ("Comment",)
        else:
            continue
        for token in line.split():
            key, separator, value = token.partition("=")
            if not separator or key not in allowed or not value:
                continue
            if key in fields:
                fields["_identity_unparseable"] = "1"
                continue
            fields[key] = value
    return fields


def _controller_array_task_identity_is_compatible(
    value: str | None,
    *,
    task_count: int,
) -> bool:
    """Accept only task ids/ranges inside the reserved forcing array."""

    text = str(value or "").strip()
    if not text:
        return False
    for raw_part in text.split(","):
        part, separator, throttle = raw_part.partition("%")
        if separator and (not throttle.isdigit() or int(throttle) < 1):
            return False
        if "-" in part:
            start_text, separator, end_text = part.partition("-")
            if not separator or "-" in end_text:
                return False
        else:
            start_text = end_text = part
        if not start_text.isdigit() or not end_text.isdigit():
            return False
        start = int(start_text)
        end = int(end_text)
        if start > end or start < 0 or end >= task_count:
            return False
    return True


def _controller_forcing_job_name_matches(job_name: str | None, expected_stage: str) -> bool:
    stage = str(job_name or "").strip().removeprefix("nhms_")
    return is_forcing_stage_name(stage) and _stages_match(stage, expected_stage)


def _query_forcing_controller_identity(
    *,
    scontrol: str,
    target_comment: str,
    expected_user: str,
    expected_account: str,
    expected_stage: str,
    expected_task_count: int,
) -> "CommentAccountingResult":
    """Find uniquely controller-retained forcing identity; never prove absence."""

    try:
        stdout = _bounded_visibility_stdout([scontrol, "show", "job", "-o"])
    except ReconcileQueryUnavailable as error:
        raise ReconcileQueryUnavailable(
            "controller forcing identity query unavailable",
            reason_class=error.reason_class,
        ) from error

    candidates: dict[str, dict[str, Any]] = {}
    identity_unproven = False
    for fields in _controller_job_records(stdout):
        if fields.get("Comment") != target_comment:
            continue
        raw_job_id = str(fields.get("JobId") or "")
        master_id = str(fields.get("ArrayJobId") or "")
        user = str(fields.get("UserId") or "").split("(", 1)[0]
        account = str(fields.get("Account") or "")
        job_name = str(fields.get("JobName") or "")
        submitted_at = _parse_fallback_submit_instant(fields.get("SubmitTime"))
        task_identity_matches = _controller_array_task_identity_is_compatible(
            fields.get("ArrayTaskId"),
            task_count=expected_task_count,
        )
        if (
            fields.get("_identity_unparseable") is not None
            or not SLURM_JOB_ID_RE.fullmatch(master_id)
            or not (
                SLURM_JOB_ID_RE.fullmatch(raw_job_id)
                or _fallback_master_id(raw_job_id) == master_id
            )
            or user != expected_user
            or account != expected_account
            or not _controller_forcing_job_name_matches(job_name, expected_stage)
            or not task_identity_matches
        ):
            identity_unproven = True
            continue
        identity = (user, account, job_name)
        candidate = candidates.setdefault(
            master_id,
            {
                "identity": identity,
                "raw_state": str(fields.get("JobState") or ""),
                "exit_code": str(fields.get("ExitCode") or "") or None,
                "submitted_at": submitted_at,
            },
        )
        if candidate["identity"] != identity or not candidate["raw_state"]:
            identity_unproven = True
            continue

    if identity_unproven or not candidates:
        raise ReconcileQueryUnavailable(
            "controller did not prove forcing identity",
            reason_class="comment_accounting_unproven",
        )
    records: list[SacctRecord] = []
    for master_id, candidate in candidates.items():
        records.append(
            SacctRecord(
                slurm_job_id=master_id,
                raw_state=str(candidate["raw_state"]),
                job_name=str(candidate["identity"][2]),
                exit_code=candidate["exit_code"],
                comment=target_comment,
                user=str(candidate["identity"][0]),
                account=str(candidate["identity"][1]),
                submitted_at=candidate["submitted_at"],
            )
        )
        if len(records) >= MAX_EXACT_COMMENT_MATCHES:
            break
    return CommentAccountingResult(
        tuple(records),
        scope="owner",
        controller_exact_comment=True,
    )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass
