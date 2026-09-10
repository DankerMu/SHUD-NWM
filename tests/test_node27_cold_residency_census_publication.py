"""Publication / no-clobber / secret / import-surface tests for the #1895 census CLI.

These tests were moved out of ``tests/test_node27_cold_residency_census.py`` for
the 1,000-line structural guard; the shared fake helpers are imported from the
core module (the repository's cross-suite helper convention).  No test was
deleted: 15 publication/no-clobber/secret/import-surface tests move here (plus
the policy-module pin that the split made necessary), 36 stay in the core
module, and the selector routes both suites together with the runbook contract
and the runtime owner.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest

from packages.common import node27_cold_residency_census_policy as census_policy
from scripts import node27_cold_residency_census as census
from tests.test_node27_cold_residency_census import (
    _DSN,
    _HEAD,
    _NOW,
    _ROOT,
    CensusConnection,
    _load,
    _main,
)

# --- publication --------------------------------------------------------------


def test_publish_refuses_existing_output(tmp_path: Path) -> None:
    target = tmp_path / "census.json"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert target.read_text(encoding="utf-8") == "keep"


def test_publish_refuses_symlink_parent_and_non_directory_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(census.CensusError):
        census.publish_artifact(link / "census.json", {"verdict": "GO"})
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(census.CensusError):
        census.publish_artifact(plain / "census.json", {"verdict": "GO"})
    with pytest.raises(census.CensusError):
        census.publish_artifact(tmp_path / "missing" / "census.json", {"verdict": "GO"})


def test_publish_refuses_temp_collision_and_writes_mode_0600_atomically(tmp_path: Path) -> None:
    target = tmp_path / "census.json"
    (tmp_path / ".census.json.tmp").write_text("stale", encoding="utf-8")
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    (tmp_path / ".census.json.tmp").unlink()
    census.publish_artifact(target, {"verdict": "GO"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "GO"
    assert not (tmp_path / ".census.json.tmp").exists()


def test_publish_race_created_target_is_never_clobbered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent creator that lands between the existence check and the
    promotion must win: the census refuses, never overwrites the attacker bytes.

    Pre-fix the implementation promoted with ``os.replace``, which clobbers, so
    this test is red.  Post-fix the implementation promotes through an exclusive
    link-first primitive; the racing wrapper here only *creates* the contender
    and then calls the genuine exclusive primitive, so a green result proves the
    real EEXIST refusal rather than a mocked success.
    """

    target = tmp_path / "census.json"
    if hasattr(census, "_promote_artifact"):
        real = census._promote_artifact

        def racing(temp: Path, destination: Path):
            destination.write_text("attacker", encoding="utf-8")
            return real(temp, destination)

        monkeypatch.setattr(census, "_promote_artifact", racing)
    else:
        real_replace = os.replace

        def racing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]):
            Path(dst).write_text("attacker", encoding="utf-8")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", racing_replace)
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert target.read_text(encoding="utf-8") == "attacker"


def test_publish_race_symlink_target_is_never_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink raced in at the promotion point must refuse, not be replaced
    (which would clobber the link target) and not be followed.
    """

    target = tmp_path / "census.json"
    real = tmp_path / "elsewhere"
    real.write_text("victim", encoding="utf-8")
    if hasattr(census, "_promote_artifact"):
        real_promote = census._promote_artifact

        def racing_symlink(temp: Path, destination: Path):
            destination.symlink_to(real)
            return real_promote(temp, destination)

        monkeypatch.setattr(census, "_promote_artifact", racing_symlink)
    else:
        real_replace = os.replace

        def racing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]):
            Path(dst).symlink_to(real)
            return real_replace(src, dst)

        monkeypatch.setattr(census.os, "replace", racing_replace)
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == "victim"


def test_publish_failure_after_promotion_never_deletes_the_published_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publication that fails *after* the exclusive link landed (e.g. the
    primitive's own parent proof raises) is indeterminate: the target holds the
    published bytes and must never be unlinked or mistaken for a temp sibling.
    The error surfaces as a stable CensusError and only the temp is removed.
    """

    target = tmp_path / "census.json"
    real = census._promote_artifact

    def link_then_fail(temp: Path, destination: Path):
        real(temp, destination)
        raise census.CensusError("parent proof failed after link")

    monkeypatch.setattr(census, "_promote_artifact", link_then_fail)
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "GO"
    assert not (tmp_path / ".census.json.tmp").exists()


