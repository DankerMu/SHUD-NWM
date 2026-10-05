"""Back up and publish both manifests, and put back what was committed when the run does not publish.

Part of ``scripts/node22_publish_merged_scheduler_registry.py`` (the entry point).
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC
from pathlib import Path
from typing import Any, NoReturn

from packages.common import succession_receipt as succession
from packages.common.provider_atomic import (
    SHARED_PROVIDER_MODE,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
)
from scripts.merged_registry_publish.model import (
    _NOTHING_WRITTEN,
    _PROVIDER_ERRORS,
    APPLY_RECEIPT_NAME,
    MergedRegistryPublishError,
    _Destination,
    _manifest_byte_cap,
    _Plan,
    _Settings,
)
from scripts.merged_registry_publish.planning import _plan
from scripts.merged_registry_publish.receipts import _load_dry_run_receipt, _receipt, _require_same_as_dry_run
from services.orchestrator.scheduler_file_providers import publish_scheduler_registry_manifest

# --- apply -------------------------------------------------------------------


def _write_backup(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, SHARED_PROVIDER_MODE), "wb") as handle:
        os.fchmod(handle.fileno(), SHARED_PROVIDER_MODE)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _current_sha256(destination: _Destination) -> str | None:
    try:
        return capture_provider_preimage(
            destination.path, containment_root=destination.containment_root, max_bytes=_manifest_byte_cap()
        ).sha256
    except _PROVIDER_ERRORS:
        return None


def _put_back(destination: _Destination, committed: ProviderPreimage | None) -> tuple[str | None, str]:
    """Return ``(current sha256, state)`` with state ``unchanged`` / ``restored`` or what went wrong.

    A destination this run committed is restored against the preimage its
    publish committed, never against a re-captured one: if anybody rewrote it
    since, the compare-and-swap fails and those bytes stay.
    """

    current = _current_sha256(destination)
    if current == destination.sha256:
        return current, "unchanged"
    if committed is None:
        return current, "changed, but not by a commit this run observed; left as it is"
    try:
        atomic_replace_provider_bytes(
            destination.path,
            destination.content,
            containment_root=destination.containment_root,
            max_bytes=_manifest_byte_cap(),
            expected_preimage=committed,
        )
    except _PROVIDER_ERRORS as error:
        reason = getattr(error, "reason", None) or str(error)
        return _current_sha256(destination), f"committed by this run, could not be restored ({reason}); left as it is"
    current = _current_sha256(destination)
    if current != destination.sha256:
        return current, "restored, but the read-back differs from the bytes read"
    return current, "restored"


class TerminatedBySignal(BaseException):
    """SIGTERM or SIGHUP during an apply; like the interrupt it is handled as, not an ``Exception``."""


@contextlib.contextmanager
def _termination_signals_raise() -> Iterator[Callable[[], None]]:
    """Make SIGTERM and SIGHUP raise ``TerminatedBySignal`` once, until the function yielded is called.

    The apply calls it when its outcome is decided: from then on (while what was
    committed is put back, and while the receipt is written) a signal is
    ignored instead of stopping that half-way.  The handlers found are put back
    on leaving.  A signal that is ignored on entry (SIGHUP under ``nohup``) stays
    ignored.  Outside the main thread no handler can be installed, and none is.
    """

    armed = [True]

    def handler(signum: int, _frame: Any) -> None:
        if armed:
            armed.clear()
            raise TerminatedBySignal(signal.Signals(signum).name)

    previous: dict[int, Any] = {}
    try:
        try:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                if signal.getsignal(signum) != signal.SIG_IGN:
                    previous[signum] = signal.signal(signum, handler)
        except ValueError:
            pass  # not the main thread: the apply runs with the handlers that are there
        yield armed.clear
    finally:
        for signum, found in previous.items():
            # None is a handler that was not installed from Python and cannot be put back as such.
            signal.signal(signum, signal.SIG_DFL if found is None else found)


def _backups_text(backups: Mapping[str, Path]) -> str:
    paths = [str(backups[name]) for name in ("canonical", "mirror") if name in backups]
    return f"Backups: {', '.join(paths)}." if paths else "No backup was written."


def _failure(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    return f"{type(error).__name__}: {reason or error}"


def _apply(settings: _Settings) -> dict[str, Any]:
    """Publish both manifests; the caller holds the provider refresh lock."""

    dry_run, dry_run_record = _load_dry_run_receipt(settings)
    dry_run_path = dry_run_record["path"]
    _require_same_as_dry_run(dry_run, dry_run_path, "operations", settings.operations.record())
    _require_same_as_dry_run(dry_run, dry_run_path, "provision_succession_id", settings.provision_succession_id)
    succession_dir = settings.receipt_root / str(settings.succession_id)
    apply_target = succession_dir / APPLY_RECEIPT_NAME
    # The receipt directory is checked before any backup or write.
    succession.prepare_receipt_target(apply_target, receipt_root=settings.receipt_root)

    plan = _plan(settings)
    # The receipt's sha256 pins the new-rows registry too: the plan refuses a registry the receipt did not record.
    # A run that introduces no row has no provision receipt on either side.
    _require_same_as_dry_run(dry_run, dry_run_path, "provision_apply_receipt", plan.provision_apply_receipt)
    _require_same_as_dry_run(dry_run, dry_run_path, "canonical_models_sha256_before", plan.canonical_models_sha256)
    _require_same_as_dry_run(dry_run, dry_run_path, "merged_model_ids", plan.merged_model_ids)

    started = settings.clock()
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    destinations = (plan.canonical, plan.mirror)
    backups = {
        item.name: item.path.with_name(f"{item.path.name}.bak-{settings.succession_id}-{stamp}")
        for item in destinations
    }
    failed_target = succession_dir / f"publish-apply-failed-{stamp}.json"
    taken = [str(path) for path in (*backups.values(), failed_target) if os.path.lexists(path)]
    if taken:
        raise MergedRegistryPublishError(
            f"--apply refused: {taken} already exist(s) for the stamp {stamp} of this second; a backup or a "
            f"receipt is never overwritten. Run the same command again. {_NOTHING_WRITTEN}"
        )

    # From here on every attempt that does not publish leaves a failed-apply receipt.
    committed: dict[str, ProviderPreimage] = {}
    written: dict[str, Path] = {}  # the backups that were completely written

    def observer(name: str) -> Callable[[ProviderPreimage], None]:
        def observe(value: ProviderPreimage) -> None:
            committed[name] = ProviderPreimage.from_value(value)

        return observe

    manifest_generated_at: str | None = None
    with _termination_signals_raise() as outcome_decided:
        try:
            for item in destinations:
                try:
                    # The very bytes whose preimage the compare-and-swap below is made against.
                    _write_backup(backups[item.name], item.content)
                except FileExistsError:
                    raise  # somebody else's file, created since the check above
                except BaseException:
                    # Nothing was at this path before this run: what is there now is a partial backup.
                    with contextlib.suppress(OSError):
                        backups[item.name].unlink()
                    raise
                written[item.name] = backups[item.name]
            for item in destinations:
                published = publish_scheduler_registry_manifest(
                    plan.merged_rows,
                    item.path,
                    object_store_root=settings.object_store_root,
                    object_store_prefix=settings.object_store_prefix,
                    require_direct_grid=True,
                    generated_at=started,
                    expected_preimage=item.preimage,
                    commit_observer=observer(item.name),
                )
                manifest_generated_at = str(published["generated_at"])
            after = {
                item.name: capture_provider_preimage(
                    item.path, containment_root=item.containment_root, max_bytes=_manifest_byte_cap()
                )
                for item in destinations
            }
            if not all(
                item.name in committed and after[item.name].sha256 == committed[item.name].sha256
                for item in destinations
            ) or after["canonical"].sha256 != after["mirror"].sha256:
                raise MergedRegistryPublishError(
                    "the read-back after both publishes differs: canonical sha256 "
                    f"{after['canonical'].sha256}, mirror sha256 {after['mirror'].sha256}"
                )
            outcome_decided()
        except BaseException as error:
            # Also an interrupt or a termination signal: a canonical manifest published without its
            # mirror must not be left behind.
            outcome_decided()
            _fail_apply(
                settings,
                plan,
                error,
                committed=committed,
                backups=written,
                failed_target=failed_target,
                dry_run_record=dry_run_record,
            )

        sha256_after = {name: preimage.sha256 for name, preimage in after.items()}
        receipt = _receipt(
            settings,
            plan,
            dry_run=False,
            outcome="published",
            sha256_after=sha256_after,
            backups=written,
            manifest_generated_at=manifest_generated_at,
            manifest_bytes=after["canonical"].size,
            dry_run_receipt=dry_run_record,
        )
        try:
            succession.write_receipt(apply_target, receipt)
        except OSError as error:
            # Nothing is rolled back: the publish is complete, only its receipt is missing.
            raise MergedRegistryPublishError(
                f"The publish is complete but unreceipted: both manifests were published and read back equal "
                f"(sha256 {sha256_after['canonical']}, manifest_generated_at {manifest_generated_at}), but the apply "
                f"receipt {apply_target} could NOT be written: {error}. Nothing was rolled back. Backups of the "
                f"previous bytes: {written['canonical']} and {written['mirror']}. Do not re-run this succession; "
                "record these values by hand and continue with the provider refresh."
            ) from error
    print(f"Publish receipt written: {apply_target}", file=sys.stderr)
    return receipt


def _fail_apply(
    settings: _Settings,
    plan: _Plan,
    error: BaseException,
    *,
    committed: Mapping[str, ProviderPreimage],
    backups: Mapping[str, Path],
    failed_target: Path,
    dry_run_record: Mapping[str, Any],
) -> NoReturn:
    """Put back what this run committed, write the failed-apply receipt and raise the outcome.

    ``backups`` holds only the backups that were completely written.
    """

    states: dict[str, str] = {}
    sha256_after: dict[str, str | None] = {}
    # Mirror first: the reverse of the publish order.
    for item in (plan.mirror, plan.canonical):
        sha256_after[item.name], states[item.name] = _put_back(item, committed.get(item.name))
    back = all(state in {"unchanged", "restored"} for state in states.values())
    conflict = getattr(error, "reason", None) == "provider_preimage_changed"
    if back:
        outcome = "rolled_back" if committed else "refused"
    elif conflict and not committed:
        # The compare-and-swap refused the first write: this run changed neither manifest.
        outcome = "refused"
    else:
        outcome = "inconsistent"
    summary = (
        f"canonical {plan.canonical.path}: {states['canonical']} (sha256 now {sha256_after['canonical']}, "
        f"read {plan.canonical.sha256}); mirror {plan.mirror.path}: {states['mirror']} (sha256 now "
        f"{sha256_after['mirror']}, read {plan.mirror.sha256})"
    )
    reason = f"{_failure(error)}. {summary}"
    receipt = _receipt(
        settings,
        plan,
        dry_run=False,
        outcome=outcome,
        sha256_after=sha256_after,
        backups=backups,
        dry_run_receipt=dry_run_record,
        reason=reason,
    )
    try:
        succession.write_receipt(failed_target, receipt)
        receipted = f"Receipt: {failed_target}."
    except OSError as receipt_error:
        receipted = f"The failed-apply receipt {failed_target} could NOT be written: {receipt_error}."
    if outcome == "refused":
        consequence = (
            "This run changed neither manifest"
            + (" (the canonical manifest was changed by another writer since it was read)" if conflict else "")
            + f". The same --succession-id can be retried. {_backups_text(backups)}"
        )
    elif outcome == "rolled_back":
        consequence = (
            "Every manifest this run committed was restored to the bytes read; both are back at the previous "
            f"generation. The same --succession-id can be retried. {_backups_text(backups)}"
        )
    elif len(backups) == 2:
        consequence = (
            "The two manifests may now DIFFER, and the workers refuse to submit while they do. Two ways out: "
            f"(1) restore both from this run's backups (copy {backups['canonical']} over {plan.canonical.path} "
            f"and {backups['mirror']} over {plan.mirror.path}), or (2) publish the mirror: run the provider "
            "refresh, which republishes the canonical rows to both. Compare the two sha256 before and after."
        )
    else:
        consequence = (
            "The two manifests may now DIFFER, and the workers refuse to submit while they do. This run did not "
            f"back up both manifests ({_backups_text(backups)}), so the way out is to publish the mirror: run the "
            "provider refresh, which republishes the canonical rows to both. Compare the two sha256 before and "
            "after."
        )
    raise MergedRegistryPublishError(
        f"--apply {outcome}: {reason}. {consequence} {receipted}",
        receipt=receipt,
        receipt_path=failed_target,
    ) from error
