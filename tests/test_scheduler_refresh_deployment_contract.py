"""The node-22 deployment surface: preflight, config, units, installer, wrapper.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). This is the partition that
`read_text`s tracked deployment paths, so it is the CI selector's owner suite
for all five of them: `infra/systemd/nhms-scheduler-file-provider-refresh`
`.service` / `.timer`,
`infra/env/compute.scheduler-provider-refresh.env.example`,
`scripts/scheduler_file_provider_refresh_once.sh` and
`scripts/install_node22_scheduler_file_provider_refresh.sh`
(`scripts/select_ci_tests.py` routes each of those paths here, and
`tests/test_select_ci_tests.py` derives the edge from the literal reads below
rather than trusting the rule).

It also owns the euid-owned lock-parent preflight, the database/relative-path
config refusals, the env-file loader, the installer enable/restore lifecycle
against a fake `systemctl`, and the wrapper's clean-environment execution legs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
)
from tests.scheduler_refresh_helpers import (
    _config,
)
from tests.scheduler_refresh_receipt_helpers import (
    _classification_stub,
    _enforced_cutover_gate,
)


def test_refresh_preflight_requires_private_euid_owned_lock_parent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.refresh_lock.parent.chmod(0o755)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_refresh_preflight_rejects_lock_parent_owned_by_another_euid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    owner = config.refresh_lock.parent.stat().st_uid
    monkeypatch.setattr(refresh.os, "geteuid", lambda: owner + 1)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_config_rejects_database_or_relative_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        "NHMS_BASINS_ROOT": str(tmp_path / "Basins"),
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "registry.json"),
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "objects/worker-registry.json"),
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": str(tmp_path / "readiness.json"),
        "NHMS_SCHEDULER_STATE_INDEX": str(tmp_path / "state.json"),
        "OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": str(tmp_path / "objects"),
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": str(tmp_path / "work"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": str(tmp_path / "emergency"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": str(tmp_path / "refresh"),
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        monkeypatch.setenv(selector, "redacted")
        with pytest.raises(refresh.RefreshError):
            refresh.RefreshConfig.from_env()
        monkeypatch.delenv(selector)
    monkeypatch.setenv("NHMS_BASINS_ROOT", "relative/Basins")
    with pytest.raises(refresh.RefreshError):
        refresh.RefreshConfig.from_env()


def test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.service").read_text()
    timer = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.timer").read_text()
    environment = (root / "infra/env/compute.scheduler-provider-refresh.env.example").read_text()
    wrapper = (root / "scripts/scheduler_file_provider_refresh_once.sh").read_text()
    installer = (root / "scripts/install_node22_scheduler_file_provider_refresh.sh").read_text()

    assert "ExecStart=/scratch/frd_muziyao/NWM/scripts/scheduler_file_provider_refresh_once.sh" in service
    assert "TimeoutStartSec=7200" in service
    assert "PrivateTmp=true" not in service
    assert "no-follow verifier must open" in service
    assert "OnCalendar=*-*-* 02:15:00 UTC" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL" in service
    assert "PGPASSWORD" in service and "PGSSLROOTCERT" in service
    assert "Before=nhms-compute-scheduler.service" in service
    assert (
        "ExecCondition=/bin/sh -c '! /usr/bin/systemctl --user is-active --quiet "
        "nhms-compute-scheduler.service'" in service
    )
    assert "is-active --quiet nhms-compute-scheduler.service" in wrapper
    assert "NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true" in environment
    assert '[[ "$NHMS_SCHEDULER_REQUIRE_DIRECT_GRID" == true ]]' in wrapper
    for selector in ("DATABASE_URL=", "PIPELINE_DATABASE_URL=", "PGHOST=", "PGPORT="):
        assert selector not in environment
    assert "stat -c '%a'" in wrapper
    assert "DATABASE_URL PIPELINE_DATABASE_URL PGAPPNAME" in wrapper
    # #2294: what the installer DOES -- its env-file checks, `cmp -s`, the
    # receipt validation before arming, the refresh-service entry gate every
    # action passes before its first mutation (`assert_refresh_service_inactive`),
    # the status lines, the restore and its read-backs, the malformed-baseline
    # refusals, and touching only the refresh units -- is asserted by running
    # it against a fake systemctl in tests/test_scheduler_refresh_installer_failure_paths.py
    # and the lifecycle case below, not by source substrings.
    assert "stat -c '%a'" in installer
    assert installer.index("stat -c '%a'") < installer.index("stat -f '%Lp'")
    assert "stat -c '%a'" in wrapper
    assert wrapper.index("stat -c '%a'") < wrapper.index("stat -f '%Lp'")
    assert "Persistent=false" in timer
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        assert selector in service
        assert selector in wrapper
        assert selector in installer


def test_env_file_loader_rejects_duplicate_keys_and_cli_rejects_conflicting_operations(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "refresh.env"
    env_file.write_text("OBJECT_STORE_ROOT=/private\nOBJECT_STORE_ROOT=/other\n")

    with pytest.raises(refresh.RefreshError, match="configuration_invalid"):
        refresh._apply_environment_file(env_file)
    with pytest.raises(SystemExit):
        refresh._build_parser().parse_args(
            [
                "--recover-emergency",
                str(tmp_path / "emergency.json"),
                "--validate-current-receipt",
                str(tmp_path / "latest.json"),
            ]
        )


def test_installer_enable_lifecycle_and_failure_restore_with_fake_systemctl(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    source_units = root / "infra/systemd"
    units = repo / "infra/systemd"
    env_dir = repo / "infra/env"
    scripts_dir = repo / "scripts"
    units.mkdir(parents=True)
    env_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    shutil.copy2(root / "scripts/scheduler_file_provider_refresh.py", scripts_dir)
    # The #1080 cutover-declaration schema is loaded at module import; the
    # installer runs the script from repo/scripts/ so schemas/ must resolve
    # relative to the copied tree too.
    (repo / "schemas").mkdir()
    shutil.copy2(
        root / "schemas/scheduler_registry_package_cutover.schema.json",
        repo / "schemas",
    )
    for name in (
        "nhms-scheduler-file-provider-refresh.service",
        "nhms-scheduler-file-provider-refresh.timer",
    ):
        shutil.copy2(source_units / name, units / name)
    basins = tmp_path / "Basins"
    private_objects = tmp_path / "private-objects"
    shared_providers = tmp_path / "shared-providers"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, private_objects, shared_providers, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (tmp_path / "private", work, receipts, emergency):
        path.chmod(0o700)
    provider_paths = {
        "registry": shared_providers / "scheduler/registry/manifest-last.json",
        "registry_worker_mirror": private_objects / "scheduler/registry/manifest-last.json",
        "readiness": shared_providers / "scheduler/canonical-readiness/index-last.json",
        "state": shared_providers / "scheduler/state-index/index-last.json",
    }
    providers = []
    for name, path in provider_paths.items():
        path.parent.mkdir(parents=True)
        path.write_text("registry\n" if name == "registry_worker_mirror" else name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = receipts / "latest.json"
    receipt.write_bytes(
        refresh._receipt_bytes(
            refresh._receipt(
                run_id="refresh_installer",
                started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
                outcome="published",
                reason="success",
                phase="complete",
                providers=providers,
                registry_classification=_classification_stub(),
                # #1144: `--enable` runs `validate_current_receipt`, which now
                # hard-rejects a `published` receipt without the audit block.
                cutover_gate=_enforced_cutover_gate(declaration_present=False),
            )
        )
    )
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text(
        "\n".join(
            (
                f"NHMS_BASINS_ROOT={basins}",
                f"OBJECT_STORE_ROOT={private_objects}",
                f"NHMS_SCHEDULER_PROVIDER_STORE_ROOT={shared_providers}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"NHMS_SCHEDULER_REGISTRY_MANIFEST={provider_paths['registry']}",
                f"NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST={provider_paths['registry_worker_mirror']}",
                f"NHMS_SCHEDULER_CANONICAL_READINESS_INDEX={provider_paths['readiness']}",
                f"NHMS_SCHEDULER_STATE_INDEX={provider_paths['state']}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT={work}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT={receipts}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT={emergency}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK={tmp_path / 'private/refresh'}",
                "NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true",
            )
        )
        + "\n"
    )
    env_file.chmod(0o600)
    fake_state = tmp_path / "systemctl-state.json"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "path = os.environ['FAKE_SYSTEMCTL_STATE']\n"
        "try:\n"
        "    state = json.load(open(path, encoding='utf-8'))\n"
        "except FileNotFoundError:\n"
        "    state = {}\n"
        "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
        "command = args[0]\n"
        "unit = args[-1] if len(args) > 1 else ''\n"
        "with open(os.environ['FAKE_SYSTEMCTL_TRACE'], 'a', encoding='utf-8') as trace:\n"
        "    trace.write(' '.join(args) + '\\n')\n"
        "current = state.setdefault(unit, {'enabled':'disabled','active':'inactive'})\n"
        "if command == 'is-enabled': print(current['enabled'])\n"
        "elif command == 'is-active': print(current['active'])\n"
        "elif command == 'enable':\n"
        "    current['enabled'] = 'enabled'\n"
        "    if '--now' in args: current['active'] = 'active'\n"
        "elif command == 'disable':\n"
        "    current['enabled'] = 'disabled'\n"
        "    if '--now' in args: current['active'] = 'inactive'\n"
        "elif command == 'start': current['active'] = 'active'\n"
        "elif command == 'stop': current['active'] = 'inactive'\n"
        "elif command != 'daemon-reload': raise SystemExit(2)\n"
        "with open(path, 'w', encoding='utf-8') as handle: json.dump(state, handle)\n"
        "if os.environ.get('FAKE_FAIL_AFTER') == command + ':' + unit: raise SystemExit(9)\n"
    )
    fake_systemctl.chmod(0o755)
    fake_trace = tmp_path / "systemctl-trace.log"
    unit_dir = tmp_path / "units"
    state_root = tmp_path / "install-state"
    environment = {
        **os.environ,
        "FAKE_SYSTEMCTL_STATE": str(fake_state),
        "FAKE_SYSTEMCTL_TRACE": str(fake_trace),
        "NHMS_SCHEDULER_REFRESH_REPO": str(repo),
        "NHMS_SCHEDULER_REFRESH_UNIT_DIR": str(unit_dir),
        "NHMS_SCHEDULER_REFRESH_INSTALL_STATE_ROOT": str(state_root),
        "NHMS_SCHEDULER_REFRESH_SYSTEMCTL": str(fake_systemctl),
        "NHMS_SCHEDULER_REFRESH_PYTHON": sys.executable,
        "NHMS_SCHEDULER_REFRESH_RECEIPT": str(receipt),
        "PYTHONPATH": str(root),
    }
    installer = root / "scripts/install_node22_scheduler_file_provider_refresh.sh"
    # Run under a resolved bash >= 4, never the shebang's first `bash` on PATH:
    # the installer's main-shell guard reads `$BASHPID`.
    bash = _require_modern_bash()

    subprocess.run([bash, str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    enabled = subprocess.run(
        [bash, str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )

    assert json.loads(enabled.stdout)["status"] == "enabled_active"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    assert state["nhms-scheduler-file-provider-refresh.service"]["active"] == "inactive"

    invalid_receipt = receipt.read_bytes()
    receipt.write_text('{"outcome":"published","database_free":true}\n')
    failed = subprocess.run(
        [bash, str(installer), "--enable"], env=environment, check=False, capture_output=True, text=True
    )
    assert failed.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    receipt.write_bytes(invalid_receipt)

    repeated = subprocess.run(
        [bash, str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(repeated.stdout)["status"] == "enabled_active"

    rolled_back = subprocess.run(
        [bash, str(installer), "--rollback"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(rolled_back.stdout)["status"] == "rolled_back"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.service"] == {"enabled": "disabled", "active": "inactive"}

    subprocess.run([bash, str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    fail_environment = {
        **environment,
        "FAKE_FAIL_AFTER": "enable:nhms-scheduler-file-provider-refresh.timer",
    }
    failed_after_enable = subprocess.run(
        [bash, str(installer), "--enable"], env=fail_environment, check=False, capture_output=True, text=True
    )
    assert failed_after_enable.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}

    for transitional in ("activating", "deactivating", "reloading", "active"):
        state["nhms-scheduler-file-provider-refresh.service"] = {
            "enabled": "disabled",
            "active": transitional,
        }
        fake_state.write_text(json.dumps(state))
        fake_trace.write_text("")
        refused = subprocess.run(
            [bash, str(installer), "--install"], env=environment, check=False, capture_output=True, text=True
        )
        assert refused.returncode != 0
        assert all(
            not line.startswith(("enable ", "disable ", "start ", "stop ", "daemon-reload"))
            for line in fake_trace.read_text().splitlines()
        )


def _bash_major_version(executable: str) -> int | None:
    try:
        probe = subprocess.run(
            [executable, "-c", 'echo "${BASH_VERSINFO[0]}"'],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        # Missing/unexecutable candidate: fall back to the next one (or skip).
        return None
    if probe.returncode != 0:
        return None
    try:
        return int(probe.stdout.strip())
    except ValueError:
        return None


def _require_modern_bash() -> str:
    """Resolve a bash >= 4 interpreter for the wrapper rejection-semantics tests
    and the installer lifecycle case.

    macOS ships bash 3.2 as ``/bin/bash``, where a failing ``[[ ]]`` does not
    abort under ``set -e``. The wrapper's allowlist/parse rejections are bare
    ``[[ ]]`` asserts, and the refresh installer's main-shell guard reads
    ``$BASHPID``, so both only hold on bash >= 4. The candidates are the ones
    ``tests/scheduler_refresh_installer_harness.py`` probes.
    """
    candidates = ["/bin/bash"]
    for candidate in (shutil.which("bash"), "/usr/bin/bash", "/opt/homebrew/bin/bash"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        version = _bash_major_version(candidate)
        if version is not None and version >= 4:
            return candidate
    pytest.skip(
        "requires bash >= 4 (bash 3.2 does not abort on a failing [[ ]] under set -e "
        f"and has no $BASHPID, so these semantics are unverifiable); probed: {candidates}"
    )


def _write_wrapper_execution_fixture(
    tmp_path: Path,
    *,
    include_forbidden: bool = False,
    declaration_path: str | None = None,
    extra_env_lines: list[str] | None = None,
) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    env_dir = repo / "infra/env"
    interpreter = repo / ".venv/bin/python"
    env_dir.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    configured = {
        "NHMS_BASINS_ROOT": "/trusted/Basins",
        "OBJECT_STORE_ROOT": "/trusted/object-store",
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": "/trusted/provider-store",
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": "/trusted/object-store/scheduler/registry/manifest-last.json",
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST": "/trusted/object-store/scheduler/registry/worker-manifest-last.json",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": "/trusted/object-store/scheduler/readiness/index-last.json",
        "NHMS_SCHEDULER_STATE_INDEX": "/trusted/object-store/scheduler/state/index-last.json",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": "/private/work",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": "/private/receipts",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": "/private/emergency",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": "/private/refresh",
        "NHMS_SCHEDULER_REQUIRE_DIRECT_GRID": "true",
    }
    lines = [f"{key}={value}" for key, value in configured.items()]
    if include_forbidden:
        lines.append("DATABASE_URL=must-not-load")
    if declaration_path is not None:
        lines.append(f"NHMS_REGISTRY_CUTOVER_DECLARATION_PATH={declaration_path}")
    if extra_env_lines:
        lines.extend(extra_env_lines)
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    marker = tmp_path / "interpreter-ran"
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {marker}\n"
        "printf 'BASINS=%s\\n' \"$NHMS_BASINS_ROOT\"\n"
        "printf 'OBJECTS=%s\\n' \"$OBJECT_STORE_ROOT\"\n"
        "printf 'PREFIX=%s\\n' \"$OBJECT_STORE_PREFIX\"\n"
        "printf 'REGISTRY=%s\\n' \"$NHMS_SCHEDULER_REGISTRY_MANIFEST\"\n"
        "printf 'WORKER_REGISTRY=%s\\n' \"$NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST\"\n"
        "printf 'READINESS=%s\\n' \"$NHMS_SCHEDULER_CANONICAL_READINESS_INDEX\"\n"
        "printf 'STATE=%s\\n' \"$NHMS_SCHEDULER_STATE_INDEX\"\n"
        "printf 'WORK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT\"\n"
        "printf 'RECEIPTS=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT\"\n"
        "printf 'EMERGENCY=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT\"\n"
        "printf 'LOCK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK\"\n"
        "printf 'REQUIRE_DIRECT_GRID=%s\\n' \"$NHMS_SCHEDULER_REQUIRE_DIRECT_GRID\"\n"
        "printf 'DATABASE_URL=%s\\n' \"${DATABASE_URL-<unset>}\"\n"
        "printf 'PGHOST=%s\\n' \"${PGHOST-<unset>}\"\n"
        "printf 'PWD=%s\\n' \"$PWD\"\n"
        "printf 'CUTOVER=%s\\n' \"${NHMS_REGISTRY_CUTOVER_DECLARATION_PATH-<unset>}\"\n"
        "printf 'ARGS=%s\\n' \"$*\"\n"
    )
    interpreter.chmod(0o755)
    wrapper = tmp_path / "refresh-wrapper.sh"
    wrapper.write_text(
        (root / "scripts/scheduler_file_provider_refresh_once.sh")
        .read_text()
        .replace("repo=/scratch/frd_muziyao/NWM", f"repo={repo}")
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def test_wrapper_clean_environment_loads_fixed_config_and_strips_inherited_db_selectors(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "DATABASE_URL": "inherited-secret",
            "PGHOST": "inherited-host",
        },
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "BASINS=/trusted/Basins" in result.stdout
    assert "OBJECTS=/trusted/object-store" in result.stdout
    assert "PREFIX=s3://nhms" in result.stdout
    assert "REGISTRY=/trusted/object-store/scheduler/registry/manifest-last.json" in result.stdout
    assert (
        "WORKER_REGISTRY=/trusted/object-store/scheduler/registry/worker-manifest-last.json"
        in result.stdout
    )
    assert "READINESS=/trusted/object-store/scheduler/readiness/index-last.json" in result.stdout
    assert "STATE=/trusted/object-store/scheduler/state/index-last.json" in result.stdout
    assert "WORK=/private/work" in result.stdout
    assert "RECEIPTS=/private/receipts" in result.stdout
    assert "EMERGENCY=/private/emergency" in result.stdout
    assert "LOCK=/private/refresh" in result.stdout
    assert "REQUIRE_DIRECT_GRID=true" in result.stdout
    assert "DATABASE_URL=<unset>" in result.stdout
    assert "PGHOST=<unset>" in result.stdout
    assert f"PWD={tmp_path / 'repo'}" in result.stdout
    # #1095: the cutover declaration path is optional; its absence must keep the
    # runner's safe-refuse default (undeclared package cutovers stay refused).
    assert "CUTOVER=<unset>" in result.stdout
    assert result.stdout.rstrip().endswith("-m scripts.scheduler_file_provider_refresh --dry-run")


def test_wrapper_admits_cutover_declaration_path_and_exports_it_to_the_runner(tmp_path: Path) -> None:
    bash = _require_modern_bash()
    declaration_path = "/private/cutover/declaration-last.json"
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path, declaration_path=declaration_path)

    result = subprocess.run(
        [bash, str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert f"CUTOVER={declaration_path}" in result.stdout
    assert result.stdout.rstrip().endswith("-m scripts.scheduler_file_provider_refresh --dry-run")
    # The wrapper allowlist and the runner reader must name the same variable,
    # otherwise the systemd path silently loses the declaration again.
    assert refresh.CUTOVER_DECLARATION_ENV == "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"


def test_wrapper_rejects_unknown_key_outside_the_allowlist(tmp_path: Path) -> None:
    bash = _require_modern_bash()
    wrapper, marker = _write_wrapper_execution_fixture(
        tmp_path, extra_env_lines=["NHMS_SOMETHING_ELSE=x"]
    )

    result = subprocess.run(
        [bash, str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "ARGS=" not in result.stdout


def test_wrapper_rejects_blank_cutover_declaration_path(tmp_path: Path) -> None:
    bash = _require_modern_bash()
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path, declaration_path="")

    result = subprocess.run(
        [bash, str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "ARGS=" not in result.stdout


def test_wrapper_rejects_forbidden_selector_in_mode_0600_env_before_exec(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path, include_forbidden=True)

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert not marker.exists()
