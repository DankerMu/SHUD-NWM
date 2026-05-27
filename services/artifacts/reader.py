from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from packages.common.redaction import redact_payload
from packages.common.safe_fs import SafeFilesystemError, read_tail_bytes_limited_no_follow
from workers.data_adapters.base import format_cycle_time

DEFAULT_PUBLISHED_URI_PREFIX = "published://"
DEFAULT_LOG_TAIL_MAX_BYTES = 1024 * 1024
PUBLIC_LOG_URI_MAX_LENGTH = 512
LOG_ERROR_NOT_PUBLISHED = "JOB_LOG_NOT_PUBLISHED"
LOG_ERROR_URI_UNSUPPORTED = "JOB_LOG_URI_UNSUPPORTED"
LOG_ERROR_ACCESS_DENIED = "JOB_LOG_ACCESS_DENIED"
LOG_ERROR_NOT_FOUND = "JOB_LOG_NOT_FOUND"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_SAFE_PUBLIC_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENCODED_FORBIDDEN_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_CREDENTIAL_WORD_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|session[_-]?key|signature)",
    re.IGNORECASE,
)
_PUBLISHED_REDACTED_URI = "published://redacted/[redacted]"
_PUBLISHED_LOGS_REDACTED_URI = "published://logs/[redacted]"


class ObjectReader(Protocol):
    def read_tail_bytes(self, bucket: str, key: str, *, max_bytes: int) -> bytes:
        raise NotImplementedError


@dataclass(frozen=True)
class ArtifactReaderConfig:
    published_root: Path | None
    uri_prefix: str = DEFAULT_PUBLISHED_URI_PREFIX
    s3_bucket: str | None = None
    s3_prefix: str = ""
    tail_max_bytes: int = DEFAULT_LOG_TAIL_MAX_BYTES
    allow_legacy_local_file_logs: bool = True
    legacy_log_root: Path | None = None
    display_readonly: bool = False

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        display_readonly: bool = False,
    ) -> ArtifactReaderConfig:
        source_env = os.environ if env is None else env
        published_root = _optional_path(source_env.get("NHMS_PUBLISHED_ARTIFACT_ROOT"))
        uri_prefix = (source_env.get("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX") or DEFAULT_PUBLISHED_URI_PREFIX).strip()
        if not uri_prefix:
            uri_prefix = DEFAULT_PUBLISHED_URI_PREFIX
        s3_bucket = source_env.get("NHMS_PUBLISHED_ARTIFACT_S3_BUCKET", "").strip() or None
        s3_prefix = source_env.get("NHMS_PUBLISHED_ARTIFACT_S3_PREFIX", "").strip().strip("/")
        tail_max_bytes = min(
            _positive_int_env(source_env, "NHMS_LOG_TAIL_MAX_BYTES", DEFAULT_LOG_TAIL_MAX_BYTES),
            DEFAULT_LOG_TAIL_MAX_BYTES,
        )
        allow_local = _allow_legacy_local_file_logs(source_env, display_readonly=display_readonly)
        legacy_root = _optional_path(source_env.get("LOG_ROOT")) or Path("workspace")
        return cls(
            published_root=published_root,
            uri_prefix=uri_prefix,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            tail_max_bytes=tail_max_bytes,
            allow_legacy_local_file_logs=allow_local,
            legacy_log_root=legacy_root,
            display_readonly=display_readonly,
        )


@dataclass(frozen=True)
class ArtifactLogReadResult:
    log_uri: str
    content: str
    truncated: bool


class ArtifactLogError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        safe_uri: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.safe_uri = safe_uri
        self.reason = reason


class Boto3ObjectReader:
    def __init__(self, client: object | None = None) -> None:
        self._client = client

    @property
    def client(self) -> object:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def read_tail_bytes(self, bucket: str, key: str, *, max_bytes: int) -> bytes:
        client = self.client
        head_object = getattr(client, "head_object")
        get_object = getattr(client, "get_object")
        size = int(head_object(Bucket=bucket, Key=key).get("ContentLength") or 0)
        range_header = f"bytes=-{max_bytes}" if size > max_bytes else None
        kwargs: dict[str, object] = {"Bucket": bucket, "Key": key}
        if range_header is not None:
            kwargs["Range"] = range_header
        response = get_object(**kwargs)
        body = response["Body"]
        try:
            return body.read(max_bytes)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()


