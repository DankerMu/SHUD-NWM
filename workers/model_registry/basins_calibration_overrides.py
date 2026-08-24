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
* **Refuse, never skip — keyed on *published but not applied*.** If a basin is
  being published and its declared override did not land, the publish fails:
  otherwise the package carries the ORIGINAL value while the declaration claims
  otherwise, which is worse than having no declaration.  A declared basin that
  the current run does not publish is NOT a refusal — it publishes nothing, so
  it can tell no lie, and keying the refusal there would kill every narrowed
  publish (`--basin-slug`, any tree that legitimately lacks the basin) and push
  operators toward not loading the declaration at all.

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

    Raises on an unparseable declared value, an unknown parameter, or an entry
    that matched nothing.
    """

    root = Path(isolated_root).expanduser()
    basin_dir = root / basin_slug
    if not overrides:
        return []
    # Value parsing is checked here, not at load: this basin is being published,
    # so an entry that cannot be applied would otherwise publish the original
    # value under a declaration that claims otherwise.
    for override in overrides:
        _require_parsed_value(override)
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
    # The declared value is NOT number-checked here.  Load happens before the
    # publish set is known, and an unparseable value is only a refusal for a
    # basin this run actually publishes (spec: "published but not applied").
    # `_require_parsed_value` runs at application time instead.
    value = _declared_value_text(item["value"])
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


def _declared_value_text(raw: Any) -> str:
    """The declared scalar, verbatim, as it will be written into the file.

    Deliberately lenient: anything that is not a plain scalar is kept as its
    ``repr`` so it survives to ``_require_parsed_value`` and is reported as an
    unparseable value against the basin that is actually being published.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return repr(raw)
    return str(raw).strip()


def _require_parsed_value(override: CalibrationOverride) -> None:
    """Refuse an unparseable declared value, at apply time.

    Reached only for a basin that IS being published: a package that carried
    the original value under a declaration claiming a different one is the lie
    this refusal exists to prevent.
    """
    try:
        parsed = float(override.value)
    except (TypeError, ValueError) as error:
        raise _unparseable_value(override) from error
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise _unparseable_value(override)


def _unparseable_value(override: CalibrationOverride) -> CalibrationOverrideError:
    return CalibrationOverrideError(
        "CALIBRATION_OVERRIDE_VALUE_UNPARSEABLE",
        (
            f"Declared calibration override '{override.entry_label}' declares a value that cannot be "
            f"parsed as a number: {override.value!r}."
        ),
        details={
            "entry": override.as_entry(),
            "basin_slug": override.basin_slug,
            "parameter": override.parameter,
            "declared_value": repr(override.value),
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
