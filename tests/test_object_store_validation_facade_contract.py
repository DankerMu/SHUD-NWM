from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import stat
import subprocess
import sys
import typing
from pathlib import Path

import shapefile

from packages.common import safe_fs
from packages.common.object_store import LocalObjectStore, ObjectStoreError
from services.production_closure import object_store_validation as facade

ROOT = Path(__file__).resolve().parents[1]
OWNER_FILENAMES = {
    "object_store_validation.py",
    "object_store_validation_contracts.py",
    "object_store_validation_path_safety.py",
    "object_store_validation_fixture.py",
    "object_store_validation_manifest.py",
    "object_store_validation_runtime.py",
    "object_store_validation_consumption.py",
    "object_store_validation_evidence.py",
}
BASELINE_NAMES = {
    "Any",
    "BasinsPackageError",
    "BasinsRegistryImportError",
    "Callable",
    "DEFAULT_BASINS_MIGRATION_SOURCE_URI",
    "DEFAULT_CLEANUP_POLICY",
    "DEFAULT_OBJECT_STORE_TARGET",
    "ENCODED_SEPARATOR_RE",
    "EvidenceWriter",
    "FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS",
    "LocalObjectStore",
    "MAX_DESCENDANT_SYMLINK_SCAN_NODES",
    "MAX_OBJECT_MANIFEST_BYTES",
    "MAX_PERCENT_DECODE_ROUNDS",
    "MAX_RAW_INTERMEDIATE_BYTES",
    "MAX_RUNTIME_STAGING_DIRECTORY_DEPTH",
    "MAX_RUNTIME_STAGING_FILE_COUNT",
    "MAX_RUNTIME_STAGING_NODE_COUNT",
    "MAX_RUNTIME_STAGING_OBJECT_BYTES",
    "MAX_RUNTIME_STAGING_TOTAL_BYTES",
    "MAX_STORED_MANIFEST_BYTES",
    "ObjectStoreError",
    "PackageChecksumReconstruction",
    "Path",
    "ProductionObjectStoreConfig",
    "ProductionObjectStoreValidationError",
    "PurePosixPath",
    "RIVER_SHP_REQUIRED_DBF_FIELDS",
    "RUNTIME_DIR_FLAGS",
    "RUNTIME_READ_FLAGS",
    "RuntimePrefixCollection",
    "RuntimeStagedObject",
    "RuntimeStagingBudget",
    "RuntimeStagingPreparation",
    "SAFE_IDENTIFIER_RE",
    "SAFE_RUN_ID_RE",
    "SENSITIVE_PREFIX_ASSIGNMENT_RE",
    "SENSITIVE_PREFIX_SEPARATOR_RE",
    "SHUDRuntimeError",
    "SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS",
    "SafeFilesystemError",
    "Sequence",
    "UTC",
    "_argparse_main",
    "_assert_directory_empty_fd",
    "_assert_runtime_package_entry_verified",
    "_assert_runtime_prefix_identity",
    "_assert_runtime_staged_object_matches_expected",
    "_assert_runtime_staging_targets_unique",
    "_assert_runtime_workspace_empty",
    "_canonical_decode_steps",
    "_cleanup_raw_lane_file",
    "_cleanup_rollback_evidence",
    "_click_main",
    "_collect_runtime_object",
    "_collect_runtime_object_or_prefix",
    "_collect_runtime_package_objects",
    "_collect_runtime_prefix_dir_fd",
    "_collect_runtime_prefix_objects",
    "_consumption_acceptance_evidence",
    "_consumption_acceptance_note",
    "_consumption_evidence",
    "_copy_fixture_shapefile_outputs",
    "_default_model_id",
    "_delete_validation_run_object",
    "_deterministic_manifest_bytes",
    "_environment_payload",
    "_first_staged_path",
    "_forbidden_runtime_source_fragments",
    "_forcing_checksum_material",
    "_guard_url_authority",
    "_infer_copied_root_relative_resolved_path",
    "_is_validation_run_object",
    "_open_existing_directory_fd",
    "_open_runtime_prefix_child_dir",
    "_open_runtime_prefix_dir",
    "_operational_prefix",
    "_package_checksum_from_stored_manifest",
    "_package_manifest_evidence",
    "_preflight_payload",
    "_prepare_runtime_staging_workspace",
    "_read_raw_worker_output",
    "_read_runtime_prefix_file",
    "_read_runtime_staging_text",
    "_refuse_descendant_symlinks_fd",
    "_refuse_existing_descendant_symlinks",
    "_refuse_run_scoped_local_object_store_symlinks",
    "_refuse_symlink_components",
    "_refuse_symlink_components_to_deepest_existing",
    "_registry_import_evidence",
    "_replace_or_append_runtime_cfg",
    "_result_blockers",
    "_run_scoped_local_object_store_prefixes",
    "_runtime_prefix_entry_allowed",
    "_runtime_staged_files_from_receipts",
    "_runtime_staged_paths_by_suffix",
    "_runtime_staging_evidence",
    "_runtime_verification_entries_by_uri",
    "_safe_fixture_dir",
    "_safe_fixture_write_bytes",
    "_safe_fixture_write_text",
    "_safe_resolved_evidence_root",
    "_safe_run_id",
    "_safe_runtime_project_name",
    "_safe_runtime_relative_path",
    "_sha256_json",
    "_source_model_identity_for_package_checksum",
    "_stored_manifest_payload_without_self_entry",
    "_summary",
    "_truthy_env",
    "_validate_config",
    "_validate_internal_lane_paths",
    "_validate_lane_path_contained",
    "_validate_local_object_store_root",
    "_validate_object_store_prefix_safe",
    "_verify_stored_objects",
    "_write_domain_shapefile",
    "_write_migration_evidence",
    "_write_raw_lane_bytes",
    "_write_raw_worker_output",
    "_write_river_shapefile",
    "_write_runtime_staging_bytes",
    "_write_segment_crosswalk_shapefile",
    "_write_validation_run_scratch_object",
    "_write_validation_scratch_object",
    "_write_wgs84_prj",
    "annotations",
    "argparse",
    "atomic_write_bytes_no_follow",
    "dataclass",
    "datetime",
    "discover_basins_inventory",
    "ensure_directory_no_follow",
    "field",
    "forcing_checksum_material_for_schema_version",
    "hashlib",
    "import_basins_registry",
    "json",
    "main",
    "os",
    "platform",
    "prepare_basins_import_sources",
    "publish_basins_package",
    "re",
    "read_bytes_limited_no_follow",
    "redact_payload",
    "replace",
    "stat",
    "stat_no_follow",
    "sys",
    "tempfile",
    "unlink_no_follow",
    "unquote",
    "urlsplit",
    "urlunsplit",
    "validate_object_store",
    "write_basins_migration_report",
    "write_inventory",
    "write_synthetic_basins_fixture",
}
SIGNATURES = {
    "Any": ("(*args, **kwargs)"),
    "BasinsPackageError": (
        "(error_code: 'str', message: 'str', *, model_id: 'str | None' = None, version: 'str | None"
        "' = None, path: 'str | None' = None, manifest_uri: 'str | None' = None, details: 'dict[str"
        ", Any] | None' = None) -> 'None'"
    ),
    "BasinsRegistryImportError": (
        "(error_code: 'str', message: 'str', *, model_id: 'str | None' = None, path: 'str | None' ="
        " None, details: 'dict[str, Any] | None' = None) -> 'None'"
    ),
    "Callable": ("(*args, **kwargs)"),
    "EvidenceWriter": (
        "(evidence_root: 'Path', lane_dir: 'Path', force: 'bool' = False, _created_paths: 'set[Path"
        "]' = <factory>) -> None"
    ),
    "LocalObjectStore": ("(root: 'Path | str', object_store_prefix: 'str' = '') -> None"),
    "PackageChecksumReconstruction": (
        "(checksum: 'str | None', status: 'str', identity_basis: 'str', limitation: 'str | None' = None) -> None"
    ),
    "Path": ("(*args, **kwargs)"),
    "ProductionObjectStoreConfig": (
        "(evidence_root: 'Path', run_id: 'str', target: 'str', endpoint: 'str', object_store_root: "
        "'Path', object_store_prefix: 'str', configured_object_store_prefix: 'str', credential_sour"
        "ce: 'str', cleanup_policy: 'str', basins_root: 'Path | None', source_uri: 'str', model_id:"
        " 'str | None', version: 'str', run_registry_import: 'bool' = False, registry_database_url:"
        " 'str | None' = None, force: 'bool' = False) -> None"
    ),
    "ProductionObjectStoreValidationError": ("(error_code: 'str', message: 'str') -> 'None'"),
    "PurePosixPath": ("(*args)"),
    "RuntimePrefixCollection": (
        "(objects: 'list[RuntimeStagedObject]', prefix_receipt: 'dict[str, Any] | None' = None) -> None"
    ),
    "RuntimeStagedObject": ("(target: 'Path', content: 'bytes', receipt: 'dict[str, Any]') -> None"),
    "RuntimeStagingBudget": (
        "(max_file_count: 'int', max_directory_depth: 'int', max_total_bytes: 'int', max_object_byt"
        "es: 'int', max_node_count: 'int | None' = None, file_count: 'int' = 0, total_bytes: 'int' "
        "= 0, node_count: 'int' = 0) -> None"
    ),
    "RuntimeStagingPreparation": (
        "(cfg_path: 'Path', package_receipts: 'list[dict[str, Any]]', forcing_receipts: 'list[dict["
        "str, Any]]', forcing_prefix_receipt: 'dict[str, Any] | None', staged_files: 'list[str]', b"
        "udgets: 'dict[str, int]') -> None"
    ),
    "SHUDRuntimeError": ("(error_code: 'str', message: 'str') -> 'None'"),
    "SafeFilesystemError": ("(message: 'str', *, kind: 'str' = 'unsafe') -> 'None'"),
    "Sequence": ("(*args, **kwargs)"),
    "_argparse_main": ("(argv: 'Sequence[str] | None' = None) -> 'int'"),
    "_assert_directory_empty_fd": ("(root_fd: 'int', path_label: 'Path', *, path_kind: 'str') -> 'None'"),
    "_assert_runtime_package_entry_verified": (
        "(manifest_entry: 'dict[str, Any]', verification_by_uri: 'dict[str, dict[str, Any]]') -> 'None'"
    ),
    "_assert_runtime_prefix_identity": (
        "(source_path: 'Path', containment_root: 'Path', expected_stat: 'os.stat_result', fd: 'int') -> 'None'"
    ),
    "_assert_runtime_staged_object_matches_expected": (
        "(expected: 'dict[str, Any]', *, actual_object_uri: 'str', actual_relative_path: 'str', act"
        "ual_size_bytes: 'int', actual_sha256: 'str') -> 'None'"
    ),
    "_assert_runtime_staging_targets_unique": (
        "(input_dir: 'Path', staged_objects: 'Sequence[RuntimeStagedObject]') -> 'None'"
    ),
    "_assert_runtime_workspace_empty": (
        "(config: 'ProductionObjectStoreConfig', root: 'Path', *, path_kind: 'str') -> 'None'"
    ),
    "_canonical_decode_steps": ("(value: 'str') -> 'tuple[str, ...]'"),
    "_cleanup_raw_lane_file": (
        "(config: 'ProductionObjectStoreConfig', raw_path: 'Path', *, path_kind: 'str') -> 'None'"
    ),
    "_cleanup_rollback_evidence": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', model_id: 'str') -> 'dict[str, Any]'"
    ),
    "_click_main": ("(argv: 'Sequence[str] | None' = None) -> 'int'"),
    "_collect_runtime_object": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', uri_or_key: 'str', targ"
        "et: 'Path', *, receipt_relative_path: 'str', budget: 'RuntimeStagingBudget', expected: 'di"
        "ct[str, Any] | None' = None, receipt_source: 'str') -> 'RuntimeStagedObject'"
    ),
    "_collect_runtime_object_or_prefix": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', uri_or_key: 'str', inpu"
        "t_dir: 'Path', budget: 'RuntimeStagingBudget', *, allowed_keys: 'set[str] | None' = None) "
        "-> 'RuntimePrefixCollection'"
    ),
    "_collect_runtime_package_objects": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', package_manifest: 'dict"
        "[str, Any]', stored_verification: 'dict[str, Any]', input_dir: 'Path', budget: 'RuntimeSta"
        "gingBudget') -> 'RuntimePrefixCollection'"
    ),
    "_collect_runtime_prefix_dir_fd": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', dir_fd: 'int', normaliz"
        "ed_prefix_key: 'str', path_label: 'Path', relative_dir: 'PurePosixPath', input_dir: 'Path'"
        ", budget: 'RuntimeStagingBudget', objects: 'list[RuntimeStagedObject]', receipts: 'list[di"
        "ct[str, Any]]', prefix_digest: 'Any', *, allowed_keys: 'set[str] | None' = None) -> 'None'"
    ),
    "_collect_runtime_prefix_objects": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', normalized_prefix_key: "
        "'str', source_path: 'Path', source_stat: 'os.stat_result', input_dir: 'Path', budget: 'Run"
        "timeStagingBudget', *, allowed_keys: 'set[str] | None' = None) -> 'RuntimePrefixCollection"
        "'"
    ),
    "_consumption_acceptance_evidence": ("(registry: 'dict[str, Any]') -> 'str'"),
    "_consumption_acceptance_note": ("(registry: 'dict[str, Any]') -> 'str'"),
    "_consumption_evidence": (
        "(config: 'ProductionObjectStoreConfig', writer: 'EvidenceWriter', store: 'LocalObjectStore"
        "', inventory_path: 'Path', package_manifest_raw_path: 'Path', manifest: 'dict[str, Any]', "
        "stored_verification: 'dict[str, Any]') -> 'dict[str, Any]'"
    ),
    "_copy_fixture_shapefile_outputs": (
        "(source_base: 'Path', target_base: 'Path', *, containment_root: 'Path') -> 'None'"
    ),
    "_default_model_id": ("(inventory: 'dict[str, Any]') -> 'str'"),
    "_delete_validation_run_object": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', key: 'str', created_key"
        "s: 'set[str]') -> 'None'"
    ),
    "_deterministic_manifest_bytes": ("(payload: 'dict[str, Any]') -> 'bytes'"),
    "_environment_payload": ("(config: 'ProductionObjectStoreConfig') -> 'dict[str, Any]'"),
    "_first_staged_path": ("(paths_by_suffix: 'dict[str, list[Path]]', suffix: 'str') -> 'Path | None'"),
    "_forbidden_runtime_source_fragments": ("(values: 'Sequence[Any]') -> 'list[str]'"),
    "_forcing_checksum_material": ("(forcing: 'Any', schema_version: 'str') -> 'Any'"),
    "_guard_url_authority": ("(netloc: 'str') -> 'None'"),
    "_infer_copied_root_relative_resolved_path": ("(stored_manifest: 'dict[str, Any]') -> 'str | None'"),
    "_is_validation_run_object": ("(config: 'ProductionObjectStoreConfig', key: 'str') -> 'bool'"),
    "_open_existing_directory_fd": ("(path: 'Path', expected_stat: 'os.stat_result', *, path_kind: 'str') -> 'int'"),
    "_open_runtime_prefix_child_dir": (
        "(parent_fd: 'int', name: 'str', path_label: 'Path', expected_stat: 'os.stat_result') -> 'int'"
    ),
    "_open_runtime_prefix_dir": ("(path: 'Path', containment_root: 'Path') -> 'int'"),
    "_operational_prefix": ("(value: 'str') -> 'str'"),
    "_package_checksum_from_stored_manifest": (
        "(stored_manifest: 'dict[str, Any]') -> 'PackageChecksumReconstruction'"
    ),
    "_package_manifest_evidence": (
        "(publish_result: 'dict[str, Any]', manifest: 'dict[str, Any]') -> 'dict[str, Any]'"
    ),
    "_preflight_payload": ("(config: 'ProductionObjectStoreConfig') -> 'dict[str, Any]'"),
    "_prepare_runtime_staging_workspace": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', runtime_manifest: 'dict"
        "[str, Any]', package_manifest: 'dict[str, Any]', stored_verification: 'dict[str, Any]', in"
        "put_dir: 'Path', output_dir: 'Path', *, allowed_forcing_keys: 'set[str] | None' = None) ->"
        " 'RuntimeStagingPreparation'"
    ),
    "_read_raw_worker_output": ("(path: 'Path', *, path_kind: 'str') -> 'bytes'"),
    "_read_runtime_prefix_file": (
        "(parent_fd: 'int', name: 'str', path_label: 'Path', expected_stat: 'os.stat_result') -> 'bytes'"
    ),
    "_read_runtime_staging_text": ("(config: 'ProductionObjectStoreConfig', path: 'Path') -> 'str'"),
    "_refuse_descendant_symlinks_fd": (
        "(dir_fd: 'int', path_label: 'Path', *, path_kind: 'str', node_count: 'int') -> 'int'"
    ),
    "_refuse_existing_descendant_symlinks": ("(root: 'Path', *, path_kind: 'str') -> 'None'"),
    "_refuse_run_scoped_local_object_store_symlinks": ("(config: 'ProductionObjectStoreConfig') -> 'None'"),
    "_refuse_symlink_components": ("(path: 'Path') -> 'None'"),
    "_refuse_symlink_components_to_deepest_existing": ("(path: 'Path') -> 'None'"),
    "_registry_import_evidence": (
        "(config: 'ProductionObjectStoreConfig', inventory_path: 'Path', package_manifest_raw_path:"
        " 'Path', manifest: 'dict[str, Any]', sources: 'Any') -> 'dict[str, Any]'"
    ),
    "_replace_or_append_runtime_cfg": ("(content: 'str', key: 'str', value: 'str') -> 'str'"),
    "_result_blockers": ("(*payloads: 'dict[str, Any]') -> 'list[dict[str, Any]]'"),
    "_run_scoped_local_object_store_prefixes": ("(config: 'ProductionObjectStoreConfig') -> 'tuple[Path, ...]'"),
    "_runtime_prefix_entry_allowed": (
        "(entry_key: 'str', entry_stat: 'os.stat_result', allowed_keys: 'set[str]') -> 'bool'"
    ),
    "_runtime_staged_files_from_receipts": (
        "(input_dir: 'Path', staged_objects: 'Sequence[RuntimeStagedObject]', cfg_path: 'Path') -> 'list[str]'"
    ),
    "_runtime_staged_paths_by_suffix": ("(staged_objects: 'Sequence[RuntimeStagedObject]') -> 'dict[str, list[Path]]'"),
    "_runtime_staging_evidence": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', manifest: 'dict[str, An"
        "y]', stored_verification: 'dict[str, Any]', writer: 'EvidenceWriter') -> 'dict[str, Any]'"
    ),
    "_runtime_verification_entries_by_uri": ("(stored_verification: 'dict[str, Any]') -> 'dict[str, dict[str, Any]]'"),
    "_safe_fixture_dir": ("(path: 'Path', *, containment_root: 'Path | None') -> 'None'"),
    "_safe_fixture_write_bytes": ("(path: 'Path', content: 'bytes', *, containment_root: 'Path | None') -> 'None'"),
    "_safe_fixture_write_text": ("(path: 'Path', content: 'str', *, containment_root: 'Path | None') -> 'None'"),
    "_safe_resolved_evidence_root": ("(evidence_root: 'Path') -> 'Path'"),
    "_safe_run_id": ("(run_id: 'str') -> 'str'"),
    "_safe_runtime_project_name": ("(runtime_manifest: 'dict[str, Any]') -> 'str'"),
    "_safe_runtime_relative_path": ("(value: 'str') -> 'Path'"),
    "_sha256_json": ("(payload: 'Any') -> 'str'"),
    "_source_model_identity_for_package_checksum": ("(stored_manifest: 'dict[str, Any]') -> 'dict[str, Any]'"),
    "_stored_manifest_payload_without_self_entry": ("(stored_manifest: 'dict[str, Any]') -> 'dict[str, Any]'"),
    "_summary": (
        "(config: 'ProductionObjectStoreConfig', *, status: 'str', blockers: 'list[dict[str, Any]]'"
        ", files: 'list[str]', selected_model_id: 'str | None' = None, version: 'str | None' = None"
        ", migration_report: 'dict[str, Any] | None' = None, package_manifest: 'dict[str, Any] | No"
        "ne' = None, consumption: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'"
    ),
    "_truthy_env": ("(value: 'str | None') -> 'bool'"),
    "_validate_config": ("(config: 'ProductionObjectStoreConfig') -> 'None'"),
    "_validate_internal_lane_paths": ("(config: 'ProductionObjectStoreConfig') -> 'None'"),
    "_validate_lane_path_contained": (
        "(config: 'ProductionObjectStoreConfig', path: 'Path', *, path_kind: 'str') -> 'None'"
    ),
    "_validate_local_object_store_root": ("(config: 'ProductionObjectStoreConfig') -> 'None'"),
    "_validate_object_store_prefix_safe": ("(prefix: 'str') -> 'None'"),
    "_verify_stored_objects": ("(store: 'LocalObjectStore', manifest: 'dict[str, Any]') -> 'dict[str, Any]'"),
    "_write_domain_shapefile": ("(base: 'Path', *, containment_root: 'Path | None' = None) -> 'None'"),
    "_write_migration_evidence": (
        "(config: 'ProductionObjectStoreConfig', writer: 'EvidenceWriter', basins_root: 'Path', blo"
        "ckers: 'list[dict[str, Any]]') -> 'dict[str, Any] | None'"
    ),
    "_write_raw_lane_bytes": (
        "(config: 'ProductionObjectStoreConfig', raw_path: 'Path', content: 'bytes', *, path_kind: 'str') -> 'None'"
    ),
    "_write_raw_worker_output": (
        "(config: 'ProductionObjectStoreConfig', raw_path: 'Path', *, path_kind: 'str', producer: '"
        "Callable[[Path], Any]') -> 'tuple[Any, bytes]'"
    ),
    "_write_river_shapefile": ("(base: 'Path', *, containment_root: 'Path | None' = None) -> 'None'"),
    "_write_runtime_staging_bytes": (
        "(config: 'ProductionObjectStoreConfig', target: 'Path', content: 'bytes') -> 'None'"
    ),
    "_write_segment_crosswalk_shapefile": ("(base: 'Path', *, containment_root: 'Path | None' = None) -> 'None'"),
    "_write_validation_run_scratch_object": (
        "(config: 'ProductionObjectStoreConfig', store: 'LocalObjectStore', key: 'str', content: 'bytes') -> 'str'"
    ),
    "_write_validation_scratch_object": ("(store: 'LocalObjectStore', key: 'str', content: 'bytes') -> 'str'"),
    "_write_wgs84_prj": ("(path: 'Path', *, containment_root: 'Path | None' = None) -> 'None'"),
    "atomic_write_bytes_no_follow": (
        "(path: 'Path', content: 'bytes', *, containment_root: 'Path | None' = None, temp_suffix: '"
        "str' = 'tmp', mode: 'int | None' = None, require_durable_replace: 'bool' = False) -> 'Path"
        "'"
    ),
    "dataclass": (
        "(cls=None, /, *, init=True, repr=True, eq=True, order=False, unsafe_hash=False, frozen=Fal"
        "se, match_args=True, kw_only=False, slots=False, weakref_slot=False)"
    ),
    "discover_basins_inventory": (
        "(basins_root: 'str | Path', *, budget: 'DiscoveryBudget | None' = None) -> 'dict[str, Any]'"
    ),
    "ensure_directory_no_follow": ("(path: 'Path', *, containment_root: 'Path | None' = None) -> 'Path'"),
    "forcing_checksum_material_for_schema_version": (
        "(forcing: 'Mapping[str, Any]', schema_version: 'str') -> 'dict[str, Any]'"
    ),
    "import_basins_registry": (
        "(*, inventory_path: 'str | Path', package_manifest_path: 'str | Path', database_url: 'str "
        "| None' = None, output_path: 'str | Path | None' = None, policy_decision: 'PolicyDecision "
        "| None' = None, preflight_policy_decision: 'PolicyDecision | None' = None, trusted_interna"
        "l: 'bool' = False, seed_output_river_segments: 'bool' = True, backfill_output_segment_geom"
        "etry: 'bool' = True) -> 'dict[str, Any]'"
    ),
    "main": ("(argv: 'Sequence[str] | None' = None) -> 'int'"),
    "prepare_basins_import_sources": (
        "(*, inventory_path: 'str | Path', package_manifest_path: 'str | Path') -> 'ImportSources'"
    ),
    "publish_basins_package": (
        "(*, inventory_path: 'str | Path', model_id: 'str', version: 'str', output_path: 'str | Pat"
        "h', copy_forcing: 'bool' = False, object_store: 'LocalObjectStore | None' = None, output_c"
        "apacity_guard: 'Callable[[Path, int], None] | None' = None, output_write_guard: 'Callable["
        "[Path, int], None] | None' = None, expected_source_identity: 'dict[str, Any] | None' = Non"
        "e, calibration_overrides: 'Sequence[Mapping[str, Any]] | None' = None) -> 'dict[str, Any]'"
    ),
    "read_bytes_limited_no_follow": (
        "(path: 'Path', *, max_bytes: 'int', containment_root: 'Path | None' = None) -> 'bytes'"
    ),
    "redact_payload": ("(value: 'Any') -> 'Any'"),
    "replace": ("(obj, /, **changes)"),
    "stat_no_follow": ("(path: 'Path', *, containment_root: 'Path | None' = None) -> 'os.stat_result'"),
    "unlink_no_follow": (
        "(path: 'Path', *, containment_root: 'Path | None' = None, missing_ok: 'bool' = False) -> 'None'"
    ),
    "unquote": ("(string, encoding='utf-8', errors='replace')"),
    "urlsplit": ("(url, scheme='', allow_fragments=True)"),
    "urlunsplit": ("(components)"),
    "validate_object_store": ("(config: 'ProductionObjectStoreConfig') -> 'dict[str, Any]'"),
    "write_basins_migration_report": (
        "(*, basins_root: 'str | Path', source_uri: 'str', output_path: 'str | Path') -> 'dict[str, Any]'"
    ),
    "write_inventory": ("(inventory: 'dict[str, Any]', output_path: 'str | Path') -> 'None'"),
    "write_synthetic_basins_fixture": ("(root: 'Path', *, containment_root: 'Path | None' = None) -> 'dict[str, Any]'"),
}
MISSING = dataclasses.MISSING
DATACLASS_FIELDS = {
    "EvidenceWriter": (
        ("evidence_root", "Path", MISSING, MISSING, True, False),
        ("lane_dir", "Path", MISSING, MISSING, True, False),
        ("force", "bool", False, MISSING, True, False),
        ("_created_paths", "set[Path]", MISSING, set, True, False),
    ),
    "ProductionObjectStoreConfig": (
        ("evidence_root", "Path", MISSING, MISSING, True, False),
        ("run_id", "str", MISSING, MISSING, True, False),
        ("target", "str", MISSING, MISSING, True, False),
        ("endpoint", "str", MISSING, MISSING, True, False),
        ("object_store_root", "Path", MISSING, MISSING, True, False),
        ("object_store_prefix", "str", MISSING, MISSING, True, False),
        ("configured_object_store_prefix", "str", MISSING, MISSING, True, False),
        ("credential_source", "str", MISSING, MISSING, True, False),
        ("cleanup_policy", "str", MISSING, MISSING, True, False),
        ("basins_root", "Path | None", MISSING, MISSING, True, False),
        ("source_uri", "str", MISSING, MISSING, True, False),
        ("model_id", "str | None", MISSING, MISSING, True, False),
        ("version", "str", MISSING, MISSING, True, False),
        ("run_registry_import", "bool", False, MISSING, True, False),
        ("registry_database_url", "str | None", None, MISSING, True, False),
        ("force", "bool", False, MISSING, True, False),
    ),
    "PackageChecksumReconstruction": (
        ("checksum", "str | None", MISSING, MISSING, True, False),
        ("status", "str", MISSING, MISSING, True, False),
        ("identity_basis", "str", MISSING, MISSING, True, False),
        ("limitation", "str | None", None, MISSING, True, False),
    ),
    "RuntimeStagingBudget": (
        ("max_file_count", "int", MISSING, MISSING, True, False),
        ("max_directory_depth", "int", MISSING, MISSING, True, False),
        ("max_total_bytes", "int", MISSING, MISSING, True, False),
        ("max_object_bytes", "int", MISSING, MISSING, True, False),
        ("max_node_count", "int | None", None, MISSING, True, False),
        ("file_count", "int", 0, MISSING, True, False),
        ("total_bytes", "int", 0, MISSING, True, False),
        ("node_count", "int", 0, MISSING, True, False),
    ),
    "RuntimeStagedObject": (
        ("target", "Path", MISSING, MISSING, True, False),
        ("content", "bytes", MISSING, MISSING, True, False),
        ("receipt", "dict[str, Any]", MISSING, MISSING, True, False),
    ),
    "RuntimeStagingPreparation": (
        ("cfg_path", "Path", MISSING, MISSING, True, False),
        ("package_receipts", "list[dict[str, Any]]", MISSING, MISSING, True, False),
        ("forcing_receipts", "list[dict[str, Any]]", MISSING, MISSING, True, False),
        ("forcing_prefix_receipt", "dict[str, Any] | None", MISSING, MISSING, True, False),
        ("staged_files", "list[str]", MISSING, MISSING, True, False),
        ("budgets", "dict[str, int]", MISSING, MISSING, True, False),
    ),
    "RuntimePrefixCollection": (
        ("objects", "list[RuntimeStagedObject]", MISSING, MISSING, True, False),
        ("prefix_receipt", "dict[str, Any] | None", None, MISSING, True, False),
    ),
    "LocalObjectStore": (
        ("root", "Path | str", MISSING, MISSING, True, False),
        ("object_store_prefix", "str", "", MISSING, True, False),
    ),
}