class ArtifactReader:
    def __init__(
        self,
        config: ArtifactReaderConfig | None = None,
        *,
        object_reader: ObjectReader | None = None,
    ) -> None:
        self.config = config or ArtifactReaderConfig.from_env()
        self.object_reader = object_reader or Boto3ObjectReader()

    def read_text_tail(self, log_uri: str | None) -> ArtifactLogReadResult:
        if log_uri is None or not str(log_uri).strip():
            raise ArtifactLogError(
                LOG_ERROR_NOT_PUBLISHED,
                "Job log has not been published.",
                status_code=404,
                reason="missing_log_uri",
            )

        raw_uri = str(log_uri).strip()
        safe_uri = safe_public_log_uri(raw_uri)
        _reject_credential_bearing_uri(raw_uri, safe_uri=safe_uri)

        if raw_uri.startswith(self.config.uri_prefix):
            return self._read_published_uri(raw_uri, safe_uri=safe_uri)
        if raw_uri.startswith("file://"):
            return self._read_file_uri(raw_uri, safe_uri=safe_uri)
        if raw_uri.startswith("s3://"):
            return self._read_s3_uri(raw_uri, safe_uri=safe_uri)
        if _has_uri_scheme(raw_uri):
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "Job log URI scheme is unsupported.",
                status_code=400,
                safe_uri=safe_uri,
                reason="unsupported_scheme",
            )
        return self._read_legacy_local_uri(raw_uri, safe_uri=safe_uri)

    def _read_published_uri(self, uri: str, *, safe_uri: str) -> ArtifactLogReadResult:
        if self.config.published_root is None:
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "Published artifact root is not configured.",
                status_code=400,
                safe_uri=safe_uri,
                reason="published_root_missing",
            )
        relative = _relative_path_from_published_uri(uri, self.config.uri_prefix, safe_uri=safe_uri)
        return self._read_local_tail(self.config.published_root / relative, self.config.published_root, safe_uri)

    def _read_file_uri(self, uri: str, *, safe_uri: str) -> ArtifactLogReadResult:
        parsed = urlsplit(uri)
        if parsed.netloc not in {"", "localhost"}:
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "File log URI host is unsupported.",
                status_code=400,
                safe_uri=safe_uri,
                reason="file_host_unsupported",
            )
        path = _absolute_file_uri_path(parsed.path, safe_uri=safe_uri)
        if self.config.published_root is not None:
            published_root = _absolute_path(self.config.published_root)
            if _path_is_relative_to(path, published_root):
                relative = _relative_posix_path(path, published_root)
                _safe_relative_uri_path(relative, safe_uri=safe_uri, require_logs_prefix=True)
                return self._read_local_tail(path, published_root, safe_uri)
        if self.config.allow_legacy_local_file_logs:
            legacy_root = _legacy_root_path(self.config)
            if _path_is_relative_to(path, legacy_root):
                return self._read_local_tail(path, legacy_root, safe_uri)
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "File log URI is outside the allowed log roots.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="path_outside_root",
        )

    def _read_s3_uri(self, uri: str, *, safe_uri: str) -> ArtifactLogReadResult:
        parsed = urlsplit(uri)
        bucket = parsed.netloc
        if not bucket:
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "S3 log URI is malformed.",
                status_code=400,
                safe_uri=safe_uri,
                reason="s3_bucket_missing",
            )
        key = _safe_relative_uri_path(parsed.path.lstrip("/"), safe_uri=safe_uri, require_logs_prefix=False)
        if self.config.s3_bucket is None:
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "Published artifact S3 allowlist is not configured.",
                status_code=400,
                safe_uri=safe_uri,
                reason="s3_allowlist_missing",
            )
        allowed_prefix = self.config.s3_prefix.strip("/")
        if bucket != self.config.s3_bucket or not _s3_key_matches_prefix(key, allowed_prefix):
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "S3 log URI is outside the published artifact allowlist.",
                status_code=403,
                safe_uri=safe_uri,
                reason="s3_allowlist_mismatch",
            )
        try:
            data = self.object_reader.read_tail_bytes(bucket, key, max_bytes=self.config.tail_max_bytes)
        except (FileNotFoundError, KeyError) as error:
            raise ArtifactLogError(
                LOG_ERROR_NOT_FOUND,
                "Published job log was not found.",
                status_code=404,
                safe_uri=safe_uri,
                reason="s3_not_found",
            ) from error
        except PermissionError as error:
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "Published job log access was denied.",
                status_code=403,
                safe_uri=safe_uri,
                reason="s3_access_denied",
            ) from error
        except Exception as error:
            error_code = str(getattr(error, "response", {}).get("Error", {}).get("Code", "")).lower()
            if error_code in {"nosuchkey", "notfound", "404"}:
                raise ArtifactLogError(
                    LOG_ERROR_NOT_FOUND,
                    "Published job log was not found.",
                    status_code=404,
                    safe_uri=safe_uri,
                    reason="s3_not_found",
                ) from error
            if error_code in {"accessdenied", "forbidden", "403"}:
                raise ArtifactLogError(
                    LOG_ERROR_ACCESS_DENIED,
                    "Published job log access was denied.",
                    status_code=403,
                    safe_uri=safe_uri,
                    reason="s3_access_denied",
                ) from error
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "Published job log could not be read.",
                status_code=403,
                safe_uri=safe_uri,
                reason="s3_read_failed",
            ) from error
        return ArtifactLogReadResult(
            log_uri=safe_uri,
            content=_redacted_text_from_bytes(data),
            truncated=len(data) >= self.config.tail_max_bytes,
        )

    def _read_legacy_local_uri(self, uri: str, *, safe_uri: str) -> ArtifactLogReadResult:
        if not self.config.allow_legacy_local_file_logs:
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "Legacy local job log access is disabled.",
                status_code=403,
                safe_uri=safe_uri,
                reason="legacy_local_disabled",
            )
        root = _legacy_root_path(self.config)
        if Path(uri).is_absolute():
            target = _absolute_local_path(uri, safe_uri=safe_uri)
            return self._read_local_tail(target, root, safe_uri)
        relative = _safe_relative_uri_path(
            uri,
            safe_uri=safe_uri,
            require_logs_prefix=False,
            reject_credential_components=False,
        )
        return self._read_local_tail(root / relative, root, safe_uri)

    def _read_local_tail(self, path: Path, root: Path, safe_uri: str) -> ArtifactLogReadResult:
        root_path = _absolute_path(root)
        target = _absolute_path(path)
        try:
            target.relative_to(root_path)
        except ValueError as error:
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "Job log path is outside the published artifact root.",
                status_code=403,
                safe_uri=_unsafe_log_uri_summary(safe_uri),
                reason="path_outside_root",
            ) from error
        if not root_path.exists():
            raise ArtifactLogError(
                LOG_ERROR_NOT_FOUND,
                "Published job log was not found.",
                status_code=404,
                safe_uri=safe_uri,
                reason="local_not_found",
            )
        try:
            data = read_tail_bytes_limited_no_follow(
                target,
                max_bytes=self.config.tail_max_bytes,
                containment_root=root_path,
            )
        except FileNotFoundError as error:
            raise ArtifactLogError(
                LOG_ERROR_NOT_FOUND,
                "Published job log was not found.",
                status_code=404,
                safe_uri=safe_uri,
                reason="local_not_found",
            ) from error
        except (OSError, SafeFilesystemError) as error:
            raise ArtifactLogError(
                LOG_ERROR_ACCESS_DENIED,
                "Published job log access was denied.",
                status_code=403,
                safe_uri=safe_uri,
                reason="unsafe_local_path",
            ) from error
        except ValueError as error:
            raise ArtifactLogError(
                LOG_ERROR_URI_UNSUPPORTED,
                "Job log URI is malformed.",
                status_code=400,
                safe_uri=_unsafe_log_uri_summary(safe_uri),
                reason="malformed_path",
            ) from error
        return ArtifactLogReadResult(
            log_uri=safe_uri,
            content=_redacted_text_from_bytes(data),
            truncated=len(data) >= self.config.tail_max_bytes,
        )


