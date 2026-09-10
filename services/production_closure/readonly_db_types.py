from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_API_PROBE_ADAPTER_MODULE = ".".join(("apps", "api", "readonly_validation_probe"))

DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "two-node-e2e"

APPROVED_EVIDENCE_ROOTS = (REPO_ROOT / "artifacts", Path("/scratch/frd_muziyao"))

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

SAFE_DDL_SUFFIX_RE = re.compile(r"[^a-z0-9_]+")

MAX_EVIDENCE_PAYLOAD_BYTES = 1024 * 1024

MAX_EVIDENCE_TRAVERSAL_DEPTH = 256

MAX_EVIDENCE_TRAVERSAL_NODES = 100_000

STATUS_PASS = "PASS"

STATUS_FAIL = "FAIL"

STATUS_BLOCKED = "BLOCKED"

LIVE_EVIDENCE_SCHEMA = "nhms.readonly_db_boundary.evidence.v1"

SIMULATED_EVIDENCE_SCHEMA = "nhms.readonly_db_boundary.evidence.simulated.v1"

FULL_PASS_SOURCES = frozenset({"GFS", "IFS"})

AUTHORITATIVE_EVIDENCE_FILENAMES = (
    "summary.json",
    "role.json",
    "route_smoke.json",
    "permission_probes.json",
)

READONLY_DB_URL_ENVS = (
    "NHMS_DISPLAY_READONLY_DATABASE_URL",
    "NHMS_READONLY_DB_VALIDATION_DATABASE_URL",
)

DISPLAY_OBJECT_STORE_ROOT_ENVS = ("OBJECT_STORE_ROOT", "NHMS_PRODUCTION_OBJECT_STORE_ROOT")

VALIDATION_ENV_PREFIX = "NHMS_READONLY_DB_VALIDATION_"

VALIDATION_CONNECT_TIMEOUT_SECONDS = 5

VALIDATION_STATEMENT_TIMEOUT_MS = 10_000

VALIDATION_LOCK_TIMEOUT_MS = 2_000

VALIDATION_IDLE_TIMEOUT_MS = 10_000

DENIED_SQLSTATES = frozenset({"25006", "42501"})

BLOCKED_SQLSTATES = frozenset({"3F000", "42P01", "42703"})

ROLE_ATTRIBUTE_WRITE_FLAGS = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
)

TABLE_CATALOG_MUTATING_OPERATIONS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)

TABLE_CATALOG_ONLY_MUTATING_OPERATIONS = ("TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")

ROUTE_FIXTURE_BLOCKER_ERROR_CODES = frozenset(
    {
        "QHH_LATEST_PRODUCT_UNAVAILABLE",
        "PIPELINE_CYCLE_NOT_FOUND",
        "PIPELINE_STRICT_IDENTITY_NOT_FOUND",
        "JOB_NOT_FOUND",
        "JOB_LOG_NOT_PUBLISHED",
        "JOB_LOG_NOT_FOUND",
        "JOB_LOG_URI_UNSUPPORTED",
        "JOB_LOG_ACCESS_DENIED",
    }
)

class ReadonlyDbValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ProbeTarget:
    schema: str
    table: str
    surface: str

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class PermissionProbeSpec:
    operation: str
    target: ProbeTarget | None
    command: str
    probe_column: str | None = None
    ddl_schema: str | None = None
    ddl_table: str | None = None


@dataclass(frozen=True)
class ProbeExecution:
    outcome: str
    sqlstate: str | None = None
    message: str | None = None
    rowcount: int | None = None
    rolled_back: bool = True


@dataclass(frozen=True)
class RouteHttpResponse:
    status_code: int
    body: Any | None = None
    text: str = ""


@dataclass(frozen=True)
class ReadonlyDbMergeSourceEvidence:
    source_dir: Path
    summary: dict[str, Any]
    artifacts: dict[str, dict[str, Any]]
    parent_binding_field: str


