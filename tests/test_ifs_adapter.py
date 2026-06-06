from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes

base = importlib.import_module("workers.data_adapters.base")
converter_module = importlib.import_module("workers.canonical_converter.converter")
ifs_module = importlib.import_module("workers.data_adapters.ifs_adapter")

DownloadManifest = base.DownloadManifest
ManifestEntry = base.ManifestEntry
parse_cycle_time = base.parse_cycle_time
IFSCanonicalConverter = converter_module.IFSCanonicalConverter
IFSCanonicalConverterConfig = converter_module.IFSCanonicalConverterConfig
RawRecord = converter_module.RawRecord
FileUnavailableError = ifs_module.FileUnavailableError
FileTooLargeError = ifs_module.FileTooLargeError
IFSAdapter = ifs_module.IFSAdapter
IFSAdapterConfig = ifs_module.IFSAdapterConfig
DownloadedPayload = ifs_module.DownloadedPayload
RateLimitedError = ifs_module.RateLimitedError
NetworkDownloadError = ifs_module.NetworkDownloadError


@pytest.fixture(autouse=True)
def _reset_source_cooldowns() -> Any:
    # The per-source cooldown table is module-level; reset around every test so a
    # rate-limit case cannot leak a cooldown into an unrelated case.
    IFSAdapter.reset_source_cooldowns()
    yield
    IFSAdapter.reset_source_cooldowns()


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
    poll_interval_seconds: float = 0,
    max_file_size_bytes: int = 500 * 1024 * 1024,
    sleeper: Any | None = None,
    clock: Any | None = None,
    rate_limit_cooldown_seconds: int = 1800,
) -> IFSAdapter:
    config = IFSAdapterConfig(
        workspace_root=tmp_path,
        poll_interval_seconds=poll_interval_seconds,
        max_wait_seconds=max_wait_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=(0,),
        max_file_size_bytes=max_file_size_bytes,
        rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
    )
    return IFSAdapter(
        config=config,
        repository=repository or FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader or (lambda _url: b"GRIB IFS mock bytes 7777"),
        availability_checker=availability_checker or (lambda _url: True),
        sleeper=sleeper or (lambda _seconds: None),
        clock=clock,
    )


def one_entry_manifest(tmp_path: Path, expected_checksum: str | None = None) -> tuple[IFSAdapter, DownloadManifest]:
    adapter = build_adapter(tmp_path)
    cycle_time = parse_cycle_time("2026050100")
    entry = ManifestEntry(
        remote_url="ecmwf-opendata://ecmwf/ifs/2026050100/ifs.t00z.f000.2t.grib2",
        local_key="raw/IFS/2026050100/ifs.t00z.f000.2t.grib2",
        variable="2t",
        forecast_hour=0,
        expected_checksum=expected_checksum,
        metadata={"cycle_time": cycle_time.isoformat()},
    )
    return adapter, DownloadManifest(source_id="IFS", cycle_time=cycle_time, entries=(entry,))


def http_429(retry_after: str = "120") -> HTTPError:
    headers = Message()
    headers["Retry-After"] = retry_after
    return HTTPError("mock://ifs", 429, "too many requests", headers, None)


def test_discover_cycles_normal_date_range_and_all_day_unavailable(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository, availability_checker=lambda _url: True)

    cycles = adapter.discover_cycles("2026-05-01")
    ranged = adapter.discover_cycles("2026-05-01", end_date="2026-05-03")

    assert [cycle.cycle_hour for cycle in cycles] == [0, 6, 12, 18]
    assert all(cycle.available for cycle in cycles)
    assert len(ranged) == 12
    assert {cycle["status"] for cycle in repository.cycles.values()} == {"discovered"}

    unavailable_repository = FakeMetRepository()
    unavailable_adapter = build_adapter(
        tmp_path / "unavailable",
        repository=unavailable_repository,
        availability_checker=lambda _url: False,
    )
    unavailable = unavailable_adapter.discover_cycles("2026-05-01")

    assert len(unavailable) == 4
    assert not any(cycle.available for cycle in unavailable)
    assert unavailable_repository.cycles == {}