def default_artifact_reader_config(
    env: Mapping[str, str] | None = None,
    *,
    display_readonly: bool = False,
) -> ArtifactReaderConfig:
    return ArtifactReaderConfig.from_env(env, display_readonly=display_readonly)


def published_log_uri(
    *,
    source: str,
    cycle_time: object,
    run_id: str,
    job_id: str,
    stream: str = "out",
    uri_prefix: str | None = None,
) -> str:
    prefix = uri_prefix or os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", DEFAULT_PUBLISHED_URI_PREFIX)
    if not prefix:
        prefix = DEFAULT_PUBLISHED_URI_PREFIX
    compact_cycle = format_cycle_time(cycle_time) if not isinstance(cycle_time, str) else cycle_time
    segments = (source, compact_cycle, run_id)
    for segment in segments:
        _validate_public_uri_segment(str(segment))
    _validate_public_log_name(f"{job_id}.{stream}")
    return f"{_prefix_with_separator(prefix)}logs/{source}/{compact_cycle}/{run_id}/{job_id}.{stream}"


def safe_public_log_uri(value: str | None, *, max_length: int = PUBLIC_LOG_URI_MAX_LENGTH) -> str | None:
    if value is None:
        return None
    redacted = str(redact_payload(_strip_unsafe_uri_parts(str(value))))
    if len(redacted) <= max_length:
        return redacted
    return f"{redacted[: max_length - 14]}...[truncated]"