FIXTURE_PATHS = {
    "basin-a/forcing/X000001.csv",
    "basin-a/input/alias-a/alias-a.cfg.calib",
    "basin-a/input/alias-a/alias-a.cfg.ic",
    "basin-a/input/alias-a/alias-a.cfg.para",
    "basin-a/input/alias-a/alias-a.para.geol",
    "basin-a/input/alias-a/alias-a.para.lc",
    "basin-a/input/alias-a/alias-a.para.soil",
    "basin-a/input/alias-a/alias-a.sp.att",
    "basin-a/input/alias-a/alias-a.sp.mesh",
    "basin-a/input/alias-a/alias-a.sp.riv",
    "basin-a/input/alias-a/alias-a.sp.rivseg",
    "basin-a/input/alias-a/alias-a.tsd.forc",
    "basin-a/input/alias-a/alias-a.tsd.lai",
    "basin-a/input/alias-a/alias-a.tsd.mf",
    "basin-a/input/alias-a/alias-a.tsd.rl",
    "basin-a/input/alias-a/gis/domain.dbf",
    "basin-a/input/alias-a/gis/domain.prj",
    "basin-a/input/alias-a/gis/domain.shp",
    "basin-a/input/alias-a/gis/domain.shx",
    "basin-a/input/alias-a/gis/river.dbf",
    "basin-a/input/alias-a/gis/river.prj",
    "basin-a/input/alias-a/gis/river.shp",
    "basin-a/input/alias-a/gis/river.shx",
    "basin-a/input/alias-a/gis/seg.dbf",
    "basin-a/input/alias-a/gis/seg.prj",
    "basin-a/input/alias-a/gis/seg.shp",
    "basin-a/input/alias-a/gis/seg.shx",
}

