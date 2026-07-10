"""Tests for :mod:`workers.mapping_builder.z_policy_verdict` (Epic #973 SUB-1 §1.3).

Coverage
--------

* Verdict-file resolution against the pinned SHA-256 authority:
  happy path against the committed evidence file, missing-file
  override, checksum-mismatch, wrong-recorded-value.
* ZPolicy provenance binding: verified checksum flows through the
  existing ``readiness_manifest_checksum`` slot; blank checksum fails
  closed via the existing binding-layer invariant.
* Sampler pin: ``SAMPLER_RULE_ID`` literal, and structural sampler
  correctness — numeric keliya oracle (inside + outside hull),
  deterministic distance-tie break to smallest ``node_id``, and
  empty-mesh -> :class:`ZPolicyCellMissingError`.
* Override plumbing: the returned :class:`VerdictResolution` records
  ``override_used`` + ``override_path`` verbatim.
"""

from __future__ import annotations

import hashlib
import pathlib

import pyproj
import pytest

from workers.mapping_builder.binding import (
    ReadinessManifestChecksumMissingError,
    ZPolicyCellMissingError,
)
from workers.mapping_builder.z_policy_verdict import (
    DEFAULT_VERDICT_PATH,
    EXPECTED_VERDICT_FILE_SHA256,
    EXPECTED_VERDICT_VALUE,
    SAMPLER_RULE_ID,
    MeshNode,
    PackageProjection,
    UsedCell,
    VerdictResolution,
    VerdictResolutionError,
    _verify_verdict_value,
    build_z_policy,
    resolve_verdict,
    sample_per_cell_z,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_KELIYA_MESH = (
    _REPO_ROOT / "tests" / "fixtures" / "mapping_builder" / "keliya" / "keliya.sp.mesh"
)
_KELIYA_PRJ = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "mapping_builder"
    / "keliya"
    / "gis"
    / "keliya.prj"
)


# --- helpers ---------------------------------------------------------------


def _parse_keliya_mesh_nodes() -> list[MeshNode]:
    """Parse the keliya ``.sp.mesh`` node table into :class:`MeshNode` records.

    Mirrors the file layout inspected during oracle authoring: the file
    contains a leading element block (``484 8`` header + 484 element
    rows), followed by a node block (``276 5`` header + one column-name
    row + 276 node rows ``ID X Y AqDepth Elevation``). This helper reads
    only the node block — element parsing is not needed here.
    """
    with open(_KELIYA_MESH) as handle:
        lines = handle.read().splitlines()
    # Element block occupies lines[0..485]: 1 header + 1 column row + 484 element rows.
    node_header_index = 1 + 1 + 484
    tokens = lines[node_header_index].split()
    n_nodes = int(tokens[0])
    node_data_start = node_header_index + 2  # skip node header + column names
    nodes: list[MeshNode] = []
    for row in lines[node_data_start : node_data_start + n_nodes]:
        parts = row.split()
        nodes.append(
            MeshNode(
                node_id=int(parts[0]),
                x=float(parts[1]),
                y=float(parts[2]),
                elevation=float(parts[4]),
            )
        )
    return nodes


class _IdentityProjection:
    """Duck-typed :class:`PackageProjection` returning (lon, lat) unchanged.

    Used by sampler tests that supply mesh node coordinates directly in
    the same axis system as the cell center — no pyproj transform
    round-trip needed to exercise the min-distance + tie-break logic.
    """

    def to_package_xy(self, longitude: float, latitude: float) -> tuple[float, float]:
        return float(longitude), float(latitude)


# --- verdict resolution ----------------------------------------------------


def test_resolve_verdict_pinned_default_path_matches_checksum(monkeypatch):
    """Happy path: default active-change file matches the pinned SHA-256 + value."""
    monkeypatch.chdir(_REPO_ROOT)

    resolution = resolve_verdict()

    assert resolution.verified_sha256 == EXPECTED_VERDICT_FILE_SHA256
    assert resolution.sampler_rule_id == SAMPLER_RULE_ID
    assert resolution.override_used is False
    assert resolution.override_path is None
    # Resolver anchors on the discovered repo root, not on cwd, so the
    # resolved path is absolute (repo root joined with the relative
    # DEFAULT_VERDICT_PATH constant).
    assert resolution.resolved_path == _REPO_ROOT / DEFAULT_VERDICT_PATH


