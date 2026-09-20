"""CLI-only support for the manual publisher entrypoint.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100. The
argparse parser and ``main`` itself stay at the historical path (the parser's
``description`` is that module's ``__doc__``, and it is the console entrypoint);
what lives here is the defaulting and the #1080 cutover-gate wiring ``main``
calls.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from packages.scheduler.registry_audit import SchedulerRegistryPublishError


def _default_registry_manifest() -> str:
    value = os.getenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", "").strip()
    if not value:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_MANIFEST_MISSING",
            "NHMS_SCHEDULER_REGISTRY_MANIFEST or --registry-manifest is required.",
        )
    return value

def _default_work_dir() -> str:
    root = os.getenv("WORKSPACE_ROOT") or os.getenv("NHMS_SCHEDULER_TEMP_ROOT") or ".nhms-work"
    return str(Path(root) / "scheduler" / "basins-file-registry-publish")

def _cutover_declaration_present(env_value: str | None) -> bool:
    """Return True when the cutover declaration env resolves to a readable file.

    R2-A1: the CLI records this fact on the summary so a later auditor can tell
    whether the operator staged a declaration at all before running the
    publisher — a "gate enforced but no declaration" run is materially
    different from a "gate enforced and declaration file was found" run.

    Any error (missing env, not a regular file, permission denied) collapses
    to ``False``.  The gate itself does the strict schema validation; this
    helper only proves the operator staged a file we could read.
    """
    if not env_value:
        return False
    path = Path(env_value).expanduser()
    try:
        # Explicit no-follow: symlinks are already rejected by the gate; the
        # audit fact should reflect the same rejection.
        stat_result = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(stat_result.st_mode):
        return False
    return os.access(str(path), os.R_OK)

def _build_manual_cutover_gate(
    *,
    registry_manifest: str | Path,
    dry_run: bool,
) -> Callable[
    [Path, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], None
]:
    """Wire the #1080 registry cutover gate for the manual CLI (finding C-D1).

    Lazy import of the refresh module keeps this file free of a top-level
    dependency cycle (`scheduler_file_provider_refresh` already imports from
    this module).  The manual CLI runs outside the refresh runner's own lock
    coordination, but the gate itself is stateless — it snapshots the
    previous canonical bytes inside the gate call and returns the same
    classification refusal that the runner path would produce.
    """
    from datetime import UTC, datetime  # local — CLI-only path.

    from scripts import scheduler_file_provider_refresh as refresh

    manifest_path = Path(registry_manifest).expanduser()

    def gate(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        # Load the previous canonical bytes; missing file legitimately
        # bootstraps and returns None.  We deliberately hand only the raw
        # bytes forward so the gate's own parser stays the source of truth.
        try:
            previous = refresh._load_previous_canonical_registry(
                str(manifest_path),
                containment_root=manifest_path.parent,
            )
        except refresh.RefreshError as error:
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry could not be read for the cutover gate.",
                details={
                    "provider_reason": error.reason,
                    "provider_phase": "precommit",
                },
            ) from error
        previous_sha: str | None
        previous_bytes: bytes | None
        if previous is None:
            previous_sha = None
            previous_bytes = None
        else:
            previous_sha, _previous_models, previous_bytes = previous
        cutover_env = os.getenv(refresh.CUTOVER_DECLARATION_ENV, "").strip() or None
        # Manual CLI does not need to bind classification to a receipt; the
        # sink still needs to be a no-op callable.
        def classification_sink(payload: dict[str, Any]) -> None:
            del payload
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=previous_sha,
            prospective_generated_at=datetime.now(UTC),
            cutover_declaration_env=cutover_env,
            dry_run=dry_run,
            classification_sink=classification_sink,
        )

    return gate
