from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlparse

from packages.common.source_identity import normalize_source_id


@dataclass(frozen=True)
class ObjectPathValidation:
    """Result returned by object storage path validation."""

    valid: bool
    category: str | None
    components: dict[str, str]
    error: str | None = None


@dataclass(frozen=True)
class ObjectPrefixPattern:
    """Configured object storage prefix pattern."""

    display: str
    category: str
    segments: tuple[str, ...]
    captured_literals: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveProvenancePaths:
    """Deterministic archive object and manifest paths for one identity."""

    archive: Path
    manifest: Path


@dataclass(frozen=True)
class ArchiveIdentity:
    """Canonical source-qualified identity for one archived product."""

    lane: str
    source: str
    cycle_identity: str
    cycle_time: str
    basin_version_id: str | None = None
    model_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identity_component(self.source)
        if self.source == LEGACY_UNQUALIFIED_ARCHIVE_SOURCE:
            if self.lane != "states":
                raise ArchiveConfigurationError(
                    f"archive source {LEGACY_UNQUALIFIED_ARCHIVE_SOURCE!r} is reserved for the states lane"
                )
            canonical_source = LEGACY_UNQUALIFIED_ARCHIVE_SOURCE
        else:
            try:
                canonical_source = normalize_source_id(self.source)
            except ValueError as error:
                raise ArchiveConfigurationError(f"invalid archive source: {self.source!r}") from error
        object.__setattr__(self, "source", canonical_source)
        _validate_archive_identity(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArchiveIdentity:
        """Build a canonical identity from a strict manifest mapping."""
        lane = _required_mapping_string(value, "lane", label="archive identity")
        allowed = {
            "forcing": {"lane", "source", "cycle_identity", "cycle_time", "basin_version_id", "model_id"},
            "runs": {"lane", "source", "cycle_identity", "cycle_time", "run_id"},
            "states": {"lane", "source", "cycle_identity", "cycle_time", "model_id"},
        }.get(lane)
        if allowed is None:
            raise ArchiveConfigurationError(f"archive lane must be one of {sorted(ARCHIVE_LANES)}: {lane!r}")
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ArchiveConfigurationError(f"archive identity contains fields invalid for {lane}: {unexpected}")
        return cls(
            lane=lane,
            source=_required_mapping_string(value, "source", label="archive identity"),
            cycle_identity=_required_mapping_string(value, "cycle_identity", label="archive identity"),
            cycle_time=_required_mapping_string(value, "cycle_time", label="archive identity"),
            basin_version_id=_optional_mapping_string(value, "basin_version_id", label="archive identity"),
            model_id=_optional_mapping_string(value, "model_id", label="archive identity"),
            run_id=_optional_mapping_string(value, "run_id", label="archive identity"),
        )


@dataclass(frozen=True)
class ArchiveStorageConfig:
    """Validated archive configuration shared by node-27 maintenance tools."""

    archive_root: Path
    archive_min_age_days: int
    retention_days: int


class ArchiveConfigurationError(ValueError):
    """Raised when archive configuration is unsafe or incomplete."""


ARCHIVE_LANES = frozenset({"forcing", "runs", "states"})
LEGACY_UNQUALIFIED_ARCHIVE_SOURCE = "legacy-unqualified"
DEFAULT_ARCHIVE_MIN_AGE_DAYS = 14

# Runner-equivalent default for the DB retention window (#1227). This constant
# exists ONLY to mirror `scripts/node27_timeseries_retention.py`, whose window
# variable is optional: a missing or empty assignment means the runner runs
# this many days. The retention runner imports this same constant so the two
# sides cannot drift. It is NEVER a fallback for an unreadable window source —
# those fail closed in `read_retention_window_days`.
DEFAULT_RETENTION_WINDOW_DAYS = 14

RETENTION_ENV_PATH_VARIABLE = "NODE27_TIMESERIES_RETENTION_ENV"
RETENTION_WINDOW_VARIABLE = "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"
RETENTION_VARIABLE_PREFIX = "NODE27_TIMESERIES_RETENTION_"

_ENV_ASSIGNMENT_PATTERN = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")

# Line-break characters `str.splitlines` would consume but a POSIX shell would
# NOT treat as a line terminator (#1227 design D1 round-1 amendments). Content
# carrying any of them is refused instead of being silently re-split: bash keeps
# e.g. a CRLF `\r` inside the value and the retention runner refuses it, so
# accepting such a file would validate against a window the runner never uses.
_NON_NEWLINE_LINE_BREAKS = "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


VALID_PREFIX_PATTERNS: tuple[ObjectPrefixPattern, ...] = (
    ObjectPrefixPattern("raw/{source}/{cycle_time}/...", "raw", ("raw", "{source}", "{cycle_time}")),
    ObjectPrefixPattern(
        "canonical/{source}/{cycle_time}/{variable}/...",
        "canonical",
        ("canonical", "{source}", "{cycle_time}", "{variable}"),
    ),
    ObjectPrefixPattern(
        "forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/...",
        "forcing",
        ("forcing", "{source}", "{cycle_time}", "{basin_version_id}", "{model_id}"),
    ),
    ObjectPrefixPattern("models/{model_id}/...", "models", ("models", "{model_id}")),
    ObjectPrefixPattern("states/{model_id}/{valid_time}/...", "states", ("states", "{model_id}", "{valid_time}")),
    ObjectPrefixPattern(
        "runs/{run_id}/input/...",
        "runs",
        ("runs", "{run_id}", "input"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "runs/{run_id}/output/...",
        "runs",
        ("runs", "{run_id}", "output"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "runs/{run_id}/logs/...",
        "runs",
        ("runs", "{run_id}", "logs"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "tiles/hydro/{run_id}/...",
        "tiles",
        ("tiles", "hydro", "{run_id}"),
        captured_literals={1: "tile_type"},
    ),
)


VALID_PREFIX_MESSAGE = ", ".join(pattern.display for pattern in VALID_PREFIX_PATTERNS)


def resolve_archive_root(
    script_name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the shared archive root, honoring a per-script override."""
    source_env = os.environ if env is None else env
    override_name = _archive_override_name(script_name) if script_name else None
    value = source_env.get(override_name, "") if override_name else ""
    if not value.strip():
        value = source_env.get("NHMS_ARCHIVE_ROOT", "")
    if not value.strip():
        expected = f" or {override_name}" if override_name else ""
        raise ArchiveConfigurationError(f"NHMS_ARCHIVE_ROOT{expected} must be set")
    return _normalized_filesystem_path(value, label="archive root")


def read_retention_window_days(env_path: str | os.PathLike[str] | None) -> int:
    """Read the LIVE DB retention window from the deployed retention env file.

    `env_path` is the absolute path named by `NODE27_TIMESERIES_RETENTION_ENV`
    — the same file `scripts/node27_timeseries_retention.py` is started with.
    Only `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` is extracted; nothing is
    sourced or executed.

    Resolution mirrors the retention runner (#1227 design D1): an ABSENT or
    EMPTY window assignment resolves to `DEFAULT_RETENTION_WINDOW_DAYS`, but
    ONLY when the file is recognizably the deployed retention env — at least
    one `NODE27_TIMESERIES_RETENTION_*` assignment was accepted, EXCLUDING
    `NODE27_TIMESERIES_RETENTION_ENV` itself, which is the archive-side pointer
    at this file (it lives in the ARCHIVE env files and is never consumed by
    the runner, so counting it would let the guard default off its own env
    file — #1227 round-2 C1). That default is the runner's live-effective
    window, never a fallback for an unreadable source.

    Everything else fails closed with `ArchiveConfigurationError` and no
    constant fallback: an unset, empty or relative path variable; a
    missing/unreadable/non-UTF-8 file; a PRESENT value that is not a positive
    integer; a readable file carrying no recognized retention-family
    assignment at all (wrong file, `/dev/null`, a stale copy).

    The file is judged by a CLOSED-WORLD line grammar (#1230 design D1):
    every line must be blank, a full-line `#` comment, or a
    `[export ]KEY=VALUE` assignment of ANY variable name. The FIRST line that
    is not raises, naming the file and the offending line. `VAR+=21`,
    `: ${VAR:=21}`, a nested `. other.env` / `source other.env`,
    `printf -v VAR 21`, `read VAR <<< 21`, `eval 'VAR'=21`,
    `readonly`/`declare` prefixes and truncated or quoted edits all fail that
    grammar — the shapes the shell WOULD export while an open-world substring
    detector saw nothing now refuse instead of silently resolving the
    runner-equivalent default (#1230).

    Inside an accepted assignment the honored lexical forms are (shell-source
    semantics, bounded on purpose): optional `export ` prefix, single/double
    quoted values taken verbatim, a trailing ` # comment` stripped, whitespace
    stripped OUTSIDE quotes only, exact variable-name match (near-name decoys
    ignored), last assignment wins.

    Beyond the grammar these still refuse rather than mis-parse (round-1
    amendments in #1227 design D1): content carrying non-`\\n` line breaks such
    as CRLF; an UNQUOTED value with leading whitespace (`VAR= 21`, which bash
    parses as an assignment prefix plus a command, leaving the variable unset);
    a value whose first character is `#` (`VAR=#21` is a present non-integer,
    not a comment); a line continuation or `$VAR` interpolation in the window
    value (a present non-integer); and the window variable appearing on a
    CONFORMING line that is not accepted as its assignment — its name embedded
    in another assignment's key (`OLD_VAR=99`) or value (`X=VAR=21`). That last
    refusal is PER LINE and fires even when a plain assignment was accepted
    elsewhere in the file (#1227 round-2 C2).

    Known residual (#1230 design D5(a)): a quoted value spanning lines is only
    partly covered. A closing bare `"` line breaks the grammar (over-strict,
    fail-closed), but a multi-line quoted value whose every line happens to
    conform is still read line-wise while bash keeps it inside the outer
    string — quoted values MUST NOT span lines in the deployed retention env.
    """
    if env_path is None:
        raise ArchiveConfigurationError(f"{RETENTION_ENV_PATH_VARIABLE} must be set to an absolute path")
    raw_path = os.fspath(env_path).strip()
    if not raw_path:
        raise ArchiveConfigurationError(f"{RETENTION_ENV_PATH_VARIABLE} must be set to an absolute path")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ArchiveConfigurationError(f"{RETENTION_ENV_PATH_VARIABLE} must be an absolute path: {raw_path}")
    try:
        # Bytes, not `read_text`: universal-newline translation would silently
        # normalize a CRLF file that the shell (and therefore the runner) reads
        # verbatim — the `\r` must survive to be refused (#1227 design D1).
        content = path.read_bytes().decode("utf-8")
    except OSError as error:
        raise ArchiveConfigurationError(f"retention env file is unreadable: {path}: {error}") from error
    except ValueError as error:
        raise ArchiveConfigurationError(f"retention env file is not valid UTF-8: {path}") from error
    scan = _scan_env_assignment(content, RETENTION_WINDOW_VARIABLE, path=path)
    if scan.mentioned_unaccepted:
        # PER LINE, regardless of any accepted plain assignment elsewhere in the
        # file (#1227 round-2 C2): `VAR=14` + `readonly VAR=30` is exported as 30
        # by `set -a; . file`, so returning the earlier 14 would be fail-open.
        raise ArchiveConfigurationError(
            f"{RETENTION_WINDOW_VARIABLE} appears on a line this extractor cannot accept as an "
            f"assignment in {path}: {scan.mentioned_line!r} (the name is embedded in another "
            "assignment's key or value); refusing instead of parsing a window the shell would "
            "not export"
        )
    if not scan.assigned:
        if not scan.retention_family_seen:
            raise ArchiveConfigurationError(
                f"retention env file does not look like the deployed retention env: {path} carries "
                f"no {RETENTION_VARIABLE_PREFIX}* assignment, so the runner-equivalent default "
                "does not apply"
            )
        return DEFAULT_RETENTION_WINDOW_DAYS
    raw_value = scan.value or ""
    if raw_value == "":
        return DEFAULT_RETENTION_WINDOW_DAYS
    if raw_value.strip() != raw_value:
        raise ArchiveConfigurationError(
            f"{RETENTION_WINDOW_VARIABLE} must not contain leading/trailing whitespace "
            f"in {path}: {raw_value!r}"
        )
    try:
        window_days = int(raw_value)
    except ValueError as error:
        raise ArchiveConfigurationError(
            f"{RETENTION_WINDOW_VARIABLE} must be an integer in {path}: {raw_value!r}"
        ) from error
    if window_days <= 0:
        raise ArchiveConfigurationError(
            f"{RETENTION_WINDOW_VARIABLE} must be positive in {path}: {window_days}"
        )
    return window_days


@dataclass(frozen=True)
class _EnvAssignmentScan:
    """What one pass over a retention env file found about the window variable."""

    value: str | None
    assigned: bool
    mentioned_unaccepted: bool
    retention_family_seen: bool
    mentioned_line: str | None


def _scan_env_assignment(content: str, name: str, *, path: Path) -> _EnvAssignmentScan:
    """Scan `content` for the last accepted assignment of `name` (#1227 D1).

    The file grammar is CLOSED-WORLD (#1230 design D1): a line is legal only
    if, after `strip()`, it is empty, a full-line `#` comment, or a fullmatch
    of `_ENV_ASSIGNMENT_PATTERN` (`[export ]KEY=VALUE`, any variable name).
    The first line outside that grammar refuses here, naming the file and the
    offending line, because the shell would still act on it: `VAR+=`,
    `: ${VAR:=}`, a nested `.`/`source`, `printf -v`, `read`, `eval`,
    `readonly`/`declare` prefixes all export a window this line-oriented
    extractor cannot see. Enumerating the LEGAL shapes is what closes them;
    enumerating illegal ones (the pre-#1230 `name=` substring test) could not
    see a nested `source` at all.

    Within that grammar the scan also records whether any conforming LINE
    mentions `name=` without being accepted as `name`'s assignment (the name
    embedded in another assignment's key or value) plus the first such line,
    and whether any retention-family assignment was accepted at all, so the
    caller can tell an absent assignment in the real retention env apart from
    the wrong file. The mention flag is per line and independent of accepted
    assignments (#1227 round-2 C2). `RETENTION_ENV_PATH_VARIABLE` is the
    ARCHIVE-side pointer at this file and is never consumed by the runner, so
    it does not count as retention-family recognition (#1227 round-2 C1).
    """
    offending = sorted({character for character in _NON_NEWLINE_LINE_BREAKS if character in content})
    if offending:
        raise ArchiveConfigurationError(
            f"retention env file contains non-newline line breaks in {path}: "
            f"{[repr(character) for character in offending]} — refusing rather than re-splitting "
            "content the shell would keep inside a value"
        )
    value: str | None = None
    assigned = False
    mentioned_unaccepted = False
    retention_family_seen = False
    mentioned_line: str | None = None
    for line in content.split("\n"):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        matched = _ENV_ASSIGNMENT_PATTERN.fullmatch(candidate)
        if matched is None:
            raise ArchiveConfigurationError(
                f"retention env line is not a supported assignment in {path}: {candidate!r} — "
                "every line must be blank, a full-line # comment, or a [export ]KEY=VALUE "
                "assignment; refusing instead of guessing what the shell would export"
            )
        accepted_here = matched.group(1) == name
        if f"{name}=" in candidate and not accepted_here:
            mentioned_unaccepted = True
            if mentioned_line is None:
                mentioned_line = candidate
        assigned_name = matched.group(1)
        if assigned_name.startswith(RETENTION_VARIABLE_PREFIX) and assigned_name != RETENTION_ENV_PATH_VARIABLE:
            retention_family_seen = True
        if not accepted_here:
            continue
        raw = matched.group(2)
        if raw[:1] in (" ", "\t"):
            raise ArchiveConfigurationError(
                f"{name} assignment is malformed in {path}: {candidate!r} — an unquoted value "
                "must not start with whitespace (the shell would not export the variable)"
            )
        value = _unquote_env_value(_strip_env_trailing_comment(raw))
        assigned = True
    return _EnvAssignmentScan(
        value=value,
        assigned=assigned,
        mentioned_unaccepted=mentioned_unaccepted,
        retention_family_seen=retention_family_seen,
        mentioned_line=mentioned_line,
    )


def _strip_env_trailing_comment(value: str) -> str:
    quote = ""
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            continue
        # A `#` opens a comment only AFTER whitespace: `VAR=#21` exports the
        # literal `#21` (a present non-integer that refuses downstream).
        if character == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index]
    return value


def _unquote_env_value(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] in "\"'" and candidate[-1] == candidate[0]:
        return candidate[1:-1]
    return candidate


def resolve_archive_storage_config(
    *,
    cleanup_roots: Mapping[str, str | os.PathLike[str]],
    retention_days: int,
    script_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ArchiveStorageConfig:
    """Resolve and validate archive root, age, and every cleanup target root."""
    source_env = os.environ if env is None else env
    archive_root = resolve_archive_root(script_name, env=source_env)
    raw_age = source_env.get("NHMS_ARCHIVE_MIN_AGE_DAYS", str(DEFAULT_ARCHIVE_MIN_AGE_DAYS)).strip()
    try:
        archive_min_age_days = int(raw_age)
    except ValueError as error:
        raise ArchiveConfigurationError("NHMS_ARCHIVE_MIN_AGE_DAYS must be an integer") from error
    return validate_archive_configuration(
        archive_root=archive_root,
        cleanup_roots=cleanup_roots,
        archive_min_age_days=archive_min_age_days,
        retention_days=retention_days,
    )


def validate_archive_configuration(
    *,
    archive_root: str | os.PathLike[str],
    cleanup_roots: Mapping[str, str | os.PathLike[str]],
    retention_days: int,
    archive_min_age_days: int = DEFAULT_ARCHIVE_MIN_AGE_DAYS,
) -> ArchiveStorageConfig:
    """Reject unsafe root overlap and archive ages shorter than DB retention.

    `retention_days` is REQUIRED (#1227): it is the LIVE window read from the
    deployed retention env file, never a compile-time constant. This is the
    single min-age comparison site for the mover and the inventory audit.
    """
    if not cleanup_roots:
        raise ArchiveConfigurationError("cleanup_roots must explicitly contain every cleanup target root")
    if retention_days <= 0:
        raise ArchiveConfigurationError("retention_days must be positive")
    if archive_min_age_days < retention_days:
        raise ArchiveConfigurationError(
            f"archive minimum age {archive_min_age_days} days is below DB retention {retention_days} days"
        )

    normalized_archive = _normalized_filesystem_path(archive_root, label="archive root")
    for name, cleanup_root in cleanup_roots.items():
        if not name.strip():
            raise ArchiveConfigurationError("cleanup root name must be non-empty")
        normalized_cleanup = _normalized_filesystem_path(cleanup_root, label=f"cleanup root {name}")
        if _paths_overlap(normalized_archive, normalized_cleanup):
            raise ArchiveConfigurationError(
                "archive root overlaps cleanup root "
                f"{name}: archive_root={normalized_archive}; cleanup_root={normalized_cleanup}"
            )

    return ArchiveStorageConfig(
        archive_root=normalized_archive,
        archive_min_age_days=archive_min_age_days,
        retention_days=retention_days,
    )


def archive_provenance_paths(
    archive_root: str | os.PathLike[str],
    *,
    identity: ArchiveIdentity,
) -> ArchiveProvenancePaths:
    """Map a safe archive identity to its tarball and sibling manifest."""
    _validate_archive_identity(identity)
    root = _normalized_filesystem_path(archive_root, label="archive root")
    parent = root.joinpath(*_archive_identity_path_components(identity))
    return ArchiveProvenancePaths(
        archive=parent / "archive.tar.zst",
        manifest=parent / "manifest.json",
    )


def archive_identity_for_state_reference(
    *,
    source_id: str | None,
    model_id: str,
    valid_time: datetime,
) -> ArchiveIdentity:
    """Derive the canonical archive identity for a persisted state reference."""
    if not isinstance(valid_time, datetime):
        raise ArchiveConfigurationError("state reference valid_time must be a datetime")
    if valid_time.tzinfo is None or valid_time.utcoffset() is None:
        raise ArchiveConfigurationError("state reference valid_time must be timezone-aware")
    valid_time_utc = valid_time.astimezone(UTC)
    if any((valid_time_utc.minute, valid_time_utc.second, valid_time_utc.microsecond)):
        raise ArchiveConfigurationError("state reference valid_time must resolve to a canonical UTC hourly instant")

    if source_id is None or source_id == "":
        source = LEGACY_UNQUALIFIED_ARCHIVE_SOURCE
    elif source_id == LEGACY_UNQUALIFIED_ARCHIVE_SOURCE:
        raise ArchiveConfigurationError(
            f"state reference source {LEGACY_UNQUALIFIED_ARCHIVE_SOURCE!r} "
            "is derived only from source_id None or an empty string"
        )
    else:
        source = source_id
    return ArchiveIdentity(
        lane="states",
        source=source,
        cycle_identity=valid_time_utc.strftime("%Y%m%d%H"),
        cycle_time=valid_time_utc.strftime("%Y-%m-%dT%H:00:00Z"),
        model_id=model_id,
    )


def validate_product_archive_manifest_binding(
    archive_root: str | os.PathLike[str],
    manifest: Mapping[str, object],
) -> ArchiveProvenancePaths:
    """Require manifest identity and declared sibling paths to bind canonically."""
    identity_value = manifest.get("identity")
    if not isinstance(identity_value, Mapping):
        raise ArchiveConfigurationError("product archive manifest identity must be an object")
    declared_source = _required_mapping_string(identity_value, "source", label="product archive manifest identity")
    try:
        identity = ArchiveIdentity.from_mapping(identity_value)
    except ArchiveConfigurationError as error:
        raise ArchiveConfigurationError(f"invalid product archive manifest identity: {error}") from error
    if declared_source != identity.source:
        raise ArchiveConfigurationError(
            "product archive manifest source must use its canonical storage ID: "
            f"declared={declared_source!r}; canonical={identity.source!r}"
        )

    archive_value = manifest.get("archive")
    if not isinstance(archive_value, Mapping):
        raise ArchiveConfigurationError("product archive manifest archive must be an object")
    declared_archive = _required_mapping_string(archive_value, "path", label="product archive manifest archive")
    declared_manifest = _required_mapping_string(
        archive_value,
        "manifest_path",
        label="product archive manifest archive",
    )
    _validate_root_relative_path(declared_archive, label="product archive path")
    _validate_root_relative_path(declared_manifest, label="product manifest path")

    paths = archive_provenance_paths(archive_root, identity=identity)
    root = _normalized_filesystem_path(archive_root, label="archive root")
    expected_archive = paths.archive.relative_to(root).as_posix()
    expected_manifest = paths.manifest.relative_to(root).as_posix()
    if declared_archive != expected_archive:
        raise ArchiveConfigurationError(
            "product archive path does not match canonical identity: "
            f"declared={declared_archive}; expected={expected_archive}"
        )
    if declared_manifest != expected_manifest:
        raise ArchiveConfigurationError(
            "product manifest path is not the canonical archive sibling: "
            f"declared={declared_manifest}; expected={expected_manifest}"
        )
    return paths


def _archive_override_name(script_name: str) -> str:
    normalized = script_name.strip().upper().replace("-", "_")
    if not normalized or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in normalized):
        raise ArchiveConfigurationError(f"invalid archive script name: {script_name!r}")
    return f"NODE27_{normalized}_ARCHIVE_ROOT"


def _normalized_filesystem_path(path: str | os.PathLike[str], *, label: str) -> Path:
    raw = os.fspath(path).strip()
    if not raw:
        raise ArchiveConfigurationError(f"{label} must be non-empty")
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ArchiveConfigurationError(f"{label} must be absolute: {raw}")
    return expanded.resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_identity_component(component: str) -> None:
    if not isinstance(component, str):
        raise ArchiveConfigurationError("archive identity components must be strings")
    if component != component.strip() or component in {"", ".", ".."}:
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")
    if (
        Path(component).is_absolute()
        or "/" in component
        or "\\" in component
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
    ):
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")


def _validate_archive_identity(identity: ArchiveIdentity) -> None:
    if identity.lane not in ARCHIVE_LANES:
        raise ArchiveConfigurationError(f"archive lane must be one of {sorted(ARCHIVE_LANES)}: {identity.lane!r}")
    _validate_identity_component(identity.source)
    _validate_identity_component(identity.cycle_identity)
    try:
        parsed_cycle_time = datetime.strptime(identity.cycle_time, "%Y-%m-%dT%H:00:00Z")
    except ValueError as error:
        raise ArchiveConfigurationError(
            "archive identity cycle_time must be a valid canonical UTC hourly timestamp (YYYY-MM-DDTHH:00:00Z)"
        ) from error
    canonical_cycle_time = parsed_cycle_time.strftime("%Y-%m-%dT%H:00:00Z")
    if identity.cycle_time != canonical_cycle_time:
        raise ArchiveConfigurationError(
            "archive identity cycle_time must use canonical UTC hourly form: "
            f"declared={identity.cycle_time}; canonical={canonical_cycle_time}"
        )
    expected_cycle_identity = parsed_cycle_time.strftime("%Y%m%d%H")
    if identity.cycle_identity != expected_cycle_identity:
        raise ArchiveConfigurationError(
            "archive identity cycle_time does not match cycle_identity: "
            f"cycle_time={identity.cycle_time}; cycle_identity={identity.cycle_identity}; "
            f"expected={expected_cycle_identity}"
        )
    fields = {
        "basin_version_id": identity.basin_version_id,
        "model_id": identity.model_id,
        "run_id": identity.run_id,
    }
    required = {
        "forcing": {"basin_version_id", "model_id"},
        "runs": {"run_id"},
        "states": {"model_id"},
    }[identity.lane]
    supplied = {name for name, value in fields.items() if value is not None}
    missing = sorted(required - supplied)
    invalid = sorted(supplied - required)
    if missing:
        raise ArchiveConfigurationError(f"archive identity for {identity.lane} is missing fields: {missing}")
    if invalid:
        raise ArchiveConfigurationError(f"archive identity contains fields invalid for {identity.lane}: {invalid}")
    for name in sorted(required):
        value = fields[name]
        if value is None:
            raise AssertionError("required archive identity field unexpectedly absent")
        _validate_identity_component(value)


def _archive_identity_path_components(identity: ArchiveIdentity) -> tuple[str, ...]:
    source_segment = identity.source.lower()
    base = (identity.lane, source_segment, identity.cycle_identity)
    if identity.lane == "forcing":
        assert identity.basin_version_id is not None and identity.model_id is not None
        return (*base, identity.basin_version_id, identity.model_id)
    if identity.lane == "runs":
        assert identity.run_id is not None
        return (*base, identity.run_id)
    assert identity.model_id is not None
    return (*base, identity.model_id)


def _required_mapping_string(value: Mapping[str, object], field: str, *, label: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ArchiveConfigurationError(f"{label} field {field} must be a non-empty string")
    return raw


def _optional_mapping_string(value: Mapping[str, object], field: str, *, label: str) -> str | None:
    if field not in value:
        return None
    return _required_mapping_string(value, field, label=label)


def _validate_root_relative_path(value: str, *, label: str) -> None:
    components = value.split("/")
    if (
        value.startswith("/")
        or (len(value) >= 3 and value[0].isalpha() and value[1:3] == ":/")
        or any(component in {"", ".", ".."} for component in components)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ArchiveConfigurationError(f"{label} must be a safe root-relative path: {value!r}")


def validate_object_path(path: str) -> ObjectPathValidation:
    """Validate an S3 object key or URI against the NHMS storage layout."""
    normalized_path = _normalize_object_path(path)
    if not normalized_path:
        return _invalid("Object path is empty.")

    parts = normalized_path.split("/")
    if any(part == "" for part in parts):
        return _invalid("Object path contains an empty path segment.")

    for pattern in VALID_PREFIX_PATTERNS:
        components = _match_pattern(parts, pattern)
        if components is not None:
            return ObjectPathValidation(
                valid=True,
                category=pattern.category,
                components=components,
                error=None,
            )

    return _invalid("Unrecognized object path prefix.")


def _normalize_object_path(path: str) -> str:
    candidate = path.strip()
    if candidate.startswith("s3://"):
        parsed = urlparse(candidate)
        candidate = parsed.path
    return candidate.strip("/")


def _match_pattern(parts: list[str], pattern: ObjectPrefixPattern) -> dict[str, str] | None:
    if len(parts) <= len(pattern.segments):
        return None

    components: dict[str, str] = {}
    captured_literals = MappingProxyType(pattern.captured_literals)
    for index, expected_segment in enumerate(pattern.segments):
        actual_segment = parts[index]
        if _is_variable_segment(expected_segment):
            components[expected_segment[1:-1]] = actual_segment
            continue

        if actual_segment != expected_segment:
            return None

        if index in captured_literals:
            components[captured_literals[index]] = actual_segment

    return components


def _is_variable_segment(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _invalid(message: str) -> ObjectPathValidation:
    return ObjectPathValidation(
        valid=False,
        category=None,
        components={},
        error=f"{message} Valid prefixes: {VALID_PREFIX_MESSAGE}",
    )