def test_resolve_verdict_works_from_arbitrary_cwd(tmp_path, monkeypatch):
    """Regression lock: §2 CLI subprocess must be able to resolve the verdict
    from any working directory, not just the repo root.

    Before the path-anchoring fix, ``DEFAULT_VERDICT_PATH.exists()`` and
    ``pathlib.Path('.').glob(...)`` were cwd-relative — a subprocess
    launched with ``cwd=/some/other/path`` would fall through both
    resolution steps and raise ``VerdictResolutionError``. After the fix,
    the resolver anchors on the discovered repo root, so any cwd works.
    """
    monkeypatch.chdir(tmp_path)  # NOT the repo root

    resolution = resolve_verdict()

    assert resolution.verified_sha256 == EXPECTED_VERDICT_FILE_SHA256
    assert resolution.override_used is False
    assert resolution.override_path is None
    # Resolved path is absolute + points at the committed evidence file
    # under the discovered repo root.
    assert resolution.resolved_path.is_absolute()
    assert resolution.resolved_path.name == "z-policy-solver-audit-verdict.md"


def test_resolve_verdict_missing_file_raises(tmp_path):
    """Nonexistent override path fails closed."""
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(VerdictResolutionError) as exc_info:
        resolve_verdict(explicit_path=missing)

    assert "not found" in str(exc_info.value)


def test_resolve_verdict_checksum_mismatch_raises(tmp_path):
    """A file whose SHA-256 differs from the pin fails closed on the checksum gate."""
    doctored = tmp_path / "doctored-verdict.md"
    doctored.write_text(
        "# doctored verdict file\n\n```\nverdict = model_dem_at_cell_center\n```\n",
        encoding="utf-8",
    )
    doctored_sha = hashlib.sha256(doctored.read_bytes()).hexdigest()
    assert doctored_sha != EXPECTED_VERDICT_FILE_SHA256  # sanity precondition

    with pytest.raises(VerdictResolutionError) as exc_info:
        resolve_verdict(explicit_path=doctored)

    message = str(exc_info.value)
    assert "checksum mismatch" in message
    assert EXPECTED_VERDICT_FILE_SHA256 in message
    assert doctored_sha in message


def test_resolve_verdict_wrong_value_raises_via_helper():
    """A verdict text with the wrong value is refused by the value gate.

    Direct test of :func:`_verify_verdict_value` so the check is
    exercised independently of the checksum-first invariant (a wrong-
    value + right-checksum combination is not physically realizable
    without breaking SHA-256).
    """
    wrong_text = "## Verdict\n\n```\nverdict = sentinel\n```\n"

    with pytest.raises(VerdictResolutionError) as exc_info:
        _verify_verdict_value(wrong_text)

    message = str(exc_info.value)
    assert "verdict value mismatch" in message
    assert EXPECTED_VERDICT_VALUE in message
    assert "sentinel" in message


def test_resolve_verdict_wrong_value_missing_verdict_line_raises():
    """A verdict text with NO `verdict = ...` line at all is refused."""
    empty_text = "# heading\n\nno verdict line here\n"

    with pytest.raises(VerdictResolutionError) as exc_info:
        _verify_verdict_value(empty_text)

    assert "does not contain" in str(exc_info.value)


def test_verdict_resolution_record_includes_override_flag_and_path(tmp_path, monkeypatch):
    """Explicit-path override is recorded verbatim on the resolution record.

    Stages the committed evidence bytes at a temp path so the override
    passes the pinned-checksum gate; asserts ``override_used`` +
    ``override_path`` reflect the caller's choice.
    """
    monkeypatch.chdir(_REPO_ROOT)
    committed_bytes = DEFAULT_VERDICT_PATH.read_bytes()
    override_target = tmp_path / "custom-verdict.md"
    override_target.write_bytes(committed_bytes)

    resolution = resolve_verdict(explicit_path=override_target)

    assert resolution.override_used is True
    assert resolution.override_path == override_target
    assert resolution.resolved_path == override_target
    assert resolution.verified_sha256 == EXPECTED_VERDICT_FILE_SHA256


# --- ZPolicy provenance binding --------------------------------------------


def test_build_z_policy_from_verified_resolution_binds_checksum(monkeypatch):
    """Verified checksum flows through ``ZPolicy.readiness_manifest_checksum``."""
    monkeypatch.chdir(_REPO_ROOT)
    resolution = resolve_verdict()

    z_policy = build_z_policy(resolution)

    assert z_policy.policy_name == EXPECTED_VERDICT_VALUE
    assert z_policy.readiness_manifest_checksum == EXPECTED_VERDICT_FILE_SHA256
    # per_cell_z is filled by sample_per_cell_z downstream; build_z_policy
    # returns the provenance skeleton with an empty coverage map.
    assert dict(z_policy.per_cell_z) == {}


