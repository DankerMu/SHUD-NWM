from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.artifacts import ArtifactLogError, ArtifactReader, ArtifactReaderConfig, safe_public_log_uri
from services.artifacts.reader import Boto3ObjectReader


class StubObjectReader:
    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None) -> None:
        self.objects = objects or {}
        self.calls: list[tuple[str, str, int]] = []

    def read_tail_bytes(self, bucket: str, key: str, *, max_bytes: int) -> bytes:
        self.calls.append((bucket, key, max_bytes))
        content = self.objects[(bucket, key)]
        return content[-max_bytes:]


def test_config_uses_canonical_env_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(tmp_path / "published"))
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_S3_BUCKET", "nhms-published")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_S3_PREFIX", "prod/published")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "/host/surface-only")
    monkeypatch.setenv("NHMS_LOG_TAIL_MAX_BYTES", "17")

    config = ArtifactReaderConfig.from_env()

    assert config.published_root == (tmp_path / "published")
    assert config.uri_prefix == "published://"
    assert config.s3_bucket == "nhms-published"
    assert config.s3_prefix == "prod/published"
    assert config.tail_max_bytes == 17


def test_published_uri_reads_bounded_tail(tmp_path: Path) -> None:
    root = tmp_path / "published"
    log_path = root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("0123456789abcdef", encoding="utf-8")
    reader = ArtifactReader(_config(root, tail=8))

    result = reader.read_text_tail("published://logs/GFS/2026050100/run_1/job_1.out")

    assert result.log_uri == "published://logs/GFS/2026050100/run_1/job_1.out"
    assert result.content == "89abcdef"
    assert result.truncated is True


def test_allowed_file_uri_reads_under_published_root(tmp_path: Path) -> None:
    root = tmp_path / "published"
    log_path = root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("published log", encoding="utf-8")
    reader = ArtifactReader(_config(root))

    result = reader.read_text_tail(log_path.as_uri())

    assert result.content == "published log"
    assert str(root) not in result.log_uri


def test_file_uri_under_published_root_requires_logs_namespace(tmp_path: Path) -> None:
    root = tmp_path / "published"
    internal_path = root / "internal" / "debug.txt"
    internal_path.parent.mkdir(parents=True)
    internal_path.write_text("private debug", encoding="utf-8")
    reader = ArtifactReader(_config(root))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(internal_path.as_uri())

    assert error.value.code == "JOB_LOG_URI_UNSUPPORTED"


@pytest.mark.parametrize(
    ("uri", "code"),
    [
        ("published://logs/GFS/2026050100/run_1/../secret.out", "JOB_LOG_ACCESS_DENIED"),
        ("published://logs/GFS/2026050100/run_1/%2e%2e/secret.out", "JOB_LOG_ACCESS_DENIED"),
        ("published://logs/GFS/2026050100/run_1/job%2Fsecret.out", "JOB_LOG_ACCESS_DENIED"),
        ("published://logs/GFS/2026050100/run_1/job%5Csecret.out", "JOB_LOG_ACCESS_DENIED"),
        ("published://logs/GFS/2026050100/run_1/job\\secret.out", "JOB_LOG_ACCESS_DENIED"),
        ("published://user:pass@logs/GFS/2026050100/run_1/job.out", "JOB_LOG_URI_UNSUPPORTED"),
        ("published://logs/GFS/2026050100/run_1/job.out?token=secret", "JOB_LOG_URI_UNSUPPORTED"),
        ("published://logs/GFS/2026050100/run_1/job.out#secret", "JOB_LOG_URI_UNSUPPORTED"),
        ("published://logs/GFS/2026050100/run_1/token-secret.out", "JOB_LOG_URI_UNSUPPORTED"),
    ],
)
def test_rejects_unsafe_published_uri_forms(tmp_path: Path, uri: str, code: str) -> None:
    reader = ArtifactReader(_config(tmp_path / "published"))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(uri)

    assert error.value.code == code
    assert "secret" not in json.dumps(error.value.safe_uri)


def test_file_uri_symlink_escape_is_access_denied(tmp_path: Path) -> None:
    root = tmp_path / "published"
    outside = tmp_path / "outside.log"
    outside.write_text("outside secret", encoding="utf-8")
    link = root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    reader = ArtifactReader(_config(root))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(link.as_uri())

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"