class ReadonlyDbProbeAdapter(Protocol):
    def current_role(self) -> dict[str, Any]: ...

    def discover_display_identity(self) -> dict[str, Any]: ...

    def schema_exists(self, schema: str) -> bool: ...

    def table_exists(self, target: ProbeTarget) -> bool: ...

    def table_privileges(self, target: ProbeTarget) -> dict[str, bool]: ...

    def column_privileges(self, target: ProbeTarget) -> dict[str, list[str]]: ...

    def sequence_privileges(self, target: ProbeTarget) -> list[dict[str, Any]]: ...

    def schema_privileges(self, schema: str) -> dict[str, bool]: ...

    def database_privileges(self) -> dict[str, Any]: ...

    def audited_schema_sequence_privileges(self, schemas: tuple[str, ...]) -> list[dict[str, Any]]: ...

    def reachable_role_privileges(
        self,
        targets: tuple[ProbeTarget, ...],
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]: ...

    def first_updatable_column(self, target: ProbeTarget) -> str | None: ...

    def execute_probe(self, spec: PermissionProbeSpec) -> ProbeExecution: ...

RouteRequester = Callable[[str, str], RouteHttpResponse]

PERMISSION_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("hydro", "hydro_run", "hydro_run_terminal_state"),
    ProbeTarget("hydro", "river_timeseries", "hydro_display_timeseries"),
    ProbeTarget("met", "forecast_cycle", "met_cycle_state"),
    ProbeTarget("met", "forcing_station_timeseries", "met_station_timeseries"),
    ProbeTarget("ops", "pipeline_job", "pipeline_job_state"),
    ProbeTarget("ops", "pipeline_event", "pipeline_event_audit"),
)

@dataclass(frozen=True)
class ReadonlyDbValidationConfig:
    evidence_root: Path
    run_id: str
    database_url: str | None = None
    source: str | None = None
    cycle_time: str | None = None
    strict_run_id: str | None = None
    model_id: str | None = None
    job_id: str | None = None
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "db" / "readonly-db-boundary"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path | None = None,
        run_id: str | None = None,
        database_url: str | None = None,
        source: str | None = None,
        cycle_time: str | None = None,
        strict_run_id: str | None = None,
        model_id: str | None = None,
        job_id: str | None = None,
        force: bool = False,
    ) -> ReadonlyDbValidationConfig:
        selected_database_url = database_url or _first_env(READONLY_DB_URL_ENVS)
        selected_evidence_root = evidence_root or _path_env(
            "NHMS_READONLY_DB_VALIDATION_EVIDENCE_ROOT",
            DEFAULT_EVIDENCE_ROOT,
        )
        return cls(
            evidence_root=_safe_resolved_evidence_root(selected_evidence_root),
            run_id=_safe_run_id(
                run_id or os.getenv("NHMS_READONLY_DB_VALIDATION_EVIDENCE_RUN_ID") or _default_run_id()
            ),
            database_url=selected_database_url.strip() if selected_database_url else None,
            source=source or os.getenv(f"{VALIDATION_ENV_PREFIX}SOURCE") or None,
            cycle_time=cycle_time or os.getenv(f"{VALIDATION_ENV_PREFIX}CYCLE_TIME") or None,
            strict_run_id=strict_run_id or os.getenv(f"{VALIDATION_ENV_PREFIX}RUN_ID") or None,
            model_id=model_id or os.getenv(f"{VALIDATION_ENV_PREFIX}MODEL_ID") or None,
            job_id=job_id or os.getenv(f"{VALIDATION_ENV_PREFIX}JOB_ID") or None,
            force=force,
        )


def _safe_resolved_evidence_root(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    approved_roots = tuple(root.expanduser().resolve(strict=False) for root in APPROVED_EVIDENCE_ROOTS)
    for root in approved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ReadonlyDbValidationError(
        "READONLY_DB_EVIDENCE_ROOT_UNAPPROVED",
        "Readonly DB evidence root must be under repository artifacts/ or /scratch/frd_muziyao.",
    )


def _refuse_symlink_components(path: Path) -> None:
    current = path.expanduser()
    candidates = [current, *current.parents]
    for component in candidates:
        if component.exists() and component.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence path component must not be a symlink: {component}.",
            )


def _safe_run_id(value: str) -> str:
    text = value.strip()
    if not SAFE_RUN_ID_RE.fullmatch(text) or ".." in text:
        raise ReadonlyDbValidationError(
            "READONLY_DB_RUN_ID_UNSAFE",
            "run_id must be a bounded alphanumeric identifier using only '.', '_' or '-'.",
        )
    return text


def _default_run_id() -> str:
    return f"readonly-db-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value) if value else default