def test_build_manifest_uses_cycle_specific_lead_policy_and_custom_hours(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)

    manifest_00 = adapter.build_manifest("2026050100")
    manifest_06 = adapter.build_manifest("2026050106")
    custom = adapter.build_manifest("2026050112", forecast_hours=[0, 3, 6])

    assert len(manifest_00.entries) == 57 * 8
    assert manifest_00.metadata["max_lead_hours"] == 168
    assert manifest_00.entries[0].local_key == "raw/IFS/2026050100/ifs.t00z.f000.2t.grib2"
    assert manifest_00.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f168.str.grib2"
    assert adapter.object_store.exists("raw/IFS/2026050100/manifest.json")

    assert len(manifest_06.entries) == 49 * 8
    assert manifest_06.metadata["max_lead_hours"] == 144

    assert len(custom.entries) == 3 * 8
    assert sorted({entry.forecast_hour for entry in custom.entries}) == [0, 3, 6]


def test_build_manifest_honors_env_forecast_end_hour_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFS_FORECAST_END_HOUR", "144")
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026050100")

    assert len(manifest.entries) == 49 * 8
    assert manifest.metadata["max_lead_hours"] == 144
    assert manifest.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f144.str.grib2"


def test_segmented_forecast_end_hour_override_must_be_native_hour(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFS_FORECAST_RESOLUTION_SEGMENTS", "144:3,360:6")
    monkeypatch.setenv("IFS_FORECAST_END_HOUR", "147")
    adapter = build_adapter(tmp_path)

    with pytest.raises(ValueError, match="native resolution schedule"):
        adapter.build_manifest("2026050100")


def test_build_manifest_honors_env_forecast_start_hour_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFS_FORECAST_START_HOUR", "3")
    monkeypatch.setenv("IFS_FORECAST_END_HOUR", "144")
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026050100")

    assert len(manifest.entries) == 48 * 8
    assert manifest.entries[0].local_key == "raw/IFS/2026050100/ifs.t00z.f003.2t.grib2"
    assert manifest.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f144.str.grib2"


@pytest.mark.parametrize(
    ("cycle_time", "forecast_hours"),
    [
        ("2026050106", [147]),
        ("2026050118", [147]),
        ("2026050100", [171]),
        ("2026050112", [171]),
    ],
)
def test_build_manifest_rejects_cycle_specific_invalid_forecast_hours(
    tmp_path: Path,
    cycle_time: str,
    forecast_hours: list[int],
) -> None:
    adapter = build_adapter(tmp_path)

    with pytest.raises(ValueError, match="IFS forecast hour"):
        adapter.build_manifest(cycle_time, forecast_hours=forecast_hours)


def test_download_plan_normal_idempotent_retry_and_mirror_switch(tmp_path: Path) -> None:
    content = b"GRIB IFS payload 7777"
    calls: list[str] = []

    def flaky_downloader(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("temporary network failure")
        return content

    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=sha256_bytes(content))
    adapter.downloader = flaky_downloader
    adapter.config = IFSAdapterConfig(
        workspace_root=tmp_path,
        max_retries=2,
        retry_backoff_seconds=(0,),
        max_wait_seconds=0,
        poll_interval_seconds=0,
    )

    result = adapter.download_plan(manifest)

    assert result.status == "raw_complete"
    assert result.retry_count == 1
    assert result.files[0].status == "downloaded"
    assert calls == ["ecmwf-opendata://aws/ifs/2026050100/ifs.t00z.f000.2t.grib2"] * 2

    repository = adapter.repository
    repository.cycles[("IFS", manifest.cycle_time)] = {
        "source_id": "IFS",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
    }
    calls.clear()
    second = adapter.download_plan(manifest)

    assert second.status == "already_done"
    assert second.files[0].status == "already_done"
    assert calls == []

    mirror_calls: list[str] = []

    def mirror_downloader(url: str) -> bytes:
        mirror_calls.append(url)
        # AWS is now the default primary; fail it so the switch to the next cloud
        # mirror (azure) is exercised.
        if "://aws/" in url:
            raise RuntimeError("primary failed")
        return b"GRIB mirror payload 7777"

    mirror_adapter, mirror_manifest = one_entry_manifest(tmp_path / "mirror")
    mirror_adapter.downloader = mirror_downloader
    mirror_result = mirror_adapter.download_plan(mirror_manifest)

    assert mirror_result.status == "raw_complete"
    assert "://aws/" in mirror_calls[0]
    assert mirror_calls[:2] == ["ecmwf-opendata://aws/ifs/2026050100/ifs.t00z.f000.2t.grib2"] * 2
    assert "://azure/" in mirror_calls[2]


def test_raw_complete_untrusted_existing_object_is_redownloaded_not_already_done(tmp_path: Path) -> None:
    stale = b"GRIB IFS stale collision bytes"
    fresh = b"GRIB IFS fresh source bytes"
    downloads = 0

    def downloader(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return fresh

    adapter, manifest = one_entry_manifest(tmp_path)
    adapter.downloader = downloader
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, stale)
    adapter.repository.cycles[("IFS", manifest.cycle_time)] = {
        "source_id": "IFS",
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
    content = b"GRIB IFS trusted existing bytes"
    downloads = 0

    def downloader(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return b"GRIB IFS should not download"

    adapter, manifest = one_entry_manifest(tmp_path)
    adapter.downloader = downloader
    adapter.object_store.write_bytes_atomic(manifest.entries[0].local_key, content)
    manifest.metadata["source_policy"] = adapter.source_policy_identity(manifest.cycle_time, [0])
    manifest.metadata["source_object_identity"] = adapter.source_object_identity(manifest.cycle_time, [0])
    manifest = replace(manifest, manifest_uri="raw/IFS/2026050100/prior_manifest.json")
    adapter.object_store.write_bytes_atomic(
        manifest.manifest_uri,
        json.dumps(manifest.as_dict(), sort_keys=True).encode("utf-8"),
    )
    adapter.repository.cycles[("IFS", manifest.cycle_time)] = {
        "source_id": "IFS",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
        "manifest_uri": manifest.manifest_uri,
    }

    result = adapter.download_plan(manifest)

    assert result.status == "already_done"
    assert result.files[0].status == "already_done"
    assert result.files[0].checksum == sha256_bytes(content)
    assert downloads == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"GRIB oversized",
        DownloadedPayload(content=b"GRIB oversized", checksum="sha", bytes_written=len(b"GRIB oversized")),
    ],
)
def test_injected_payload_enforces_max_size_before_object_store_write(tmp_path: Path, payload: Any) -> None:
    adapter = build_adapter(tmp_path, downloader=lambda _url: payload, max_file_size_bytes=4)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    entry = manifest.entries[0]

    result = adapter.download_plan(DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(entry,)))

    assert result.status == "failed_download"
    assert result.files[0].error_code == "FILE_TOO_LARGE"
    assert not adapter.object_store.exists(entry.local_key)