STABLE_TEXT_SHA256 = {
    "basin-a/forcing/X000001.csv": "423a249b26ea5e3f783ad57b5f682511b44d89f29bca75ab2e61dc99d485ac32",
    "basin-a/input/alias-a/alias-a.cfg.calib": "251d4e26182b3a103b3d3e28650a9f86347da575063d8191a71b705284a41d69",
    "basin-a/input/alias-a/alias-a.cfg.ic": "3794d3ac00cea34902d3b37bbbf067893ce49357511c057554f8525474ec78b0",
    "basin-a/input/alias-a/alias-a.cfg.para": "d33c75dd6a746a4d8f80758e8877d4bf74defbff42791c376f86f9dee5ca7208",
    "basin-a/input/alias-a/alias-a.para.geol": "aac52d2a3f350690edb12bd133b9a43d7e2b89b928b2348ebefc4a8855645830",
    "basin-a/input/alias-a/alias-a.para.lc": "23de0c9541291c2859ff30f7527bb5655cf510571b69acc462f82a88daf8a265",
    "basin-a/input/alias-a/alias-a.para.soil": "0639649f9c1e77154c4cdba301818513b7d709b43dad5006afde28d8e315c398",
    "basin-a/input/alias-a/alias-a.sp.att": "9dffc9ea997725ce961d405ddbe2f6521a8da67777659cec6d23d31a76d57f05",
    "basin-a/input/alias-a/alias-a.sp.mesh": "b5f37f42d323523d14e8df16228dfb94707a10cf3b3e696072daf37efa8cc452",
    "basin-a/input/alias-a/alias-a.sp.riv": "debf08491b0e22a39c06502d0354b9ae14c169fbd581d2941b78ac61f7907863",
    "basin-a/input/alias-a/alias-a.sp.rivseg": "4eaccab2a297cdd5d09f13f193cb73c9048996dab9423340ce4092383188cb0f",
    "basin-a/input/alias-a/alias-a.tsd.forc": "ac60bea2aced8b2c409ccc5f4546d0cb468a67242cf45796963e2dc962ef7404",
    "basin-a/input/alias-a/alias-a.tsd.lai": "c4b1c17939cce478599268db0b09e636311babaebc5b1538b506342111d14c52",
    "basin-a/input/alias-a/alias-a.tsd.mf": "c00cbb6c21249c20f09ed9a2e0e1867dda37f556128a85f5ee6056f2b3c6b498",
    "basin-a/input/alias-a/alias-a.tsd.rl": "6da82a0759e5b2edb913322eae73e253b9936faac836227de342812ab3636f94",
    "basin-a/input/alias-a/gis/domain.prj": "5d3b39697820a6d6dfe49413bea38603f41331c162725fd1bd958941034a1c37",
    "basin-a/input/alias-a/gis/river.prj": "5d3b39697820a6d6dfe49413bea38603f41331c162725fd1bd958941034a1c37",
    "basin-a/input/alias-a/gis/seg.prj": "5d3b39697820a6d6dfe49413bea38603f41331c162725fd1bd958941034a1c37",
}

