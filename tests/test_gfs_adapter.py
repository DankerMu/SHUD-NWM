from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes

base = importlib.import_module("workers.data_adapters.base")
cli_module = importlib.import_module("workers.data_adapters.cli")
converter_module = importlib.import_module("workers.canonical_converter.converter")
gfs_module = importlib.import_module("workers.data_adapters.gfs_adapter")

DownloadManifest = base.DownloadManifest
ManifestEntry = base.ManifestEntry
parse_cycle_time = base.parse_cycle_time
CanonicalConverter = converter_module.CanonicalConverter
CanonicalConverterConfig = converter_module.CanonicalConverterConfig
RawRecord = converter_module.RawRecord
FileUnavailableError = gfs_module.FileUnavailableError
FileTooLargeError = gfs_module.FileTooLargeError
GFSAdapter = gfs_module.GFSAdapter
GFSAdapterConfig = gfs_module.GFSAdapterConfig
DownloadedPayload = gfs_module.DownloadedPayload
ForbiddenSourceError = gfs_module.ForbiddenSourceError


@pytest.fixture(autouse=True)
def _clear_gfs_cycle_hours_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("GFS_CYCLE_HOURS_UTC", raising=False)


class FakeMetRepository:
    def __init__(self) -> None:
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}
        self.ensure_calls = 0
        self.cycle_upsert_calls = 0
        self.update_calls = 0

    def ensure_data_source(self, **kwargs: Any) -> dict[str, Any]:
        self.ensure_calls += 1
        self.data_sources[kwargs["source_id"]] = dict(kwargs)
        return self.data_sources[kwargs["source_id"]]

    def upsert_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.cycle_upsert_calls += 1
        key = (kwargs["source_id"], kwargs["cycle_time"])
        existing = self.cycles.get(key, {})
        existing.update(kwargs)
        self.cycles[key] = existing
        return existing

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any] | None:
        self.update_calls += 1
        key = (kwargs["source_id"], kwargs["cycle_time"])
        cycle = self.cycles.setdefault(key, {"source_id": kwargs["source_id"], "cycle_time": kwargs["cycle_time"]})
        for field in ("status", "manifest_uri", "retry_count", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                cycle[field] = kwargs[field]
        return cycle

    def get_forecast_cycle(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.cycles.get((kwargs["source_id"], kwargs["cycle_time"]))


class FakeCanonicalRepository:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        product = self.products.get(canonical_product_id)
        return dict(product) if product is not None else None

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> list[dict[str, Any]]:
        return [
            dict(product)
            for product in self.products.values()
            if product.get("source_id") == source_id and product.get("cycle_time") == cycle_time
        ]

    def upsert_canonical_product(self, record: dict[str, Any]) -> dict[str, Any]:
        self.products[record["canonical_product_id"]] = dict(record)
        return self.products[record["canonical_product_id"]]

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["source_id"], kwargs["cycle_time"])
        cycle = self.cycles.setdefault(key, {"source_id": kwargs["source_id"], "cycle_time": kwargs["cycle_time"]})
        for field in ("status", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                cycle[field] = kwargs[field]
        return cycle


def build_adapter(
    tmp_path: Path,
    *,
    repository: FakeMetRepository | None = None,
    downloader: Any | None = None,
    availability_checker: Any | None = None,
    max_retries: int = 1,
    max_wait_seconds: float = 0,
    download_chunk_size_bytes: int = 8 * 1024 * 1024,
    max_file_size_bytes: int = 500 * 1024 * 1024,
) -> GFSAdapter:
    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        # NOMADS-only chain keeps these tests focused on the grib-filter download
        # semantics (server-side subset, polling, retries). The cloud-mirror idx+Range
        # path is exercised by the dedicated mirror tests below.
        source_backends=("nomads",),
        poll_interval_seconds=0,
        nomads_min_interval_seconds=0,
        max_wait_seconds=max_wait_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=(0,),
        download_chunk_size_bytes=download_chunk_size_bytes,
        max_file_size_bytes=max_file_size_bytes,
    )
    return GFSAdapter(
        config=config,
        repository=repository or FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader or (lambda _url: b"GRIB mock bytes 7777"),
        availability_checker=availability_checker or (lambda _url: True),
        sleeper=lambda _seconds: None,
    )


def one_entry_manifest(tmp_path: Path, expected_checksum: str | None = None) -> tuple[GFSAdapter, DownloadManifest]:
    adapter = build_adapter(tmp_path)
    cycle_time = parse_cycle_time("2026050700")
    entry = ManifestEntry(
        remote_url="mock://gfs/file",
        local_key="raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f000.tmp2m.grib2",
        variable="tmp2m",
        forecast_hour=0,
        expected_checksum=expected_checksum,
    )
    return adapter, DownloadManifest(source_id="gfs", cycle_time=cycle_time, entries=(entry,))


def _query_items(url: str) -> frozenset[tuple[str, tuple[str, ...]]]:
    return frozenset((key, tuple(values)) for key, values in parse_qs(urlparse(url).query).items())


def test_cycle_discovery_upserts_four_available_cycles(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository, availability_checker=lambda _url: True)

    cycles = adapter.discover_cycles("2026-05-07")

    assert [cycle.cycle_id for cycle in cycles] == [
        "gfs_2026050700",
        "gfs_2026050706",
        "gfs_2026050712",
        "gfs_2026050718",
    ]
    assert all(cycle.available for cycle in cycles)
    assert len(repository.cycles) == 4
    assert {cycle["status"] for cycle in repository.cycles.values()} == {"discovered"}
    assert repository.data_sources["gfs"]["adapter_name"] == "gfs_adapter"


def test_cycle_hours_env_narrows_gfs_discovery_and_data_source_config(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GFS_CYCLE_HOURS_UTC", "0,12")
    repository = FakeMetRepository()
    calls: list[str] = []

    def checker(url: str) -> bool:
        calls.append(url)
        return True

    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        source_backends=("nomads",),
        nomads_min_interval_seconds=0,
    )
    adapter = GFSAdapter(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(tmp_path),
        availability_checker=checker,
        sleeper=lambda _seconds: None,
    )

    cycles = adapter.discover_cycles("2026-05-07")

    assert config.cycle_hours_utc == (0, 12)
    assert [cycle.cycle_hour for cycle in cycles] == [0, 12]
    assert repository.data_sources["gfs"]["config_json"]["cycle_hours_utc"] == [0, 12]
    assert [("t00z" in url, "t12z" in url) for url in calls] == [(True, False), (False, True)]
    assert all("t06z" not in url and "t18z" not in url for url in calls)


@pytest.mark.parametrize("env_value", ["12,0,12", "12, 0,12"])
def test_cycle_hours_env_normalizes_duplicate_unordered_gfs_hours(
    tmp_path: Path,
    monkeypatch: Any,
    env_value: str,
) -> None:
    monkeypatch.setenv("GFS_CYCLE_HOURS_UTC", env_value)

    config = GFSAdapterConfig(workspace_root=tmp_path)

    assert config.cycle_hours_utc == (0, 12)


@pytest.mark.parametrize("env_value", ["", "0,,12", "abc", "24", "-1"])
def test_cycle_hours_env_rejects_malformed_gfs_hours(
    tmp_path: Path,
    monkeypatch: Any,
    env_value: str,
) -> None:
    monkeypatch.setenv("GFS_CYCLE_HOURS_UTC", env_value)

    with pytest.raises(ValueError, match="GFS_CYCLE_HOURS_UTC"):
        GFSAdapterConfig(workspace_root=tmp_path)


def test_cycle_hours_direct_config_normalizes_duplicate_unordered_gfs_hours(tmp_path: Path) -> None:
    config = GFSAdapterConfig(workspace_root=tmp_path, cycle_hours_utc=(12, 0, 12))

    assert config.cycle_hours_utc == (0, 12)


@pytest.mark.parametrize("cycle_hours_utc", [(True,), ("12",), (12.5,)])
def test_cycle_hours_direct_config_rejects_non_integer_gfs_hours(
    tmp_path: Path,
    cycle_hours_utc: tuple[Any, ...],
) -> None:
    with pytest.raises(ValueError, match="cycle_hours_utc must contain integer UTC cycle hours"):
        GFSAdapterConfig(workspace_root=tmp_path, cycle_hours_utc=cycle_hours_utc)


def test_cycle_hours_env_unset_preserves_legacy_gfs_default(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("GFS_CYCLE_HOURS_UTC", raising=False)

    config = GFSAdapterConfig(workspace_root=tmp_path)

    assert config.cycle_hours_utc == (0, 6, 12, 18)


def test_forbidden_nomads_discovery_does_not_gate_available_cloud_cycle(tmp_path: Path) -> None:
    def checker(url: str) -> bool:
        if url.endswith(".idx") and "s3.amazonaws.com" in url:
            return True
        raise ForbiddenSourceError("rate-limited", attempts=1)

    adapter = GFSAdapter(
        config=GFSAdapterConfig(
            workspace_root=tmp_path,
            source_backends=("s3", "nomads"),
        ),
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=lambda _url: b"GRIB mock bytes 7777",
        availability_checker=checker,
        sleeper=lambda _seconds: None,
    )

    cycles = adapter.discover_cycles("2026-05-07")

    assert cycles, "expected at least one discovered cycle"
    for cycle in cycles:
        assert cycle.status == "discovered"
        assert cycle.available is True
        assert "s3.amazonaws.com" in cycle.probe_uri


def test_manifest_keeps_f000_instantaneous_fields_and_omits_f000_interval_fields(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    manifest = adapter.build_manifest("2026050700")

    assert len(manifest.entries) == 56 * 7 + 5
    assert manifest.metadata["total_file_count"] == 397
    assert manifest.metadata["physical_file_layout"] == "per_forecast_hour_bundle"
    assert manifest.metadata["physical_file_count"] == 57
    assert len({entry.local_key for entry in manifest.entries}) == 57
    assert manifest.metadata["variable_count"] == 7
    assert manifest.metadata["requested_forecast_hours"][0] == 0
    assert manifest.metadata["forecast_hours"][0] == 0
    assert manifest.metadata["first_forecast_hour"] == 0
    assert manifest.metadata["last_forecast_hour"] == 168
    assert not any(entry.forecast_hour == 0 and entry.variable in {"apcp", "dswrf"} for entry in manifest.entries)
    assert sorted(entry.variable for entry in manifest.entries if entry.forecast_hour == 0) == [
        "pressfc",
        "rh2m",
        "tmp2m",
        "u10m",
        "v10m",
    ]
    assert {entry.local_key for entry in manifest.entries if entry.forecast_hour == 0} == {
        "raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f000.bundle.grib2"
    }
    assert all(entry.metadata["bundle"]["layout"] == "per_forecast_hour" for entry in manifest.entries)
    assert all("cfgrib_filter_by_keys" in entry.metadata for entry in manifest.entries)
    assert manifest.manifest_uri == "raw/gfs/2026050700/manifest.json"
    assert adapter.object_store.exists("raw/gfs/2026050700/manifest.json")


def test_manifest_uses_one_physical_gfs_bundle_per_forecast_hour(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])

    assert len(manifest.entries) == 7
    assert {entry.local_key for entry in manifest.entries} == {
        "raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f003.bundle.grib2"
    }
    expected_url = adapter.remote_bundle_url("2026050700", 3, tuple(adapter.config.variables))
    assert {
        (urlparse(entry.remote_url).scheme, urlparse(entry.remote_url).netloc, urlparse(entry.remote_url).path)
        for entry in manifest.entries
    } == {(urlparse(expected_url).scheme, urlparse(expected_url).netloc, urlparse(expected_url).path)}
    assert {_query_items(entry.remote_url) for entry in manifest.entries} == {_query_items(expected_url)}
    assert all(entry.metadata["bundle"]["variables"] == list(adapter.config.variables) for entry in manifest.entries)
    assert all(
        entry.metadata["logical_remote_url"] == adapter.remote_url("2026050700", 3, entry.variable)
        for entry in manifest.entries
    )


def test_download_plan_downloads_gfs_bundle_once_for_logical_entries(tmp_path: Path) -> None:
    content = b"GRIB bundled GFS payload 7777"
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        return content

    adapter = build_adapter(tmp_path, downloader=downloader)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])

    result = adapter.download_plan(manifest)

    assert result.status == "raw_complete"
    assert len(result.files) == 7
    assert {file.local_key for file in result.files} == {
        "raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f003.bundle.grib2"
    }
    assert calls == [manifest.entries[0].remote_url]
    assert adapter.object_store.read_bytes(manifest.entries[0].local_key) == content