def test_file_uri_outside_root_is_access_denied(tmp_path: Path) -> None:
    root = tmp_path / "published"
    outside = tmp_path / "outside.log"
    outside.write_text("outside secret", encoding="utf-8")
    reader = ArtifactReader(_config(root))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(outside.as_uri())

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"


def test_legacy_absolute_log_root_path_remains_available_when_allowed(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    log_path = log_root / "job.log"
    log_path.write_text("absolute dev log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=True,
        )
    )

    result = reader.read_text_tail(str(log_path))

    assert result.content == "absolute dev log"


def test_legacy_file_uri_under_log_root_remains_available_when_allowed(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    log_path = log_root / "job.log"
    log_path.write_text("file dev log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=True,
        )
    )

    result = reader.read_text_tail(log_path.as_uri())

    assert result.content == "file dev log"
    assert str(log_root) not in result.log_uri


def test_legacy_file_uri_outside_log_root_is_denied_and_redacted(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    outside = tmp_path / "outside" / "job.log"
    outside.parent.mkdir()
    outside.write_text("outside dev log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=True,
        )
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(outside.as_uri())

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"
    assert str(outside) not in json.dumps(error.value.safe_uri)


def test_legacy_absolute_private_workspace_path_under_log_root_is_denied(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    private_path = log_root / ".nhms-runs" / "run_1" / "job.log"
    private_path.parent.mkdir(parents=True)
    private_path.write_text("private workspace log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=True,
        )
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(str(private_path))

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"
    assert error.value.reason == "private_workspace_path"


@pytest.mark.parametrize("uri", ["/scratch/node22/job.out", "/tmp/job.out", ".nhms-runs/run/log.out"])
def test_private_local_paths_are_rejected(tmp_path: Path, uri: str) -> None:
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            legacy_log_root=tmp_path,
            allow_legacy_local_file_logs=True,
        )
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(uri)

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"


@pytest.mark.parametrize("uri", ["http://example.test:bad/log.out", "http://[::1/log.out"])
def test_malformed_uri_maps_to_stable_unsupported(tmp_path: Path, uri: str) -> None:
    reader = ArtifactReader(_config(tmp_path / "published"))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(uri)

    assert error.value.code == "JOB_LOG_URI_UNSUPPORTED"
    assert error.value.reason == "malformed_uri"


def test_query_rejection_redacts_credential_like_path_component(tmp_path: Path) -> None:
    reader = ArtifactReader(_config(tmp_path / "published"))
    uri = "published://logs/GFS/2026050100/run_1/token-supersecret.out?x=y"

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(uri)

    assert error.value.code == "JOB_LOG_URI_UNSUPPORTED"
    assert "token-supersecret" not in json.dumps(error.value.safe_uri)


def test_display_false_local_file_gate_blocks_legacy_log_root(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "job.log").write_text("dev log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=False,
            display_readonly=True,
        )
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail("job.log")

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"


def test_safe_dev_local_log_root_remains_available(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "job.log").write_text("dev log", encoding="utf-8")
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=None,
            legacy_log_root=log_root,
            allow_legacy_local_file_logs=True,
        )
    )

    assert reader.read_text_tail("job.log").content == "dev log"


def test_missing_published_file_maps_not_found(tmp_path: Path) -> None:
    reader = ArtifactReader(_config(tmp_path / "published"))

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail("published://logs/GFS/2026050100/run_1/missing.out")

    assert error.value.code == "JOB_LOG_NOT_FOUND"


def test_allowlisted_s3_reads_with_mock_and_bounds_tail(tmp_path: Path) -> None:
    object_reader = StubObjectReader(
        {("nhms-published", "prod/logs/GFS/2026050100/run_1/job_1.out"): b"0123456789abcdef"}
    )
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms-published",
            s3_prefix="prod",
            tail_max_bytes=6,
        ),
        object_reader=object_reader,
    )

    result = reader.read_text_tail("s3://nhms-published/prod/logs/GFS/2026050100/run_1/job_1.out")

    assert result.content == "abcdef"
    assert object_reader.calls == [("nhms-published", "prod/logs/GFS/2026050100/run_1/job_1.out", 6)]


def test_allowlisted_s3_without_prefix_requires_logs_prefix(tmp_path: Path) -> None:
    object_reader = StubObjectReader({("nhms-published", "logs/GFS/2026050100/run_1/job_1.out"): b"ok"})
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms-published",
            s3_prefix="",
        ),
        object_reader=object_reader,
    )

    result = reader.read_text_tail("s3://nhms-published/logs/GFS/2026050100/run_1/job_1.out")

    assert result.content == "ok"
    assert object_reader.calls == [("nhms-published", "logs/GFS/2026050100/run_1/job_1.out", 1024 * 1024)]


