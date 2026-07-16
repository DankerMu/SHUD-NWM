"""Assemble the verifier input ``bundle.json`` from a supervisor replay work dir.

Issue #1069 G10.  The independent verifier
(:mod:`scripts.node27_timeseries_compression_live_evidence`) consumes a single
``bundle.json`` whose exact top-level key set and per-artifact
``{"path", "sha256", "bytes"}`` reference shape is defined by
``verify_bundle``.  Before this module existed the bundle had to be
hand-assembled from the supervisor's replay work directory -- a manual,
error-prone "ten-step procedure" that is exactly the kind of gap this lane
must never have.

``build_bundle`` is a pure, deterministic, read-only assembler: it reads the
supervisor run-plan and the append-only ledger, discovers each produced
artifact's on-disk path from the ledger's ``artifact_association`` /
``artifact_associations`` records (the single source of truth for what the
supervisor produced), recomputes every ``{path, sha256, bytes}`` reference
from the exact on-disk bytes, and returns the bundle mapping the verifier
accepts.  It never connects to a database, executes a child, or mutates an
artifact.

The verifier re-derives the invocation references
(``recovery.invocation``, ``migration.first_invocation`` /
``second_invocation``, ``receipts.dry_run_invocation`` /
``enforce_invocation``) from the ledger itself, so in the input bundle those
are all simply the ledger artifact reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import node27_timeseries_compression_live_evidence as evidence

MIGRATION_RELATIVE_PATH = "db/migrations/000047_hypertable_compression_settings.sql"
_STREAM_CHUNK_BYTES = 1024 * 1024


class BundleAuthorError(RuntimeError):
    """A supervisor replay work directory cannot be assembled into a bundle."""


def _stream_ref(path: Path) -> dict[str, Any]:
    """Return ``{path, sha256, bytes}`` hashed from the exact on-disk bytes.

    The digest streams the file so a multi-gigabyte custom-format pg_dump is
    never materialised in memory; the byte count is the ``os.stat`` size.
    """

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": size}


def _ref_for(path_str: str, *, label: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_absolute():
        raise BundleAuthorError(f"{label} path is not absolute: {path_str!r}")
    if not path.is_file() or path.is_symlink():
        raise BundleAuthorError(f"{label} is not a regular non-symlink file: {path_str!r}")
    return _stream_ref(path)


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleAuthorError(f"{label} could not be read as JSON: {error}") from error


def _index_ledger_artifacts(ledger_path: Path) -> dict[str, str]:
    """Map every semantic output name to its on-disk path from the ledger.

    Capture events carry a single ``artifact_association``; child events carry
    a mapping of named associations.  Only association entries that actually
    published an artifact (``{"artifact": {...}}``) are indexed; tool-identity
    associations (``dump_sha256`` etc.) are skipped.
    """

    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BundleAuthorError(f"supervisor ledger could not be read: {error}") from error
    paths: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise BundleAuthorError("supervisor ledger is not append-only JSONL") from error
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("event_type")
        if event_type == "capture":
            association = event.get("artifact_association")
            kind = str(event.get("kind", ""))
            path = _association_path(association)
            if kind and path is not None:
                _register(paths, kind, path)
        elif event_type == "child_exit":
            associations = event.get("artifact_associations")
            if isinstance(associations, Mapping):
                for name, association in associations.items():
                    path = _association_path(association)
                    if path is not None:
                        _register(paths, str(name), path)
    return paths


def _association_path(association: Any) -> str | None:
    if not isinstance(association, Mapping):
        return None
    artifact = association.get("artifact")
    if not isinstance(artifact, Mapping):
        return None
    path = artifact.get("path")
    return str(path) if isinstance(path, str) else None


def _register(paths: dict[str, str], name: str, path: str) -> None:
    existing = paths.get(name)
    if existing is not None and existing != path:
        raise BundleAuthorError(f"ledger records conflicting paths for {name!r}: {existing!r} vs {path!r}")
    paths[name] = path


def _require_path(paths: Mapping[str, str], name: str) -> str:
    try:
        return paths[name]
    except KeyError as error:
        raise BundleAuthorError(f"supervisor ledger has no published artifact for {name!r}") from error


def _authorization(mutation_head_sha: str) -> dict[str, Any]:
    """The issue #1069 bound-1 authorization envelope, sourced from the module.

    The constants are imported rather than copied so the author can never drift
    from the verifier's own pinned envelope.
    """

    return {
        "lag_seconds": evidence.EXPECTED_LAG_SECONDS,
        "bound": evidence.EXPECTED_BOUND,
        "max_selected_bytes": evidence.MAX_SELECTED_BYTES,
        "min_free_bytes": evidence.MIN_FREE_BYTES,
        "timeout_seconds": evidence.EXPECTED_TIMEOUT_SECONDS,
        "enforce_invocations": 1,
        "replay_decompression": True,
        "decompress_invocations": 1,
        "migration_invocations": 2,
        "dry_run_invocations": 1,
        "sole_db_user_during_window": True,
        "database_audit_proof": False,
        "acceptance_claim": evidence.PASS_CLAIM,
        "repo_path": evidence.EXPECTED_REPO_PATH,
        "remote_identity": evidence.EXPECTED_REMOTE_IDENTITY,
        "reviewed_mutation_sha": mutation_head_sha,
        "reviewed_remote_ref": evidence.EXPECTED_REVIEWED_REMOTE_REF,
    }


def build_bundle(
    *,
    work_dir: str | os.PathLike[str],
    repo_path: str | os.PathLike[str],
    run_plan_path: str | os.PathLike[str],
    ledger_path: str | os.PathLike[str],
    schema_dump_path: str | os.PathLike[str],
    mutation_head_sha: str,
    verifier_head_sha: str,
    generated_at: str,
    node: str = "node-27",
) -> dict[str, Any]:
    """Assemble the verifier input bundle from a supervisor replay work dir.

    Pure and deterministic: identical inputs yield an identical bundle.  The
    ``generated_at`` stamp is an explicit argument (never ``datetime.now``)
    so callers -- including the dress rehearsal -- stay reproducible.
    """

    work_dir = Path(work_dir)
    run_plan_path = Path(run_plan_path)
    ledger_path = Path(ledger_path)
    schema_dump_path = Path(schema_dump_path)
    repo_path = Path(repo_path)

    paths = _index_ledger_artifacts(ledger_path)

    ledger_ref = _ref_for(str(ledger_path.resolve() if not ledger_path.is_absolute() else ledger_path), label="ledger")
    run_plan_ref = _ref_for(
        str(run_plan_path.resolve() if not run_plan_path.is_absolute() else run_plan_path),
        label="run_plan",
    )

    preflight_evidence_path = Path(_require_path(paths, "preflight_evidence"))
    preflight_document = _read_json(preflight_evidence_path, label="preflight evidence")
    if not isinstance(preflight_document, Mapping) or "database_identity" not in preflight_document:
        raise BundleAuthorError("preflight evidence document is missing database_identity")
    database_identity = preflight_document["database_identity"]

    migration_file = (repo_path / MIGRATION_RELATIVE_PATH)
    if not migration_file.is_file():
        raise BundleAuthorError(f"reviewed migration file is absent: {migration_file}")

    def ref(name: str) -> dict[str, Any]:
        return _ref_for(_require_path(paths, name), label=name)

    bundle: dict[str, Any] = {
        "schema_version": evidence.SCHEMA_VERSION,
        "issue": evidence.ISSUE,
        "generated_at": generated_at,
        "node": node,
        "mutation_head_sha": mutation_head_sha,
        "verifier_head_sha": verifier_head_sha,
        "database_identity": database_identity,
        "authorization": _authorization(mutation_head_sha),
        "execution": {
            "run_plan": run_plan_ref,
            "ledger": ledger_ref,
        },
        "recovery": {
            "preflight": ref("recovery_preflight"),
            "receipt": ref("recovery_receipt"),
            "invocation": ledger_ref,
        },
        "preflight": {
            "evidence": _ref_for(str(preflight_evidence_path), label="preflight_evidence"),
            "schema_dump": _ref_for(str(schema_dump_path), label="schema_dump"),
            "schema_dump_list": ref("schema_dump_list"),
            "catalog_before": ref("catalog_before"),
        },
        "migration": {
            "migration_file": _stream_ref(migration_file),
            "first_invocation": ledger_ref,
            "catalog_after_first": ref("catalog_after_first"),
            "second_invocation": ledger_ref,
            "catalog_after_second": ref("catalog_after_second"),
        },
        "selection": {
            "post_dry_run": ref("post_dry_selection"),
            "pre_enforce": ref("pre_enforce_selection"),
        },
        "receipts": {
            "dry_run": ref("dry_run_receipt"),
            "dry_run_invocation": ledger_ref,
            "enforce": ref("enforce_receipt"),
            "enforce_invocation": ledger_ref,
        },
        "sizes": {
            "pre": ref("sizes_pre"),
            "post": ref("sizes_post"),
        },
        "catalog": {
            "post": ref("catalog_post"),
        },
        "benchmarks": {
            "evidence": ref("benchmarks"),
        },
        "cleanup": {
            "evidence": ref("cleanup"),
        },
        "out_of_scope": {
            "retention_mutated": False,
            "drill_run": False,
            "node22_touched": False,
            "decompress_run": True,
            "role_mutated": False,
        },
    }
    return bundle


def _atomic_write_json(output_path: Path, document: Mapping[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(output_path.parent),
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, output_path)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--schema-dump", required=True, type=Path)
    parser.add_argument("--mutation-head-sha", required=True)
    parser.add_argument("--verifier-head-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO-8601 Z stamp; defaults to the current UTC instant when omitted.",
    )
    parser.add_argument("--run-plan", type=Path, default=None, help="Defaults to <work-dir>/run-plan.json")
    parser.add_argument(
        "--ledger", type=Path, default=None, help="Defaults to <work-dir>/supervisor-ledger.jsonl"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_plan_path = args.run_plan or (args.work_dir / "run-plan.json")
    ledger_path = args.ledger or (args.work_dir / "supervisor-ledger.jsonl")
    generated_at = args.generated_at
    if generated_at is None:
        from datetime import UTC, datetime

        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bundle = build_bundle(
        work_dir=args.work_dir,
        repo_path=args.repo,
        run_plan_path=run_plan_path,
        ledger_path=ledger_path,
        schema_dump_path=args.schema_dump,
        mutation_head_sha=args.mutation_head_sha,
        verifier_head_sha=args.verifier_head_sha,
        generated_at=generated_at,
    )
    _atomic_write_json(args.output, bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
