#!/usr/bin/env python3
"""Read-only node-27 hot/cold inventory and completeness-receipt publisher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

import jsonschema

from packages.common.source_identity import normalize_source_id
from packages.common.storage import (
    ArchiveConfigurationError,
    ArchiveIdentity,
    archive_identity_for_state_reference,
    archive_provenance_paths,
    validate_product_archive_manifest_binding,
)

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SALVAGE_MANIFESTS = 10_000
MAX_SALVAGE_DEPTH = 8
MAX_SUBJECTS = 100_000
STATEMENT_TIMEOUT_MS = 20_000
SCHEMA_VERSION = "1.0"
_ROOT = Path(__file__).resolve().parents[1]
COMPLETENESS_SCHEMA_PATH = _ROOT / "schemas/archive_completeness_receipt.schema.json"
PRODUCT_SCHEMA_PATH = _ROOT / "schemas/product_archive_manifest.schema.json"
SALVAGE_SCHEMA_PATH = _ROOT / "schemas/salvage_manifest.schema.json"


class AuditBlocked(RuntimeError):
    """Raised when evidence is unsafe or the gate receipt cannot be proved."""


@dataclass(frozen=True)
class InventorySubject:
    lane: str
    subject_id: str
    source_id: str | None
    cycle_time: datetime
    start: datetime
    end: datetime
    model_id: str
    basin_version_id: str | None = None
    hot_uri: str = ""
    checksum: str | None = None
    state_id: str | None = None
    cloned_from_state_id: str | None = None
    cloned_from_model_id: str | None = None
    clone_gate_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.lane not in {"forcing", "runs", "states"}:
            raise AuditBlocked(f"unknown subject lane: {self.lane}")
        if not self.subject_id or not self.model_id:
            raise AuditBlocked("subject identity fields must be non-empty")
        for value in (self.cycle_time, self.start, self.end):
            _require_aware(value)
        if self.start > self.end:
            raise AuditBlocked(f"inverted subject window: {self.stable_key}")

    @property
    def stable_key(self) -> tuple[str, str]:
        return self.lane, self.subject_id

    @property
    def window(self) -> dict[str, str]:
        return {"start": _time(self.start), "end": _time(self.end)}

    @property
    def selector(self) -> dict[str, Any] | None:
        if self.lane == "states":
            return None
        identity_key = "forcing_version_id" if self.lane == "forcing" else "run_id"
        table = "met.forcing_station_timeseries" if self.lane == "forcing" else "hydro.river_timeseries"
        return {"table": table, "identity": {identity_key: self.subject_id}, "window": self.window}

    @property
    def archive_identity(self) -> ArchiveIdentity:
        if self.lane == "states":
            physical_model = self.cloned_from_model_id or self.model_id
            return archive_identity_for_state_reference(
                source_id=self.source_id, model_id=physical_model, valid_time=self.cycle_time
            )
        source = normalize_source_id(self.source_id or "")
        cycle = self.cycle_time.astimezone(UTC)
        return ArchiveIdentity(
            lane=self.lane,
            source=source,
            cycle_identity=cycle.strftime("%Y%m%d%H"),
            cycle_time=cycle.strftime("%Y-%m-%dT%H:00:00Z"),
            basin_version_id=self.basin_version_id if self.lane == "forcing" else None,
            model_id=self.model_id if self.lane == "forcing" else None,
            run_id=self.subject_id if self.lane == "runs" else None,
        )


@dataclass(frozen=True)
class AuditConfig:
    database_url: str
    object_store_root: Path
    object_store_prefix: str
    archive_root: Path
    archive_min_age_days: int
    receipt_path: Path


@dataclass(frozen=True)
class Coverage:
    mechanism: str
    evidence: tuple[str, ...] = ()


class ConnectionFactory(Protocol):
    def __call__(self, dsn: str) -> Any: ...


FORCING_INVENTORY_SQL = """
SELECT fv.forcing_version_id, fv.model_id, fv.source_id, fv.cycle_time,
       fv.start_time, fv.end_time, fv.forcing_package_uri, fv.checksum,
       fst_presence.basin_version_id,
       EXISTS (SELECT 1 FROM met.forcing_station_timeseries x
               WHERE x.forcing_version_id = fv.forcing_version_id
                 AND x.valid_time < fv.start_time LIMIT 1) AS before_window,
       EXISTS (SELECT 1 FROM met.forcing_station_timeseries x
               WHERE x.forcing_version_id = fv.forcing_version_id
                 AND x.valid_time > fv.end_time LIMIT 1) AS after_window,
       EXISTS (SELECT 1 FROM met.forcing_station_timeseries x
                 WHERE x.forcing_version_id = fv.forcing_version_id
                   AND (x.basin_version_id <> fst_presence.basin_version_id
                        OR LOWER(x.source_id) <> LOWER(fv.source_id)) LIMIT 1) AS identity_drift
