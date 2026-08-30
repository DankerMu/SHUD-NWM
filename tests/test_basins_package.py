"""#1832: the package manifest is where a calibration override is recorded.

#1816's most damning measurement was that of eight silently rewritten packages
exactly one publish receipt survived, in a scratch directory: the receipt lives
in the publisher workspace, the package itself said nothing.  These tests pin
the seam that fixes that -- ``publish_basins_package`` carries the applied
overrides into ``manifest["calibration"]["overrides"]``, which travels with the
bytes -- and the absence rule that keeps "not overridden" readable.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.provision_direct_grid_scheduler_registry as direct_grid
import scripts.publish_scheduler_file_registry as scheduler_registry
import workers.model_registry.basins_package as basins_package
from packages.common.object_store import LocalObjectStore
from tests.test_basins_package_publication import _object_store_env, _write_valid_inventory
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import _argparse_main

_HETIANHE_OVERRIDE = {
    "basin_slug": "basin-a",
    "parameter": "GEOL_DMAC",
    "value": "4",
    "source_value": "5",
    "reason": "GEOL_DMAC 5 and 4.75 both produce NaN / EXIT 10; 4.5 and 4 run clean.",
    "approver": "danker",
    "date": "2026-08-24",
    "relative_path": "input/alias-a/alias-a.cfg.calib",
    "sha256": "0" * 64,
}


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_name: str,
    calibration_text: str | None = None,
    calibration_overrides: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], Path]:
    run_root = tmp_path / run_name
    inventory_path, model_id = _write_valid_inventory(run_root, calibration_count=1)
    if calibration_text is not None:
        calib = run_root / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.calib"
        calib.write_text(calibration_text, encoding="utf-8")
        # Re-discover so the inventory's checksums match the edited bytes, the
        # same way the publisher re-discovers from its staging copy.
        write_inventory(discover_basins_inventory(run_root / "basins"), inventory_path)
    _object_store_env(run_root, monkeypatch)
    model = json.loads(inventory_path.read_text(encoding="utf-8"))["models"][0]
    identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    kwargs: dict[str, object] = {}
    if calibration_overrides is not None:
        kwargs["calibration_overrides"] = calibration_overrides
    basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version=scheduler_registry.package_version_for_model(model, source_identity=identity),
        output_path=run_root / "manifest.json",
        **kwargs,
    )
    return (
        json.loads((run_root / "manifest.json").read_text(encoding="utf-8")),
        run_root / "object-store",
    )


def test_package_without_declared_overrides_carries_no_override_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §2.4: absence is meaningful, so there must be no key at all."""
    manifest, _object_root = _publish(tmp_path, monkeypatch, run_name="plain")

    calibration = manifest["calibration"]
    assert "overrides" not in calibration, calibration
    # Guard against a vacuous pass: the calibration block itself is present.
    assert calibration["included_count"] >= 1


def test_empty_override_list_is_not_recorded_as_an_empty_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list must be indistinguishable from "nothing was declared".

    ``[]`` in the manifest would read as "the publisher considered overrides
    and applied none", which is a claim this publisher is not entitled to make
    for a basin it never staged.
    """
    manifest, _object_root = _publish(tmp_path, monkeypatch, run_name="empty", calibration_overrides=[])

    assert "overrides" not in manifest["calibration"]


def test_manifest_records_parameter_value_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §2.3 / spec scenario 2, third clause."""
    manifest, object_root = _publish(
        tmp_path,
        monkeypatch,
        run_name="declared",
        calibration_text="GEOL_KSATH\t0.00977999747288218\nGEOL_DMAC\t4\nSOIL_ALPHA\t8.19327372615961\n",
        calibration_overrides=[_HETIANHE_OVERRIDE],
    )

    overrides = manifest["calibration"]["overrides"]
    assert len(overrides) == 1
    recorded = overrides[0]
    assert recorded["parameter"] == "GEOL_DMAC"
    assert recorded["value"] == "4"
    assert "NaN" in recorded["reason"]
    assert recorded["source_value"] == "5"
    # The record must be in the STORED manifest, not just the local receipt:
    # the stored copy is the one that travels with the package.
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    stored = json.loads(store.read_bytes(str(manifest["manifest_uri"])).decode("utf-8"))
    assert stored["calibration"]["overrides"] == overrides