SHAPEFILE_SEMANTICS = {
    "domain": {
        "shape_type": shapefile.POLYGON,
        "fields": [("ID", "N", 50, 0)],
        "records": [[1]],
        "points": [[[100.0, 30.0], [101.0, 30.0], [101.0, 31.0], [100.0, 31.0], [100.0, 30.0]]],
        "parts": [[0]],
    },
    "river": {
        "shape_type": shapefile.POLYLINE,
        "fields": [
            ("Index", "N", 50, 0),
            ("Down", "N", 50, 0),
            ("Type", "N", 50, 0),
            ("Slope", "F", 50, 6),
            ("Length", "F", 50, 6),
            ("BC", "N", 50, 0),
            ("Depth", "F", 50, 6),
            ("BankSlope", "F", 50, 6),
            ("Width", "F", 50, 6),
            ("Sinuosity", "F", 50, 6),
            ("Manning", "F", 50, 6),
            ("Cwr", "F", 50, 6),
            ("KsatH", "F", 50, 6),
            ("BedThick", "F", 50, 6),
        ],
        "records": [
            [1, 2, 1, 0.001, 50000.0, 0, 2.5, 0.5, 30.0, 1.1, 0.035, 0.2, 0.00001, 1.0],
            [2, 0, 1, 0.001, 60000.0, 0, 2.8, 0.5, 32.0, 1.1, 0.035, 0.2, 0.00001, 1.0],
        ],
        "points": [
            [[100.1, 30.1], [100.5, 30.4]],
            [[100.5, 30.4], [100.8, 30.8]],
        ],
        "parts": [[0], [0]],
    },
    "seg": {
        "shape_type": shapefile.POLYLINE,
        "fields": [("iRiv", "N", 50, 0), ("iEle", "N", 50, 0), ("Length", "F", 50, 3)],
        "records": [[1, 1, 100.0], [2, 2, 120.0]],
        "points": [
            [[100.1, 30.1], [100.5, 30.4]],
            [[100.5, 30.4], [100.8, 30.8]],
        ],
        "parts": [[0], [0]],
    },
}