def test_direct_client_retrieval_enforces_max_size_before_object_store_write(tmp_path: Path) -> None:
    original_read_bytes = Path.read_bytes
    read_calls: list[Path] = []

    def tracking_read_bytes(self: Path) -> bytes:
        read_calls.append(self)
        return original_read_bytes(self)

    class OversizedClient:
        def retrieve(self, **kwargs: Any) -> None:
            Path(kwargs["target"]).write_bytes(b"GRIB oversized direct payload")

    adapter = build_adapter(tmp_path, max_file_size_bytes=4)
    adapter.downloader = adapter._download_url
    adapter._client_for_source = lambda _source: OversizedClient()  # type: ignore[method-assign]
    ifs_module.Path.read_bytes = tracking_read_bytes
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    entry = manifest.entries[0]

    try:
        result = adapter.download_plan(
            DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(entry,))
        )
    finally:
        ifs_module.Path.read_bytes = original_read_bytes

    assert result.status == "failed_download"
    assert result.files[0].error_code == "FILE_TOO_LARGE"
    assert "exceeded maximum size" in (result.files[0].error_message or "")
    assert not adapter.object_store.exists(entry.local_key)
    assert read_calls == []


def test_source_object_identity_changes_when_remote_or_policy_content_changes(tmp_path: Path) -> None:
    first = build_adapter(tmp_path / "first")
    second = build_adapter(tmp_path / "second")
    second.config = IFSAdapterConfig(
        workspace_root=tmp_path / "second",
        preferred_source="aws",
        fallback_sources=("google",),
    )

    first_identity = first.source_object_identity("2026050100", [0, 3])
    second_identity = second.source_object_identity("2026050100", [0, 3])
    changed_policy_identity = first.source_object_identity("2026050100", [0])

    assert first_identity["identity_schema_version"] == "nhms.source_object_identity.v2"
    assert first_identity["manifest_digest"] != second_identity["manifest_digest"]
    assert first_identity["raw_entry_digest"] != second_identity["raw_entry_digest"]
    assert first_identity["manifest_digest"] != changed_policy_identity["manifest_digest"]
    assert first_identity["raw_entry_count"] == 16
    assert len(first_identity["raw_entry_samples"]) == 2