def test_manifest_keeps_f000_when_configured_variables_are_available(tmp_path: Path) -> None:
    adapter = GFSAdapter(
        config=GFSAdapterConfig(
            workspace_root=tmp_path,
            variables=("tmp2m",),
            forecast_end_hour=3,
        ),
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=lambda _url: b"GRIB mock bytes 7777",
        availability_checker=lambda _url: True,
        sleeper=lambda _seconds: None,
    )

    manifest = adapter.build_manifest("2026050700")

    assert [entry.forecast_hour for entry in manifest.entries] == [0, 3]
    assert manifest.metadata["forecast_hours"] == [0, 3]
    assert manifest.metadata["source_policy"]["variable_availability"][
        "f000_keeps_available_instantaneous_variables"
    ] is True


def test_forecast_hours_can_be_limited_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GFS_FORECAST_START_HOUR", "3")
    monkeypatch.setenv("GFS_FORECAST_END_HOUR", "24")
    repository = FakeMetRepository()
    adapter = GFSAdapter(
        config=GFSAdapterConfig(workspace_root=tmp_path),
        repository=repository,
        object_store=LocalObjectStore(tmp_path),
        downloader=lambda _url: b"GRIB mock bytes 7777",
        availability_checker=lambda _url: True,
        sleeper=lambda _seconds: None,
    )

    manifest = adapter.build_manifest("2026050700")

    assert len(manifest.entries) == 8 * 7
    assert len({entry.local_key for entry in manifest.entries}) == 8
    assert manifest.metadata["first_forecast_hour"] == 3
    assert manifest.metadata["last_forecast_hour"] == 24
    assert repository.data_sources["gfs"]["config_json"]["forecast_hours"]["start"] == 3
    assert repository.data_sources["gfs"]["config_json"]["forecast_hours"]["end"] == 24


