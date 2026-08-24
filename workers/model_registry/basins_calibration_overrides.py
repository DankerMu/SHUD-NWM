"""Declared calibration overrides (#1832).

#1816 deleted ``basins_soil_alpha_repair``, which scanned every basin and
silently clamped calibration parameters against two hard-coded bounds, without
recording anything that travelled with the package.  Two of its three findings
hold: the rewrite was unrecorded, and it fired everywhere it scanned.

The third does not.  ``GEOL_DMAC = 5`` on ``hetianhe`` makes SHUD produce
``ERROR: NAN error for QeleSub[i][j] 5`` and ``EXIT 10``; it is an empirical
stability bound with no SHUD counterpart, so no repository grep could have
found it.  This module brings back the ability to change a calibration value,
but as an explicit, declared, recorded exception:

* **Named, not scanned** — an override applies to exactly one
  ``(basin_slug, parameter)`` pair that a checked-in declaration names.
* **Declared value, not derived value** — the declaration states the number;
  nobody has to re-derive it to know what a package will carry.
* **Refuse, never skip** — an entry that cannot be applied fails the publish.
  A silently unapplied entry would publish the ORIGINAL value while the
  declaration claims otherwise, which is worse than having no declaration.

Application itself happens on an isolated staging copy owned by the caller;
this module never resolves the Basins source tree.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

CALIBRATION_OVERRIDE_SCHEMA_VERSION = "basins.calibration_override.v1"
DECLARATION_SCHEMA_KEY = "calibration_overrides"
DEFAULT_CALIBRATION_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "config" / "calibration_overrides.yaml"

_REQUIRED_ENTRY_FIELDS = ("basin_slug", "parameter", "value", "reason", "approver", "date")
_PARAMETER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class CalibrationOverrideError(RuntimeError):
    """Raised when a declared calibration override cannot be loaded or applied."""

    def __init__(self, error_code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "message": str(self), **self.details}


@dataclass(frozen=True)
class CalibrationOverride:
    """One declared ``(basin, parameter) -> value`` exception."""

    basin_slug: str
    parameter: str
    value: str
    reason: str
    approver: str
    date: str

    @property
    def entry_label(self) -> str:
        return f"{self.basin_slug}:{self.parameter}"

    def as_entry(self) -> dict[str, str]:
        return {
            "basin_slug": self.basin_slug,
            "parameter": self.parameter,
            "value": self.value,
            "reason": self.reason,
            "approver": self.approver,
            "date": self.date,
        }


def load_calibration_overrides(path: str | Path) -> tuple[CalibrationOverride, ...]:
    """Parse the checked-in declaration.

    Refuses an unparseable value here, before any basin tree is touched, so the
    operator sees the offending entry rather than a half-published run.
    """

    declaration_path = Path(path).expanduser()
    try:
        raw_text = declaration_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_UNREADABLE",
            f"Calibration override declaration is unreadable: {error}",
            details={"declaration_path": str(declaration_path)},
        ) from error
    try:
        payload = yaml.safe_load(raw_text)
    except yaml.YAMLError as error:
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override declaration is not valid YAML: {error}",
            details={"declaration_path": str(declaration_path)},
        ) from error
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            "Calibration override declaration must be a mapping.",
            details={"declaration_path": str(declaration_path)},
        )
    entries = payload.get(DECLARATION_SCHEMA_KEY)
    if entries is None:
        entries = []
    if not isinstance(entries, Sequence) or isinstance(entries, str | bytes | bytearray):
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override declaration key '{DECLARATION_SCHEMA_KEY}' must be a list.",
            details={"declaration_path": str(declaration_path)},
        )

    overrides: list[CalibrationOverride] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(entries):
        overrides.append(_override_from_entry(item, index=index, declaration_path=declaration_path, seen=seen))
    return tuple(overrides)


def overrides_for_basin(
    overrides: Iterable[CalibrationOverride],
    basin_slug: str,
) -> tuple[CalibrationOverride, ...]:
    return tuple(override for override in overrides if override.basin_slug == basin_slug)


def declared_basin_slugs(overrides: Iterable[CalibrationOverride]) -> tuple[str, ...]:
    return tuple(sorted({override.basin_slug for override in overrides}))


def apply_calibration_overrides_for_basin(
    *,
    isolated_root: str | Path,
    basin_slug: str,
    overrides: Sequence[CalibrationOverride],
    write_bytes: Callable[[Path, bytes], None] | None = None,
) -> list[dict[str, Any]]:
    """Apply declared overrides inside a private basin copy.

    ``isolated_root`` MUST be a staging copy: this function writes to
    ``isolated_root / basin_slug`` and refuses to escape it.  Returns the
    manifest-shaped record of what was applied, sorted deterministically.

    Raises on an unknown parameter or on an entry that matched nothing.
    """

    root = Path(isolated_root).expanduser()
    basin_dir = root / basin_slug
    if not overrides:
        return []
    input_parent = basin_dir / "input"
    calib_files = (
        sorted(
            path
            for input_dir in sorted(item for item in input_parent.iterdir() if item.is_dir())
            for path in input_dir.glob("*.cfg.calib")
            if path.is_file() and not path.is_symlink()
        )
        if input_parent.is_dir()
        else []
    )

    applied: list[dict[str, Any]] = []
    applied_parameters: set[str] = set()
    for calib_path in calib_files:
        _require_under_root(calib_path, basin_dir)
        original_text = calib_path.read_text(encoding="utf-8")
        text = original_text
        file_records: list[dict[str, Any]] = []
        for override in overrides:
            text, source_value = _replace_parameter(text, override)
            if source_value is None:
                raise CalibrationOverrideError(
                    "CALIBRATION_OVERRIDE_UNKNOWN_PARAMETER",
                    (
                        f"Declared calibration override '{override.entry_label}' names a parameter that "
                        f"'{calib_path.name}' does not contain."
                    ),
                    details={
                        "entry": override.as_entry(),
                        "basin_slug": basin_slug,
                        "parameter": override.parameter,
                        "calibration_file": calib_path.relative_to(basin_dir).as_posix(),
                    },
                )
            file_records.append(
                {
                    "basin_slug": basin_slug,
                    "parameter": override.parameter,
                    "value": override.value,
                    "source_value": source_value,
                    "reason": override.reason,
                    "approver": override.approver,
                    "date": override.date,
                    "relative_path": calib_path.relative_to(basin_dir).as_posix(),
                }
            )
            applied_parameters.add(override.parameter)
        content = text.encode("utf-8")
        if write_bytes is None:
            calib_path.write_bytes(content)
        else:
            write_bytes(calib_path, content)
        digest = hashlib.sha256(content).hexdigest()
        for record in file_records:
            record["sha256"] = digest
        applied.extend(file_records)

    unmatched = [override for override in overrides if override.parameter not in applied_parameters]
    if unmatched:
        override = unmatched[0]
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_MATCHED_NOTHING",
            (
                f"Declared calibration override '{override.entry_label}' matched no calibration file; "
                "refusing rather than publishing the original value under a declaration that claims otherwise."
            ),
            details={
                "entry": override.as_entry(),
                "basin_slug": basin_slug,
                "parameter": override.parameter,
                "calibration_file_count": len(calib_files),
            },
        )
    return sorted(applied, key=lambda item: (item["relative_path"], item["parameter"]))


def _override_from_entry(
    item: Any,
    *,
    index: int,
    declaration_path: Path,
    seen: set[tuple[str, str]],
) -> CalibrationOverride:
    location = {"declaration_path": str(declaration_path), "entry_index": index}
    if not isinstance(item, Mapping):
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override entry #{index} must be a mapping.",
            details=location,
        )
    missing = [field for field in _REQUIRED_ENTRY_FIELDS if item.get(field) in (None, "")]
    if missing:
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override entry #{index} is missing required field(s): {', '.join(missing)}.",
            details={**location, "missing_fields": missing, "entry": _stringify_entry(item)},
        )
    basin_slug = str(item["basin_slug"]).strip()
    parameter = str(item["parameter"]).strip()
    if not _PARAMETER_RE.match(parameter):
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override entry #{index} declares a malformed parameter name: {parameter!r}.",
            details={**location, "entry": _stringify_entry(item)},
        )
    value = _parsed_value(item["value"], index=index, declaration_path=declaration_path, entry=item)
    key = (basin_slug, parameter)
    if key in seen:
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_DECLARATION_INVALID",
            f"Calibration override declares '{basin_slug}:{parameter}' more than once.",
            details={**location, "entry": _stringify_entry(item)},
        )
    seen.add(key)
    return CalibrationOverride(
        basin_slug=basin_slug,
        parameter=parameter,
        value=value,
        reason=str(item["reason"]).strip(),
        approver=str(item["approver"]).strip(),
        date=str(item["date"]).strip(),
    )


def _parsed_value(raw: Any, *, index: int, declaration_path: Path, entry: Mapping[str, Any]) -> str:
    # bool is an int subclass; a YAML `true` is never a calibration value.
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise _unparseable_value(raw, index=index, declaration_path=declaration_path, entry=entry)
    text = str(raw).strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError) as error:
        raise _unparseable_value(raw, index=index, declaration_path=declaration_path, entry=entry) from error
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise _unparseable_value(raw, index=index, declaration_path=declaration_path, entry=entry)
    return text


def _unparseable_value(
    raw: Any,
    *,
    index: int,
    declaration_path: Path,
    entry: Mapping[str, Any],
) -> CalibrationOverrideError:
    label = f"{entry.get('basin_slug')}:{entry.get('parameter')}"
    return CalibrationOverrideError(
        "CALIBRATION_OVERRIDE_VALUE_UNPARSEABLE",
        f"Calibration override '{label}' declares a value that cannot be parsed as a number: {raw!r}.",
        details={
            "declaration_path": str(declaration_path),
            "entry_index": index,
            "entry": _stringify_entry(entry),
            "declared_value": repr(raw),
        },
    )


def _stringify_entry(entry: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in entry.items()}


def _replace_parameter(text: str, override: CalibrationOverride) -> tuple[str, str | None]:
    """Rewrite one ``NAME<TAB>VALUE`` line, preserving every other byte."""

    pattern = re.compile(
        rf"^(?P<prefix>[ \t]*{re.escape(override.parameter)}[ \t]+)(?P<value>\S+)(?P<suffix>.*)$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match is None:
        return text, None
    source_value = match.group("value")
    replacement = f"{match.group('prefix')}{override.value}{match.group('suffix')}"
    return text[: match.start()] + replacement + text[match.end() :], source_value


def _require_under_root(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise CalibrationOverrideError(
            "CALIBRATION_OVERRIDE_PATH_ESCAPE",
            "Calibration override target escapes the isolated staging root.",
            details={"path": str(path), "isolated_root": str(root)},
        )
