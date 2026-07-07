"""Readiness manifest completeness check (task 1.3 evidence gate).

Verifies readiness-manifest.v1.json against its .sha256 companion and
enforces the following FAIL classes:

1. Missing top-level required key.
2. null / literal ``"unresolved"`` / empty string, list, or dict at ANY depth
   (recursive scan across nested containers). The ONLY exemption is
   ``forcing_producer_limits.<limit>.override``, which may be null to
   encode "no deployment env override in effect" per tasks.md 1.1 line 5
   ("any deployment env overrides in effect" -> override=null == none in
   effect). No other ``override`` key anywhere is exempt.
3. Filename ``manifest_version`` segment does not match manifest's
   ``manifest_version`` value (filename ``readiness-manifest.<seg>.json``).
4. Missing required sub-key inside a structured identity block. For
   ``canonical_converter_versions`` the pinned set {gfs, ifs, era5} MUST be
   present as non-empty strings; additional converter kinds are permitted
   but MUST also be non-empty strings. For SHUD runtime staging limits,
   forcing producer limits, PROJ CRS metadata, and source_locations
   entries, the required keyset is enforced as documented per-block below.
5. ``schema_identity_status`` value not in the accepted set (``{"resolved"}``).
6. Given ``schema_identity_status == "resolved"`` (already enforced by
   class 5), ``db_schema_migration_repo_head`` MUST equal
   ``db_schema_migration_version``. If they disagree, the manifest is
   internally inconsistent (status claims resolved but the pins don't
   match) and the gate fails.

Read-only. stdlib-only. Exit 0 with ``PASS`` on success; exit 1 with
``FAIL:<key>:<reason>`` on the first failure.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

MANIFEST_FILENAME = "readiness-manifest.v1.json"
SHA256_FILENAME = "readiness-manifest.v1.json.sha256"

REQUIRED_TOP_LEVEL_KEYS = (
    "manifest_version", "created_utc", "baseline_commit",
    "forcing_producer_version", "forcing_producer_limits",
    "canonical_converter_versions", "shud_runtime_commit", "shud_executable",
    "shud_runtime_staging_limits", "db_schema_migration_repo_head",
    "db_schema_migration_version", "schema_identity_status",
    "schema_identity_note", "proj_crs_database_version",
    "mapping_builder_algorithm_version", "source_locations",
)

REQUIRED_CANONICAL_CONVERTER_KEYS = frozenset({"gfs", "ifs", "era5"})

REQUIRED_SHUD_STAGING_KEYS = frozenset({
    "MAX_DIRECT_GRID_TSD_FORC_BYTES",
    "MAX_DIRECT_GRID_FORCING_CSV_BYTES",
    "MAX_DIRECT_GRID_SP_ATT_BYTES",
    "MAX_DIRECT_GRID_TSD_FORC_LINES",
    "MAX_DIRECT_GRID_FORCING_CSV_LINES",
    "MAX_DIRECT_GRID_SP_ATT_LINES",
    "MAX_DIRECT_GRID_STAGING_LINE_BYTES",
})

# forcing_producer_limits key names match the v1 manifest's actual shape
# (not the paraphrased shorthand). Each of these sub-blocks itself carries
# {default, override, effective, env_var} which the recursive nullish scan
# will police (override may be null and is exempted below).
REQUIRED_FORCING_PRODUCER_LIMIT_KEYS = frozenset({
    "max_station_count",
    "max_timestep_count",
    "max_timeseries_row_count",
    "max_manifest_bytes",
})

REQUIRED_PROJ_CRS_KEYS = frozenset({"proj_version", "proj_db_metadata"})

# Every source_locations[key] entry SHALL carry at least a locator (file or
# command) and a host. line_or_range / line / range are optional because
# some identity sources are pure commands or self-references.
SOURCE_LOCATION_LOCATOR_KEYS = frozenset({"file", "command"})
SOURCE_LOCATION_REQUIRED_KEYS = frozenset({"host"})

SCHEMA_IDENTITY_ACCEPTED = frozenset({"resolved"})

# Path-anchored null exemption: only forcing_producer_limits.<limit>.override
# may be null (documented "no deployment env override in effect"). Every
# other ``override`` key anywhere else in the manifest is NOT exempt.
FORCING_PRODUCER_LIMITS_KEY = "forcing_producer_limits"

FILENAME_VERSION_RE = re.compile(r"^readiness-manifest\.(?P<seg>[^.]+)\.json$")


def fail(key: str, reason: str) -> None:
    print(f"FAIL:{key}:{reason}")
    sys.exit(1)


def _is_permitted_null(path: str, key: str) -> bool:
    """Return True iff ``path.key`` is exempt from the null-value gate.

    Only ``forcing_producer_limits.<any-limit>.override`` may be null
    (means "no env-override in effect", per tasks.md 1.1 line 5). Any
    other ``override`` key, anywhere else in the manifest, is NOT exempt.
    """
    if key != "override":
        return False
    parts = path.split(".") if path else []
    return len(parts) >= 2 and parts[0] == FORCING_PRODUCER_LIMITS_KEY


def scan_nullish(node: object, path: str) -> None:
    """Recursively assert that no leaf/container in ``node`` is null,
    the literal string ``"unresolved"``, or an empty string/list/dict.

    The only null exemption is ``forcing_producer_limits.<limit>.override``
    (see ``_is_permitted_null``). No other ``override`` leaf is exempt.
    """
    if node is None:
        fail(path, "value is null")
    if isinstance(node, str):
        if node == "unresolved":
            fail(path, "value is literal 'unresolved'")
        if len(node) == 0:
            fail(path, "value is empty string")
        return
    if isinstance(node, dict):
        if len(node) == 0:
            fail(path, "value is empty dict")
        for k, v in node.items():
            child_path = f"{path}.{k}" if path else k
            if v is None and _is_permitted_null(path, k):
                # forcing_producer_limits.<limit>.override may legitimately
                # be null; skip nullish scan for this exact leaf coordinate.
                continue
            scan_nullish(v, child_path)
        return
    if isinstance(node, list):
        if len(node) == 0:
            fail(path, "value is empty list")
        for i, item in enumerate(node):
            scan_nullish(item, f"{path}[{i}]")
        return
    # ints, floats, bools are leaf non-nullish values -> OK.


def require_exact_keyset(container: dict, actual_key: str, expected: frozenset) -> None:
    """Assert ``container`` has exactly the expected keyset (no missing, no extras)."""
    if not isinstance(container, dict):
        fail(actual_key, f"expected object, got {type(container).__name__}")
    actual = set(container.keys())
    missing = expected - actual
    if missing:
        fail(actual_key, f"missing required sub-keys: {sorted(missing)}")
    extras = actual - expected
    if extras:
        fail(actual_key, f"unexpected extra sub-keys: {sorted(extras)}")


def require_min_keyset(container: dict, actual_key: str, required: frozenset) -> None:
    """Assert ``container`` contains at least ``required`` sub-keys (extras OK)."""
    if not isinstance(container, dict):
        fail(actual_key, f"expected object, got {type(container).__name__}")
    missing = required - set(container.keys())
    if missing:
        fail(actual_key, f"missing required sub-keys: {sorted(missing)}")


def main() -> None:
    here = Path(__file__).resolve().parent
    manifest_path = here / MANIFEST_FILENAME
    sha_path = here / SHA256_FILENAME
    if not manifest_path.is_file():
        fail(MANIFEST_FILENAME, "manifest file missing")
    if not sha_path.is_file():
        fail(SHA256_FILENAME, "sha256 companion missing")

    raw = manifest_path.read_bytes()
    recomputed = hashlib.sha256(raw).hexdigest()
    expected_line = sha_path.read_text(encoding="utf-8").strip()
    expected = expected_line.split()[0] if expected_line else ""
    if recomputed != expected:
        fail(SHA256_FILENAME, f"sha256 mismatch: file={expected} recomputed={recomputed}")

    manifest = json.loads(raw.decode("utf-8"))

    # FAIL class 1: missing top-level key.
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in manifest:
            fail(key, "missing from manifest")

    # FAIL class 2: nullish (null / "unresolved" / empty str/list/dict) at any depth.
    for key in REQUIRED_TOP_LEVEL_KEYS:
        scan_nullish(manifest[key], key)

    # FAIL class 3: filename version segment must match manifest_version.
    m = FILENAME_VERSION_RE.match(MANIFEST_FILENAME)
    if m is None:
        fail("manifest_version", f"filename {MANIFEST_FILENAME!r} does not match expected pattern")
    expected_version = m.group("seg")
    if manifest["manifest_version"] != expected_version:
        fail(
            "manifest_version",
            f"filename segment {expected_version!r} != manifest value {manifest['manifest_version']!r}",
        )

    # FAIL class 4: required sub-key structure inside identity blocks.
    # canonical_converter_versions MUST contain gfs/ifs/era5 (additive:
    # extra converter kinds are permitted but MUST also be non-empty
    # strings). Every entry is validated for shape and non-emptiness.
    require_min_keyset(
        manifest["canonical_converter_versions"],
        "canonical_converter_versions",
        REQUIRED_CANONICAL_CONVERTER_KEYS,
    )
    for lang, value in manifest["canonical_converter_versions"].items():
        entry_key = f"canonical_converter_versions.{lang}"
        if not isinstance(value, str):
            fail(entry_key, f"expected string, got {type(value).__name__}")
        # Recursive nullish scan covers pinned keys (walked from the top),
        # but extras must also be non-empty strings. scan_nullish handles
        # both empty-string and null cases with a proper FAIL path.
        scan_nullish(value, entry_key)

    require_exact_keyset(
        manifest["shud_runtime_staging_limits"],
        "shud_runtime_staging_limits",
        REQUIRED_SHUD_STAGING_KEYS,
    )
    for limit_name, value in manifest["shud_runtime_staging_limits"].items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            fail(
                f"shud_runtime_staging_limits.{limit_name}",
                f"expected positive int, got {value!r}",
            )

    require_exact_keyset(
        manifest["forcing_producer_limits"],
        "forcing_producer_limits",
        REQUIRED_FORCING_PRODUCER_LIMIT_KEYS,
    )

    require_min_keyset(
        manifest["proj_crs_database_version"],
        "proj_crs_database_version",
        REQUIRED_PROJ_CRS_KEYS,
    )
    if not isinstance(manifest["proj_crs_database_version"]["proj_version"], str):
        fail(
            "proj_crs_database_version.proj_version",
            "expected string",
        )
    if not isinstance(manifest["proj_crs_database_version"]["proj_db_metadata"], dict):
        fail(
            "proj_crs_database_version.proj_db_metadata",
            "expected object",
        )

    # source_locations coverage: keys 1:1 with the other identity keys.
    identity_keys = {k for k in REQUIRED_TOP_LEVEL_KEYS if k != "source_locations"}
    source_locations = manifest["source_locations"]
    if not isinstance(source_locations, dict):
        fail("source_locations", "not an object")
    covered = set(source_locations.keys())
    missing = identity_keys - covered
    if missing:
        fail("source_locations", f"missing coverage: {sorted(missing)}")
    extra = covered - identity_keys
    if extra:
        fail("source_locations", f"unexpected keys: {sorted(extra)}")

    # Each source_locations entry must be a dict with at least a locator
    # (file or command) plus the required constant keys (host).
    for sl_key, entry in source_locations.items():
        entry_key = f"source_locations.{sl_key}"
        if not isinstance(entry, dict):
            fail(entry_key, f"expected object, got {type(entry).__name__}")
        require_min_keyset(entry, entry_key, SOURCE_LOCATION_REQUIRED_KEYS)
        if not (SOURCE_LOCATION_LOCATOR_KEYS & set(entry.keys())):
            fail(
                entry_key,
                f"missing locator: entry must carry at least one of {sorted(SOURCE_LOCATION_LOCATOR_KEYS)}",
            )

    # FAIL class 5: schema_identity_status must be in the accepted set.
    status = manifest["schema_identity_status"]
    if status not in SCHEMA_IDENTITY_ACCEPTED:
        fail(
            "schema_identity_status",
            f"value {status!r} not in accepted set {sorted(SCHEMA_IDENTITY_ACCEPTED)}",
        )

    # FAIL class 6: internal consistency between the two migration pins.
    # Class 5 already required schema_identity_status == "resolved", so at
    # this point repo_head MUST equal version — otherwise the manifest is
    # internally inconsistent (status claims resolved but the pins disagree).
    repo_head = manifest["db_schema_migration_repo_head"]
    version = manifest["db_schema_migration_version"]
    if repo_head != version:
        fail(
            "schema_identity_status",
            f"schema_identity_status='resolved' but "
            f"db_schema_migration_repo_head={repo_head!r} != db_schema_migration_version={version!r}; "
            f"manifest is internally inconsistent",
        )

    print("PASS")


if __name__ == "__main__":
    main()