def test_native_resolution_segments_drive_variable_step_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GFS_FORECAST_RESOLUTION_SEGMENTS", "6:1,168:3")
    monkeypatch.setenv("GFS_FORECAST_START_HOUR", "0")
    monkeypatch.setenv("GFS_FORECAST_END_HOUR", "12")
    repository = FakeMetRepository()
    adapter = GFSAdapter(
        config=GFSAdapterConfig(workspace_root=tmp_path),
        repository=repository,
        object_store=LocalObjectStore(tmp_path),
        downloader=lambda _url: b"GRIB mock bytes 7777",
        availability_checker=lambda _url: True,
        sleeper=lambda _seconds: None,
    )

    manifest = adapter.build_manifest("2026050700")

    forecast_hours = sorted({entry.forecast_hour for entry in manifest.entries})
    assert forecast_hours == [0, 1, 2, 3, 4, 5, 6, 9, 12]
    assert manifest.metadata["requested_forecast_hours"] == [0, 1, 2, 3, 4, 5, 6, 9, 12]
    assert manifest.metadata["forecast_hours"] == [0, 1, 2, 3, 4, 5, 6, 9, 12]
    assert manifest.metadata["source_policy"]["forecast_resolution_segments"] == [[6, 1], [168, 3]]


@pytest.mark.parametrize("forecast_hours", [[-3], [1], [171]])
def test_build_manifest_rejects_invalid_forecast_hours(tmp_path: Path, forecast_hours: list[int]) -> None:
    adapter = build_adapter(tmp_path)

    with pytest.raises(ValueError, match="GFS forecast hour"):
        adapter.build_manifest("2026050700", forecast_hours=forecast_hours)


def test_verify_manifest_checksum_match_and_mismatch(tmp_path: Path) -> None:
    content = b"GRIB checksum payload 7777"
    checksum = sha256_bytes(content)
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=checksum)
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, content)

    assert adapter.verify_manifest(manifest).passed is True

    bad_entry = ManifestEntry(
        remote_url=manifest.entries[0].remote_url,
        local_key=manifest.entries[0].local_key,
        variable="tmp2m",
        forecast_hour=0,
        expected_checksum="bad",
    )
    bad_manifest = DownloadManifest(source_id="gfs", cycle_time=manifest.cycle_time, entries=(bad_entry,))

    result = adapter.verify_manifest(bad_manifest)

    assert result.status == "partial_fail"
    assert result.failures[0].error_code == "CHECKSUM_MISMATCH"


def test_download_plan_polling_timeout_records_failure(tmp_path: Path) -> None:
    repository = FakeMetRepository()

    def unavailable(_url: str) -> bytes:
        raise FileUnavailableError("not published", attempts=1)

    adapter = build_adapter(
        tmp_path,
        repository=repository,
        downloader=unavailable,
        max_retries=1,
        max_wait_seconds=0,
    )
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])

    result = adapter.download_plan(
        DownloadManifest(source_id="gfs", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))
    )

    cycle = repository.cycles[("gfs", manifest.cycle_time)]
    assert result.status == "failed_download"
    assert cycle["status"] == "failed_download"
    assert cycle["error_code"] == "POLL_TIMEOUT"
    assert "Timed out" in cycle["error_message"]


def test_download_plan_is_idempotent_when_checksum_matches(tmp_path: Path) -> None:
    content = b"GRIB already downloaded 7777"
    checksum = sha256_bytes(content)
    calls = {"download": 0}

    def downloader(_url: str) -> bytes:
        calls["download"] += 1
        return content

    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=checksum)
    adapter.downloader = downloader
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, content)

    first = adapter.download_plan(manifest)
    second = adapter.download_plan(manifest)

    assert first.files[0].status == "already_done"
    assert second.files[0].status == "already_done"
    assert first.total_bytes_written == 0
    assert second.total_bytes_written == 0
    assert calls["download"] == 0


def test_download_plan_raw_complete_idempotency_does_not_modify_cycle(tmp_path: Path) -> None:
    content = b"GRIB already downloaded 7777"
    checksum = sha256_bytes(content)
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=checksum)
    repository = adapter.repository
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, content)
    repository.cycles[("gfs", manifest.cycle_time)] = {
        "source_id": "gfs",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
        "retry_count": 7,
    }
    updates_before = repository.update_calls

    result = adapter.download_plan(manifest)

    assert result.status == "already_done"
    assert result.files[0].status == "already_done"
    assert repository.update_calls == updates_before
    assert repository.cycles[("gfs", manifest.cycle_time)]["retry_count"] == 7


def test_raw_complete_untrusted_existing_object_is_redownloaded_not_already_done(tmp_path: Path) -> None:
    stale = b"GRIB stale collision bytes"
    fresh = b"GRIB fresh source bytes"
    downloads = 0

    def downloader(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return fresh

    adapter, manifest = one_entry_manifest(tmp_path)
    adapter.downloader = downloader
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, stale)
    adapter.repository.cycles[("gfs", manifest.cycle_time)] = {
        "source_id": "gfs",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
    }

    result = adapter.download_plan(manifest)

    assert result.status == "raw_complete"
    assert result.files[0].status == "downloaded"
    assert result.files[0].checksum == sha256_bytes(fresh)
    assert adapter.object_store.read_bytes(manifest.entries[0].local_key) == fresh
    assert downloads == 1