def test_publish_failure_after_promotion_leaves_a_pre_existing_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing target (no race) refuses before any temp creation and is
    never replaced or removed.
    """

    target = tmp_path / "census.json"
    target.write_text("already-published", encoding="utf-8")
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert target.read_text(encoding="utf-8") == "already-published"
    assert not (tmp_path / ".census.json.tmp").exists()


def test_publish_race_refusal_cleans_the_temp_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a race refusal the private temp sibling is removed and nothing is
    left behind to confuse the next no-clobber attempt.
    """

    target = tmp_path / "census.json"
    if hasattr(census, "_promote_artifact"):
        real = census._promote_artifact

        def racing(temp: Path, destination: Path):
            destination.write_text("attacker", encoding="utf-8")
            return real(temp, destination)

        monkeypatch.setattr(census, "_promote_artifact", racing)
    else:
        real_replace = os.replace

        def racing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]):
            Path(dst).write_text("attacker", encoding="utf-8")
            return real_replace(src, dst)

        monkeypatch.setattr(census.os, "replace", racing_replace)
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert not (tmp_path / ".census.json.tmp").exists()
    assert target.read_text(encoding="utf-8") == "attacker"


def test_publish_promotion_never_calls_clobbering_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural guard: promotion must go through the exclusive link-first
    primitive, so ``os.replace`` (which silently replaces an existing target)
    must never be reachable on the publication path.  This wraps only the
    allegedly-forbidden primitive; the exclusive publication is the real one.
    """

    replaced: list[tuple[object, ...]] = []

    def forbidden_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object):
        replaced.append((src, dst))
        raise AssertionError("publication must not clobber via os.replace")

    monkeypatch.setattr(census.os, "replace", forbidden_replace)
    target = tmp_path / "census.json"
    census.publish_artifact(target, {"verdict": "GO"})
    assert replaced == []
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "GO"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_publish_promotion_seam_is_shared_with_the_exclusive_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion seam must call the same exclusive primitive the no-clobber
    contract is built on; wrapping that primitive must make a second publication
    to the same target refuse with EEXIST, not overwrite.
    """

    from packages.common import safe_fs_publication

    real_promote = safe_fs_publication.move_regular_file_no_follow_exclusive
    invocations: list[tuple[object, ...]] = []

    def recording(parent: Path, name: str, dest_parent: Path, dest_name: str, **kwargs: object):
        invocations.append((name, dest_name))
        return real_promote(parent, name, dest_parent, dest_name, **kwargs)

    monkeypatch.setattr(safe_fs_publication, "move_regular_file_no_follow_exclusive", recording)
    assert hasattr(census, "_promote_artifact")
    target = tmp_path / "census.json"
    census.publish_artifact(target, {"verdict": "GO"})
    assert invocations == [(".census.json.tmp", "census.json")]
    with pytest.raises(census.CensusError):
        census.publish_artifact(target, {"verdict": "GO"})
    assert invocations == [(".census.json.tmp", "census.json")]
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "GO"


def test_publish_refuses_oversized_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census, "MAX_ARTIFACT_BYTES", 16)
    with pytest.raises(census.CensusError):
        census.publish_artifact(tmp_path / "census.json", {"verdict": "GO", "blob": "x" * 64})


