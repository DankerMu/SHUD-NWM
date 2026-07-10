"""§1.3 z_policy verdict resolution + ZPolicy provenance binding + per-cell sampler.

This module implements OpenSpec change ``direct-grid-build-enablement``
capability ``z-policy-verdict`` (Epic #973 SUB-1). It is the mapping
builder's single ``z_policy`` input authority: it resolves the committed
verdict evidence file against pinned code constants (expected verdict
value + SHA-256), binds the verified checksum into the existing
:class:`workers.mapping_builder.binding.ZPolicy` provenance slot, and
implements the pinned per-cell sampler ``nearest_mesh_node_elevation_v1``
for the ``model_dem_at_cell_center`` verdict.

Boundary
--------
This module is landed under §1.3 so the §2 CLI can consume it without
touching any existing library stage. It IMPORTS
:class:`workers.mapping_builder.binding.ZPolicy` and
:class:`workers.mapping_builder.binding.ZPolicyCellMissingError` but
never modifies :mod:`workers.mapping_builder.binding`; the naming debt
around ``readiness_manifest_checksum`` is intentionally NOT resolved
here (deferred per tasks.md §1.3 non-goal).

Pinned authority
----------------
* :data:`EXPECTED_VERDICT_VALUE` — the audited solver verdict
  (``model_dem_at_cell_center``). Any other recorded value in the
  evidence file fails closed.
* :data:`EXPECTED_VERDICT_FILE_SHA256` — the committed evidence file's
  SHA-256. Any drift (accidental edit, substituted file via
  ``--z-policy-verdict-path``) fails closed. The checksum, not the path,
  is the authority anchor per the ``z-policy-verdict`` spec.
* :data:`SAMPLER_RULE_ID` — the pinned per-cell sampler identifier
  recorded in the evidence package; changing the sampler mechanism
  requires a new identifier + re-audit.

Public API
----------
* :func:`resolve_verdict` — locate the verdict file (default active-change
  path, then archive glob, or explicit override), verify SHA-256 against
  the pin, verify the recorded verdict value against the pin, and return
  a frozen :class:`VerdictResolution` record carrying the resolved path,
  override flag, verified SHA-256, and sampler rule identifier.
* :func:`build_z_policy` — construct a :class:`workers.mapping_builder.binding.ZPolicy`
  binding the verified checksum through the existing
  ``readiness_manifest_checksum`` provenance slot. Per-cell coverage is
  filled by the caller from :func:`sample_per_cell_z` output.
* :func:`sample_per_cell_z` — the pinned ``nearest_mesh_node_elevation_v1``
  sampler: for each used cell transform the registered WGS84 center into
  the package CRS, take the ``Elevation`` of the nearest mesh node under
  planar Euclidean distance, break distance ties toward the smallest
  node ``ID``. Total over used cells including centers outside the mesh
  hull (never a numeric default, never a silent skip).

Error family
------------
:class:`VerdictResolutionError` — module-local root for verdict-file
resolution / verification failures. Distinct from the binding-family
errors so callers can tell "verdict file could not be verified" apart
from "binding failed a G5 gate".
:class:`ZPolicyCellMissingError` is re-exported from
:mod:`workers.mapping_builder.binding` for consumer convenience.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

import pyproj

from workers.mapping_builder.binding import (
    ZPolicy,
    ZPolicyCellMissingError,
)

# --- pinned constants -----------------------------------------------------

#: Pinned verdict value. The narrow three-question solver audit (Epic
#: #973 SUB-1) concluded that the SHUD solver DOES consume station ``Z``
#: numerically (temperature lapse correction, ``MD_ET.cpp:32``), so
#: ``sentinel`` is rejected and an explicit elevation source is required.
#: ``model_dem_at_cell_center`` is the sole authorized verdict for the
#: direct-grid pilot; ``canonical_orography`` is deferred until the
#: registry stores per-cell orography. Any other value in the evidence
#: file fails closed.
EXPECTED_VERDICT_VALUE: str = "model_dem_at_cell_center"

#: Pinned SHA-256 of the committed verdict evidence file. Any drift
#: (accidental edit, substituted file via ``--z-policy-verdict-path``)
#: fails closed with no binding. The checksum, not the path, is the
#: authority anchor per the ``z-policy-verdict`` spec.
EXPECTED_VERDICT_FILE_SHA256: str = (
    "bdd34965337c76cc385d12588a277a7eea161a5c287f7c0a60a6ffe10daf756d"
)

#: Pinned per-cell sampler identifier recorded in the evidence package.
#: The sampler behavior (nearest mesh node under planar Euclidean
#: distance in the package CRS, tie -> smallest node ID) is fixed under
#: this ID; changing the sampler requires bumping this identifier and
#: re-auditing downstream evidence.
SAMPLER_RULE_ID: str = "nearest_mesh_node_elevation_v1"

#: Default active-change path for the committed verdict evidence file.
#: Kept as a **relative** path (documented API + backward compatibility
#: for any external callers that read the constant). Internally the
#: resolver joins it with :data:`_REPO_ROOT` before existence checks and
#: SHA computation, so ``resolve_verdict`` works from any invocation cwd
#: (see the ``_discover_repo_root`` walk below). The §2 CLI subprocess
#: relies on this cwd-independence — if a subprocess is launched with
#: ``cwd=/some/other/path`` the resolver must still find the evidence
#: file from the module's own filesystem location, not from the cwd.
DEFAULT_VERDICT_PATH: pathlib.Path = pathlib.Path(
    "openspec/changes/direct-grid-build-enablement/evidence/"
    "z-policy-solver-audit-verdict.md"
)

#: Post-archive glob for the same evidence file after OpenSpec archives
#: the change directory. Any date-stamped archive directory containing
#: the evidence file matches; the pinned checksum still gates authority.
ARCHIVE_VERDICT_GLOB: str = (
    "openspec/changes/archive/*-direct-grid-build-enablement/evidence/"
    "z-policy-solver-audit-verdict.md"
)

#: Regex for the recorded ``verdict = <value>`` line inside the evidence
#: file. Matches the literal line
#: ``verdict = model_dem_at_cell_center`` (with tolerant whitespace)
#: inside a fenced code block at the "## Verdict" section, per the
#: committed evidence file structure.
_VERDICT_LINE_RE: re.Pattern[str] = re.compile(
    r"^\s*verdict\s*=\s*(\S+)\s*$", re.MULTILINE
)


def _discover_repo_root() -> pathlib.Path:
    """Walk up from this module's file until a directory containing both
    ``openspec/`` and ``.git/`` is found; fall back to :func:`pathlib.Path.cwd`
    if the walk exhausts (unusual — e.g. the module was extracted from
    its source tree).

    Both markers must be present so we don't accidentally lock onto a
    parent directory that happens to have an ``openspec/`` folder but is
    not the NWM repo root. The fall-back to ``Path.cwd`` preserves the
    prior cwd-relative behavior in edge cases (imported from a wheel,
    tested in isolation) and never crashes at import time.
    """
    current = pathlib.Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / "openspec").is_dir() and (parent / ".git").exists():
            return parent
    return pathlib.Path.cwd()


#: Repo root discovered at import time — used to anchor
#: :data:`DEFAULT_VERDICT_PATH` and :data:`ARCHIVE_VERDICT_GLOB` so the
#: resolver is cwd-independent (see the constants' docstrings for the
#: §2 CLI motivation).
_REPO_ROOT: pathlib.Path = _discover_repo_root()


# --- exception ------------------------------------------------------------


class VerdictResolutionError(RuntimeError):
    """The verdict evidence file could not be resolved or verified.

    Raised when the resolver cannot locate the evidence file at the
    default active-change path, its archive relocation, or an explicit
    override; when the file's SHA-256 does not match the pinned constant;
    or when the file's recorded verdict value does not match the pinned
    expected value. Distinct from
    :class:`workers.mapping_builder.binding.ZPolicyCellMissingError` and
    the ``BindingArtifactError`` family so callers can tell "verdict file
    could not be verified" apart from "binding failed a G5 gate".
    """


# --- structured records ---------------------------------------------------


@dataclass(frozen=True)
class VerdictResolution:
    """Result of a successful verdict-file resolution + verification.

    Attributes
    ----------
    resolved_path:
        Filesystem path the evidence file was actually loaded from
        (default active-change path, archive relocation, or explicit
        override). Recorded so the evidence package can trace which
        physical file backed a given build.
    override_used:
        ``True`` iff the caller supplied an explicit override path;
        ``False`` iff the resolver fell back to the default active-change
        or archive path.
    override_path:
        The exact override path the caller supplied (``None`` when
        ``override_used`` is ``False``). Recorded verbatim into the
        evidence package per the ``z-policy-verdict`` spec's
        "path override is explicit and evidence-recorded" scenario.
    verified_sha256:
        The SHA-256 hex of the resolved file's bytes. Equal to
        :data:`EXPECTED_VERDICT_FILE_SHA256` by construction — the
        resolver raises before returning if the hashes differ. This is
        the value bound into
        :attr:`workers.mapping_builder.binding.ZPolicy.readiness_manifest_checksum`
        by :func:`build_z_policy`.
    sampler_rule_id:
        Pinned :data:`SAMPLER_RULE_ID`. Carried on the resolution so the
        evidence bundler records which sampler produced the per-cell z
        map alongside the verdict provenance.
    """

    resolved_path: pathlib.Path
    override_used: bool
    override_path: pathlib.Path | None
    verified_sha256: str
    sampler_rule_id: str


@dataclass(frozen=True)
class UsedCell:
    """One used-cell record consumed by :func:`sample_per_cell_z`.

    Attributes
    ----------
    cell_id:
        The registered snapshot ``grid_cell_id`` (matches the ``grid_cell_id``
        emitted on the binding row).
    wgs84_lon, wgs84_lat:
        The registered WGS84 cell center. The sampler transforms this
        pair through the package CRS before nearest-node search.
    """

    cell_id: str
    wgs84_lon: float
    wgs84_lat: float


@dataclass(frozen=True)
class MeshNode:
    """One mesh node parsed from the ``.sp.mesh`` node table.

    Attributes
    ----------
    node_id:
        Node ``ID`` column (1-based per SHUD mesh convention). Used to
        break distance ties deterministically toward the smallest ID.
    x, y:
        Node coordinates in the PACKAGE CRS (Albers meters for the
        keliya baseline). No transform is applied inside
        :func:`sample_per_cell_z` — the caller supplies nodes already in
        package coordinates because the ``.sp.mesh`` node table is
        authored in package coordinates.
    elevation:
        The ``Elevation`` column of the node table — the same value the
        solver uses to derive element ``z_surf``. Copied verbatim into
        ``per_cell_z``.
    """

    node_id: int
    x: float
    y: float
    elevation: float


@dataclass(frozen=True)
class PackageProjection:
    """WGS84 -> package-CRS forward transformer wrapper.

    Wraps a :class:`pyproj.Transformer` built from the package's
    checksum-bound ``gis/*.prj`` so the caller can build the transformer
    once and hand it into :func:`sample_per_cell_z` for every used cell.
    The wrapping keeps the sampler's signature free of ``pyproj`` types
    and lets tests substitute a trivial identity transformer.
    """

    transformer: pyproj.Transformer

    @classmethod
    def from_prj_wkt(cls, wkt: str) -> PackageProjection:
        """Build a projection from a package ``.prj`` WKT string.

        The transformer produces ``(x, y)`` in the package CRS from
        ``(lon, lat)`` in WGS84 (EPSG:4326); ``always_xy=True`` matches
        the convention used by
        :func:`workers.mapping_builder.binding.verify_x_y_recomputable`
        so the two derivations share the same axis order.
        """
        model_crs = pyproj.CRS.from_wkt(wkt)
        transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", model_crs, always_xy=True
        )
        return cls(transformer=transformer)

    def to_package_xy(self, longitude: float, latitude: float) -> tuple[float, float]:
        """Transform ``(longitude, latitude)`` WGS84 -> package CRS ``(x, y)``."""
        x, y = self.transformer.transform(float(longitude), float(latitude))
        return float(x), float(y)


# --- internal helpers -----------------------------------------------------


def _sha256_file(path: pathlib.Path) -> str:
    """Return the SHA-256 hex digest of the file's byte contents."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_verdict_value(text: str) -> str:
    """Return the recorded verdict value; raise if missing or mismatched.

    Exposed at module scope (not underscored-only) so the negative-path
    test can exercise the value-check independently of the checksum
    invariant. In production the resolver runs the checksum check first;
    the value check is defense in depth so a doctored file that somehow
    matched the pinned hash but recorded a different verdict value would
    still fail closed.
    """
    match = _VERDICT_LINE_RE.search(text)
    if match is None:
        raise VerdictResolutionError(
            "verdict evidence file does not contain a "
            "`verdict = <value>` line (expected literal per "
            "z-policy-solver-audit-verdict.md structure)"
        )
    value = match.group(1)
    if value != EXPECTED_VERDICT_VALUE:
        raise VerdictResolutionError(
            f"verdict value mismatch: expected {EXPECTED_VERDICT_VALUE!r}, "
            f"got {value!r}"
        )
    return value


def _resolve_default_verdict_path() -> pathlib.Path:
    """Return the default verdict path (active change) or the archive fallback.

    Resolution order (paths are anchored on :data:`_REPO_ROOT`, discovered
    at import time — the resolver works from any invocation cwd):

    1. :data:`DEFAULT_VERDICT_PATH` joined with :data:`_REPO_ROOT`
       (active-change location) if the file exists.
    2. :data:`ARCHIVE_VERDICT_GLOB` matched against :data:`_REPO_ROOT`;
       returns the lexically-largest match so the most recent archive
       relocation wins.

    Raises
    ------
    VerdictResolutionError
        Neither the active-change path nor any archive-glob match
        exists on disk.
    """
    default_absolute = _REPO_ROOT / DEFAULT_VERDICT_PATH
    if default_absolute.exists():
        return default_absolute
    matches = sorted(_REPO_ROOT.glob(ARCHIVE_VERDICT_GLOB))
    if matches:
        return matches[-1]
    raise VerdictResolutionError(
        f"verdict evidence file not found at {default_absolute!s} "
        f"nor via archive glob {ARCHIVE_VERDICT_GLOB!r} "
        f"under repo root {_REPO_ROOT!s}"
    )


# --- public API -----------------------------------------------------------


def resolve_verdict(explicit_path: pathlib.Path | None = None) -> VerdictResolution:
    """Resolve and verify the verdict evidence file against the pinned authority.

    Resolution order:

    1. ``explicit_path`` if supplied (override channel, recorded on the
       returned :class:`VerdictResolution` for evidence).
    2. :data:`DEFAULT_VERDICT_PATH` (active-change location).
    3. :data:`ARCHIVE_VERDICT_GLOB` (post-archive relocation).

    Verification (both invariants must pass — either failure raises with
    no binding output):

    * The resolved file's SHA-256 equals :data:`EXPECTED_VERDICT_FILE_SHA256`.
    * The resolved file's recorded ``verdict = ...`` line equals
      :data:`EXPECTED_VERDICT_VALUE`.

    Parameters
    ----------
    explicit_path:
        Optional override path (typically supplied via CLI
        ``--z-policy-verdict-path``). When ``None`` the default-path /
        archive-glob resolver is used.

    Returns
    -------
    VerdictResolution
        Frozen record carrying the resolved path, override flag +
        override path, verified SHA-256, and pinned sampler rule ID.

    Raises
    ------
    VerdictResolutionError
        The file cannot be located, is not a regular file, its SHA-256
        does not match the pin, or the recorded verdict value does not
        match the pin.
    """
    if explicit_path is not None:
        resolved = pathlib.Path(explicit_path)
        override_used = True
        override_path: pathlib.Path | None = pathlib.Path(explicit_path)
    else:
        resolved = _resolve_default_verdict_path()
        override_used = False
        override_path = None

    if not resolved.exists():
        raise VerdictResolutionError(
            f"verdict evidence file not found at {resolved!s}"
        )
    if not resolved.is_file():
        raise VerdictResolutionError(
            f"verdict evidence path {resolved!s} is not a regular file"
        )

    actual_sha = _sha256_file(resolved)
    if actual_sha != EXPECTED_VERDICT_FILE_SHA256:
        raise VerdictResolutionError(
            f"verdict file checksum mismatch: expected "
            f"{EXPECTED_VERDICT_FILE_SHA256!r}, got {actual_sha!r} "
            f"for path {resolved!s}"
        )

    text = resolved.read_text(encoding="utf-8")
    _verify_verdict_value(text)

    return VerdictResolution(
        resolved_path=resolved,
        override_used=override_used,
        override_path=override_path,
        verified_sha256=actual_sha,
        sampler_rule_id=SAMPLER_RULE_ID,
    )


def build_z_policy(resolution: VerdictResolution) -> ZPolicy:
    """Construct a :class:`ZPolicy` binding the verified checksum through provenance.

    The returned policy carries :data:`EXPECTED_VERDICT_VALUE` as
    ``policy_name`` and ``resolution.verified_sha256`` as
    ``readiness_manifest_checksum`` (the existing provenance slot on
    :class:`workers.mapping_builder.binding.ZPolicy` — the field name
    remains ``readiness_manifest_checksum`` per tasks.md §1.3 non-goal
    on the naming debt). Per-cell coverage is filled by a subsequent
    call to :func:`sample_per_cell_z`; this constructor returns the
    provenance skeleton with an empty ``per_cell_z``.

    A blank ``verified_sha256`` on the supplied resolution fails closed
    via :class:`workers.mapping_builder.binding.ReadinessManifestChecksumMissingError`
    raised inside :class:`ZPolicy.__post_init__` — the constructor never
    silently accepts unauthored provenance.

    Parameters
    ----------
    resolution:
        A :class:`VerdictResolution` from :func:`resolve_verdict`
        (or a caller-constructed record for negative-path testing).

    Returns
    -------
    ZPolicy
        Provenance-bound policy with an empty ``per_cell_z`` map.

    Raises
    ------
    workers.mapping_builder.binding.ReadinessManifestChecksumMissingError
        ``resolution.verified_sha256`` is blank or whitespace-only.
    workers.mapping_builder.binding.InvalidZPolicyError
        The pinned :data:`EXPECTED_VERDICT_VALUE` is not in
        :data:`workers.mapping_builder.binding.ALLOWED_Z_POLICIES` (a
        defensive check — the constant is authored to satisfy the
        allowlist, and any drift there is a build-configuration bug).
    """
    return ZPolicy(
        policy_name=EXPECTED_VERDICT_VALUE,
        readiness_manifest_checksum=resolution.verified_sha256,
    )


def sample_per_cell_z(
    used_cells: Sequence[UsedCell],
    mesh_nodes: Sequence[MeshNode],
    projection: PackageProjection,
) -> dict[str, float]:
    """Derive ``per_cell_z`` under the pinned ``nearest_mesh_node_elevation_v1`` sampler.

    For each used cell:

    1. Transform the registered WGS84 center ``(wgs84_lon, wgs84_lat)``
       into package-CRS ``(x, y)`` via :meth:`PackageProjection.to_package_xy`.
    2. Find the mesh node minimizing planar Euclidean distance to the
       transformed cell center; break distance ties toward the smallest
       ``node_id``.
    3. Emit the winning node's :attr:`MeshNode.elevation` verbatim.

    The sampler is TOTAL over ``used_cells`` — including cells whose
    registered centers lie outside the mesh hull (routine for boundary
    cells under nearest-cell ownership, guaranteed for small basins
    such as the keliya fixture). Nearest-node sampling requires no
    containment test, so an outside-hull center still resolves
    deterministically to the closest boundary node; never a numeric
    default, never a silent skip.

    Parameters
    ----------
    used_cells:
        Registered used cells (from the ownership stage). Coordinates
        are WGS84.
    mesh_nodes:
        Parsed ``.sp.mesh`` node rows in PACKAGE-CRS coordinates. Must
        be non-empty when ``used_cells`` is non-empty; an empty mesh
        with used cells raises :class:`ZPolicyCellMissingError` (the
        caller MUST NOT substitute a numeric default).
    projection:
        WGS84 -> package-CRS transformer built from the package's
        checksum-bound ``gis/*.prj``.

    Returns
    -------
    dict[str, float]
        Mapping of ``cell_id`` -> nearest-node elevation for every cell
        in ``used_cells``.

    Raises
    ------
    ZPolicyCellMissingError
        A used cell has no candidate mesh node (empty ``mesh_nodes``
        supplied). Reused from
        :mod:`workers.mapping_builder.binding` so downstream callers can
        catch a single missing-cell error family.
    """
    result: dict[str, float] = {}
    for cell in used_cells:
        if not mesh_nodes:
            raise ZPolicyCellMissingError(
                grid_cell_id=cell.cell_id,
                policy_name=EXPECTED_VERDICT_VALUE,
            )
        cx, cy = projection.to_package_xy(cell.wgs84_lon, cell.wgs84_lat)
        # Ties on squared distance break to smallest node_id — sorting
        # by (distance_sq, node_id) makes min() deterministic without a
        # second pass.
        best = min(
            mesh_nodes,
            key=lambda node, cx=cx, cy=cy: (
                (float(node.x) - cx) ** 2 + (float(node.y) - cy) ** 2,
                int(node.node_id),
            ),
        )
        result[cell.cell_id] = float(best.elevation)
    return result


__all__ = [
    "ARCHIVE_VERDICT_GLOB",
    "DEFAULT_VERDICT_PATH",
    "EXPECTED_VERDICT_FILE_SHA256",
    "EXPECTED_VERDICT_VALUE",
    "MeshNode",
    "PackageProjection",
    "SAMPLER_RULE_ID",
    "UsedCell",
    "VerdictResolution",
    "VerdictResolutionError",
    "ZPolicyCellMissingError",
    "build_z_policy",
    "resolve_verdict",
    "sample_per_cell_z",
]