def test_raw_complete_trusted_existing_object_reuses_already_done(tmp_path: Path) -> None:
    content = b"GRIB trusted existing bytes"
    downloads = 0

    def downloader(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return b"GRIB should not download"

    adapter = build_adapter(tmp_path)
    built_manifest = adapter.build_manifest("2026050700", forecast_hours=[3])
    entry = built_manifest.entries[0]
    manifest = DownloadManifest(source_id="gfs", cycle_time=built_manifest.cycle_time, entries=(entry,))
    adapter.downloader = downloader
    adapter.object_store.write_bytes_atomic(entry.local_key, content)
    manifest.metadata["source_policy"] = adapter.source_policy_identity([3])
    manifest.metadata["source_object_identity"] = adapter.source_object_identity(manifest.cycle_time, [3])
    manifest = replace(manifest, manifest_uri="raw/gfs/2026050700/prior_manifest.json")
    adapter.object_store.write_bytes_atomic(
        manifest.manifest_uri,
        json.dumps(manifest.as_dict(), sort_keys=True).encode("utf-8"),
    )
    adapter.repository.cycles[("gfs", manifest.cycle_time)] = {
        "source_id": "gfs",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
        "manifest_uri": manifest.manifest_uri,
    }

    result = adapter.download_plan(manifest)

    assert result.status == "already_done"
    assert result.files[0].status == "already_done"
    assert result.files[0].checksum == sha256_bytes(content)
    assert downloads == 0


def test_selector_policy_change_invalidates_prior_raw_complete_manifest(tmp_path: Path) -> None:
    stale = b"GRIB stale apcp bucket bytes"
    fresh = b"GRIB fresh cumulative bytes"
    downloads = 0

    def downloader(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return fresh

    adapter = build_adapter(tmp_path, downloader=downloader)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])
    apcp_entry = next(entry for entry in manifest.entries if entry.variable == "apcp")
    adapter.object_store.write_bytes_atomic(apcp_entry.local_key, stale)

    old_manifest = replace(manifest, entries=(apcp_entry,), manifest_uri="raw/gfs/2026050700/old_manifest.json")
    old_manifest.metadata["source_policy"] = {
        "source": "gfs",
        "forecast_hours": [3],
        "variables": ["apcp"],
    }
    old_manifest.metadata["source_object_identity"] = {
        "identity_schema_version": "nhms.source_object_identity.v2",
        "source": "gfs",
        "cycle_time": manifest.cycle_time.isoformat(),
        "raw_entry_observation_digest_by_key": {
            apcp_entry.local_key: {
                "status": "present",
                "checksum": sha256_bytes(stale),
                "size_bytes": len(stale),
            }
        },
    }
    adapter.object_store.write_bytes_atomic(
        old_manifest.manifest_uri or "",
        json.dumps(old_manifest.as_dict(), sort_keys=True).encode("utf-8"),
    )
    adapter.repository.cycles[("gfs", manifest.cycle_time)] = {
        "source_id": "gfs",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
        "manifest_uri": old_manifest.manifest_uri,
    }

    result = adapter.download_plan(
        DownloadManifest(source_id="gfs", cycle_time=manifest.cycle_time, entries=(apcp_entry,))
    )

    assert result.status == "raw_complete"
    assert result.files[0].status == "downloaded"
    assert adapter.object_store.read_bytes(apcp_entry.local_key) == fresh
    assert downloads == 1


def test_data_source_initialization_is_upserted_not_duplicated(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    adapter.initialize_data_source()
    adapter.initialize_data_source()

    assert len(repository.data_sources) == 1
    assert repository.ensure_calls == 2
    assert repository.data_sources["gfs"]["source_name"] == "gfs"
    assert repository.data_sources["gfs"]["native_format"] == "GRIB2"


def test_network_failure_records_error_code_and_retry_count(tmp_path: Path) -> None:
    repository = FakeMetRepository()

    def failing_downloader(_url: str) -> bytes:
        raise RuntimeError("connection refused")

    adapter = build_adapter(
        tmp_path,
        repository=repository,
        downloader=failing_downloader,
        max_retries=2,
        max_wait_seconds=0,
    )
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])

    result = adapter.download_plan(
        DownloadManifest(source_id="gfs", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))
    )

    cycle = repository.cycles[("gfs", manifest.cycle_time)]
    assert result.status == "failed_download"
    assert cycle["status"] == "failed_download"
    assert cycle["error_code"] == "NETWORK_ERROR"
    assert result.retry_count == 1
    assert cycle["retry_count"] == 1
    assert "connection refused" in cycle["error_message"]


def test_url_download_reads_bounded_chunks_and_enforces_max_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ChunkedResponse:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.chunks = [b"ab", b"cd"]
            self.read_sizes: list[int] = []

        def __enter__(self) -> ChunkedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return self.chunks.pop(0) if self.chunks else b""

    response = ChunkedResponse()
    adapter = build_adapter(tmp_path, download_chunk_size_bytes=2, max_file_size_bytes=3)
    monkeypatch.setattr(gfs_module, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(FileTooLargeError):
        adapter._download_url("https://example.test/gfs.grib2")

    assert response.read_sizes == [2, 2]


def test_http_get_range_rejects_200_ok_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FullResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> FullResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return b"GRIB full object"

    adapter = build_adapter(tmp_path)
    adapter.downloader = adapter._download_url
    adapter._has_injected_downloader = False
    monkeypatch.setattr(gfs_module, "urlopen", lambda *_args, **_kwargs: FullResponse())

    with pytest.raises(gfs_module.NetworkDownloadError, match="expected 206"):
        adapter._http_get_range("https://example.test/gfs.grib2", 0, 10)


def test_http_get_range_accepts_matching_content_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class RangeResponse:
        status = 206
        headers = {"Content-Range": "bytes 0-9/100"}

        def __init__(self) -> None:
            self.sent = False

        def __enter__(self) -> RangeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b"0123456789"

    adapter = build_adapter(tmp_path)
    adapter.downloader = adapter._download_url
    adapter._has_injected_downloader = False
    monkeypatch.setattr(gfs_module, "urlopen", lambda *_args, **_kwargs: RangeResponse())

    assert adapter._http_get_range("https://example.test/gfs.grib2", 0, 10) == b"0123456789"


@pytest.mark.parametrize(
    "payload",
    [
        b"GRIB oversized",
        DownloadedPayload(content=b"GRIB oversized", checksum="sha", bytes_written=len(b"GRIB oversized")),
    ],
)
def test_injected_payload_enforces_max_size_before_object_store_write(tmp_path: Path, payload: Any) -> None:
    adapter = build_adapter(tmp_path, downloader=lambda _url: payload, max_file_size_bytes=4)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])
    entry = manifest.entries[0]

    result = adapter.download_plan(DownloadManifest(source_id="gfs", cycle_time=manifest.cycle_time, entries=(entry,)))

    assert result.status == "failed_download"
    assert result.files[0].error_code == "FILE_TOO_LARGE"
    assert not adapter.object_store.exists(entry.local_key)


