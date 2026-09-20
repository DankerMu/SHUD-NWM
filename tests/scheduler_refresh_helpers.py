"""Shared fixture surface of the scheduler file-provider refresh suites.

Non-collectible support module (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Every name here builds or
drives a refresh run: the `RefreshConfig` factory and its #1513 explicit-mode
provider parents, the two provider-pipeline stubs, the four-lane tracked
transaction fixture, the #1080 registry-row / previous-canonical / declaration
builders and the direct gate driver.

Only definitions used by MORE THAN ONE partition live here; a fixture consumed
by a single suite stayed in that suite. The static receipt and classification
corpora are the sibling module `tests/scheduler_refresh_receipt_helpers.py`,
which this module imports (the dependency runs one way only).

Nothing here is a monkeypatch target: every `monkeypatch.setattr` in the corpus
names a PRODUCTION module (`scripts.scheduler_file_provider_refresh` as
`refresh`, `packages.common.provider_atomic`,
`services.orchestrator.scheduler_file_providers`), so design D1's repoint
obligation does not arise for this split -- the patch targets are
byte-identical to the monolith's.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
)
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination
from tests.scheduler_refresh_receipt_helpers import (
    _classification_stub,
    _enforced_cutover_gate,
)


def _config(tmp_path: Path) -> refresh.RefreshConfig:
    basins = tmp_path / "Basins"
    objects = tmp_path / "objects"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, objects, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (work, receipts, emergency, tmp_path / "private"):
        path.chmod(0o700)
    scheduler = objects / "scheduler"
    # Explicit mode, not the ambient umask (#1513): these three are the DIRECT
    # parents of the registry / readiness / state provider locks, and
    # `provider_atomic`'s gate is fail-closed on any 0o022 bit there. A bare
    # mkdir lands 0o775 under umask 0002 and every lane below raises
    # provider_lock_parent_unsafe. (`work` / `receipts` / `emergency` above are
    # already safe -- they are explicitly chmod-ed to 0o700.)
    make_directory_with_explicit_mode(scheduler / "registry")
    make_directory_with_explicit_mode(scheduler / "canonical-readiness")
    make_directory_with_explicit_mode(scheduler / "state-index")
    return refresh.RefreshConfig(
        basins_root=basins,
        registry_uri=str(scheduler / "registry" / "manifest-last.json"),
        readiness_uri=str(scheduler / "canonical-readiness" / "index-last.json"),
        state_uri=str(scheduler / "state-index" / "index-last.json"),
        object_store_root=objects,
        provider_store_root=objects,
        object_store_prefix="s3://nhms",
        workspace_root=work,
        receipt_root=receipts,
        emergency_root=emergency,
        refresh_lock=tmp_path / "private" / "refresh",
    )


def _minimal_registry_manifest_bytes(name: str) -> bytes:
    """Minimal but shape-valid manifest bytes for gate tests.

    #1080 gate parses the previous canonical manifest; opaque byte fixtures
    (b"registry\n") would fail as ``provider_invalid`` before the classifier
    can run.  Empty ``models`` array covers the "first publication / fresh
    inventory" case so the gate classifies everything as ``added``.
    """
    return json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "models": [],
            "checksum": f"sha256:{'0' * 64}",
            "note": name,
        },
        sort_keys=True,
    ).encode() + b"\n"


def _write_current_published_receipt(config: refresh.RefreshConfig) -> tuple[Path, dict[str, object]]:
    providers = []
    provider_paths = [("registry", config.registry_uri)]
    if config.worker_registry_uri is not None:
        provider_paths.append(("registry_worker_mirror", config.worker_registry_uri))
    provider_paths.extend((("readiness", config.readiness_uri), ("state", config.state_uri)))
    for name, uri in provider_paths:
        path = Path(uri)
        # Deliberately ambient-umask, NOT pinned (#1513, tasks.md §6 item 2).
        # These four lanes are receipt *preimage* surfaces: no caller of this
        # helper (three call sites, in
        # tests/test_scheduler_refresh_emergency_receipts.py and
        # tests/test_scheduler_refresh_receipt_block_presence.py) ever takes a
        # provider lock or publishes over them, and the only consumers are pure
        # reads -- `capture_scheduler_provider_preimage`
        # (scheduler_file_providers.py:1751) and `refresh.validate_current_receipt`
        # (`scripts/scheduler_refresh/providers.py`, whose sole provider touch
        # is another preimage capture). So neither gate is
        # reached and the landed modes are inert: under umask 0002 the files
        # come out 0o664 and the `registry_worker_mirror` parent 0o775 -- that
        # lane's parent is created ONLY here, whereas `registry`,
        # `canonical-readiness` and `state-index` arrive pre-pinned to 0o755
        # from `_config`. Left as-is on purpose; a future assertion that made
        # one of these lanes actually publish would redden on umask-0002 hosts
        # only, which is why the site is recorded as report-don't-fix.
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in {"registry", "registry_worker_mirror"}:
            # Use shape-valid manifest bytes so the #1080 gate can parse the
            # previous canonical without treating it as provider_invalid.
            path.write_bytes(_minimal_registry_manifest_bytes("registry"))
        else:
            path.write_text(name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = refresh._receipt(
        run_id="refresh_current",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=providers,
        registry_classification=_classification_stub(),
        # #1144: a `published` receipt without the audit block is rejected by
        # both validators, so the current-receipt fixture must carry one.
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )
    receipt_path = config.receipt_root / "latest.json"
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    return receipt_path, receipt


def _stub_provider_pipeline(monkeypatch: pytest.MonkeyPatch, *, committed: bool = True) -> None:
    del committed
    preimage = ProviderPreimage(exists=False)

    def capture(uri: str, *args: object, **kwargs: object) -> ProviderPreimage:
        del args, kwargs
        del uri
        return preimage

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(
        refresh,
        "_read_provider_header",
        lambda *args, **kwargs: {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-01T00:00:00Z",
            "checksum": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"entry": "valid"}], {"status": "ready", "entry_count": 26}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [
                {"model_id": f"model-{index}", "basin_id": f"basin-{index}"}
                for index in range(13)
            ],
        )
        return {
            "selected_model_count": 13,
            "registry": None
            if kwargs["dry_run"]
            else {"checksum": "sha256:" + "d" * 64, "model_count": 13},
        }

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "e" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        refresh,
        "publish_state_snapshot_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "f" * 64, "entry_count": 1},
    )


def _tracked_transaction_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_lane: str,
    conflict_lane: str = "",
    unowned_lane: str = "",
) -> tuple[refresh.RefreshConfig, dict[str, bytes], dict[str, Path]]:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    paths = {
        "registry": Path(config.registry_uri),
        "registry_worker_mirror": Path(config.worker_registry_uri),
        "readiness": Path(config.readiness_uri),
        "state": Path(config.state_uri),
    }
    # #1080 gate parses previous canonical registry bytes as JSON with a
    # models list; use shape-valid manifest content for the registry lanes.
    _valid_registry = _minimal_registry_manifest_bytes("previous")
    old = {
        "registry": _valid_registry,
        "registry_worker_mirror": _valid_registry,
        "readiness": b"old-readiness-generation",
        "state": b"old-state-generation",
    }
    for name, path in paths.items():
        # Explicit modes, not the ambient umask (#1513): lock parent and
        # provider destination both feed a fail-closed mode gate.
        make_directory_with_explicit_mode(path.parent)
        write_provider_destination(path, old[name])
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture_scheduler_provider_preimage)

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return (
                [{"entry": "valid"}],
                {"checksum": "sha256:" + "c" * 64},
                capture_scheduler_provider_preimage(paths["state"]),
            )

    def replace_bytes(
        name: str,
        content: bytes,
        expected: ProviderPreimage,
        commit_observer: object,
    ) -> dict[str, object]:
        if conflict_lane == name:
            atomic_replace_provider_bytes(
                paths[name],
                f"concurrent-authoritative-{name}".encode(),
                containment_root=(
                    config.object_store_root
                    if name == "registry_worker_mirror"
                    else config.provider_store_root
                ),
                max_bytes=refresh.MAX_READINESS_INDEX_BYTES,
                expected_preimage=expected,
            )
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        committed = atomic_replace_provider_bytes(
            paths[name],
            content,
            containment_root=(
                config.object_store_root
                if name == "registry_worker_mirror"
                else config.provider_store_root
            ),
            max_bytes=refresh.MAX_READINESS_INDEX_BYTES,
            expected_preimage=expected,
        )
        result = {
            "content_sha256": committed.sha256,
            "checksum": "sha256:" + "1" * 64,
            "generated_at": "2026-07-14T02:00:00Z",
            "model_count": 13,
        }
        if name in {"readiness", "state"}:
            result["entry_count"] = 1
        if unowned_lane != name:
            assert callable(commit_observer)
            commit_observer(committed)
        if fail_lane == name:
            raise refresh.RefreshError("provider_invalid", phase="postcommit")
        if unowned_lane == name:
            raise refresh.RefreshError("provider_invalid", phase="postcommit")
        return result

    def publish_mirror(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "registry_worker_mirror",
            b"new-registry-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        evidence = replace_bytes(
            "registry",
            b"new-registry-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("registry_commit_observer"),
        )
        return {
            "selected_model_count": 13,
            "registry": evidence,
            "packages": [],
        }

    def publish_readiness(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "readiness",
            b"new-readiness-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    def publish_state(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "state",
            b"new-state-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(refresh, "publish_scheduler_registry_manifest", publish_mirror)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(refresh, "publish_canonical_readiness_index", publish_readiness)
    monkeypatch.setattr(refresh, "publish_state_snapshot_index", publish_state)
    return config, old, paths


# ---------------------------------------------------------------------------
# #1080 Registry Cutover Gate — builders for the direct classification and
# refusal coverage.  #1101 put the cases themselves in
# tests/test_scheduler_refresh_cutover_gate.py and its sibling partitions, and
# moved only the row builders below into this helper.
# ---------------------------------------------------------------------------


def _registry_row(
    model_id: str,
    package_checksum: str,
    *,
    basin_id: str | None = None,
    basin_version_id: str = "v1",
    river_network_version_id: str = "r1",
    shud_code_version: str = "basins-shud",
    segment_count: int = 100,
    output_segment_count: int = 50,
    lifecycle_state: str = "active",
    source_inventory_checksum: str | None = None,
    resource_profile_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Shape-realistic registry row covering every identity field the classifier checks.

    Defaults deliberately mirror what
    ``scripts.publish_scheduler_file_registry.scheduler_registry_row_from_sources``
    emits (see #1080 finding C-C1) so tests express drift on any of the
    documented identity fields without having to reconstruct the whole row.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/v1/manifest.json",
        "package_checksum": package_checksum,
        "model_package_uri": f"s3://nhms/models/{model_id}/v1/package.tgz",
        "source_inventory_checksum": source_inventory_checksum
        or f"sha256:{'0' * 62}{ord(model_id[-1]) & 0xFF:02x}",
    }
    if resource_profile_extra:
        profile.update(resource_profile_extra)
    return {
        "model_id": model_id,
        "basin_id": basin_id or f"basin-{model_id}",
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "shud_code_version": shud_code_version,
        "segment_count": segment_count,
        "output_segment_count": output_segment_count,
        "lifecycle_state": lifecycle_state,
        "model_package_uri": f"s3://nhms/models/{model_id}/v1/package.tgz",
        "manifest_uri": f"s3://nhms/models/{model_id}/v1/manifest.json",
        "package_checksum": package_checksum,
        "resource_profile": profile,
    }


def _valid_previous_manifest(models: list[dict[str, object]]) -> bytes:
    """Shape-valid canonical manifest bytes for gate tests."""
    return json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "models": models,
            "checksum": f"sha256:{'0' * 64}",
        },
        sort_keys=True,
    ).encode() + b"\n"


def _write_previous_canonical(config: refresh.RefreshConfig, models: list[dict[str, object]]) -> Path:
    path = Path(config.registry_uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_valid_previous_manifest(models))
    return path


def _write_declaration(
    tmp_path: Path,
    *,
    generation: str,
    entries: list[dict[str, object]],
    generated_at: str = "2026-07-14T12:00:00Z",
) -> Path:
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": generated_at,
                "generation": generation,
                "entries": entries,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return declaration


def _run_gate(
    tmp_path: Path,
    config: refresh.RefreshConfig,
    *,
    prospective_models: list[dict[str, object]],
    previous_models: list[dict[str, object]] | None = None,
    declaration_path: Path | None = None,
    dry_run: bool = False,
    generated_at: refresh.datetime | None = None,
    now: refresh.datetime | None = None,
) -> tuple[list[dict[str, object]], Exception | None]:
    workspace = tmp_path / "gate-workspace"
    workspace.mkdir(exist_ok=True)
    generated_at = generated_at or refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    now = now or generated_at
    registry_path = Path(config.registry_uri)
    if previous_models is not None:
        # Only overwrite if the caller has NOT already staged a specific
        # bytes snapshot on disk.  Rewriting bumps mtime which round-2
        # tests (T8 / C-E9) need to survive across the gate call.
        if not registry_path.exists():
            _write_previous_canonical(config, previous_models)
        previous_bytes = registry_path.read_bytes()
        previous_sha = refresh.hashlib.sha256(previous_bytes).hexdigest()
    else:
        previous_bytes = None
        previous_sha = None
    captured: list[dict[str, object]] = []

    def sink(payload: dict[str, object]) -> None:
        captured.append(payload)

    caught: Exception | None = None
    try:
        refresh._registry_precommit_gate(
            workspace,
            [],
            prospective_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=previous_sha,
            prospective_generated_at=generated_at,
            cutover_declaration_env=str(declaration_path) if declaration_path else None,
            dry_run=dry_run,
            classification_sink=sink,
            now=now,
        )
    except Exception as error:  # noqa: BLE001 -- intentional
        caught = error
    return captured, caught


def _stub_provider_pipeline_with_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prospective_models: list[dict[str, object]],
) -> None:
    """Version of `_stub_provider_pipeline` that emits caller-provided rows.

    The default `_stub_provider_pipeline` bakes a 13-row prospective set; the
    round-2 tests need to drive the runner with fully-shaped registry rows so
    published receipts carry real classification content.
    """
    preimage = ProviderPreimage(exists=False)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", lambda *args, **kwargs: preimage)
    monkeypatch.setattr(
        refresh,
        "_read_provider_header",
        lambda *args, **kwargs: {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-01T00:00:00Z",
            "checksum": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"entry": "valid"}], {"status": "ready", "entry_count": 26}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})

    class _Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", _Repository)

    committed_sha_holder: dict[str, str] = {}

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](workspace, [], prospective_models)
        # Simulate a canonical commit by writing the manifest bytes so
        # downstream provider evidence has a real SHA to bind to.
        if not kwargs.get("dry_run"):
            registry_uri = Path(str(kwargs["registry_manifest"]))
            registry_uri.parent.mkdir(parents=True, exist_ok=True)
            content, sha = refresh._prospective_registry_content(
                prospective_models,
                generated_at=kwargs.get("registry_generated_at")
                or refresh.datetime.now(refresh.UTC),
            )
            registry_uri.write_bytes(content)
            committed_sha_holder["sha"] = sha
            observer = kwargs.get("registry_commit_observer")
            if observer is not None:
                observer(
                    ProviderPreimage(
                        exists=True,
                        sha256=sha,
                        inode=registry_uri.stat().st_ino,
                        size=len(content),
                    )
                )
            return {
                "selected_model_count": len(prospective_models),
                "registry": {
                    "checksum": f"sha256:{sha}",
                    "model_count": len(prospective_models),
                    "content_sha256": sha,
                    "entry_count": len(prospective_models),
                },
                "packages": [],
            }
        return {
            "selected_model_count": len(prospective_models),
            "registry": None,
            "packages": [],
        }

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "e" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        refresh,
        "publish_state_snapshot_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "f" * 64, "entry_count": 1},
    )