def test_facade_owns_exactly_eight_sub_thousand_line_files() -> None:
    production_dir = ROOT / "services" / "production_closure"
    owners = {path.name for path in production_dir.glob("object_store_validation*.py")}
    assert owners == OWNER_FILENAMES
    assert all(
        sum(1 for _ in path.open(encoding="utf-8")) < 1000 for path in (production_dir / name for name in owners)
    )


def test_facade_preserves_baseline_surface_signatures_dataclasses_and_identities() -> None:
    actual_names = {name for name in vars(facade) if not name.startswith("__")}
    assert len(BASELINE_NAMES) == 159
    assert BASELINE_NAMES <= actual_names
    assert "_ConfigAnnotation" not in actual_names
    assert len(SIGNATURES) + 1 == 123
    assert len(DATACLASS_FIELDS) == 8
    for name, expected in SIGNATURES.items():
        assert str(inspect.signature(getattr(facade, name))) == expected
    field_signature = str(inspect.signature(facade.field))
    assert re.sub(r"0x[0-9a-f]+", "<address>", field_signature) == (
        "(*, default=<dataclasses._MISSING_TYPE object at <address>>, "
        "default_factory=<dataclasses._MISSING_TYPE object at <address>>, init=True, repr=True, hash=None, "
        "compare=True, metadata=None, kw_only=<dataclasses._MISSING_TYPE object at <address>>)"
    )
    for name, expected_fields in DATACLASS_FIELDS.items():
        actual_fields = dataclasses.fields(getattr(facade, name))
        assert [
            (field.name, field.type, field.default, field.default_factory, field.init, field.kw_only)
            for field in actual_fields
        ] == list(expected_fields)
    assert facade.LocalObjectStore is LocalObjectStore
    assert facade.ObjectStoreError is ObjectStoreError
    assert facade.SafeFilesystemError is safe_fs.SafeFilesystemError
    for name in (
        "ProductionObjectStoreValidationError",
        "ProductionObjectStoreConfig",
        "EvidenceWriter",
        "PackageChecksumReconstruction",
        "RuntimePrefixCollection",
        "RuntimeStagedObject",
        "RuntimeStagingBudget",
        "RuntimeStagingPreparation",
    ):
        assert getattr(facade, name).__module__ == facade.__name__
    expected_config_annotated = {
        name for name, signature in SIGNATURES.items() if "config: 'ProductionObjectStoreConfig'" in signature
    }
    config_annotated = {
        name
        for name, value in vars(facade).items()
        if getattr(value, "__annotations__", {}).get("config") == "ProductionObjectStoreConfig"
    }
    assert config_annotated == expected_config_annotated
    for name in config_annotated:
        assert typing.get_type_hints(getattr(facade, name))["config"] is facade.ProductionObjectStoreConfig
    for name in ("main", "validate_object_store", "write_synthetic_basins_fixture"):
        assert getattr(facade, name).__module__ == facade.__name__