def _strip_unsafe_uri_parts(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[redacted]"
    if parsed.scheme:
        if parsed.scheme == "published":
            return _strip_unsafe_published_uri_parts(parsed)
        if parsed.scheme == "file":
            return "file://redacted"
        try:
            hostname = parsed.hostname or parsed.netloc.split("@")[-1]
            port = parsed.port
        except ValueError:
            return "[redacted]"
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        path = (
            "[redacted]"
            if _local_path_needs_redaction(parsed.path, redact_absolute=False)
            or _path_has_credential_like_part(parsed.path)
            else parsed.path
        )
        return urlunsplit((parsed.scheme, netloc, path, "", ""))
    stripped = value.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return (
        "[redacted]"
        if _local_path_needs_redaction(stripped, redact_absolute=True) or _path_has_credential_like_part(stripped)
        else stripped
    )


def _strip_unsafe_published_uri_parts(parsed: SplitResult) -> str:
    namespace = _published_namespace_from_parsed(parsed)
    if namespace is None:
        return _PUBLISHED_REDACTED_URI
    if _published_namespace_is_public_safe(namespace):
        return f"published://{namespace}"
    if _published_namespace_has_logs_prefix(namespace):
        return _PUBLISHED_LOGS_REDACTED_URI
    return _PUBLISHED_REDACTED_URI


def _published_namespace_from_parsed(parsed: SplitResult) -> str | None:
    try:
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        return None
    if username or password:
        return None
    if parsed.netloc:
        if port is not None:
            return None
        authority = parsed.hostname or parsed.netloc.rsplit("@", maxsplit=1)[-1]
        path = parsed.path.lstrip("/")
        return f"{authority}/{path}" if path else authority
    return parsed.path.lstrip("/")


def _published_namespace_is_public_safe(namespace: str) -> bool:
    if not namespace or "%" in namespace or "\\" in namespace or _ENCODED_FORBIDDEN_RE.search(namespace):
        return False
    try:
        decoded = unquote(namespace)
    except Exception:
        return False
    if _contains_control_character(namespace) or _contains_control_character(decoded) or "\\" in decoded:
        return False
    raw_parts = namespace.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return False
    parts = PurePosixPath(decoded).parts
    if not parts or parts[0] != "logs":
        return False
    if ".nhms-runs" in parts or _path_has_credential_like_part(namespace):
        return False
    return all(_SAFE_PUBLIC_SEGMENT_RE.fullmatch(part) for part in parts)


def _published_namespace_has_logs_prefix(namespace: str) -> bool:
    return namespace.split("/", maxsplit=1)[0] == "logs"


def _relative_path_from_published_uri(uri: str, uri_prefix: str, *, safe_uri: str) -> Path:
    suffix = uri.removeprefix(uri_prefix).lstrip("/")
    return Path(_safe_relative_uri_path(suffix, safe_uri=safe_uri, require_logs_prefix=True))


def _absolute_file_uri_path(raw_path: str, *, safe_uri: str) -> Path:
    if not raw_path.startswith("/"):
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "File log URI must contain an absolute path.",
            status_code=400,
            safe_uri=safe_uri,
            reason="file_path_not_absolute",
        )
    return _absolute_local_path(raw_path, safe_uri=safe_uri)