FROM met.forcing_version fv
CROSS JOIN LATERAL (
  SELECT x.basin_version_id
  FROM met.forcing_station_timeseries x
  WHERE x.forcing_version_id = fv.forcing_version_id
  LIMIT 1
) fst_presence
ORDER BY fv.forcing_version_id
LIMIT 100001
"""

RUN_INVENTORY_SQL = """
SELECT r.run_id, r.model_id, r.basin_version_id, r.source_id, r.cycle_time,
       r.start_time, r.end_time, r.run_manifest_uri, r.output_uri,
       rt_presence.detail_present,
       EXISTS (SELECT 1 FROM hydro.river_timeseries x
               WHERE x.run_id = r.run_id AND x.valid_time < r.start_time LIMIT 1) AS before_window,
       EXISTS (SELECT 1 FROM hydro.river_timeseries x
               WHERE x.run_id = r.run_id AND x.valid_time > r.end_time LIMIT 1) AS after_window,
       EXISTS (SELECT 1 FROM hydro.river_timeseries x
                 WHERE x.run_id = r.run_id
                   AND x.basin_version_id <> r.basin_version_id LIMIT 1) AS identity_drift
FROM hydro.hydro_run r
CROSS JOIN LATERAL (
  SELECT 1 AS detail_present
  FROM hydro.river_timeseries x
  WHERE x.run_id = r.run_id
  LIMIT 1
) rt_presence
ORDER BY r.run_id
LIMIT 100001
"""

STATE_INVENTORY_SQL = """
SELECT state_id, model_id, run_id, source_id, valid_time, state_uri, checksum,
       cloned_from_state_id, cloned_from_model_id, clone_gate_fingerprint