def test_source_object_identity_changes_when_remote_or_policy_content_changes(tmp_path: Path) -> None:
    first = build_adapter(tmp_path / "first")
    second = build_adapter(tmp_path / "second")
    second.config = GFSAdapterConfig(
        workspace_root=tmp_path / "second",
        base_url="https://alternate.example.test/gfs",
        forecast_end_hour=3,
    )

    first_identity = first.source_object_identity("2026050700", [0, 3])
    second_identity = second.source_object_identity("2026050700", [0, 3])
    changed_policy_identity = first.source_object_identity("2026050700", [0])

    assert first_identity["identity_schema_version"] == "nhms.source_object_identity.v3"
    assert first_identity["apcp_selector_policy"] == "prefer_cycle_cumulative_else_unique_interval_bucket"
    assert first_identity["manifest_digest"] != second_identity["manifest_digest"]
    assert first_identity["raw_entry_digest"] != second_identity["raw_entry_digest"]
    assert first_identity["manifest_digest"] != changed_policy_identity["manifest_digest"]
    assert first_identity["raw_entry_count"] == 12
    assert len(first_identity["raw_entry_samples"]) == 2


def test_source_object_identity_changes_when_same_key_content_changes(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])
    entry = manifest.entries[0]

    before = adapter.source_object_identity("2026050700", [3])
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB first payload 7777")
    first = adapter.source_object_identity("2026050700", [3])
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB second payload 7777")
    second = adapter.source_object_identity("2026050700", [3])

    assert before["raw_entry_digest"] != first["raw_entry_digest"]
    assert first["raw_entry_digest"] != second["raw_entry_digest"]
    assert first["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"
    assert first["raw_entry_samples"][0]["observed_raw_object"]["checksum"] != (
        second["raw_entry_samples"][0]["observed_raw_object"]["checksum"]
    )


def test_source_object_identity_blocks_oversized_raw_before_checksum(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path, max_file_size_bytes=4)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[3])
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB oversized raw payload")
    original_store = adapter.object_store

    class NoChecksumStore:
        def exists(self, key_or_uri: str) -> bool:
            return original_store.exists(key_or_uri)

        def size(self, key_or_uri: str) -> int:
            return original_store.size(key_or_uri)

        def checksum(self, key_or_uri: str) -> str:
            del key_or_uri
            raise AssertionError("oversized raw object must block before checksum")

    adapter.object_store = NoChecksumStore()  # type: ignore[assignment]

    identity = adapter.source_object_identity(manifest.cycle_time, [3])
    observation = identity["raw_entry_observation_digest_by_key"][entry.local_key]

    assert observation["status"] == "oversized"
    assert observation["checksum"] is None


def test_downloaded_manifest_identity_feeds_gfs_canonical_readiness_and_blocks_stale_reuse(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    manifest = adapter.build_manifest("2026050700", forecast_hours=[0, 3])
    pre_download_identity = manifest.metadata["source_object_identity"]
    assert pre_download_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "missing"

    adapter.downloader = lambda url: f"GRIB GFS bundle fixture {url}".encode("utf-8")

    download = adapter.download_plan(manifest)

    effective_forecast_hours = manifest.metadata["forecast_hours"]
    observed_identity = adapter.source_object_identity(manifest.cycle_time, effective_forecast_hours)
    persisted_manifest = adapter.load_manifest(manifest.manifest_uri or "")
    assert download.status == "raw_complete"
    assert manifest.metadata["source_object_identity"] == observed_identity
    assert persisted_manifest.metadata["source_object_identity"] == observed_identity
    assert observed_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"
    assert observed_identity["raw_entry_digest"] != pre_download_identity["raw_entry_digest"]

    repository = FakeCanonicalRepository()
    converter = CanonicalConverter(
        config=CanonicalConverterConfig(workspace_root=tmp_path),
        repository=repository,
        object_store=LocalObjectStore(tmp_path),
    )

    def read_record(entry: dict[str, Any]) -> Any:
        variable = str(entry["variable"])
        forecast_hour = int(entry["forecast_hour"])
        values_by_variable = {
            "tmp2m": 285.0 + forecast_hour,
            "rh2m": 50.0,
            "u10m": 3.0,
            "v10m": 4.0,
            "pressfc": 101325.0,
            "apcp": float(forecast_hour),
            "dswrf": 100.0,
        }
        metadata = dict(entry.get("metadata") or {})
        if variable == "apcp":
            metadata.setdefault("idx_selector", {"accumulation_type": "cumulative_since_cycle", "step_range": "0-3"})
        return RawRecord(
            source_file=converter.object_store.uri_for_key(str(entry["local_key"])),
            native_variable=variable,
            forecast_hour=forecast_hour,
            values=(values_by_variable[variable],),
            longitudes=(0.0,),
            latitudes=(0.0,),
            shape=(1,),
            metadata=metadata,
        )

    converter._read_record = read_record  # type: ignore[method-assign]
    conversion = converter.convert_manifest(manifest.as_dict())
    readiness = converter.canonical_readiness(
        cycle_time=manifest.cycle_time,
        forecast_hours=[3],
        policy_identity=manifest.metadata["source_policy"],
        source_object_identity=observed_identity,
        canonical_product_id="canon_gfs_2026050700",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert conversion.status == "canonical_ready"
    assert readiness.ready is True
    lineage_identity = repository.products["gfs_2026050700_air_temperature_2m_f003"]["lineage_json"][
        "source_object_identity"
    ]
    assert lineage_identity == observed_identity
    assert lineage_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"

    changed_entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(
        changed_entry.local_key,
        b"GRIB GFS changed bundle fixture",
    )
    changed_identity = adapter.source_object_identity(manifest.cycle_time, effective_forecast_hours)
    stale_readiness = converter.canonical_readiness(
        cycle_time=manifest.cycle_time,
        forecast_hours=[3],
        policy_identity=manifest.metadata["source_policy"],
        source_object_identity=changed_identity,
        canonical_product_id="canon_gfs_2026050700",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert changed_identity["raw_entry_digest"] != observed_identity["raw_entry_digest"]
    assert stale_readiness.ready is False
    assert stale_readiness.evidence["reason"] == "canonical_identity_mismatch"
    assert stale_readiness.evidence["source_object_identity_matched"] is False


def test_cli_download_exits_nonzero_on_failed_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_downloader(_url: str) -> bytes:
        raise RuntimeError("connection refused")

    adapter = build_adapter(tmp_path, downloader=failing_downloader, max_retries=1, max_wait_seconds=0)
    monkeypatch.setattr(cli_module.GFSAdapter, "from_env", staticmethod(lambda: adapter))

    with pytest.raises(SystemExit) as error:
        cli_module._download("gfs", "2026050700")

    assert error.value.code == 1
    assert "Download failed for gfs 2026050700: NETWORK_ERROR" in capsys.readouterr().err


# --------------------------------------------------------------------------- cloud mirror chain

IdxParseError = gfs_module.IdxParseError
AmbiguousIdxRecordError = gfs_module.AmbiguousIdxRecordError
IdxRecordNotFoundError = gfs_module.IdxRecordNotFoundError
IdxSelectorPolicyError = gfs_module.IdxSelectorPolicyError
CdoMissingError = gfs_module.CdoMissingError
CdoClipError = gfs_module.CdoClipError
AllSourcesUnavailableError = gfs_module.AllSourcesUnavailableError
RateLimitedError = gfs_module.RateLimitedError


# A wgrib2 .idx for cycle 2026050700. f000 carries the instantaneous fields (anl) but
# NOT apcp/dswrf (undefined at analysis time); the fNNN form is "<fh> hour fcst"; the
# accumulated/averaged fields carry a window token "0-<fh> hour acc/ave fcst".
F000_IDX = "\n".join(
    [
        "1:0:d=2026050700:PRMSL:mean sea level:anl:",
        "2:1000:d=2026050700:TMP:2 m above ground:anl:",
        "3:2000:d=2026050700:RH:2 m above ground:anl:",
        "4:3000:d=2026050700:UGRD:10 m above ground:anl:",
        "5:4000:d=2026050700:VGRD:10 m above ground:anl:",
        "6:5000:d=2026050700:PRES:surface:anl:",
        "7:6000:d=2026050700:HGT:surface:anl:",
    ]
)
F003_IDX = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:3 hour fcst:",
        "2:1500:d=2026050700:RH:2 m above ground:3 hour fcst:",
        "3:3000:d=2026050700:UGRD:10 m above ground:3 hour fcst:",
        "4:4500:d=2026050700:VGRD:10 m above ground:3 hour fcst:",
        "5:6000:d=2026050700:PRES:surface:3 hour fcst:",
        "6:7500:d=2026050700:APCP:surface:0-3 hour acc fcst:",
        "7:9000:d=2026050700:DSWRF:surface:0-3 hour ave fcst:",
    ]
)
# f006/f012 carry FV3-GFS APCP dual semantics: old interval/bucket records and
# cycle-cumulative 0-fhr records may coexist in the same .idx.
F006_IDX_DUP_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:6 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:0-6 hour acc fcst:",
        "3:3000:d=2026050700:APCP:surface:0-6 hour acc fcst:",
        "4:4500:d=2026050700:DSWRF:surface:0-6 hour ave fcst:",
    ]
)
F009_IDX_MIXED_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:9 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:6-9 hour acc fcst:",
        "3:3000:d=2026050700:APCP:surface:0-9 hour acc fcst:",
        "4:4500:d=2026050700:DSWRF:surface:6-9 hour ave fcst:",
    ]
)
F009_IDX_DUP_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:9 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:0-9 hour acc fcst:",
        "3:3000:d=2026050700:APCP:surface:0-9 hour acc fcst:",
        "4:4500:d=2026050700:DSWRF:surface:6-9 hour ave fcst:",
    ]
)
F012_IDX_MIXED_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:12 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:6-12 hour acc fcst:",
        "3:3000:d=2026050700:APCP:surface:0-12 hour acc fcst:",
        "4:4500:d=2026050700:DSWRF:surface:6-12 hour ave fcst:",
    ]
)
F003_IDX_DUP_TMP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:3 hour fcst:",
        "2:1500:d=2026050700:TMP:2 m above ground:3 hour fcst:",
        "3:3000:d=2026050700:RH:2 m above ground:3 hour fcst:",
        "4:4500:d=2026050700:APCP:surface:0-3 hour acc fcst:",
    ]
)
F012_IDX_BUCKET_ONLY_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:12 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:6-12 hour acc fcst:",
        "3:3000:d=2026050700:DSWRF:surface:6-12 hour ave fcst:",
    ]
)
F024_IDX_DAY_CUMULATIVE_APCP = "\n".join(
    [
        "1:0:d=2026050700:TMP:2 m above ground:24 hour fcst:",
        "2:1500:d=2026050700:APCP:surface:18-24 hour acc fcst:",
        "3:3000:d=2026050700:APCP:surface:0-1 day acc fcst:",
        "4:4500:d=2026050700:DSWRF:surface:18-24 hour ave fcst:",
    ]
)