def _absolute_local_path(raw_path: str, *, safe_uri: str) -> Path:
    path = _safe_decoded_path(raw_path, safe_uri=safe_uri)
    pure = PurePosixPath(path)
    if any(part in {".", ".."} for part in pure.parts):
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Job log path contains unsafe components.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="unsafe_path_component",
        )
    if ".nhms-runs" in pure.parts:
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Private run workspace logs are not published artifacts.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="private_workspace_path",
        )
    return _absolute_path(Path(path))


def _safe_relative_uri_path(
    raw_path: str,
    *,
    safe_uri: str,
    require_logs_prefix: bool,
    reject_credential_components: bool = True,
) -> str:
    path = _safe_decoded_path(raw_path, safe_uri=safe_uri)
    if path.startswith("/"):
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Job log path must be relative to the published namespace.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="absolute_path_forbidden",
        )
    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Job log path contains unsafe components.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="unsafe_path_component",
        )
    if ".nhms-runs" in parts:
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Private run workspace logs are not published artifacts.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="private_workspace_path",
        )
    if reject_credential_components:
        for part in parts:
            if _CREDENTIAL_WORD_RE.search(part):
                raise ArtifactLogError(
                    LOG_ERROR_URI_UNSUPPORTED,
                    "Job log URI contains credential-like path components.",
                    status_code=400,
                    safe_uri=_unsafe_log_uri_summary(safe_uri),
                    reason="credential_path_component",
                )
    if require_logs_prefix and parts[0] != "logs":
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "Published job log URI must be under logs/.",
            status_code=400,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="published_logs_prefix_required",
        )
    return "/".join(parts)


def _safe_decoded_path(raw_path: str, *, safe_uri: str) -> str:
    if "\\" in raw_path or _ENCODED_FORBIDDEN_RE.search(raw_path):
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Job log URI contains unsafe path separators or traversal.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="encoded_or_backslash_path",
        )
    decoded = unquote(raw_path)
    if _contains_control_character(decoded):
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "Job log URI contains malformed path characters.",
            status_code=400,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="malformed_path",
        )
    if "\\" in decoded:
        raise ArtifactLogError(
            LOG_ERROR_ACCESS_DENIED,
            "Job log URI contains unsafe path separators.",
            status_code=403,
            safe_uri=_unsafe_log_uri_summary(safe_uri),
            reason="backslash_path",
        )
    return decoded


def _reject_credential_bearing_uri(raw_uri: str, *, safe_uri: str) -> None:
    try:
        parsed = urlsplit(raw_uri)
        parsed.hostname
        parsed.port
    except ValueError as error:
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "Job log URI is malformed.",
            status_code=400,
            safe_uri=safe_uri,
            reason="malformed_uri",
        ) from error
    rejection_safe_uri = _unsafe_log_uri_summary(safe_uri) if _path_has_credential_like_part(parsed.path) else safe_uri
    if parsed.username or parsed.password:
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "Job log URI must not include credentials.",
            status_code=400,
            safe_uri=rejection_safe_uri,
            reason="userinfo_forbidden",
        )
    if parsed.query or parsed.fragment:
        raise ArtifactLogError(
            LOG_ERROR_URI_UNSUPPORTED,
            "Job log URI must not include query strings or fragments.",
            status_code=400,
            safe_uri=rejection_safe_uri,
            reason="query_or_fragment_forbidden",
        )


def _s3_key_matches_prefix(key: str, prefix: str) -> bool:
    if not prefix:
        return key.startswith("logs/")
    if key.startswith(f"{prefix}/logs/"):
        return True
    return _s3_key_matches_legacy_run_log_prefix(key, prefix)