FROM hydro.state_snapshot
ORDER BY state_id
LIMIT 100001
"""


def load_inventory(connection: Any) -> tuple[datetime, list[InventorySubject]]:
    """Capture all subjects in one bounded, read-only repeatable-read snapshot."""
    with connection.cursor() as cursor:
        cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
        cursor.execute("SELECT statement_timestamp() AS audit_time")
        audit_time = _row_mapping(cursor, cursor.fetchone())["audit_time"]
        cursor.execute(FORCING_INVENTORY_SQL)
        forcing_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
        cursor.execute(RUN_INVENTORY_SQL)
        run_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
        cursor.execute(STATE_INVENTORY_SQL)
        state_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
    connection.rollback()
    if len(forcing_rows) + len(run_rows) + len(state_rows) > MAX_SUBJECTS:
        raise AuditBlocked(f"inventory exceeds {MAX_SUBJECTS} subjects")
    subjects: list[InventorySubject] = []
    for row in forcing_rows:
        _validate_detail_bounds(row, "forcing_version_id")
        if not row["cycle_time"]:
            raise AuditBlocked(f"forcing version {row['forcing_version_id']} has no cycle_time")
        subjects.append(
            InventorySubject(
                lane="forcing",
                subject_id=str(row["forcing_version_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                start=row["start_time"],
                end=row["end_time"],
                model_id=str(row["model_id"]),
                basin_version_id=str(row["basin_version_id"]),
                hot_uri=str(row["forcing_package_uri"]),
                checksum=str(row["checksum"] or ""),
            )
        )
    for row in run_rows:
        _validate_detail_bounds(row, "run_id")
        if not row["cycle_time"]:
            raise AuditBlocked(f"run {row['run_id']} has no cycle_time")
        subjects.append(
            InventorySubject(
                lane="runs",
                subject_id=str(row["run_id"]),
                source_id=str(row["source_id"] or ""),
                cycle_time=row["cycle_time"],
                start=row["start_time"],
                end=row["end_time"],
                model_id=str(row["model_id"]),
                basin_version_id=str(row["basin_version_id"]),
                hot_uri=json.dumps({"manifest": row["run_manifest_uri"], "output": row["output_uri"]}),
            )
        )
    for row in state_rows:
        clone_values = [
            row.get(name) for name in ("cloned_from_state_id", "cloned_from_model_id", "clone_gate_fingerprint")
        ]
        if any(clone_values) and not all(clone_values):
            raise AuditBlocked(f"state {row['state_id']} has incomplete clone provenance")
        subjects.append(
            InventorySubject(
                lane="states",
                subject_id=str(row["state_id"]),
                state_id=str(row["state_id"]),
                source_id=str(row["source_id"]) if row["source_id"] not in (None, "") else None,
                cycle_time=row["valid_time"],
                start=row["valid_time"],
                end=row["valid_time"],
                model_id=str(row["model_id"]),
                hot_uri=str(row["state_uri"]),
                checksum=str(row["checksum"]),
                cloned_from_state_id=row.get("cloned_from_state_id"),
                cloned_from_model_id=row.get("cloned_from_model_id"),
                clone_gate_fingerprint=row.get("clone_gate_fingerprint"),
            )
        )
    if not subjects:
        raise AuditBlocked("inventory is empty")
    if len(subjects) > MAX_SUBJECTS:
        raise AuditBlocked(f"inventory exceeds {MAX_SUBJECTS} subjects")
    if len({subject.stable_key for subject in subjects}) != len(subjects):
        raise AuditBlocked("inventory contains duplicate stable subjects")
    return _require_aware(audit_time), sorted(subjects, key=lambda value: value.stable_key)


def discover_salvage(
    archive_root: Path, *, mismatch_evidence: dict[str, str] | None = None
) -> tuple[dict[str, Any], ...]:
    """Return verified exact selectors from a bounded, symlink-safe namespace scan."""
    base = archive_root / "db-export"
    if archive_root.is_symlink() or base.is_symlink():
        raise AuditBlocked("archive/db-export root must not be a symlink")
    if not archive_root.exists() or not base.exists():
        return ()
    _require_directory(base, archive_root)
    manifests: list[Path] = []
    stack = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise AuditBlocked(f"symlink in salvage namespace: {entry}")
            if stat.S_ISDIR(info.st_mode):
                if depth >= MAX_SALVAGE_DEPTH:
                    raise AuditBlocked(f"salvage scan exceeds depth {MAX_SALVAGE_DEPTH}: {entry}")
                stack.append((entry, depth + 1))
            elif entry.name == "manifest.json" and stat.S_ISREG(info.st_mode):
                manifests.append(entry)
                if len(manifests) > MAX_SALVAGE_MANIFESTS:
                    raise AuditBlocked(f"salvage scan exceeds {MAX_SALVAGE_MANIFESTS} manifests")
    schema = _load_schema(SALVAGE_SCHEMA_PATH)
    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for path in manifests:
        manifest = _read_json(path, archive_root)
        _validate_schema(manifest, schema, str(path))
        for export in manifest["exports"]:
            selector = export["selector"]
            key = _canonical(selector)
            if key in seen:
                raise AuditBlocked(f"duplicate/conflicting salvage selector: {key}")
            seen.add(key)
            obj = export["object"]
            target = _contained_file(archive_root / obj["path"], archive_root)
            size, digest = _size_sha256(target)
            if size != obj["size_bytes"] or digest != obj["sha256"]:
                if mismatch_evidence is not None:
                    mismatch_evidence[key] = "db-export object size/sha256 mismatch"
                continue
            found[key] = selector
    return tuple(found[key] for key in sorted(found))


def verify_product_archive(subject: InventorySubject, archive_root: Path) -> Coverage | None:
    paths = archive_provenance_paths(archive_root, identity=subject.archive_identity)
    if archive_root.is_symlink() or paths.manifest.is_symlink() or paths.archive.is_symlink():
        raise AuditBlocked(f"symlink in product archive evidence: {subject.stable_key}")
    if not archive_root.exists() or (not paths.manifest.exists() and not paths.archive.exists()):
        return None
    if paths.manifest.exists() != paths.archive.exists():
        return None
    manifest = _read_json(paths.manifest, archive_root)
    _validate_schema(manifest, _load_schema(PRODUCT_SCHEMA_PATH), str(paths.manifest))
    try:
        expected = validate_product_archive_manifest_binding(archive_root, manifest)
    except ArchiveConfigurationError as error:
        raise AuditBlocked(str(error)) from error
    if expected != paths:
        raise AuditBlocked(f"archive path binding differs for {subject.stable_key}")
    target = _contained_file(paths.archive, archive_root)
    size, digest = _size_sha256(target)
    declared = manifest["archive"]
    if size != declared["size_bytes"] or digest != declared["sha256"]:
        return Coverage("none", ("product archive size/sha256 mismatch",))
    return Coverage("product-archive", ("checksum-verified product archive present",))


def verify_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    if subject.lane == "forcing":
        return _verify_forcing_hot(subject, config)
    if subject.lane == "runs":
        return _verify_run_hot(subject, config)
    return _verify_state_hot(subject, config)


def _verify_forcing_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    cycle = subject.cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    source = normalize_source_id(subject.source_id or "").lower()
    expected = f"forcing/{source}/{cycle}/{subject.basin_version_id}/{subject.model_id}"
    key = _object_key(subject.hot_uri, config.object_store_prefix)
    if key != expected:
        raise AuditBlocked(f"forcing URI identity mismatch for {subject.subject_id}: {key}")
    manifest_path = config.object_store_root / key / "forcing_package.json"
    if manifest_path.is_symlink():
        raise AuditBlocked(f"forcing manifest is a symlink: {subject.subject_id}")
    if not manifest_path.exists():
        return None
    manifest = _read_json(manifest_path, config.object_store_root)
    if not subject.checksum or _sha256(manifest_path) != subject.checksum:
        raise AuditBlocked(f"forcing manifest checksum mismatch for {subject.subject_id}")
    identity = {
        "forcing_version_id": subject.subject_id,
        "source_id": subject.source_id,
        "cycle_time": _time(subject.cycle_time),
        "model_id": subject.model_id,
        "basin_version_id": subject.basin_version_id,
    }
    for name, expected_value in identity.items():
        actual = manifest.get(name)
        if name == "source_id":
            try:
                actual, expected_value = normalize_source_id(str(actual)), normalize_source_id(str(expected_value))
            except ValueError as error:
                raise AuditBlocked(f"forcing manifest source invalid: {error}") from error
        elif name == "cycle_time":
            actual = _time(_parse_time(actual))
        if actual != expected_value:
            raise AuditBlocked(f"forcing manifest {name} mismatch for {subject.subject_id}")
    manifest_start = _parse_time(manifest.get("start_time"))
    manifest_end = _parse_time(manifest.get("end_time"))
    if manifest_start > subject.start or manifest_end < subject.end:
        raise AuditBlocked(f"forcing manifest range does not contain DB window: {subject.subject_id}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AuditBlocked(f"forcing manifest has no files: {subject.subject_id}")
    for entry in files:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("uri"), str)
            or not isinstance(entry.get("checksum"), str)
        ):
            raise AuditBlocked(f"malformed forcing file entry: {subject.subject_id}")
        file_key = _object_key(entry["uri"], config.object_store_prefix)
        if not file_key.startswith(expected + "/"):
            raise AuditBlocked(f"forcing file escapes package: {file_key}")
        path = _contained_file(config.object_store_root / file_key, config.object_store_root)
        if _sha256(path) != entry["checksum"]:
            raise AuditBlocked(f"forcing file checksum mismatch: {file_key}")
    return Coverage("hot-object-store", ("row-bound forcing package and files checksum-verified",))


def _verify_run_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    try:
        refs = json.loads(subject.hot_uri)
    except json.JSONDecodeError as error:
        raise AuditBlocked(f"malformed internal run refs: {subject.subject_id}") from error
    manifest_key = _object_key(str(refs.get("manifest") or ""), config.object_store_prefix)
    output_key = _object_key(str(refs.get("output") or ""), config.object_store_prefix).rstrip("/")
    expected_manifest = f"runs/{subject.subject_id}/input/manifest.json"
    expected_output = f"runs/{subject.subject_id}/output"
    if manifest_key != expected_manifest or output_key != expected_output:
        raise AuditBlocked(f"run URI identity mismatch for {subject.subject_id}")
    manifest_path = config.object_store_root / manifest_key
    output_path = config.object_store_root / output_key
    if manifest_path.is_symlink() or output_path.is_symlink():
        raise AuditBlocked(f"run evidence is a symlink: {subject.subject_id}")
    if not manifest_path.exists() and not output_path.exists():
        return None
    manifest = _read_json(manifest_path, config.object_store_root)
    expected = {
        "run_id": subject.subject_id,
        "source_id": normalize_source_id(subject.source_id or ""),
        "cycle_time": _time(subject.cycle_time),
        "start_time": _time(subject.start),
        "end_time": _time(subject.end),
    }
    actual_model = manifest.get("model")
    if not isinstance(actual_model, Mapping):
        raise AuditBlocked(f"run manifest missing model identity: {subject.subject_id}")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise AuditBlocked(f"run manifest missing output identity: {subject.subject_id}")
    actual = {
        "run_id": manifest.get("run_id"),
        "source_id": normalize_source_id(str(manifest.get("source_id") or "")),
        "cycle_time": _time(_parse_time(manifest.get("cycle_time"))),
        "start_time": _time(_parse_time(manifest.get("start_time"))),
        "end_time": _time(_parse_time(manifest.get("end_time"))),
    }
    if (
        actual != expected
        or actual_model.get("model_id") != subject.model_id
        or actual_model.get("basin_version_id") != subject.basin_version_id
        or _object_key(str(outputs.get("run_manifest_uri") or ""), config.object_store_prefix) != expected_manifest
        or _object_key(str(outputs.get("output_uri") or ""), config.object_store_prefix).rstrip("/") != expected_output
    ):
        raise AuditBlocked(f"run manifest row identity mismatch: {subject.subject_id}")
    _require_directory(output_path, config.object_store_root)
    if not _directory_has_regular_file(output_path, config.object_store_root):
        raise AuditBlocked(f"run output has no regular product: {subject.subject_id}")
    return Coverage("hot-object-store", ("row-bound input manifest and run output present",))


def _verify_state_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    key = _object_key(subject.hot_uri, config.object_store_prefix)
    physical_model = subject.cloned_from_model_id or subject.model_id
    cycle = subject.cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    if subject.source_id:
        expected_prefix = f"states/{normalize_source_id(subject.source_id)}/{physical_model}/{cycle}/"
    else:
        expected_prefix = f"states/{physical_model}/{cycle}/"
    if not key.startswith(expected_prefix):
        raise AuditBlocked(f"state URI row/provenance identity mismatch: {subject.subject_id}")
    path = config.object_store_root / key
    if path.is_symlink():
        raise AuditBlocked(f"state evidence is a symlink: {subject.subject_id}")
    if not path.exists():
        return None
    target = _contained_file(path, config.object_store_root)
    if _sha256(target) != subject.checksum:
        raise AuditBlocked(f"state checksum mismatch: {subject.subject_id}")
    return Coverage("hot-object-store", ("state artifact checksum-verified",))


def build_receipt(
    subjects: Sequence[InventorySubject],
    *,
    audit_time: datetime,
    archive_min_age_days: int,
    product_coverage: Mapping[tuple[str, str], Coverage | None],
    salvage_selectors: Sequence[Mapping[str, Any]],
    hot_coverage: Mapping[tuple[str, str], Coverage | None],
    salvage_mismatches: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not subjects:
        raise AuditBlocked("inventory is empty")
    audit_time = _require_aware(audit_time)
    keys = [subject.stable_key for subject in subjects]
    if len(set(keys)) != len(keys):
        raise AuditBlocked("duplicate inventory subject")
    salvage = {_canonical(selector): selector for selector in salvage_selectors}
    windows: list[dict[str, Any]] = []
    required_selectors: dict[str, dict[str, Any]] = {}
    for subject in sorted(subjects, key=lambda value: value.stable_key):
        evidence: list[str] = []
        product = product_coverage.get(subject.stable_key)
        selector_key = _canonical(subject.selector) if subject.selector is not None else None
        if product and product.mechanism == "product-archive":
            coverage, verdict = "product-archive", "complete"
            evidence.extend(product.evidence)
        elif selector_key is not None and selector_key in salvage:
            coverage, verdict = "db-export", "complete"
            evidence.append("checksum-verified exact db-export selector present")
        else:
            if product:
                evidence.extend(product.evidence)
            if selector_key is not None and salvage_mismatches and selector_key in salvage_mismatches:
                evidence.append(salvage_mismatches[selector_key])
            hot = hot_coverage.get(subject.stable_key)
            if hot and hot.mechanism == "hot-object-store":
                coverage = "hot-object-store"
                if subject.end > audit_time - timedelta(days=archive_min_age_days):
                    verdict = "complete"
                else:
                    verdict = "pending-archive"
                evidence.extend(hot.evidence)
            else:
                coverage, verdict = "none", "gap"
                evidence.append("no verified archive, db-export, or hot product")
                if subject.selector is not None:
                    required_selectors[selector_key] = subject.selector
        identity_key = {"forcing": "forcing_version_id", "runs": "run_id", "states": "state_id"}[subject.lane]
        windows.append(
            {
                "lane": subject.lane,
                "subject": {identity_key: subject.subject_id},
                "window": subject.window,
                "coverage": coverage,
                "verdict": verdict,
                "evidence": evidence,
            }
        )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _time(audit_time),
        "coverage_bounds": {
            "start": min(item["window"]["start"] for item in windows),
            "end": max(item["window"]["end"] for item in windows),
        },
        "windows": windows,
        "salvage_selectors": [required_selectors[key] for key in sorted(required_selectors)],
    }
    validate_receipt_semantics(receipt, subjects)
    _validate_schema(receipt, _load_schema(COMPLETENESS_SCHEMA_PATH), "archive completeness receipt")
    return receipt


def validate_receipt_semantics(receipt: Mapping[str, Any], subjects: Sequence[InventorySubject] | None = None) -> None:
    windows = receipt.get("windows")
    selectors = receipt.get("salvage_selectors")
    if not isinstance(windows, list) or not windows or not isinstance(selectors, list):
        raise AuditBlocked("receipt windows/selectors have invalid shape")
    subject_keys: list[tuple[str, str]] = []
    expected_selectors: set[str] = set()
    starts: list[str] = []
    ends: list[str] = []
    for item in windows:
        lane = item["lane"]
        identity_key = {"forcing": "forcing_version_id", "runs": "run_id", "states": "state_id"}[lane]
        subject_keys.append((lane, item["subject"][identity_key]))
        start, end = item["window"]["start"], item["window"]["end"]
        if _parse_time(start) > _parse_time(end):
            raise AuditBlocked("receipt contains inverted window")
        starts.append(start)
        ends.append(end)
        if item["verdict"] == "gap" and lane != "states":
            table = "met.forcing_station_timeseries" if lane == "forcing" else "hydro.river_timeseries"
            expected_selectors.add(
                _canonical(
                    {
                        "table": table,
                        "identity": {identity_key: item["subject"][identity_key]},
                        "window": item["window"],
                    }
                )
            )
    if len(set(subject_keys)) != len(subject_keys):
        raise AuditBlocked("receipt contains duplicate subject")
    if subjects is not None and set(subject_keys) != {subject.stable_key for subject in subjects}:
        raise AuditBlocked("receipt omitted or invented inventory subjects")
    actual_selectors = [_canonical(selector) for selector in selectors]
    if len(set(actual_selectors)) != len(actual_selectors) or set(actual_selectors) != expected_selectors:
        raise AuditBlocked("forcing/run gap-selector bijection failed")
    if receipt.get("coverage_bounds") != {"start": min(starts), "end": max(ends)}:
        raise AuditBlocked("receipt coverage_bounds do not match subject set")


def publish_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path = _validate_output_path(path)
    payload = (_canonical(receipt) + "\n").encode()
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_audit(config: AuditConfig, *, connect: ConnectionFactory | None = None) -> dict[str, Any]:
    _validate_audit_roots(config)
    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    connection = connect(config.database_url)
    try:
        audit_time, subjects = load_inventory(connection)
    finally:
        connection.close()
    salvage_mismatches: dict[str, str] = {}
    salvage = discover_salvage(config.archive_root, mismatch_evidence=salvage_mismatches)
    product: dict[tuple[str, str], Coverage | None] = {}
    hot: dict[tuple[str, str], Coverage | None] = {}
    for subject in subjects:
        product[subject.stable_key] = verify_product_archive(subject, config.archive_root)
        hot[subject.stable_key] = verify_hot(subject, config)
    receipt = build_receipt(
        subjects,
        audit_time=audit_time,
        archive_min_age_days=config.archive_min_age_days,
        product_coverage=product,
        salvage_selectors=salvage,
        hot_coverage=hot,
        salvage_mismatches=salvage_mismatches,
    )
    publish_receipt(config.receipt_path, receipt)
    return receipt


def config_from_args(args: argparse.Namespace) -> AuditConfig:
    database_url = (args.database_url or os.getenv("DATABASE_URL") or "").strip()
    object_root = _absolute(args.object_store_root or os.getenv("OBJECT_STORE_ROOT"), "object_store_root")
    archive_root = _absolute(
        args.archive_root or os.getenv("NODE27_STORAGE_INVENTORY_ARCHIVE_ROOT") or os.getenv("NHMS_ARCHIVE_ROOT"),
        "archive_root",
    )
    receipt_path = _absolute(args.receipt_path or os.getenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH"), "receipt_path")
    if not database_url:
        raise AuditBlocked("DATABASE_URL is required")
    raw_age = args.archive_min_age_days or os.getenv("NHMS_ARCHIVE_MIN_AGE_DAYS") or "45"
    try:
        age = int(raw_age)
    except ValueError as error:
        raise AuditBlocked("NHMS_ARCHIVE_MIN_AGE_DAYS must be an integer") from error
    if age < 1:
        raise AuditBlocked("NHMS_ARCHIVE_MIN_AGE_DAYS must be positive")
    return AuditConfig(
        database_url,
        object_root,
        (args.object_store_prefix or os.getenv("OBJECT_STORE_PREFIX") or "").strip(),
        archive_root,
        age,
        receipt_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--object-store-root")
    parser.add_argument("--object-store-prefix")
    parser.add_argument("--archive-root")
    parser.add_argument("--archive-min-age-days", type=int)
    parser.add_argument("--receipt-path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    config: AuditConfig | None = None
    try:
        config = config_from_args(build_parser().parse_args(argv))
        receipt = run_audit(config)
    except Exception as error:
        message = str(error)
        if config is not None and config.database_url:
            message = message.replace(config.database_url, "[DATABASE_URL]")
        print(
            _canonical({"status": "blocked", "error_type": type(error).__name__, "message": message}),
            file=sys.stderr,
        )
        return 1
    print(
        _canonical(
            {"status": "published", "receipt_path": str(config.receipt_path), "subjects": len(receipt["windows"])}
        )
    )
    return 0


def _validate_detail_bounds(row: Mapping[str, Any], id_key: str) -> None:
    if row.get("before_window") or row.get("after_window") or row.get("identity_drift"):
        raise AuditBlocked(f"detail bounds drift outside metadata window: {row[id_key]}")


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(instance: Any, schema: Mapping[str, Any], label: str) -> None:
    try:
        jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(instance)
    except jsonschema.ValidationError as error:
        raise AuditBlocked(
            f"{label} schema validation failed at {list(error.absolute_path)}: {error.message}"
        ) from error


def _read_json(path: Path, root: Path) -> dict[str, Any]:
    target = _contained_file(path, root)
    if target.stat().st_size > MAX_MANIFEST_BYTES:
        raise AuditBlocked(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes: {target}")
    try:
        with target.open("rb") as stream:
            content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise AuditBlocked(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes: {target}")
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditBlocked(f"cannot read JSON evidence {target}: {error}") from error
    if not isinstance(value, dict):
        raise AuditBlocked(f"JSON evidence must be an object: {target}")
    return value


def _contained_file(path: Path, root: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        info = path.lstat()
    except (OSError, ValueError) as error:
        raise AuditBlocked(f"unsafe or unreadable evidence path {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AuditBlocked(f"evidence is not a regular non-symlink file: {path}")
    _assert_no_symlink_components(path)
    return resolved


def _require_directory(path: Path, root: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        info = path.lstat()
    except (OSError, ValueError) as error:
        raise AuditBlocked(f"unsafe or unreadable directory {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AuditBlocked(f"not a regular non-symlink directory: {path}")
    _assert_no_symlink_components(path)
    return resolved


def _assert_no_symlink_components(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise AuditBlocked(f"symlink path component: {current}")
        if current == current.parent:
            return
        current = current.parent


def _directory_has_regular_file(directory: Path, root: Path) -> bool:
    found = False
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError as error:
            raise AuditBlocked(f"cannot read run output directory {current}: {error}") from error
        for entry in entries:
            try:
                info = entry.lstat()
                entry.resolve(strict=True).relative_to(root.resolve(strict=True))
            except (OSError, ValueError) as error:
                raise AuditBlocked(f"unsafe run output entry {entry}: {error}") from error
            if stat.S_ISLNK(info.st_mode):
                raise AuditBlocked(f"symlink in run output: {entry}")
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry)
            elif stat.S_ISREG(info.st_mode):
                found = True
            else:
                raise AuditBlocked(f"unsafe non-regular run output: {entry}")
    return found


def _object_key(uri: str, prefix: str) -> str:
    raw = uri.strip()
    if not raw or "?" in raw or "#" in raw or "\\" in raw:
        raise AuditBlocked(f"invalid object-store URI: {raw!r}")
    if raw.startswith("s3://"):
        parsed = urlparse(raw)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise AuditBlocked(f"invalid object-store URI: {raw!r}")
        if not prefix:
            raise AuditBlocked("OBJECT_STORE_PREFIX is required for s3 URI binding")
        expected = urlparse(prefix.rstrip("/"))
        if expected.scheme != "s3" or expected.netloc != parsed.netloc:
            raise AuditBlocked(f"object URI outside configured prefix: {raw}")
        object_path, prefix_path = unquote(parsed.path).strip("/"), unquote(expected.path).strip("/")
        if prefix_path:
            if not object_path.startswith(prefix_path + "/"):
                raise AuditBlocked(f"object URI outside configured prefix: {raw}")
            object_path = object_path[len(prefix_path) + 1 :]
        raw = object_path
    elif "://" in raw or raw.startswith("/"):
        raise AuditBlocked(f"unsupported object-store URI: {raw}")
    key = raw.strip("/")
    parts = key.split("/")
    if (
        not key
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
    ):
        raise AuditBlocked(f"unsafe object key: {key!r}")
    return key


def _validate_output_path(path: Path) -> Path:
    if not path.is_absolute():
        raise AuditBlocked(f"receipt path must be absolute: {path}")
    parent = path.parent
    if not parent.exists():
        raise AuditBlocked(f"receipt parent does not exist: {parent}")
    _require_directory(parent, Path(path.anchor))
    if path.is_symlink():
        raise AuditBlocked(f"receipt target is a symlink: {path}")
    return path


def _validate_audit_roots(config: AuditConfig) -> None:
    _require_directory(config.object_store_root, Path(config.object_store_root.anchor))
    if config.archive_root.is_symlink():
        raise AuditBlocked(f"archive root is a symlink: {config.archive_root}")
    if config.archive_root.exists():
        _require_directory(config.archive_root, Path(config.archive_root.anchor))
    else:
        current = config.archive_root.parent
        while not current.exists():
            if current == current.parent:
                raise AuditBlocked(f"archive root has no existing parent: {config.archive_root}")
            current = current.parent
        _require_directory(current, Path(current.anchor))
    _validate_output_path(config.receipt_path)


def _absolute(value: str | None, label: str) -> Path:
    if not value or not value.strip():
        raise AuditBlocked(f"{label} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AuditBlocked(f"{label} must be absolute: {path}")
    return path


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuditBlocked(f"timestamp must be timezone-aware: {value!r}")
    return value.astimezone(UTC)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AuditBlocked(f"timestamp must be a string: {value!r}")
    try:
        return _require_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise AuditBlocked(f"invalid timestamp: {value!r}") from error


def _time(value: datetime) -> str:
    return _require_aware(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(path: Path) -> str:
    return _size_sha256(path)[1]


def _size_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise AuditBlocked(f"cannot checksum evidence {path}: {error}") from error
    return size, digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