def test_build_z_policy_blank_checksum_raises(tmp_path):
    """Blank verified checksum fails closed via the existing binding invariant."""
    resolution = VerdictResolution(
        resolved_path=tmp_path / "unused.md",
        override_used=False,
        override_path=None,
        verified_sha256="",
        sampler_rule_id=SAMPLER_RULE_ID,
    )

    with pytest.raises(ReadinessManifestChecksumMissingError):
        build_z_policy(resolution)


# --- sampler pin -----------------------------------------------------------


def test_sampler_rule_id_pinned_to_spec_literal():
    """Sampler identifier equals the literal recorded in the spec."""
    assert SAMPLER_RULE_ID == "nearest_mesh_node_elevation_v1"


# --- sampler numeric behavior ---------------------------------------------


def test_sample_per_cell_z_matches_keliya_fixture_including_outside_hull():
    """Keliya-fixture numeric oracle: inside-hull + outside-hull cells.

    Uses the committed keliya fixture (484 elements / 276 nodes; all
    node ``Elevation`` = 100.0 by construction) and the checksum-bound
    ``gis/keliya.prj`` package projection. Two used cells are sampled:

    * ``in_hull`` — WGS84 (100.25, 36.15), transformed near the center
      of the mesh footprint (node x range -437110..-401910, y range
      3863789..3885789 meters).
    * ``south_of_hull`` — WGS84 (100.24, 35.50), transformed well south
      of the mesh y range. Nearest-node sampling requires no
      containment test, so this outside-hull center still resolves.

    Independently recomputed expected values (see the oracle recorded
    at authoring time in the module docstring): both cells sample the
    literal ``Elevation`` value 100.0 — pinned as the numeric oracle.
    """
    nodes = _parse_keliya_mesh_nodes()
    # Sanity: fixture invariants the oracle depends on.
    assert len(nodes) == 276
    assert all(node.elevation == 100.0 for node in nodes)

    projection = PackageProjection.from_prj_wkt(_KELIYA_PRJ.read_text())

    # Runtime lock on the docstring claim that ``south_of_hull`` transforms
    # OUTSIDE the mesh y-range envelope. If a future rebase of the keliya
    # fixture shifts the mesh southward and this cell ends up inside the
    # hull, coverage silently degrades — surface it here rather than in a
    # downstream consumer.
    _, south_cy = projection.to_package_xy(100.24, 35.50)
    node_y_min = min(node.y for node in nodes)
    node_y_max = max(node.y for node in nodes)
    assert south_cy < node_y_min or south_cy > node_y_max, (
        f"south_of_hull cell must transform outside the mesh y-range "
        f"[{node_y_min}, {node_y_max}]; got cy={south_cy}. "
        "Docstring claims this cell is outside hull; if fixture changed, "
        "update coords."
    )

    used = [
        UsedCell(cell_id="in_hull", wgs84_lon=100.25, wgs84_lat=36.15),
        UsedCell(cell_id="south_of_hull", wgs84_lon=100.24, wgs84_lat=35.50),
    ]

    result = sample_per_cell_z(used, nodes, projection)

    # Literal expected values — recomputed independently from the mesh
    # + prj at authoring time and pinned here.
    assert result == {
        "in_hull": 100.0,
        "south_of_hull": 100.0,
    }

    # Additional independent oracle: recompute the WINNING node index
    # via a straight-from-file min() search and pin the identity of the
    # winning node so a future accidental change to the tie-break rule
    # or transform axis order surfaces here rather than in a downstream
    # consumer.
    transformer = pyproj.Transformer.from_crs(
        "EPSG:4326", pyproj.CRS.from_wkt(_KELIYA_PRJ.read_text()), always_xy=True
    )

    def _independent_winner(lon: float, lat: float) -> int:
        cx, cy = transformer.transform(lon, lat)
        return min(
            nodes,
            key=lambda node: ((node.x - cx) ** 2 + (node.y - cy) ** 2, node.node_id),
        ).node_id

    assert _independent_winner(100.25, 36.15) == 127
    assert _independent_winner(100.24, 35.50) == 9