def _s3_key_matches_legacy_run_log_prefix(key: str, prefix: str) -> bool:
    key_parts = PurePosixPath(key).parts
    prefix_parts = PurePosixPath(prefix).parts
    if not prefix_parts or key_parts[: len(prefix_parts)] != prefix_parts:
        return False
    legacy_parts = key_parts[len(prefix_parts) :] if prefix_parts[-1] == "runs" else key_parts[len(prefix_parts) + 1 :]
    if prefix_parts[-1] != "runs" and (
        len(key_parts) < len(prefix_parts) + 1 or key_parts[len(prefix_parts)] != "runs"
    ):
        return False
    if len(legacy_parts) < 3 or legacy_parts[1] != "logs":
        return False
    run_id = legacy_parts[0]
    log_parts = legacy_parts[2:]
    return _SAFE_PUBLIC_SEGMENT_RE.fullmatch(run_id) is not None and all(
        _SAFE_PUBLIC_SEGMENT_RE.fullmatch(part) is not None for part in log_parts
    )


def _allow_legacy_local_file_logs(env: Mapping[str, str], *, display_readonly: bool) -> bool:
    raw = env.get("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS")
    if raw is None:
        return not display_readonly
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return False


def _positive_int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(1, value)


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or not raw.strip():
        return None
    return _absolute_path(Path(raw.strip()).expanduser())


def _legacy_root_path(config: ArtifactReaderConfig) -> Path:
    return _absolute_path((config.legacy_log_root or Path("workspace")).expanduser())


def _absolute_path(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        _absolute_path(path).relative_to(_absolute_path(root))
    except ValueError:
        return False
    return True


def _relative_posix_path(path: Path, root: Path) -> str:
    return _absolute_path(path).relative_to(_absolute_path(root)).as_posix()


def _local_path_needs_redaction(raw_path: str, *, redact_absolute: bool) -> bool:
    if not raw_path:
        return False
    try:
        path = unquote(raw_path)
    except Exception:
        path = raw_path
    path = path.replace("\\", "/")
    parts = PurePosixPath(path).parts
    if redact_absolute and PurePosixPath(path).is_absolute():
        return True
    if ".nhms-runs" in parts:
        return True
    return path == "/scratch" or path.startswith("/scratch/") or path == "/tmp" or path.startswith("/tmp/")


def _path_has_credential_like_part(raw_path: str) -> bool:
    if not raw_path:
        return False
    try:
        path = unquote(raw_path)
    except Exception:
        path = raw_path
    return any(_CREDENTIAL_WORD_RE.search(part) for part in PurePosixPath(path).parts)


def _redacted_text_from_bytes(data: bytes) -> str:
    redacted = redact_payload(data.decode("utf-8", errors="replace"))
    return redacted if isinstance(redacted, str) else str(redacted)


def _has_uri_scheme(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value.strip()) is not None


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_public_uri_segment(value: str) -> None:
    if not _SAFE_PUBLIC_SEGMENT_RE.fullmatch(value):
        raise ValueError(f"Unsafe published artifact URI segment: {value!r}")


def _validate_public_log_name(value: str) -> None:
    name, dot, suffix = value.rpartition(".")
    if dot != "." or suffix not in {"out", "err", "log"} or not _SAFE_PUBLIC_SEGMENT_RE.fullmatch(name):
        raise ValueError(f"Unsafe published artifact log name: {value!r}")


def _prefix_with_separator(prefix: str) -> str:
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _unsafe_log_uri_summary(safe_uri: str) -> str:
    try:
        parsed = urlsplit(safe_uri)
    except ValueError:
        return "[redacted]"
    if parsed.scheme == "published":
        namespace = _published_namespace_from_parsed(parsed)
        if namespace is not None and _published_namespace_has_logs_prefix(namespace):
            return _PUBLISHED_LOGS_REDACTED_URI
        return _PUBLISHED_REDACTED_URI
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, "[redacted]", "", ""))
    if parsed.scheme:
        return f"{parsed.scheme}://[redacted]"
    return "[redacted]"
