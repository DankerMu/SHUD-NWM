"""Readiness manifest completeness check (task 1.3 evidence gate).

Verifies readiness-manifest.v1.json against its .sha256, and asserts every
required identity field is present, non-null, not "unresolved", not empty.
source_locations must cover every identity key 1:1. Read-only.
Exit 0 PASS, 1 FAIL. Prints PASS or FAIL:<key>:<reason>.
"""

from __future__ import annotations

import hashlib
import json
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


def fail(key: str, reason: str) -> None:
    print(f"FAIL:{key}:{reason}")
    sys.exit(1)


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
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in manifest:
            fail(key, "missing from manifest")
        value = manifest[key]
        if value is None:
            fail(key, "value is null")
        if isinstance(value, str) and value == "unresolved":
            fail(key, "value is literal 'unresolved'")
        if isinstance(value, (str, list, dict)) and len(value) == 0:
            fail(key, "value is empty string/list/dict")

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

    print("PASS")


if __name__ == "__main__":
    main()
