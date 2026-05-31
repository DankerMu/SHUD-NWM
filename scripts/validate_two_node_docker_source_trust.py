from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow

REPORT_JSON_NAME = "two-node-docker-source-trust.json"
REPORT_TEXT_NAME = "two-node-docker-source-trust.txt"
VALID_ROLES = frozenset({"compute", "display"})
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SCRATCH_ROOT = Path("/scratch/frd_muziyao")


@dataclass(frozen=True)
class SourcePath:
    path: Path
    label: str
    expected_kind: str


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _mode_octal(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def _is_trusted_owner(owner: str, uid: int, trusted_owners: set[str]) -> bool:
    return owner in trusted_owners or str(uid) in trusted_owners


def _add_blocker(blockers: list[dict[str, str]], code: str, message: str, path: Path | None = None) -> None:
    blocker = {"code": code, "message": message}
    if path is not None:
        blocker["path"] = str(path)
    blockers.append(blocker)


def _inspect_path(
    *,
    path: Path,
    label: str,
    expected_kind: str,
    trusted_owners: set[str],
    blockers: list[dict[str, str]],
    require_mode: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": label,
        "path": str(path),
        "expected_kind": expected_kind,
        "exists": False,
    }
    try:
        st = os.lstat(path)
    except OSError as exc:
        record["error"] = str(exc)
        _add_blocker(blockers, "PATH_MISSING", f"{label} is missing or cannot be inspected: {path}", path)
        return record

    owner = _owner_name(st.st_uid)
    mode_bits = stat.S_IMODE(st.st_mode)
    is_symlink = stat.S_ISLNK(st.st_mode)
    is_directory = stat.S_ISDIR(st.st_mode)
    is_regular = stat.S_ISREG(st.st_mode)
    group_writable = bool(mode_bits & stat.S_IWGRP)
    world_writable = bool(mode_bits & stat.S_IWOTH)
    trusted_owner = _is_trusted_owner(owner, st.st_uid, trusted_owners)
    record.update(
        {
            "exists": True,
            "owner": owner,
            "uid": st.st_uid,
            "group": _group_name(st.st_gid),
            "gid": st.st_gid,
            "mode": _mode_octal(st.st_mode),
            "mode_symbolic": stat.filemode(st.st_mode),
            "is_symlink": is_symlink,
            "is_directory": is_directory,
            "is_regular": is_regular,
            "trusted_owner": trusted_owner,
            "group_writable": group_writable,
            "world_writable": world_writable,
        }
    )

    if is_symlink:
        _add_blocker(blockers, "SYMLINK_REJECTED", f"{label} must not be a symlink: {path}", path)
    if not trusted_owner:
        _add_blocker(
            blockers,
            "UNTRUSTED_OWNER",
            f"{label} has untrusted owner {owner}; trusted owners: {', '.join(sorted(trusted_owners))}",
            path,
        )
    if not is_symlink and (group_writable or world_writable):
        _add_blocker(
            blockers,
            "GROUP_OR_WORLD_WRITABLE",
            f"{label} must not be group/world-writable: {path} mode {_mode_octal(st.st_mode)}",
            path,
        )
    if expected_kind == "directory" and not is_directory:
        _add_blocker(blockers, "WRONG_PATH_TYPE", f"{label} must be a directory: {path}", path)
    elif expected_kind == "file" and not is_regular:
        _add_blocker(blockers, "WRONG_PATH_TYPE", f"{label} must be a regular file: {path}", path)
    if require_mode is not None and mode_bits != require_mode:
        _add_blocker(
            blockers,
            "ROLE_ENV_MODE",
            f"{label} must be mode {require_mode:04o}: {path} mode {_mode_octal(st.st_mode)}",
            path,
        )
    return record


def _path_components(trust_root: Path, target: Path, blockers: list[dict[str, str]]) -> list[Path]:
    try:
        relative = target.relative_to(trust_root)
    except ValueError:
        _add_blocker(
            blockers,
            "TRUST_ROOT_MISMATCH",
            f"trust root {trust_root} must be the same as or a parent of checkout infra path {target}",
            target,
        )
        return [trust_root, target]

    components = [trust_root]
    current = trust_root
    for part in relative.parts:
        current = current / part
        components.append(current)
    return components


def _common_sources(checkout_root: Path) -> list[SourcePath]:
    return [
        SourcePath(checkout_root, "checkout root", "directory"),
        SourcePath(checkout_root / "infra", "infra directory", "directory"),
        SourcePath(checkout_root / "infra" / "compose.compute.yml", "compute compose source", "file"),
        SourcePath(checkout_root / "infra" / "compose.display.yml", "display compose source", "file"),
        SourcePath(checkout_root / "infra" / "env", "env source directory", "directory"),
        SourcePath(checkout_root / "infra" / "systemd", "systemd source directory", "directory"),
        SourcePath(
            checkout_root / "infra" / "systemd" / "nhms-compute-compose.service",
            "compute systemd unit source",
            "file",
        ),
        SourcePath(
            checkout_root / "infra" / "systemd" / "nhms-display-compose.service",
            "display systemd unit source",
            "file",
        ),
    ]


def _role_sources(checkout_root: Path, roles: Sequence[str]) -> list[SourcePath]:
    role_to_source = {
        "compute": SourcePath(checkout_root / "infra" / "env" / "compute.env", "compute role env", "file"),
        "display": SourcePath(checkout_root / "infra" / "env" / "display.env", "display role env", "file"),
    }
    return [role_to_source[role] for role in roles]


def validate_source_trust(
    *,
    checkout_root: Path,
    evidence_root: Path,
    trusted_owners: set[str],
    roles: Sequence[str],
    trust_root: Path | None,
    evidence_run_id: str | None = None,
) -> dict[str, Any]:
    checkout_root = _absolute_path(checkout_root)
    evidence_root = _absolute_path(evidence_root)
    trust_root = _absolute_path(trust_root) if trust_root is not None else checkout_root.parent
    blockers: list[dict[str, str]] = []
    checked_paths: list[dict[str, Any]] = []

    if not trusted_owners:
        _add_blocker(blockers, "TRUSTED_OWNER_REQUIRED", "at least one --trusted-owner value is required")

    if not checkout_root.exists():
        _add_blocker(blockers, "CHECKOUT_ROOT_MISSING", f"checkout root is missing: {checkout_root}", checkout_root)
    if not (checkout_root / "infra").exists():
        _add_blocker(blockers, "INFRA_MISSING", f"infra directory is missing: {checkout_root / 'infra'}")

    infra_path = checkout_root / "infra"
    for component in _path_components(trust_root, infra_path, blockers):
        checked_paths.append(
            _inspect_path(
                path=component,
                label="trust path component",
                expected_kind="directory",
                trusted_owners=trusted_owners,
                blockers=blockers,
            )
        )

    for source in _common_sources(checkout_root):
        checked_paths.append(
            _inspect_path(
                path=source.path,
                label=source.label,
                expected_kind=source.expected_kind,
                trusted_owners=trusted_owners,
                blockers=blockers,
            )
        )

    for role_source in _role_sources(checkout_root, roles):
        checked_paths.append(
            _inspect_path(
                path=role_source.path,
                label=role_source.label,
                expected_kind=role_source.expected_kind,
                trusted_owners=trusted_owners,
                blockers=blockers,
                require_mode=0o600,
            )
        )

    status = "BLOCKED" if blockers else "PASS"
    resolved_run_id = evidence_run_id or _evidence_run_id_from_root(evidence_root)
    if resolved_run_id is not None:
        resolved_run_id = _safe_evidence_run_id(resolved_run_id)
    return {
        "schema": "nhms.two_node_docker.source_trust.v1",
        "status": status,
        "evidence_run_id": resolved_run_id,
        "checkout_root": str(checkout_root),
        "trust_root": str(trust_root),
        "evidence_root": str(evidence_root),
        "trusted_owners": sorted(trusted_owners),
        "roles": list(roles),
        "checked_paths": checked_paths,
        "blockers": blockers,
    }


def _evidence_run_id_from_root(path: Path) -> str | None:
    parts = _absolute_path(path).parts
    for marker in ("two-node-e2e", "test-two-node-e2e-evidence"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _safe_evidence_run_id(value: str) -> str:
    text = str(value).strip()
    if not SAFE_RUN_ID_RE.fullmatch(text) or ".." in text:
        raise ValueError("evidence_run_id must use only alphanumerics, '.', '_' or '-' and be at most 128 chars")
    return text


def _approved_evidence_root(evidence_root: Path, *, checkout_root: Path) -> Path:
    evidence_root = _absolute_path(evidence_root)
    checkout_root = _absolute_path(checkout_root)
    checkout_artifacts = checkout_root / "artifacts"
    allowed_roots = (checkout_artifacts.resolve(strict=False), SCRATCH_ROOT.resolve(strict=False))
    _reject_symlink_components(evidence_root)
    resolved = evidence_root.resolve(strict=False)
    if not any(_path_is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError("source-trust evidence root must be under checkout artifacts/ or /scratch/frd_muziyao")
    _reject_symlink_components(resolved)
    try:
        ensure_directory_no_follow(resolved)
    except SafeFilesystemError as error:
        raise OSError(str(error)) from error
    _reject_symlink_components(resolved)
    return resolved


def _write_json_atomic(path: Path, payload: dict[str, Any], *, containment_root: Path) -> None:
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        atomic_write_bytes_no_follow(path, content, containment_root=containment_root)
    except SafeFilesystemError as error:
        raise OSError(str(error)) from error


def _write_text_report(path: Path, payload: dict[str, Any], *, containment_root: Path) -> None:
    lines = [
        f"status: {payload['status']}",
        f"checkout_root: {payload['checkout_root']}",
        f"trust_root: {payload['trust_root']}",
        f"trusted_owners: {', '.join(payload['trusted_owners'])}",
        f"roles: {', '.join(payload['roles']) if payload['roles'] else '<common-sources-only>'}",
        "checked_paths:",
    ]
    for record in payload["checked_paths"]:
        owner = record.get("owner", "<missing>")
        mode = record.get("mode", "<missing>")
        lines.append(f"- {record['label']}: {record['path']} owner={owner} mode={mode}")
    if payload["blockers"]:
        lines.append("blockers:")
        for blocker in payload["blockers"]:
            lines.append(f"- BLOCKED: {blocker['message']}")
    try:
        atomic_write_bytes_no_follow(
            path,
            ("\n".join(lines) + "\n").encode("utf-8"),
            containment_root=containment_root,
        )
    except SafeFilesystemError as error:
        raise OSError(str(error)) from error


def _preflight_output_target(path: Path, *, containment_root: Path) -> None:
    if path.parent != containment_root:
        raise ValueError("source-trust output target must be directly under the evidence root")
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise OSError(f"cannot inspect source-trust output target {path}: {error}") from error
    if stat.S_ISLNK(status.st_mode):
        raise OSError(f"source-trust output target must not be a symlink: {path}")
    if not stat.S_ISREG(status.st_mode):
        raise OSError(f"source-trust output target must be a regular file: {path}")


def _preflight_output_targets(paths: Sequence[Path], *, containment_root: Path) -> None:
    for path in paths:
        _preflight_output_target(path, containment_root=containment_root)


def write_evidence(
    evidence_root: Path,
    payload: dict[str, Any],
    *,
    output_name: str | None = None,
    checkout_root: Path | None = None,
) -> Path:
    checkout_root = checkout_root or Path(str(payload.get("checkout_root") or "."))
    evidence_root = _approved_evidence_root(evidence_root, checkout_root=checkout_root)
    json_name = output_name or _default_report_json_name(payload)
    if not json_name.endswith(".json") or "/" in json_name or "\\" in json_name:
        raise ValueError("source-trust output name must be a local .json filename")
    json_path = evidence_root / json_name
    text_path = evidence_root / f"{json_path.stem}.txt"
    _preflight_output_targets((json_path, text_path), containment_root=evidence_root)
    _write_text_report(text_path, payload, containment_root=evidence_root)
    _write_json_atomic(json_path, payload, containment_root=evidence_root)
    return json_path


def _default_report_json_name(payload: dict[str, Any]) -> str:
    roles = payload.get("roles")
    if isinstance(roles, list) and len(roles) == 1 and roles[0] in VALID_ROLES:
        return f"two-node-docker-source-trust-{roles[0]}.json"
    return REPORT_JSON_NAME


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    current = path.expanduser()
    for component in (current, *current.parents):
        if component.exists() and component.is_symlink():
            raise ValueError(f"source-trust evidence path component must not be a symlink: {component}")


def _split_csv(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    parsed: list[str] = []
    for value in values:
        parsed.extend(item.strip() for item in value.split(",") if item.strip())
    return parsed


def _parse_roles(values: Sequence[str] | None) -> list[str]:
    roles: list[str] = []
    for role in _split_csv(values):
        if role not in VALID_ROLES:
            raise argparse.ArgumentTypeError(f"unsupported role {role!r}; expected compute or display")
        if role not in roles:
            roles.append(role)
    return roles


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail-closed source trust preflight for two-node Docker operations.")
    parser.add_argument("--checkout-root", type=Path, required=True, help="Repository checkout root containing infra/.")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        required=True,
        help="Directory for source-trust evidence reports.",
    )
    parser.add_argument(
        "--trusted-owner",
        action="append",
        default=[],
        help="Trusted owner allowlist entry. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        help="Role env file to require: compute, display. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--trust-root",
        type=Path,
        default=None,
        help="Path component root to check through CHECKOUT_ROOT/infra; defaults to CHECKOUT_ROOT parent.",
    )
    parser.add_argument(
        "--evidence-run-id",
        help="Current final E2E evidence run id. Defaults to path inference when omitted.",
    )
    parser.add_argument(
        "--output-name",
        help="Optional local JSON report filename. Defaults to role-scoped names for single-role reports.",
    )
    args = parser.parse_args(argv)
    args.trusted_owners = set(_split_csv(args.trusted_owner))
    if args.evidence_run_id is not None:
        try:
            args.evidence_run_id = _safe_evidence_run_id(args.evidence_run_id)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        args.roles = _parse_roles(args.role)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = validate_source_trust(
        checkout_root=args.checkout_root,
        evidence_root=args.evidence_root,
        trusted_owners=args.trusted_owners,
        roles=args.roles,
        trust_root=args.trust_root,
        evidence_run_id=args.evidence_run_id,
    )
    try:
        evidence_path = write_evidence(
            _absolute_path(args.evidence_root),
            payload,
            output_name=args.output_name,
            checkout_root=args.checkout_root,
        )
    except ValueError as exc:
        print(f"BLOCKED: invalid source-trust evidence settings: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"BLOCKED: cannot write source-trust evidence under {args.evidence_root}: {exc}", file=sys.stderr)
        return 2

    for blocker in payload["blockers"]:
        print(f"BLOCKED: {blocker['message']}", file=sys.stderr)
    print(json.dumps({"status": payload["status"], "evidence_path": str(evidence_path)}, sort_keys=True))
    return 2 if payload["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