def test_source_object_identity_changes_when_same_key_content_changes(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    entry = manifest.entries[0]

    before = adapter.source_object_identity("2026050100", [0])
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB first IFS payload 7777")
    first = adapter.source_object_identity("2026050100", [0])
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB second IFS payload 7777")
    second = adapter.source_object_identity("2026050100", [0])

    assert before["raw_entry_digest"] != first["raw_entry_digest"]
    assert first["raw_entry_digest"] != second["raw_entry_digest"]
    assert first["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"
    assert first["raw_entry_samples"][0]["observed_raw_object"]["checksum"] != (
        second["raw_entry_samples"][0]["observed_raw_object"]["checksum"]
    )


def test_source_object_identity_blocks_oversized_raw_before_checksum(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path, max_file_size_bytes=4)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, b"GRIB IFS oversized raw payload")
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

    identity = adapter.source_object_identity(manifest.cycle_time, [0])
    observation = identity["raw_entry_observation_digest_by_key"][entry.local_key]

    assert observation["status"] == "oversized"
    assert observation["checksum"] is None


def test_downloaded_manifest_identity_feeds_ifs_canonical_readiness_and_blocks_stale_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = build_adapter(tmp_path)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0, 3])
    pre_download_identity = manifest.metadata["source_object_identity"]
    assert pre_download_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "missing"

    adapter.downloader = lambda url: f"GRIB IFS fixture {url}".encode("utf-8")

    download = adapter.download_plan(manifest)

    observed_identity = adapter.source_object_identity(manifest.cycle_time, [0, 3])
    persisted_manifest = adapter.load_manifest(manifest.manifest_uri or "")
    assert download.status == "raw_complete"
    assert manifest.metadata["source_object_identity"] == observed_identity
    assert persisted_manifest.metadata["source_object_identity"] == observed_identity
    assert observed_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"
    assert observed_identity["raw_entry_digest"] != pre_download_identity["raw_entry_digest"]

    repository = FakeCanonicalRepository()
    converter = IFSCanonicalConverter(
        config=IFSCanonicalConverterConfig(workspace_root=tmp_path),
        repository=repository,
        object_store=LocalObjectStore(tmp_path),
    )

    def read_record(entry: dict[str, Any]) -> Any:
        variable = str(entry["variable"])
        forecast_hour = int(entry["forecast_hour"])
        values_by_variable = {
            "2t": 285.0 + forecast_hour,
            "2d": 278.0,
            "10u": 3.0,
            "10v": 4.0,
            "tp": forecast_hour * 0.001,
            "sp": 101325.0,
            "ssr": forecast_hour * 3600.0 * 100.0,
            "str": forecast_hour * 3600.0 * -50.0,
        }
        return RawRecord(
            source_file=converter.object_store.uri_for_key(str(entry["local_key"])),
            native_variable=variable,
            forecast_hour=forecast_hour,
            values=(values_by_variable[variable],),
            longitudes=(0.0,),
            latitudes=(0.0,),
            shape=(1,),
        )

    monkeypatch.setattr(converter, "_read_record", read_record)

    conversion = converter.convert_manifest(manifest.as_dict())
    readiness = converter.canonical_readiness(
        cycle_time=manifest.cycle_time,
        forecast_hours=[0, 3],
        policy_identity=manifest.metadata["source_policy"],
        source_object_identity=observed_identity,
        canonical_product_id="canon_ifs_2026050100",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert conversion.status == "canonical_ready"
    assert readiness.ready is True
    lineage_identity = repository.products["IFS_2026050100_air_temperature_2m_f000"]["lineage_json"][
        "source_object_identity"
    ]
    assert lineage_identity == observed_identity
    assert lineage_identity["raw_entry_samples"][0]["observed_raw_object"]["status"] == "present"

    changed_entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(changed_entry.local_key, b"GRIB IFS changed fixture bytes")
    changed_identity = adapter.source_object_identity(manifest.cycle_time, [0, 3])
    stale_readiness = converter.canonical_readiness(
        cycle_time=manifest.cycle_time,
        forecast_hours=[0, 3],
        policy_identity=manifest.metadata["source_policy"],
        source_object_identity=changed_identity,
        canonical_product_id="canon_ifs_2026050100",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert changed_identity["raw_entry_digest"] != observed_identity["raw_entry_digest"]
    assert stale_readiness.ready is False
    assert stale_readiness.evidence["reason"] == "canonical_identity_mismatch"
    assert stale_readiness.evidence["source_object_identity_matched"] is False


def test_download_plan_rate_limit_honors_retry_after_and_switches_mirror(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        if "://aws/" in url:
            raise http_429("120")
        return b"GRIB rate limit fallback 7777"

    adapter = build_adapter(
        tmp_path,
        downloader=downloader,
        max_wait_seconds=300,
        poll_interval_seconds=60,
        sleeper=sleeps.append,
    )
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    single = DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))

    result = adapter.download_plan(single)

    assert result.status == "raw_complete"
    assert sleeps == [120.0]
    assert "://aws/" in calls[0]
    assert "://azure/" in calls[1]


def test_download_plan_polling_timeout_records_failure(tmp_path: Path) -> None:
    repository = FakeMetRepository()

    def unavailable(_url: str) -> bytes:
        raise FileUnavailableError("not published", attempts=1)

    adapter, manifest = one_entry_manifest(tmp_path)
    adapter.repository = repository
    adapter.downloader = unavailable

    result = adapter.download_plan(manifest)

    cycle = repository.cycles[("IFS", manifest.cycle_time)]
    assert result.status == "failed_download"
    assert cycle["status"] == "failed_download"
    assert cycle["error_code"] == "POLL_TIMEOUT"


def test_download_plan_persistent_rate_limit_records_failure(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path, downloader=lambda _url: (_ for _ in ()).throw(http_429("10")), max_wait_seconds=0)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    single = DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))

    result = adapter.download_plan(single)

    assert result.status == "failed_download"
    assert result.files[0].error_code == "RATE_LIMITED"
    assert "rate limited" in (result.files[0].error_message or "")