def test_go_artifact_is_not_clobbered_by_main(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    target = tmp_path / "census.json"
    target.write_text("do-not-touch", encoding="utf-8")
    code = census.main(
        ["--require-count", "1", "--output", str(target)],
        env={"DATABASE_URL": _DSN},
        connect=lambda dsn: connection,
        head_observer=lambda: (_HEAD, True, False),
        watermark_fetcher=lambda dsn, connect=None: _NOW,
    )
    assert code == 2
    assert target.read_text(encoding="utf-8") == "do-not-touch"


def test_dsn_secret_never_reaches_artifact_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    code, target = _main(census, tmp_path, connection, require_count="1")
    assert code == 0
    assert "secretpw" not in target.read_text(encoding="utf-8")
    assert "secretpw" not in capsys.readouterr().err
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["config"]["database_url_masked"] == "postgresql://***@127.0.0.1:55432/nhms"


def test_dsn_secret_not_echoed_on_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = census.main(
        ["--require-count", "6", "--output", str(tmp_path / "census.json")],
        env={"DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms"},
        connect=lambda dsn: (_ for _ in ()).throw(RuntimeError("driver exploded")),
        head_observer=lambda: (_HEAD, True, False),
        watermark_fetcher=lambda dsn, connect=None: _NOW,
    )
    assert code == 2
    assert "secretpw" not in capsys.readouterr().err


# --- import surface -----------------------------------------------------------


def test_census_import_surface_excludes_runtime_target_and_probe() -> None:
    source = (_ROOT / "scripts/node27_cold_residency_census.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
    assert found == {
        "__future__",
        "argparse",
        "collections.abc",
        "datetime",
        "hashlib",
        "json",
        "os",
        "packages.common.compressed_chunk_cold_residency",
        "packages.common.compressed_chunk_cold_runtime_catalog",
        "packages.common.display_watermark",
        # narrow canonical-decimal capacity policy owner (no target/preflight/
        # runtime/probe import by construction).
        "packages.common.node27_cold_residency_census_policy",
        "packages.common.safe_fs_publication",  # narrow no-clobber publication owner
        "pathlib",
        "psycopg2",  # lazy inside the connect seam only; production dependency
        "re",
        "stat",
        "subprocess",
        "sys",
        "typing",
        "urllib.parse",
    }
    assert "from packages.common.compressed_chunk_cold_runtime import" not in source
    assert "import packages.common.compressed_chunk_cold_runtime" not in source
    assert "compressed_chunk_cold_runtime_target" not in source
    assert "compressed_chunk_cold_target" not in source
    assert "compressed_chunk_cold_probe" not in source
    # The disposable LSN observation may appear only as an excluded value named in
    # prose; no code path may parse, default to, or compare against it.
    assert "int(165736)" not in source
    assert "== 165736" not in source
    assert "= 165736" not in source or "disposable" in source


def test_census_policy_module_import_surface_stays_narrow() -> None:
    policy = (_ROOT / "packages/common/node27_cold_residency_census_policy.py").read_text(
        encoding="utf-8"
    )
    found: set[str] = set()
    for node in ast.walk(ast.parse(policy)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
    assert found == {"__future__", "collections.abc", "typing"}
    for forbidden in (
        "compressed_chunk_cold_runtime",
        "compressed_chunk_cold_target",
        "compressed_chunk_cold_probe",
        "node27_cold_tablespace",
        "display_watermark",
    ):
        assert forbidden not in policy
    # The policy module is pure arithmetic: no SQL, no DSN handling, no file IO.
    assert "SELECT" not in policy
    assert "DATABASE_URL" not in policy
    assert "open(" not in policy


def test_capacity_policy_module_default_refusal_type_and_attributes() -> None:
    # The shared policy module's own public seam: its default refusal is
    # CensusPolicyError with the stable error_class/stage attributes the CLI
    # forwards into its CensusError. The CLI wrapper (core suite) proves the
    # translation; this pin proves the module-level contract directly, so a
    # refactor that drops the default error type reddens here instead of only
    # through the CLI.
    with pytest.raises(census_policy.CensusPolicyError, match="must be positive") as excinfo:
        census_policy.capacity_policy(expansions=[0], retained=[1], group_count=1)
    assert excinfo.value.error_class == "capacity"
    assert excinfo.value.stage == "policy"
    resolved = census_policy.capacity_policy(expansions=[7], retained=[3], group_count=1)
    assert resolved["installer_required_cold_free_bytes"] == "17"
    # The exact arithmetic is exercised through the CLI in the core suite; here
    # the module refuses the same overflow with its own type.
    with pytest.raises(census_policy.CensusPolicyError, match="ROLLBACK_HEADROOM"):
        census_policy.capacity_policy(expansions=[2**62], retained=[1], group_count=1)