def test_leaf_owners_do_not_import_the_historical_facade() -> None:
    production_dir = ROOT / "services" / "production_closure"
    module_name = "services.production_closure.object_store_validation"
    for name in OWNER_FILENAMES - {"object_store_validation.py"}:
        source = (production_dir / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports_facade = any(
            isinstance(node, ast.ImportFrom)
            and node.module == module_name
            or isinstance(node, ast.Import)
            and any(alias.name == module_name for alias in node.names)
            for node in ast.walk(tree)
        )
        assert not imports_facade, name
        assert not re.search(r"(?:^|\\n)\\s*(?:from|import)\\s+" + re.escape(module_name), source), name


def test_fresh_process_importers_and_module_usage_keep_historical_facade() -> None:
    for module_name in (
        "services.production_closure.object_store_validation",
        "services.production_closure.slurm_validation",
    ):
        imported = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert imported.returncode == 0, imported.stderr
    usage = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.production_closure.object_store_validation",
            "validate-object-store",
            "--bad-option",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode == 2
    assert usage.stdout == ""
    assert "Usage:" in usage.stderr and "No such option" in usage.stderr and "Traceback" not in usage.stderr


def test_fixture_stable_text_hashes_and_shapefile_semantics_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "Basins"
    facade.write_synthetic_basins_fixture(root)
    entries = list(root.rglob("*"))
    assert not any(path.is_symlink() for path in entries)
    actual_files = [path for path in entries if stat.S_ISREG(path.lstat().st_mode)]
    assert len(actual_files) == 27
    assert {path.relative_to(root).as_posix() for path in actual_files} == FIXTURE_PATHS
    assert {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest() for relative in STABLE_TEXT_SHA256
    } == STABLE_TEXT_SHA256
    gis_dir = root / "basin-a" / "input" / "alias-a" / "gis"
    for name, expected in SHAPEFILE_SEMANTICS.items():
        reader = shapefile.Reader(str(gis_dir / f"{name}.shp"))
        try:
            assert reader.shapeType == expected["shape_type"]
            assert [tuple(field) for field in reader.fields[1:]] == expected["fields"]
            assert [list(record) for record in reader.records()] == expected["records"]
            assert [[list(point) for point in shape.points] for shape in reader.shapes()] == expected["points"]
            assert [list(shape.parts) for shape in reader.shapes()] == expected["parts"]
        finally:
            reader.close()