def test_verify_manifest_pass_missing_empty_and_checksum(tmp_path: Path) -> None:
    content = b"GRIB checksum payload 7777"
    checksum = sha256_bytes(content)
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=checksum)
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, content)

    assert adapter.verify_manifest(manifest).passed is True

    missing = DownloadManifest(
        source_id="IFS",
        cycle_time=manifest.cycle_time,
        entries=(
            ManifestEntry(
                remote_url=entry.remote_url,
                local_key="raw/IFS/2026050100/missing.grib2",
                variable="2t",
                forecast_hour=0,
            ),
        ),
    )
    missing_result = adapter.verify_manifest(missing)
    assert missing_result.status == "failed"
    assert missing_result.failures[0].error_code == "MISSING_FILE"

    adapter.object_store.write_bytes_atomic(entry.local_key, b"")
    empty_result = adapter.verify_manifest(manifest)
    assert empty_result.status == "failed"
    assert empty_result.failures[0].error_code == "EMPTY_FILE"


def test_verify_manifest_checksum_mismatch(tmp_path: Path) -> None:
    content = b"GRIB checksum payload 7777"
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum="bad")
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, content)

    result = adapter.verify_manifest(manifest)

    assert result.status == "failed"
    assert result.failures[0].error_code == "CHECKSUM_MISMATCH"


def test_verify_manifest_invalid_grib(tmp_path: Path) -> None:
    adapter, manifest = one_entry_manifest(tmp_path)
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, b"<html>error</html>")

    result = adapter.verify_manifest(manifest)

    assert result.status == "failed"
    assert result.failures[0].error_code == "INVALID_GRIB"


def test_initialize_data_source_registers_correctly_and_is_idempotent(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    adapter.initialize_data_source()
    adapter.initialize_data_source()

    assert len(repository.data_sources) == 1
    assert repository.ensure_calls == 2
    source = repository.data_sources["IFS"]
    assert source["source_name"] == "IFS Open Data"
    assert source["native_format"] == "GRIB2"
    assert source["adapter_name"] == "ifs_adapter"
    assert source["config_json"]["lead_time_policy"]["06"] == 144


def http_503(message: str = "SlowDown") -> HTTPError:
    return HTTPError("mock://ifs", 503, message, Message(), None)


def _single_entry_manifest(adapter: IFSAdapter) -> DownloadManifest:
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    return DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))