def test_sample_per_cell_z_distinct_elevations_via_keliya_prj_detects_wrong_node():
    """Regression lock: uniform keliya elevations (all 100.0) can't distinguish
    which node the sampler picked.

    Every keliya mesh node carries ``Elevation = 100.0`` by construction,
    so the numeric-oracle test above can't tell a wrong-node bug apart
    from a right-node pick. This test builds a small synthetic mesh with
    DISTINCT elevations, transforms a WGS84 cell center through the REAL
    keliya ``keliya.prj`` transformer, and asserts the sampler picks the
    unambiguously-nearer node. If the axis order flipped in
    ``PackageProjection.to_package_xy`` (e.g. lon/lat swapped), or the
    nearest-node selection logic broke, the elevation would come back as
    the wrong value (222.0 instead of 111.0) and this test would fail.

    Distance calculation for the pinned coordinates
    -----------------------------------------------
    WGS84 ``(100.20, 36.05)`` transforms to
    ``(cx, cy) ≈ (-424469.1, 3863712.8)`` under the keliya Albers prj.

    * Node 1 at ``(-424000.0, 3864000.0)`` -> dist² ≈ 3.0e5
    * Node 2 at ``(-400000.0, 3900000.0)`` -> dist² ≈ 1.9e9

    Node 1 wins by ~4 orders of magnitude — no ambiguity.
    """
    prj_text = _KELIYA_PRJ.read_text(encoding="utf-8")
    projection = PackageProjection.from_prj_wkt(prj_text)
    # Two synthetic mesh nodes in package CRS with distinct elevations.
    # Coordinates chosen so WGS84 (100.20, 36.05) transforms nearer to
    # node 1 than to node 2 under the real keliya prj (verified: dist1²
    # ≈ 3.0e5 vs dist2² ≈ 1.9e9).
    nodes = (
        MeshNode(node_id=1, x=-424000.0, y=3864000.0, elevation=111.0),
        MeshNode(node_id=2, x=-400000.0, y=3900000.0, elevation=222.0),
    )
    used_cells = (
        UsedCell(cell_id="near_1", wgs84_lon=100.20, wgs84_lat=36.05),
    )

    # Sanity: recompute the distance ordering independently so the test's
    # premise is verified before the sampler is exercised. If a future
    # pyproj upgrade changes the axis convention or transform accuracy
    # enough to flip the ordering, this pre-check fails before the main
    # assertion, giving a targeted diagnostic.
    cx, cy = projection.to_package_xy(100.20, 36.05)
    d1_sq = (nodes[0].x - cx) ** 2 + (nodes[0].y - cy) ** 2
    d2_sq = (nodes[1].x - cx) ** 2 + (nodes[1].y - cy) ** 2
    assert d1_sq < d2_sq, (
        f"Test premise violated: node 1 should be closer to "
        f"WGS84 (100.20, 36.05); got dist1²={d1_sq}, dist2²={d2_sq}"
    )

    result = sample_per_cell_z(used_cells, nodes, projection)

    assert result == {"near_1": 111.0}, (
        f"Expected sampler to pick node 1 (elevation 111.0); got {result}. "
        "If this test fails, either the axis order flipped in "
        "PackageProjection.to_package_xy or nearest-node selection changed."
    )


def test_sample_per_cell_z_distance_tie_selects_smallest_node_id():
    """Two mesh nodes equidistant from a cell center: smaller ID wins."""
    nodes = [
        MeshNode(node_id=7, x=0.0, y=1.0, elevation=777.0),
        MeshNode(node_id=3, x=0.0, y=-1.0, elevation=333.0),
    ]
    used = [UsedCell(cell_id="tie", wgs84_lon=0.0, wgs84_lat=0.0)]

    result = sample_per_cell_z(used, nodes, _IdentityProjection())

    # Smaller node_id (3) wins the tie; its elevation is emitted.
    assert result == {"tie": 333.0}


def test_sample_per_cell_z_missing_cell_raises_ZPolicyCellMissingError():
    """A used cell with no candidate mesh node fails closed with the shared error."""
    used = [UsedCell(cell_id="alone", wgs84_lon=0.0, wgs84_lat=0.0)]
    empty_mesh: list[MeshNode] = []

    with pytest.raises(ZPolicyCellMissingError) as exc_info:
        sample_per_cell_z(used, empty_mesh, _IdentityProjection())

    assert exc_info.value.grid_cell_id == "alone"
    assert exc_info.value.policy_name == EXPECTED_VERDICT_VALUE
