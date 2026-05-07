from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes

base = importlib.import_module("workers.data_adapters.base")
cli_module = importlib.import_module("workers.data_adapters.cli")
gfs_module = importlib.import_module("workers.data_adapters.gfs_adapter")

DownloadManifest = base.DownloadManifest
ManifestEntry = base.ManifestEntry
parse_cycle_time = base.parse_cycle_time
FileUnavailableError = gfs_module.FileUnavailableError
FileTooLargeError = gfs_module.FileTooLargeError
GFSAdapter = gfs_module.GFSAdapter
GFSAdapterConfig = gfs_module.GFSAdapterConfig


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
        poll_interval_seconds=0,
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


def test_manifest_contains_57_forecast_hours_times_7_variables(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    manifest = adapter.build_manifest("2026050700")

    assert len(manifest.entries) == 57 * 7
    assert manifest.metadata["total_file_count"] == 399
    assert manifest.metadata["variable_count"] == 7
    assert manifest.metadata["first_forecast_hour"] == 0
    assert manifest.metadata["last_forecast_hour"] == 168
    assert manifest.manifest_uri == "raw/gfs/2026050700/manifest.json"
    assert adapter.object_store.exists("raw/gfs/2026050700/manifest.json")


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
    manifest = adapter.build_manifest("2026050700", forecast_hours=[0])

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
    manifest = adapter.build_manifest("2026050700", forecast_hours=[0])

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