def test_sources_default_order_is_cloud_first_ecmwf_last() -> None:
    assert IFSAdapterConfig().sources() == ("aws", "azure", "google", "ecmwf")


def test_sources_env_override_preserves_ecmwf_tail_with_all_four(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFS_OPEN_DATA_SOURCE", "azure")
    monkeypatch.setenv("IFS_OPEN_DATA_FALLBACK_SOURCES", "google,aws,ecmwf")

    assert IFSAdapterConfig().sources() == ("azure", "google", "aws", "ecmwf")


def test_sources_pins_ecmwf_last_even_when_configured_first() -> None:
    config = IFSAdapterConfig(preferred_source="ecmwf", fallback_sources=("aws", "azure"))

    assert config.sources() == ("aws", "azure", "ecmwf")


def test_sources_omits_ecmwf_when_not_configured() -> None:
    # Explicit config without ecmwf must be respected; ecmwf is never force-injected.
    config = IFSAdapterConfig(preferred_source="aws", fallback_sources=("azure", "google"))

    assert config.sources() == ("aws", "azure", "google")
    assert "ecmwf" not in config.sources()


@pytest.mark.parametrize(
    "raise_value",
    [http_503("SlowDown"), RuntimeError("503 SlowDown: please reduce your request rate")],
)
def test_http_503_or_slowdown_text_switches_mirror(tmp_path: Path, raise_value: Exception) -> None:
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        if "://aws/" in url:
            raise raise_value
        return b"GRIB slowdown fallback 7777"

    adapter = build_adapter(
        tmp_path,
        downloader=downloader,
        max_retries=0,
        max_wait_seconds=300,
        poll_interval_seconds=1,
    )

    result = adapter.download_plan(_single_entry_manifest(adapter))

    assert result.status == "raw_complete"
    assert "://aws/" in calls[0]
    assert "://azure/" in calls[1]


def test_all_cloud_mirrors_rate_limited_falls_back_to_ecmwf(tmp_path: Path) -> None:
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        if "://ecmwf/" in url:
            return b"GRIB ecmwf last-resort 7777"
        raise http_503("SlowDown")

    adapter = build_adapter(
        tmp_path,
        downloader=downloader,
        max_retries=0,
        max_wait_seconds=300,
        poll_interval_seconds=1,
    )

    result = adapter.download_plan(_single_entry_manifest(adapter))

    assert result.status == "raw_complete"
    assert "://ecmwf/" in calls[-1]
    assert any("://aws/" in url for url in calls)


def test_rate_limited_source_is_skipped_until_cooldown_expires(tmp_path: Path) -> None:
    now = {"t": 1000.0}

    def clock() -> float:
        return now["t"]

    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        if "://aws/" in url:
            raise http_503("SlowDown")
        return b"GRIB cooldown fallback 7777"

    adapter = build_adapter(
        tmp_path,
        downloader=downloader,
        max_retries=0,
        max_wait_seconds=300,
        poll_interval_seconds=1,
        clock=clock,
        rate_limit_cooldown_seconds=1800,
    )

    first = adapter.download_plan(_single_entry_manifest(adapter))
    assert first.status == "raw_complete"
    assert calls[0].endswith("ifs.t00z.f000.2t.grib2")
    assert "://aws/" in calls[0]

    # Second pass while still inside cooldown: aws must be skipped entirely.
    calls.clear()
    second_manifest = adapter.build_manifest("2026050106", forecast_hours=[0])
    second = adapter.download_plan(
        DownloadManifest(source_id="IFS", cycle_time=second_manifest.cycle_time, entries=(second_manifest.entries[0],))
    )
    assert second.status == "raw_complete"
    assert all("://aws/" not in url for url in calls)
    assert "://azure/" in calls[0]

    # Advance the clock past cooldown: aws is tried again (and 503s, then falls back).
    now["t"] += 1801.0
    calls.clear()
    third_manifest = adapter.build_manifest("2026050112", forecast_hours=[0])
    third = adapter.download_plan(
        DownloadManifest(source_id="IFS", cycle_time=third_manifest.cycle_time, entries=(third_manifest.entries[0],))
    )
    assert third.status == "raw_complete"
    assert "://aws/" in calls[0]