def test_applying_an_override_re_derives_the_package_and_model_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec scenario 4: the same package with and without the override differs.

    That is the intended cost (design D4), not a defect: a package with a
    different calibration IS a different package, and the ``dg_*`` model id is
    seeded from ``package_checksum``.
    """
    source_text = "GEOL_KSATH\t0.00977999747288218\nGEOL_DMAC\t5\nSOIL_ALPHA\t8.19327372615961\n"
    overridden_text = source_text.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")

    plain, _plain_root = _publish(
        tmp_path, monkeypatch, run_name="identity-source", calibration_text=source_text
    )
    overridden, _overridden_root = _publish(
        tmp_path,
        monkeypatch,
        run_name="identity-override",
        calibration_text=overridden_text,
        calibration_overrides=[_HETIANHE_OVERRIDE],
    )

    assert plain["package_checksum"] != overridden["package_checksum"]
    assert plain["version"] != overridden["version"]

    snapshot = SimpleNamespace(grid_id="gfs-0p25", grid_signature="sig")
    identities = {
        direct_grid._package_identity(
            {"model_id": manifest["model_id"], "package_checksum": manifest["package_checksum"]},
            "gfs",
            snapshot,
        )
        for manifest in (plain, overridden)
    }
    assert len(identities) == 2, identities


_REPO_ROOT = Path(__file__).resolve().parents[1]
# #1910: frozen pre-split facade contract as a tracked deterministic literal.
# This is the complete non-dunder name list plus every callable signature captured
# at baseline SHA 44fcd416. Inline so module collection needs no external data.
_BASELINE_FACADE_NAMES = (
'Any',
'BASINS_MIGRATION_REPORT_SCHEMA_VERSION',
'BASINS_PACKAGE_SCHEMA_VERSION',
'BASINS_PACKAGE_SCHEMA_VERSION_V1',
'BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION',
'BasinsDiscoveryError',
'BasinsPackageError',
'BinaryIO',
'Callable',
'ENOENT',
'FORCING_SAMPLE_BYTE_LIMIT',
'FORCING_SAMPLE_FILE_LIMIT',
'FORCING_SAMPLE_LINE_LIMIT',
'GIS_REQUIRED_FILES',
'Iterator',
'LocalObjectStore',
'MAX_EXISTING_MANIFEST_BYTES',
'MAX_OBJECT_MANIFEST_BYTES',
'Mapping',
'ObjectStoreError',
'ObjectStoreParent',
'Path',
'SHUD_REQUIRED_PATTERNS',
'SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS',
'Sequence',
'SourceFile',
'UTC',
'_OS_MKDIR_SUPPORTS_DIR_FD',
'_OS_OPENAT_OBJECT_STORE_AVAILABLE',
'_OS_OPEN_SUPPORTS_DIR_FD',
'_OS_RENAME_SUPPORTS_DIR_FD',
'_OS_STAT_SUPPORTS_DIR_FD',
'_OS_STAT_SUPPORTS_FOLLOW_SYMLINKS',
'_OS_UNLINK_SUPPORTS_DIR_FD',
'_acquire_publish_lock',
'_basins_slug_id',
'_calibration_metadata',
'_canonical_basin_slug_from_source_path',
'_canonical_shud_required_file_name',
'_classify_basins_root_metadata',
'_csv_time_evidence',
'_directory_evidence',
'_directory_uri',
'_ensure_inventory_path_matches_expected',
'_ensure_under_root',
'_ensure_under_source_root',
'_expected_forcing_dir',
'_expected_input_dir',
'_find_publishable_model',
'_forcing_checksum_material',
'_forcing_metadata',
'_forcing_metadata_from_written_entries',
'_is_ignored_source_path',
'_json_bytes',
'_manifest_file_entry',
'_manifest_file_entry_for_source_file',
'_manifest_payload_without_self_entry',
'_manifest_with_manifest_entry',
'_migration_source_file_evidence',
'_normalize_relative_path',
'_object_cloexec_flag',
'_object_exists_no_symlinks',
'_object_key_parts',
'_object_no_follow_flag',
'_object_os_open',
'_object_os_replace',
'_object_os_stat',
'_object_os_unlink',
'_object_parent_for_existing_read',
'_object_parent_for_existing_write',
'_object_parent_for_write',
'_object_path_component_is_symlink',
'_object_path_for_key',
'_object_path_rejecting_symlinks',
'_object_path_unsafe_error',
'_object_size_and_checksum_streaming',
'_object_store_from_env',
'_open_object_file_no_symlinks',
'_open_object_parent_at',
'_open_verified_source_file',
'_open_verified_source_file_at',
'_optional_shud_runtime_files',
'_package_source_files',
'_planned_file_entry',
'_preflight_json_output_path',
'_preflight_object_store_keys',
'_read_existing_manifest',
'_read_inventory',
'_read_object_bytes_no_symlinks',
'_recorded_relative_inventory_root',
'_reject_source_symlink_path',
'_relative_inventory_path_matches_expected',
'_release_publish_lock',
'_remove_object_temp_path',
'_resolve_package_path',
'_resolved_inventory_root',
'_resolved_source_root',
'_safe_source_dir',
'_safe_source_file',
'_sha256_bytes',
'_sha256_file',
'_sha256_handle',
'_sha256_json',
'_source_dir_from_relative_inventory_value',
'_source_file_evidence',
'_source_file_for_package',
'_source_file_size',
'_source_identity_from_plan',
'_success_payload',
'_validate_object_key_segment',
'_validated_canonical_required_source_files',
'_verified_source_file_evidence',
'_verify_existing_manifest_consistency',
'_verify_expected_source_identity',
'_verify_model_id_matches_canonical_identity',
'_verify_object_bytes',
'_walk_source_files',
'_write_bytes_to_store_atomic',
'_write_file_to_store_streaming',
'_write_json_file',
'_write_source_file_to_store',
'annotations',
'basins_package_source_identity',
'contextmanager',
'dataclass',
'datetime',
'discover_basins_inventory',
'fnmatchcase',
'forcing_checksum_material_for_schema_version',
'hashlib',
'json',
'os',
'publish_basins_package',
'stat',
'uuid',
'validate_object_path',
'write_basins_migration_report',
)

_BASELINE_FACADE_SIGNATURES = {
    'Any': '(*args, **kwargs)',
    'BasinsDiscoveryError': "(error_code: 'str', message: 'str', *, path: 'str | None' = None) -> 'None'",
    'BasinsPackageError': (
        "(error_code: 'str', message: 'str', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, path: 'str | None' = None, manifest_"
        "uri: 'str | None' = None, details: 'dict[str, Any] | None' = None) -"
        "> 'None'"
    ),
    'BinaryIO': '()',
    'Iterator': '()',
    'LocalObjectStore': "(root: 'Path | str', object_store_prefix: 'str' = '') -> None",
    'Mapping': '()',
    'ObjectStoreParent': "(path: 'Path', name: 'str', parent_fd: 'int | None' = None) -> None",
    'Path': '(*args, **kwargs)',
    'Sequence': '()',
    'SourceFile': (
        "(source_path: 'Path', source_root: 'Path', relative_path: 'str', obj"
        "ect_key: 'str', object_uri: 'str', role: 'str') -> None"
    ),
    '_acquire_publish_lock': (
        "(store: 'LocalObjectStore', lock_key: 'str', model_id: 'str', versio"
        "n: 'str', manifest_uri: 'str') -> 'None'"
    ),
    '_basins_slug_id': "(value: 'str') -> 'str'",
    '_calibration_metadata': (
        "(model: 'dict[str, Any]', included_files: 'list[dict[str, Any]]', ca"
        "libration_overrides: 'Sequence[Mapping[str, Any]] | None' = None) ->"
        " 'dict[str, Any]'"
    ),
    '_canonical_basin_slug_from_source_path': "(model: 'dict[str, Any]', model_id: 'str', version: 'str') -> 'str'",
    '_canonical_shud_required_file_name': "(input_name: 'str', pattern: 'str') -> 'str'",
    '_classify_basins_root_metadata': "(root: 'Path', *, error_prefix: 'str' = 'BASINS_ROOT') -> 'bool'",
    '_csv_time_evidence': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'tuple[str | None, str | None, str | None, int]'"
    ),
    '_directory_evidence': "(root: 'Path') -> 'tuple[int, int, str]'",
    '_directory_uri': "(object_store: 'LocalObjectStore', key: 'str') -> 'str'",
    '_ensure_inventory_path_matches_expected': (
        "(actual: 'Path', expected: 'Path', field_name: 'str', *, model_id: '"
        "str | None' = None, version: 'str | None' = None) -> 'None'"
    ),
    '_ensure_under_root': (
        "(path: 'Path', root: 'Path', *, error_code: 'str', message: 'str', m"
        "odel_id: 'str | None' = None, version: 'str | None' = None, manifest"
        "_uri: 'str | None' = None) -> 'None'"
    ),
    '_ensure_under_source_root': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'None'"
    ),
    '_expected_forcing_dir': (
        "(model: 'dict[str, Any]', source_root: 'Path', *, model_id: 'str', v"
        "ersion: 'str') -> 'Path'"
    ),
    '_expected_input_dir': (
        "(model: 'dict[str, Any]', source_root: 'Path', *, model_id: 'str', v"
        "ersion: 'str') -> 'Path'"
    ),
    '_find_publishable_model': (
        "(inventory: 'dict[str, Any]', model_id: 'str', version: 'str') -> 'd"
        "ict[str, Any]'"
    ),
    '_forcing_checksum_material': "(forcing: 'Mapping[str, Any]') -> 'dict[str, Any]'",
    '_forcing_metadata': (
        "(*, model: 'dict[str, Any]', inventory_root: 'Path', inventory_relat"
        "ive_root: 'Path | None', source_root: 'Path', object_store: 'LocalOb"
        "jectStore | None', forcing_key: 'str', copy_forcing: 'bool', model_i"
        "d: 'str', version: 'str', manifest_uri: 'str | None' = None) -> 'tup"
        "le[dict[str, Any], list[SourceFile]]'"
    ),
    '_forcing_metadata_from_written_entries': (
        "(forcing: 'dict[str, Any]', included_files: 'list[dict[str, Any]]') "
        "-> 'dict[str, Any]'"
    ),
    '_is_ignored_source_path': "(path: 'Path') -> 'bool'",
    '_json_bytes': "(payload: 'dict[str, Any]') -> 'bytes'",
    '_manifest_file_entry': (
        "(*, object_store: 'LocalObjectStore', manifest_key: 'str', content_b"
        "ytes: 'bytes', final_size_bytes: 'int') -> 'dict[str, Any]'"
    ),
    '_manifest_file_entry_for_source_file': (
        "(source_file: 'SourceFile', *, size_bytes: 'int', sha256: 'str') -> "
        "'dict[str, Any]'"
    ),
    '_manifest_payload_without_self_entry': "(manifest: 'dict[str, Any]') -> 'dict[str, Any]'",
    '_manifest_with_manifest_entry': (
        "(manifest_without_self_entry: 'dict[str, Any]', included_files: 'lis"
        "t[dict[str, Any]]', *, object_store: 'LocalObjectStore', manifest_ke"
        "y: 'str') -> 'tuple[dict[str, Any], bytes]'"
    ),
    '_migration_source_file_evidence': "(path: 'Path', source_root: 'Path') -> 'tuple[int, str]'",
    '_normalize_relative_path': "(value: 'str') -> 'str'",
    '_object_cloexec_flag': "() -> 'int'",
    '_object_exists_no_symlinks': (
        "(store: 'LocalObjectStore', key: 'str', *, model_id: 'str', version:"
        " 'str', manifest_uri: 'str') -> 'bool'"
    ),
    '_object_key_parts': "(store: 'LocalObjectStore', key_or_uri: 'str') -> 'tuple[str, tuple[str, ...]]'",
    '_object_no_follow_flag': "() -> 'int'",
    '_object_os_open': "(name: 'str', flags: 'int', mode: 'int', target: 'ObjectStoreParent') -> 'int'",
    '_object_os_replace': "(source_name: 'str', target_name: 'str', target: 'ObjectStoreParent') -> 'None'",
    '_object_os_stat': "(name: 'str', target: 'ObjectStoreParent') -> 'os.stat_result'",
    '_object_os_unlink': "(name: 'str', target: 'ObjectStoreParent') -> 'None'",
    '_object_parent_for_existing_read': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, model_id: 'str | N"
        "one' = None, version: 'str | None' = None, manifest_uri: 'str | None"
        "' = None) -> 'Iterator[ObjectStoreParent]'"
    ),
    '_object_parent_for_existing_write': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, model_id: 'str | N"
        "one' = None, version: 'str | None' = None, manifest_uri: 'str | None"
        "' = None) -> 'Iterator[ObjectStoreParent]'"
    ),
    '_object_parent_for_write': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, model_id: 'str | N"
        "one' = None, version: 'str | None' = None, manifest_uri: 'str | None"
        "' = None) -> 'Iterator[ObjectStoreParent]'"
    ),
    '_object_path_component_is_symlink': "(path: 'Path') -> 'bool'",
    '_object_path_for_key': "(store: 'LocalObjectStore', key_or_uri: 'str') -> 'Path'",
    '_object_path_rejecting_symlinks': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, model_id: 'str | N"
        "one' = None, version: 'str | None' = None, manifest_uri: 'str | None"
        "' = None) -> 'Path'"
    ),
    '_object_path_unsafe_error': (
        "(path: 'Path', *, model_id: 'str | None' = None, version: 'str | Non"
        "e' = None, manifest_uri: 'str | None' = None) -> 'BasinsPackageError"
        "'"
    ),
    '_object_size_and_checksum_streaming': (
        "(store: 'LocalObjectStore', key: 'str', *, model_id: 'str | None' = "
        "None, version: 'str | None' = None, manifest_uri: 'str | None' = Non"
        "e) -> 'tuple[int, str]'"
    ),
    '_object_store_from_env': "(*, model_id: 'str', version: 'str') -> 'LocalObjectStore'",
    '_open_object_file_no_symlinks': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, model_id: 'str | N"
        "one' = None, version: 'str | None' = None, manifest_uri: 'str | None"
        "' = None) -> 'Iterator[BinaryIO]'"
    ),
    '_open_object_parent_at': (
        "(store: 'LocalObjectStore', key_or_uri: 'str', *, create: 'bool', mo"
        "del_id: 'str | None' = None, version: 'str | None' = None, manifest_"
        "uri: 'str | None' = None) -> 'ObjectStoreParent'"
    ),
    '_open_verified_source_file': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'BinaryIO'"
    ),
    '_open_verified_source_file_at': (
        "(resolved: 'Path', source_root: 'Path', *, model_id: 'str | None' = "
        "None, version: 'str | None' = None, manifest_uri: 'str | None' = Non"
        "e) -> 'BinaryIO'"
    ),
    '_optional_shud_runtime_files': (
        "(input_dir: 'Path', source_root: 'Path', object_store: 'LocalObjectS"
        "tore | None', package_key: 'str', *, model_id: 'str', version: 'str'"
        ", manifest_uri: 'str | None') -> 'list[SourceFile]'"
    ),
    '_package_source_files': (
        "(model: 'dict[str, Any]', inventory_root: 'Path', inventory_relative"
        "_root: 'Path | None', source_root: 'Path', object_store: 'LocalObjec"
        "tStore | None', package_key: 'str', *, model_id: 'str', version: 'st"
        "r', manifest_uri: 'str | None') -> 'list[SourceFile]'"
    ),
    '_planned_file_entry': (
        "(source_file: 'SourceFile', *, model_id: 'str', version: 'str', mani"
        "fest_uri: 'str | None') -> 'dict[str, Any]'"
    ),
    '_preflight_json_output_path': (
        "(path: 'str | Path', *, error_code: 'str', model_id: 'str | None' = "
        "None, version: 'str | None' = None, manifest_uri: 'str | None' = Non"
        "e) -> 'None'"
    ),
    '_preflight_object_store_keys': (
        "(store: 'LocalObjectStore', keys: 'list[str]', *, model_id: 'str', v"
        "ersion: 'str', manifest_uri: 'str') -> 'None'"
    ),
    '_read_existing_manifest': (
        "(store: 'LocalObjectStore', manifest_key: 'str', model_id: 'str', ve"
        "rsion: 'str', manifest_uri: 'str') -> 'dict[str, Any]'"
    ),
    '_read_inventory': "(path: 'str | Path') -> 'tuple[dict[str, Any], bytes]'",
    '_read_object_bytes_no_symlinks': (
        "(store: 'LocalObjectStore', key: 'str', *, model_id: 'str', version:"
        " 'str', manifest_uri: 'str', max_bytes: 'int | None' = None) -> 'byt"
        "es'"
    ),
    '_recorded_relative_inventory_root': "(inventory: 'dict[str, Any]') -> 'Path | None'",
    '_reject_source_symlink_path': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None, e"
        "rror_path: 'Path | None' = None) -> 'None'"
    ),
    '_relative_inventory_path_matches_expected': (
        "(relative_path: 'Path', inventory_root: 'Path', inventory_relative_r"
        "oot: 'Path | None', source_root: 'Path', expected_path: 'Path') -> '"
        "bool'"
    ),
    '_release_publish_lock': "(store: 'LocalObjectStore', lock_key: 'str') -> 'None'",
    '_remove_object_temp_path': "(store: 'LocalObjectStore', key_or_uri: 'str', temp_name: 'str') -> 'None'",
    '_resolve_package_path': (
        "(path: 'Path', *, model_id: 'str | None' = None, version: 'str | Non"
        "e' = None) -> 'Path'"
    ),
    '_resolved_inventory_root': "(inventory: 'dict[str, Any]', model_id: 'str', version: 'str') -> 'Path'",
    '_resolved_source_root': (
        "(model: 'dict[str, Any]', inventory_root: 'Path', model_id: 'str', v"
        "ersion: 'str') -> 'Path'"
    ),
    '_safe_source_dir': (
        "(value: 'Any', inventory_root: 'Path', inventory_relative_root: 'Pat"
        "h | None', source_root: 'Path', field_name: 'str', *, expected_path:"
        " 'Path', model_id: 'str | None' = None, version: 'str | None' = None"
        ", manifest_uri: 'str | None' = None) -> 'Path'"
    ),
    '_safe_source_file': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'Path'"
    ),
    '_sha256_bytes': "(content: 'bytes') -> 'str'",
    '_sha256_file': "(path: 'Path') -> 'str'",
    '_sha256_handle': "(handle: 'BinaryIO') -> 'str'",
    '_sha256_json': "(payload: 'Any') -> 'str'",
    '_source_dir_from_relative_inventory_value': (
        "(path: 'Path', inventory_root: 'Path', inventory_relative_root: 'Pat"
        "h | None', source_root: 'Path', expected_path: 'Path', field_name: '"
        "str', *, model_id: 'str | None' = None, version: 'str | None' = None"
        ", manifest_uri: 'str | None' = None) -> 'Path'"
    ),
    '_source_file_evidence': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'tuple[int, str]'"
    ),
    '_source_file_for_package': (
        "(source_path: 'Path', relative_path: 'str', object_store: 'LocalObje"
        "ctStore | None', package_key: 'str', *, source_root: 'Path', role: '"
        "str') -> 'SourceFile'"
    ),
    '_source_file_size': (
        "(path: 'Path', source_root: 'Path', *, model_id: 'str | None' = None"
        ", version: 'str | None' = None, manifest_uri: 'str | None' = None) -"
        "> 'int'"
    ),
    '_source_identity_from_plan': (
        "(*, model: 'dict[str, Any]', package_files: 'list[SourceFile]', forc"
        "ing: 'dict[str, Any]', copy_forcing: 'bool', model_id: 'str', versio"
        "n: 'str', manifest_uri: 'str | None') -> 'tuple[dict[str, Any], list"
        "[dict[str, Any]]]'"
    ),
    '_success_payload': "(status: 'str', manifest: 'dict[str, Any]') -> 'dict[str, Any]'",
    '_validate_object_key_segment': (
        "(value: 'str', field_name: 'str', *, model_id: 'str', version: 'str'"
        ") -> 'None'"
    ),
    '_validated_canonical_required_source_files': (
        "(required_files: 'dict[str, Any]', input_dir: 'Path', source_root: '"
        "Path', object_store: 'LocalObjectStore | None', package_key: 'str', "
        "*, model_id: 'str', version: 'str', manifest_uri: 'str | None') -> '"
        "list[SourceFile]'"
    ),
    '_verified_source_file_evidence': (
        "(path: 'Path', source_root: 'Path', *, read_error_code: 'str', read_"
        "error_message: 'str', model_id: 'str | None' = None, version: 'str |"
        " None' = None, manifest_uri: 'str | None' = None) -> 'tuple[int, str"
        "]'"
    ),
    '_verify_existing_manifest_consistency': (
        "(store: 'LocalObjectStore', manifest: 'dict[str, Any]', *, checksum_"
        "material: 'dict[str, Any]', model_id: 'str', version: 'str', manifes"
        "t_uri: 'str') -> 'None'"
    ),
    '_verify_expected_source_identity': (
        "(expected: 'dict[str, Any] | None', actual: 'dict[str, Any]', *, mod"
        "el_id: 'str', version: 'str', manifest_uri: 'str') -> 'None'"
    ),
    '_verify_model_id_matches_canonical_identity': (
        "(model: 'dict[str, Any]', model_id: 'str', version: 'str') -> 'None'"
    ),
    '_verify_object_bytes': (
        "(store: 'LocalObjectStore', key: 'str', *, expected_size: 'int', exp"
        "ected_sha256: 'str', model_id: 'str | None' = None, version: 'str | "
        "None' = None, manifest_uri: 'str | None' = None) -> 'None'"
    ),
    '_walk_source_files': "(root: 'Path', source_root: 'Path') -> 'Iterator[Path]'",
    '_write_bytes_to_store_atomic': (
        "(store: 'LocalObjectStore', key: 'str', content: 'bytes', *, model_i"
        "d: 'str | None' = None, version: 'str | None' = None, manifest_uri: "
        "'str | None' = None) -> 'str'"
    ),
    '_write_file_to_store_streaming': (
        "(store: 'LocalObjectStore', key: 'str', source_path: 'Path', source_"
        "root: 'Path', *, model_id: 'str | None' = None, version: 'str | None"
        "' = None, manifest_uri: 'str | None' = None) -> 'tuple[int, str]'"
    ),
    '_write_json_file': (
        "(path: 'str | Path', payload: 'dict[str, Any]', *, error_code: 'str'"
        ", model_id: 'str | None' = None, version: 'str | None' = None, manif"
        "est_uri: 'str | None' = None, before_write: 'Callable[[Path, int], N"
        "one] | None' = None) -> 'None'"
    ),
    '_write_source_file_to_store': (
        "(source_file: 'SourceFile', store: 'LocalObjectStore', *, model_id: "
        "'str', version: 'str', manifest_uri: 'str') -> 'dict[str, Any]'"
    ),
    'basins_package_source_identity': "(*, inventory_path: 'str | Path', model_id: 'str') -> 'dict[str, Any]'",
    'contextmanager': '(func)',
    'dataclass': (
        '(cls=None, /, *, init=True, repr=True, eq=True, order=False, unsafe_'
        'hash=False, frozen=False, match_args=True, kw_only=False, slots=Fals'
        'e, weakref_slot=False)'
    ),
    'discover_basins_inventory': (
        "(basins_root: 'str | Path', *, budget: 'DiscoveryBudget | None' = No"
        "ne) -> 'dict[str, Any]'"
    ),
    'fnmatchcase': '(name, pat)',
    'forcing_checksum_material_for_schema_version': (
        "(forcing: 'Mapping[str, Any]', schema_version: 'str') -> 'dict[str, "
        "Any]'"
    ),
    'publish_basins_package': (
        "(*, inventory_path: 'str | Path', model_id: 'str', version: 'str', o"
        "utput_path: 'str | Path', copy_forcing: 'bool' = False, object_store"
        ": 'LocalObjectStore | None' = None, output_capacity_guard: 'Callable"
        "[[Path, int], None] | None' = None, output_write_guard: 'Callable[[P"
        "ath, int], None] | None' = None, expected_source_identity: 'dict[str"
        ", Any] | None' = None, calibration_overrides: 'Sequence[Mapping[str,"
        " Any]] | None' = None) -> 'dict[str, Any]'"
    ),
    'validate_object_path': "(path: 'str') -> 'ObjectPathValidation'",
    'write_basins_migration_report': (
        "(*, basins_root: 'str | Path', source_uri: 'str', output_path: 'str "
        "| Path') -> 'dict[str, Any]'"
    ),
}

_PUBLIC_ENTRYPOINT_MODULE = {
    "publish_basins_package": "workers.model_registry.basins_package",
    "basins_package_source_identity": "workers.model_registry.basins_package",
    "write_basins_migration_report": "workers.model_registry.basins_package",
}
_BASELINE_MODULE_CONTRACT = {
    "names": list(_BASELINE_FACADE_NAMES),
    "callables": {
        name: {"signature": sig, "module": _PUBLIC_ENTRYPOINT_MODULE.get(name)}
        for name, sig in _BASELINE_FACADE_SIGNATURES.items()
    },
}
_BASELINE_MODULE_CONTRACT["callables"]["ObjectStoreError"] = {
    "signature": None,
    "module": "packages.common.object_store",
}
_BASELINE_MODULE_CONTRACT["callables"]["datetime"] = {"signature": None, "module": "datetime"}
_BASINS_PACKAGE_OWNERS = (
    "workers/model_registry/basins_package.py",
    "workers/model_registry/basins_package_contracts.py",
    "workers/model_registry/basins_package_inventory.py",
    "workers/model_registry/basins_package_source_io.py",
    "workers/model_registry/basins_package_manifest.py",
    "workers/model_registry/basins_package_object_store.py",
)
_PUBLIC_ENTRYPOINTS = (
    "publish_basins_package",
    "basins_package_source_identity",
    "write_basins_migration_report",
)
_SEAM_FORWARDING_EDGES = (
    ("_package_source_files", "publish_basins_package"),
    ("_package_source_files", "basins_package_source_identity"),
    ("_walk_source_files", "_package_source_files"),
    ("_walk_source_files", "_forcing_metadata"),
    ("_walk_source_files", "_directory_evidence"),
    ("_csv_time_evidence", "_forcing_metadata"),
    ("_migration_source_file_evidence", "_directory_evidence"),
    ("_source_file_size", "_forcing_metadata"),
    ("_write_file_to_store_streaming", "_write_source_file_to_store"),
    ("_verify_object_bytes", "publish_basins_package"),
    ("_verify_object_bytes", "_verify_existing_manifest_consistency"),
    ("_verify_object_bytes", "_write_source_file_to_store"),
    ("_object_size_and_checksum_streaming", "_verify_object_bytes"),
)


def _callable_signature(value: object) -> str | None:
    try:
        return str(inspect.signature(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def test_basins_package_has_exactly_six_under_limit_owners() -> None:
    registry = _REPO_ROOT / "workers" / "model_registry"
    owners = sorted(path.name for path in registry.glob("basins_package*.py"))
    assert owners == [
        "basins_package.py",
        "basins_package_contracts.py",
        "basins_package_inventory.py",
        "basins_package_manifest.py",
        "basins_package_object_store.py",
        "basins_package_source_io.py",
    ]
    over_limit = []
    for relative in _BASINS_PACKAGE_OWNERS:
        path = _REPO_ROOT / relative
        assert path.is_file(), relative
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count >= 1000:
            over_limit.append((relative, line_count))
    assert over_limit == []


def test_basins_package_facade_preserves_baseline_contract() -> None:
    current_names = [name for name in dir(basins_package) if not name.startswith("__")]
    missing = [name for name in _BASELINE_MODULE_CONTRACT["names"] if name not in current_names]
    assert missing == []

    signature_drift = []
    public_module_drift = []
    for name, expected in _BASELINE_MODULE_CONTRACT["callables"].items():
        current = getattr(basins_package, name)
        if expected["signature"] is not None:
            current_signature = _callable_signature(current)
            if current_signature != expected["signature"]:
                signature_drift.append((name, expected["signature"], current_signature))
        if name in _PUBLIC_ENTRYPOINTS and getattr(current, "__module__", None) != expected["module"]:
            public_module_drift.append((name, expected["module"], getattr(current, "__module__", None)))
    assert signature_drift == []
    assert public_module_drift == []

    import packages.common.object_store as object_store_mod

    assert basins_package.LocalObjectStore is object_store_mod.LocalObjectStore
    assert basins_package.ObjectStoreError is object_store_mod.ObjectStoreError
    assert inspect.ismodule(basins_package.os)


@pytest.mark.parametrize(("seam", "caller_name"), _SEAM_FORWARDING_EDGES)
def test_basins_package_callable_seam_is_forwarded_by_caller(seam: str, caller_name: str) -> None:
    caller = getattr(basins_package, caller_name)
    code = getattr(caller, "__code__", None)
    assert code is not None, caller_name
    assert seam in code.co_names, (seam, caller_name, code.co_names)


def test_publish_observes_facade_forcing_sample_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=4)
    _object_store_env(tmp_path, monkeypatch)
    sampled_paths: list[Path] = []
    original_csv_time_evidence = basins_package._csv_time_evidence

    def counting_csv_time_evidence(
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[str | None, str | None, str | None, int]:
        sampled_paths.append(path)
        return original_csv_time_evidence(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "FORCING_SAMPLE_FILE_LIMIT", 1)
    monkeypatch.setattr(basins_package, "_csv_time_evidence", counting_csv_time_evidence)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-facade-sample-limit",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(sampled_paths) == 1
    assert manifest["forcing"]["sampled_file_count"] == 1
    assert manifest["forcing"]["sample_file_limit"] == 1
    assert manifest["forcing"]["csv_count"] == 4


def test_publish_observes_facade_existing_manifest_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-facade-manifest-limit",
        "--output",
        str(tmp_path / "manifest.json"),
    ]
    assert _argparse_main(args) == 0
    capsys.readouterr()

    monkeypatch.setattr(basins_package, "MAX_EXISTING_MANIFEST_BYTES", 1)
    exit_code = _argparse_main(args)
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_MANIFEST_INVALID"


def test_publish_object_store_error_uses_facade_class_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)
    constructed: list[str] = []
    original_init = basins_package.ObjectStoreError.__init__

    def tracking_init(self: object, message: str = "", *args: object, **kwargs: object) -> None:
        constructed.append(str(message))
        original_init(self, message, *args, **kwargs)

    def failing_size_and_checksum(*args: object, **kwargs: object) -> tuple[int, str]:
        raise basins_package.ObjectStoreError("facade-class verification failure")

    monkeypatch.setattr(basins_package.ObjectStoreError, "__init__", tracking_init)
    monkeypatch.setattr(basins_package, "_object_size_and_checksum_streaming", failing_size_and_checksum)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-facade-object-store-error",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert "facade-class verification failure" in constructed
    assert error["error_code"] == "BASINS_PACKAGE_WRITE_FAILED"