@pytest.mark.parametrize(
    "uri",
    [
        "s3://other/prod/logs/GFS/2026050100/run_1/job_1.out",
        "s3://nhms-published/private/logs/GFS/2026050100/run_1/job_1.out",
        "s3://nhms-published/prod/private/logs/GFS/2026050100/run_1/job_1.out",
    ],
)
def test_unallowlisted_s3_rejects_without_read_attempt(tmp_path: Path, uri: str) -> None:
    object_reader = StubObjectReader()
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms-published",
            s3_prefix="prod",
        ),
        object_reader=object_reader,
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail(uri)

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"
    assert object_reader.calls == []


def test_s3_without_prefix_rejects_private_logs_without_read_attempt(tmp_path: Path) -> None:
    object_reader = StubObjectReader()
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms-published",
            s3_prefix="",
        ),
        object_reader=object_reader,
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail("s3://nhms-published/private/logs/GFS/2026050100/run_1/job_1.out")

    assert error.value.code == "JOB_LOG_ACCESS_DENIED"
    assert object_reader.calls == []


def test_missing_s3_object_maps_not_found(tmp_path: Path) -> None:
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms-published",
            s3_prefix="prod",
        ),
        object_reader=StubObjectReader(),
    )

    with pytest.raises(ArtifactLogError) as error:
        reader.read_text_tail("s3://nhms-published/prod/logs/GFS/2026050100/run_1/missing.out")

    assert error.value.code == "JOB_LOG_NOT_FOUND"


def test_boto3_object_reader_closes_body_on_success() -> None:
    body = _ClosingBody(b"0123456789")
    reader = Boto3ObjectReader(_Boto3Client(body))

    result = reader.read_tail_bytes("bucket", "key", max_bytes=4)

    assert result == b"0123"
    assert body.closed is True


def test_boto3_object_reader_closes_body_on_read_error() -> None:
    body = _ClosingBody(b"", fail=True)
    reader = Boto3ObjectReader(_Boto3Client(body))

    with pytest.raises(OSError):
        reader.read_tail_bytes("bucket", "key", max_bytes=4)

    assert body.closed is True


def test_safe_public_log_uri_redacts_query_and_truncates() -> None:
    redacted = safe_public_log_uri(
        "s3://user:pass@bucket/prod/logs/very-long-run-id/job.out?X-Amz-Signature=supersecret&token=raw",
        max_length=36,
    )

    assert redacted is not None
    assert "pass" not in redacted
    assert "supersecret" not in redacted
    assert "token" not in redacted
    assert redacted.endswith("...[truncated]")


def test_safe_public_log_uri_redacts_credential_like_path_without_query() -> None:
    redacted = safe_public_log_uri("published://logs/GFS/2026050100/run_1/token-supersecret.out")

    assert redacted == "published://logs/[redacted]"


@pytest.mark.parametrize(
    "uri",
    [
        "/scratch/node22/.nhms-runs/run_1/job.out",
        "/tmp/nhms/job.out",
        "file:///scratch/node22/.nhms-runs/run_1/job.out",
    ],
)
def test_safe_public_log_uri_redacts_private_local_paths(uri: str) -> None:
    redacted = safe_public_log_uri(uri)

    assert redacted is not None
    assert "/scratch" not in redacted
    assert "/tmp" not in redacted
    assert ".nhms-runs" not in redacted


class _ClosingBody:
    def __init__(self, content: bytes, *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.closed = False

    def read(self, max_bytes: int) -> bytes:
        if self.fail:
            raise OSError("read failed")
        return self.content[:max_bytes]

    def close(self) -> None:
        self.closed = True


class _Boto3Client:
    def __init__(self, body: _ClosingBody) -> None:
        self.body = body

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, int]:
        del Bucket, Key
        return {"ContentLength": len(self.body.content)}

    def get_object(self, **kwargs: object) -> dict[str, _ClosingBody]:
        del kwargs
        return {"Body": self.body}


def _config(root: Path, *, tail: int = 1024 * 1024) -> ArtifactReaderConfig:
    return ArtifactReaderConfig(published_root=root, tail_max_bytes=tail, legacy_log_root=root)