def _mirror_grib_bytes() -> bytes:
    # 7000 bytes of deterministic content so Range slices are distinguishable.
    return bytes((i % 251) for i in range(7000))


def _cloud_adapter(
    tmp_path: Path,
    *,
    idx_by_url: dict[str, str],
    grib_bytes: bytes,
    backends: tuple[str, ...] = ("s3", "gcs", "azure", "nomads"),
    failing_idx_urls: dict[str, Exception] | None = None,
    wall_clock: Any | None = None,
) -> tuple[GFSAdapter, dict[str, list[str]]]:
    calls: dict[str, list[str]] = {"idx": [], "range": [], "nomads": []}
    failing = failing_idx_urls or {}

    def downloader(url: str) -> bytes:
        if "#range=" in url:
            base, _, rng = url.partition("#range=")
            calls["range"].append(rng)
            # parse "bytes=start-end" inclusive
            spec = rng.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s)
            end = int(end_s) + 1 if end_s else len(grib_bytes)
            return grib_bytes[start:end]
        if url.endswith(".idx"):
            calls["idx"].append(url)
            if url in failing:
                raise failing[url]
            if url not in idx_by_url:
                raise FileUnavailableError("no idx", attempts=1)
            return idx_by_url[url].encode("utf-8")
        calls["nomads"].append(url)
        return b"GRIB nomads subset payload"

    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        source_backends=backends,
        poll_interval_seconds=0,
        nomads_min_interval_seconds=0,
        max_wait_seconds=0,
        max_retries=1,
        retry_backoff_seconds=(0,),
    )
    adapter = GFSAdapter(
        config=config,
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader,
        availability_checker=lambda _url: True,
        sleeper=lambda _seconds: None,
        wall_clock=wall_clock,
    )
    return adapter, calls


def _entry(variable: str, forecast_hour: int) -> ManifestEntry:
    return ManifestEntry(
        remote_url="https://nomads.example/grib-filter",
        local_key=f"raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f{forecast_hour:03d}.{variable}.grib2",
        variable=variable,
        forecast_hour=forecast_hour,
        metadata={"cycle_time": parse_cycle_time("2026050700").isoformat()},
    )


