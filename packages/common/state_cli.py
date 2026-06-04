from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager, StateManagerError


@dataclass(frozen=True)
class StateRunContext:
    run_id: str
    model_id: str
    end_time: datetime
    output_uri: str | None


class StateRunRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> StateRunRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise StateManagerError("DATABASE_URL is required for state save operations.")
        return cls(database_url)

    def load_run_context(self, run_id: str) -> StateRunContext:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as error:
            raise StateManagerError("psycopg2 is required for state save operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT run_id, model_id, end_time, output_uri
                    FROM hydro.hydro_run
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
            if row is None:
                raise StateManagerError(f"hydro_run not found: {run_id}")
            return StateRunContext(
                run_id=str(row["run_id"]),
                model_id=str(row["model_id"]),
                end_time=_ensure_utc(row["end_time"]),
                output_uri=row.get("output_uri"),
            )
        except psycopg2.Error as error:
            raise StateManagerError(f"Failed to load hydro_run {run_id}: {error}") from error
        finally:
            if connection is not None:
                connection.close()


def save_state_for_run(
    run_id: str,
    *,
    manager: StateManager | None = None,
    repository: StateRunRepository | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root or os.getenv("WORKSPACE_ROOT", ".")).expanduser().resolve()
    object_root = Path(os.getenv("OBJECT_STORE_ROOT", str(workspace))).expanduser().resolve()
    object_prefix = os.getenv("OBJECT_STORE_PREFIX", "")
    state_manager = manager or StateManager(
        repository=PsycopgStateSnapshotRepository.from_env(),
        object_store=LocalObjectStore(object_root, object_prefix),
    )
    run_repository = repository or StateRunRepository.from_env()
    run = run_repository.load_run_context(run_id)
    ic_file = _find_ic_file(run, workspace, LocalObjectStore(object_root, object_prefix))
    # The native SHUD end-of-segment restart artifact is ``*.cfg.ic.update``; the
    # canonical object key is ``state.cfg.ic`` (state_manager._state_object_key). Record
    # the original SHUD filename and key the snapshot at end_time == T_{N+1} so the saved
    # interim state is valid at the next cycle's init time (M24 §2 Lane 2).
    result = state_manager.save_state_snapshot(
        model_id=run.model_id,
        run_id=run.run_id,
        valid_time=run.end_time,
        ic_file_path=ic_file,
        original_shud_filename=ic_file.name,
    )
    qc_passed = state_manager.run_qc(result.state_id)
    return {
        "run_id": run.run_id,
        "state_id": result.state_id,
        "status": result.status,
        "qc_passed": qc_passed,
        "state_uri": result.snapshot.state_uri,
        "checksum": result.snapshot.checksum,
    }


def _find_ic_file(run: StateRunContext, workspace_root: Path, object_store: LocalObjectStore) -> Path:
    # Prefer the native SHUD end-of-segment restart artifact ``*.cfg.ic.update`` (the
    # interim T_{N+1} state written by the restart cadence); fall back to ``*.cfg.ic``.
    update_candidates: list[Path] = []
    ic_candidates: list[Path] = []

    def _collect(root: Path) -> None:
        update_candidates.extend(sorted(p for p in root.rglob("*.cfg.ic.update") if p.is_file()))
        ic_candidates.extend(
            sorted(p for p in root.rglob("*.cfg.ic") if p.is_file() and not p.name.endswith(".cfg.ic.update"))
        )

    workspace_output = workspace_root / "runs" / run.run_id / "output"
    if workspace_output.exists():
        _collect(workspace_output)

    if run.output_uri:
        output_path = object_store.resolve_path(run.output_uri)
        if output_path.is_file():
            if output_path.name.endswith(".cfg.ic.update"):
                update_candidates.append(output_path)
            elif output_path.name.endswith(".cfg.ic"):
                ic_candidates.append(output_path)
        elif output_path.is_dir():
            _collect(output_path)

    if update_candidates:
        return update_candidates[0]
    if ic_candidates:
        return ic_candidates[0]
    raise StateManagerError(f"No .cfg.ic / .cfg.ic.update state file found for run {run.run_id}.")


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("save")
    @click.option("--run-id", required=True)
    def save(run_id: str) -> None:
        try:
            click.echo(json.dumps(save_state_for_run(run_id), sort_keys=True))
        except StateManagerError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "save":
        try:
            print(json.dumps(save_state_for_run(args.run_id), sort_keys=True))
        except StateManagerError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