def _patch_cdo(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any] | None = None) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if captured is not None:
            captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"GRIB clipped payload")
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(gfs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(gfs_module.subprocess, "run", fake_run)


# ---- idx parser -----------------------------------------------------------------

def test_idx_parser_computes_byte_ranges_and_last_record_open_ended() -> None:
    records = gfs_module._parse_gfs_idx(F003_IDX)
    assert len(records) == 7
    # TMP f003 -> record 1 [0, 1500)
    start, end = gfs_module._select_idx_byte_range(records, "tmp2m", 3)
    assert (start, end) == (0, 1500)
    # DSWRF f003 -> last record, open-ended end
    start, end = gfs_module._select_idx_byte_range(records, "dswrf", 3)
    assert (start, end) == (9000, None)


def test_idx_parser_rejects_malformed_lines() -> None:
    with pytest.raises(IdxParseError):
        gfs_module._parse_gfs_idx("1:0:d=2026050700:TMP\n")


def test_idx_apcp_duplicate_identical_cumulative_windows_choose_deterministically() -> None:
    records = gfs_module._parse_gfs_idx(F006_IDX_DUP_APCP)
    start, end = gfs_module._select_idx_byte_range(records, "apcp", 6)

    assert (start, end) == (1500, 3000)


def test_idx_apcp_prefers_cycle_cumulative_record_over_bucket_record() -> None:
    records = gfs_module._parse_gfs_idx(F012_IDX_MIXED_APCP)
    start, end = gfs_module._select_idx_byte_range(records, "apcp", 12)

    assert (start, end) == (3000, 4500)


def test_idx_apcp_f009_prefers_cycle_cumulative_record_over_bucket_record() -> None:
    records = gfs_module._parse_gfs_idx(F009_IDX_MIXED_APCP)
    start, end = gfs_module._select_idx_byte_range(records, "apcp", 9)

    assert (start, end) == (3000, 4500)


def test_idx_apcp_f009_duplicate_identical_cumulative_windows_choose_deterministically() -> None:
    records = gfs_module._parse_gfs_idx(F009_IDX_DUP_APCP)
    start, end = gfs_module._select_idx_byte_range(records, "apcp", 9)

    assert (start, end) == (1500, 3000)


def test_idx_apcp_rejects_bucket_when_window_does_not_match_expected_interval() -> None:
    records = gfs_module._parse_gfs_idx(F012_IDX_BUCKET_ONLY_APCP)
    with pytest.raises(IdxSelectorPolicyError, match="expected_interval_hours=3"):
        gfs_module._select_idx_record(records, "apcp", 12, expected_interval_hours=3)


def test_idx_apcp_accepts_bucket_when_window_matches_expected_interval() -> None:
    records = gfs_module._parse_gfs_idx(F012_IDX_BUCKET_ONLY_APCP)
    selection = gfs_module._select_idx_record(records, "apcp", 12, expected_interval_hours=6)

    assert selection.byte_range == (1500, 3000)
    assert selection.step_range == "6-12"
    assert selection.accumulation_type == "interval_bucket"


def test_idx_apcp_normalizes_day_cumulative_window_to_hours() -> None:
    records = gfs_module._parse_gfs_idx(F024_IDX_DAY_CUMULATIVE_APCP)
    selection = gfs_module._select_idx_record(records, "apcp", 24, expected_interval_hours=3)

    assert selection.byte_range == (3000, 4500)
    assert selection.step_range == "0-24"
    assert selection.accumulation_type == "cumulative_since_cycle"


def test_incompatible_bucket_only_apcp_idx_fails_without_nomads_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f012"
    adapter, calls = _cloud_adapter(
        tmp_path,
        idx_by_url={f"{s3_grib}.idx": F012_IDX_BUCKET_ONLY_APCP},
        grib_bytes=grib,
        backends=("s3", "nomads"),
    )
    _patch_cdo(monkeypatch)

    with pytest.raises(IdxSelectorPolicyError, match="expected_interval_hours=3"):
        adapter._download_entry(_entry("apcp", 12))

    assert calls["nomads"] == []
    assert calls["range"] == []


def test_cloud_apcp_day_cumulative_selection_persists_idx_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f024"
    adapter, calls = _cloud_adapter(
        tmp_path,
        idx_by_url={f"{s3_grib}.idx": F024_IDX_DAY_CUMULATIVE_APCP},
        grib_bytes=grib,
        backends=("s3",),
    )
    _patch_cdo(monkeypatch)
    entry = _entry("apcp", 24)

    result, _retries = adapter._download_entry(entry)

    assert result.status == "downloaded"
    assert calls["range"] == ["bytes=3000-4499"]
    assert entry.metadata["idx_selector"]["step_range"] == "0-24"
    assert entry.metadata["idx_selector"]["accumulation_type"] == "cumulative_since_cycle"


def test_idx_non_apcp_duplicate_records_remain_ambiguous() -> None:
    records = gfs_module._parse_gfs_idx(F003_IDX_DUP_TMP)
    with pytest.raises(AmbiguousIdxRecordError):
        gfs_module._select_idx_byte_range(records, "tmp2m", 3)


def test_idx_f000_omits_apcp_and_dswrf() -> None:
    records = gfs_module._parse_gfs_idx(F000_IDX)
    with pytest.raises(IdxRecordNotFoundError):
        gfs_module._select_idx_byte_range(records, "apcp", 0)
    with pytest.raises(IdxRecordNotFoundError):
        gfs_module._select_idx_byte_range(records, "dswrf", 0)


def test_idx_matches_all_seven_variables_at_f003() -> None:
    records = gfs_module._parse_gfs_idx(F003_IDX)
    for variable in ("tmp2m", "rh2m", "u10m", "v10m", "pressfc", "apcp", "dswrf"):
        start, end = gfs_module._select_idx_byte_range(records, variable, 3)
        assert start >= 0
    # f000 instantaneous fields match anl, not fcst
    f000 = gfs_module._parse_gfs_idx(F000_IDX)
    for variable in ("tmp2m", "rh2m", "u10m", "v10m", "pressfc"):
        start, end = gfs_module._select_idx_byte_range(f000, variable, 0)
        assert start >= 0


# ---- s3 happy path + Range -------------------------------------------------------

def test_s3_happy_path_uses_idx_byte_range_and_clips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    idx_by_url = {f"{s3_grib}.idx": F003_IDX}
    adapter, calls = _cloud_adapter(tmp_path, idx_by_url=idx_by_url, grib_bytes=grib)
    captured: dict[str, Any] = {}
    _patch_cdo(monkeypatch, captured)

    result, _retries = adapter._download_entry(_entry("tmp2m", 3))

    assert result.status == "downloaded"
    assert adapter.object_store.exists("raw/gfs/2026050700/gfs.t00z.pgrb2.0p25.f003.tmp2m.grib2")
    # TMP f003 -> [0, 1500): Range bytes=0-1499
    assert calls["range"] == ["bytes=0-1499"]
    assert calls["idx"] == [f"{s3_grib}.idx"]
    assert calls["nomads"] == []
    # cdo clip invoked with sellonlatbox in W,E,S,N order
    assert any(arg.startswith("sellonlatbox,") for arg in captured["argv"])
    assert captured["argv"][1:3] == ["-f", "grb2"]


def test_s3_clip_uses_bbox_west_east_south_north_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    adapter, _calls = _cloud_adapter(tmp_path, idx_by_url={f"{s3_grib}.idx": F003_IDX}, grib_bytes=grib)
    captured: dict[str, Any] = {}
    _patch_cdo(monkeypatch, captured)

    adapter._download_entry(_entry("pressfc", 3))

    bbox = adapter.config.bbox
    expected = f"sellonlatbox,{bbox.west:g},{bbox.east:g},{bbox.south:g},{bbox.north:g}"
    assert expected in captured["argv"]


# ---- multi-mirror fallback -------------------------------------------------------

def test_mirror_fallback_s3_idx_404_falls_through_to_gcs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    gcs_grib = "https://storage.googleapis.com/global-forecast-system/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    # Only GCS has the idx; S3 idx is absent (404), azure absent too.
    adapter, calls = _cloud_adapter(tmp_path, idx_by_url={f"{gcs_grib}.idx": F003_IDX}, grib_bytes=grib)
    _patch_cdo(monkeypatch)

    result, _retries = adapter._download_entry(_entry("rh2m", 3))

    assert result.status == "downloaded"
    # both s3 and gcs idx were probed; gcs succeeded; nomads never reached.
    assert any("s3.amazonaws.com" in url for url in calls["idx"])
    assert any("googleapis.com" in url for url in calls["idx"])
    assert calls["nomads"] == []


def test_mirror_fallback_to_azure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    azure_grib = "https://noaagfs.blob.core.windows.net/gfs/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    adapter, calls = _cloud_adapter(tmp_path, idx_by_url={f"{azure_grib}.idx": F003_IDX}, grib_bytes=grib)
    _patch_cdo(monkeypatch)

    result, _retries = adapter._download_entry(_entry("u10m", 3))

    assert result.status == "downloaded"
    assert any("blob.core.windows.net" in url for url in calls["idx"])


# ---- cdo fail-loud ---------------------------------------------------------------

def test_mirror_clip_fails_loud_when_cdo_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    adapter, _calls = _cloud_adapter(
        tmp_path, idx_by_url={f"{s3_grib}.idx": F003_IDX}, grib_bytes=grib, backends=("s3",)
    )
    monkeypatch.setattr(gfs_module.shutil, "which", lambda _name: None)

    with pytest.raises(CdoMissingError) as exc:
        adapter._download_entry(_entry("tmp2m", 3))
    assert exc.value.error_code == "CDO_MISSING"


def test_mirror_clip_fails_loud_on_cdo_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    adapter, _calls = _cloud_adapter(
        tmp_path, idx_by_url={f"{s3_grib}.idx": F003_IDX}, grib_bytes=grib, backends=("s3",)
    )

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        return type("R", (), {"returncode": 1, "stderr": b"cdo sellonlatbox: boom"})()

    monkeypatch.setattr(gfs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(gfs_module.subprocess, "run", fake_run)

    with pytest.raises(CdoClipError) as exc:
        adapter._download_entry(_entry("tmp2m", 3))
    assert exc.value.error_code == "CDO_CLIP_FAILED"


def test_mirror_clip_fails_before_reading_oversized_cdo_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grib = _mirror_grib_bytes()
    s3_grib = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260507/00/atmos/gfs.t00z.pgrb2.0p25.f003"
    adapter, _calls = _cloud_adapter(
        tmp_path,
        idx_by_url={f"{s3_grib}.idx": F003_IDX},
        grib_bytes=grib,
        backends=("s3",),
    )
    adapter.config = replace(adapter.config, max_file_size_bytes=4)

    def fake_run(argv: list[str], **_kwargs: Any) -> Any:
        Path(argv[-1]).write_bytes(b"GRIB oversized clipped payload")
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(gfs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(gfs_module.subprocess, "run", fake_run)

    with pytest.raises(FileTooLargeError):
        adapter._download_entry(_entry("tmp2m", 3))


# ---- NOMADS 403 circuit breaker --------------------------------------------------

def test_nomads_403_trips_breaker_without_retry_or_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []
    nomads_calls = {"n": 0}

    def downloader(url: str) -> bytes:
        if url.endswith(".idx"):
            raise FileUnavailableError("no mirror idx", attempts=1)
        nomads_calls["n"] += 1
        raise ForbiddenSourceError("nomads 403", attempts=1)

    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        source_backends=("s3", "nomads"),
        poll_interval_seconds=0,
        nomads_min_interval_seconds=0,
        max_wait_seconds=0,
        max_retries=3,
        retry_backoff_seconds=(5, 5, 5),
    )
    adapter = GFSAdapter(
        config=config,
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader,
        sleeper=sleeps.append,
    )

    with pytest.raises(AllSourcesUnavailableError):
        adapter._download_entry(_entry("tmp2m", 3))

    # NOMADS hit exactly once: no _download_with_retries loop, no backoff sleep.
    assert nomads_calls["n"] == 1
    assert sleeps == []
    assert adapter._nomads_circuit_open() is True


def test_breaker_open_skips_nomads_until_cooldown_expires(tmp_path: Path) -> None:
    now = {"t": datetime(2026, 5, 7, 0, 0, tzinfo=UTC)}

    def wall_clock() -> datetime:
        return now["t"]

    grib = _mirror_grib_bytes()
    nomads_hits = {"n": 0}

    def downloader(url: str) -> bytes:
        if "#range=" in url:
            spec = url.split("=", 2)[-1]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s)
            end = int(end_s) + 1 if end_s else len(grib)
            return grib[start:end]
        if url.endswith(".idx"):
            raise FileUnavailableError("no mirror idx", attempts=1)
        nomads_hits["n"] += 1
        raise ForbiddenSourceError("nomads 403", attempts=1)

    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        source_backends=("s3", "nomads"),
        poll_interval_seconds=0,
        nomads_min_interval_seconds=0,
        nomads_cooldown_minutes=60,
        max_wait_seconds=0,
        max_retries=1,
        retry_backoff_seconds=(0,),
    )
    adapter = GFSAdapter(
        config=config,
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader,
        sleeper=lambda _s: None,
        wall_clock=wall_clock,
    )

    # First pass: mirror 404 + NOMADS 403 -> breaker opens, persisted to disk.
    with pytest.raises(AllSourcesUnavailableError):
        adapter._download_entry(_entry("tmp2m", 3))
    assert nomads_hits["n"] == 1
    circuit_file = tmp_path / "state" / "source_circuit" / "gfs_nomads.json"
    assert circuit_file.exists()

    # Second pass within cooldown (fresh adapter = fresh process): NOMADS skipped.
    adapter2 = GFSAdapter(
        config=config,
        repository=FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader,
        sleeper=lambda _s: None,
        wall_clock=wall_clock,
    )
    with pytest.raises(AllSourcesUnavailableError):
        adapter2._download_entry(_entry("tmp2m", 3))
    assert nomads_hits["n"] == 1  # NOMADS not hit again during cooldown

    # Advance past cooldown: NOMADS eligible again, idx now present so mirror succeeds.
    now["t"] = now["t"] + timedelta(minutes=61)
    assert adapter2._nomads_circuit_open() is False
