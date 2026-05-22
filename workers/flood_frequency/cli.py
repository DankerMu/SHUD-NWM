from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.auth import PolicyDecision, cli_policy_decision_from_evidence, require_policy_evidence
from packages.common.manifest_index import ManifestValidationError, load_manifest_entry, resolve_task_id
from workers.flood_frequency.config import HindcastConfig
from workers.flood_frequency.frequency import FrequencyFitError, fit_curves
from workers.flood_frequency.hindcast import (
    HINDCAST_FORCING_PACKAGE_UNAVAILABLE,
    HindcastError,
    hindcast_status,
    hindcast_year,
    mark_hindcast_runs_failed,
    submit_hindcast,
    submit_hindcast_slurm,
)
from workers.flood_frequency.return_period import ReturnPeriodError, compute_return_periods


def _session_from_env() -> Session:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HindcastError("DATABASE_URL_MISSING", "DATABASE_URL is required for nhms-flood commands.")
    return Session(create_engine(database_url, future=True))


def _hindcast_submit(
    model_id: str,
    source_id: str,
    start_time: str,
    end_time: str,
    purpose: str,
    *,
    policy_decision: PolicyDecision | None = None,
) -> dict[str, object]:
    with _session_from_env() as session:
        result = submit_hindcast(
            model_id,
            source_id,
            start_time,
            end_time,
            purpose,
            session,
            policy_decision=policy_decision,
        )
        years = _years_from_run_ids(result.run_ids)
        config = HindcastConfig.from_env()
        try:
            slurm = submit_hindcast_slurm(
                model_id,
                source_id,
                years,
                HindcastConfig(
                    workspace_root=config.workspace_root,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    slurm_gateway_url=config.slurm_gateway_url,
                    slurm_client=config.slurm_client,
                    db_session=session,
                    era5_required_variables=config.era5_required_variables,
                ),
            )
        except HindcastError as error:
            if error.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE:
                mark_hindcast_runs_failed(session, result.run_ids, error.error_code, error.message)
            raise
        output: dict[str, object] = {
            "total_runs": result.total_runs,
            "run_ids": result.run_ids,
            "skipped_years": result.skipped_years,
            "active_years": result.active_years,
            "slurm_job_array_id": slurm.slurm_job_array_id,
        }
        if policy_decision is not None:
            output["auth_policy_decision"] = policy_decision.to_dict()
        return output


def _hindcast_year(model_id: str, source_id: str, year: int) -> dict[str, object]:
    with _session_from_env() as session:
        result = hindcast_year(model_id, source_id, year, session)
        return {
            "run_id": result.run_id,
            "forcing_version_id": result.forcing_version_id,
            "status": result.status,
            "shud_result": result.shud_result,
            "parse_result": result.parse_result,
        }


def _hindcast_status(model_id: str) -> dict[str, object]:
    with _session_from_env() as session:
        return {"items": hindcast_status(model_id, session)}


def _fit_curves(
    model_id: str,
    segment_id: str | None,
    duration: str | None,
    method: str,
    dry_run: bool,
    supersede_model_id: str | None = None,
    verbose: bool = False,
    policy_decision: PolicyDecision | None = None,
) -> dict[str, object]:
    with _session_from_env() as session:
        result = fit_curves(
            model_id,
            session,
            segment_id=segment_id,
            duration=duration,
            method=method,
            dry_run=dry_run,
            supersede_model_id=supersede_model_id,
            policy_decision=policy_decision,
        )
        output: dict[str, object] = {
            "total_segments": result.total_segments,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "skipped": result.skipped,
        }
        if verbose or result.total_segments <= 20:
            output["items"] = result.items
        if policy_decision is not None:
            output["auth_policy_decision"] = policy_decision.to_dict()
        return output


def _compute_return_period(run_id: str, quality_contract: dict[str, object] | None = None) -> dict[str, object]:
    with _session_from_env() as session:
        result = compute_return_periods(run_id, session, quality_contract=quality_contract)
        return {
            "total_segments": result.total_segments,
            "with_curve": result.with_curve,
            "without_curve": result.without_curve,
            "warning_counts": result.warning_counts,
            "rows_written": result.rows_written,
            "status": result.status,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "quality_state": result.quality_state,
            "unavailable_products": list(result.unavailable_products),
            "residual_blockers": list(result.residual_blockers),
        }


def _call_compute_return_period(
    run_id: str,
    quality_contract: dict[str, object] | None,
) -> dict[str, object]:
    try:
        return _compute_return_period(run_id, quality_contract)
    except TypeError as exc:
        if quality_contract is None:
            return _compute_return_period(run_id)
        raise exc


def _resolve_return_period_request(
    run_id: str | None,
    manifest_index: str | None,
    task_id: int | None,
) -> tuple[str, dict[str, object] | None]:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = load_manifest_entry(manifest_index, resolved_task_id)
        return str(entry["run_id"]), _quality_contract_from_manifest_entry(entry)
    if not run_id:
        raise ManifestValidationError(
            "Explicit return-period computation requires --run-id.",
            {"missing_fields": ["run_id"]},
        )
    return run_id, None


def _resolve_run_id(run_id: str | None, manifest_index: str | None, task_id: int | None) -> str:
    resolved_run_id, _quality_contract = _resolve_return_period_request(run_id, manifest_index, task_id)
    return resolved_run_id


def _quality_contract_from_manifest_entry(entry: dict[str, object]) -> dict[str, object] | None:
    quality_states = entry.get("quality_states")
    if isinstance(quality_states, dict):
        frequency_state = quality_states.get("frequency")
        if isinstance(frequency_state, dict):
            return dict(frequency_state)
    assembly = entry.get("model_run_assembly")
    if isinstance(assembly, dict):
        assembly_quality = assembly.get("quality_states")
        if isinstance(assembly_quality, dict):
            frequency_state = assembly_quality.get("frequency")
            if isinstance(frequency_state, dict):
                return dict(frequency_state)
        frequency_contract = assembly.get("frequency_contract")
        if isinstance(frequency_contract, dict):
            return dict(frequency_contract)
    frequency_contract = entry.get("frequency_contract")
    if isinstance(frequency_contract, dict):
        return dict(frequency_contract)
    return None


def _cli_policy_decision(
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision | None:
    return cli_policy_decision_from_evidence(
        action_id,
        target_type=target_type,
        target_id=target_id,
        actor_id=auth_actor_id,
        roles=auth_roles,
    )


def _require_cli_policy_decision(
    action_id: str,
    *,
    target_type: str,
    target_id: str,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision:
    return require_policy_evidence(
        _cli_policy_decision(
            action_id,
            target_type=target_type,
            target_id=target_id,
            auth_actor_id=auth_actor_id,
            auth_roles=auth_roles,
        ),
        action_id=action_id,
        target_type=target_type,
        target_id=target_id,
    )


def _require_hindcast_submit_cli_policy(
    model_id: str,
    *,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision:
    decision = _require_cli_policy_decision(
        "pipeline.rerun_cycle",
        target_type="hindcast",
        target_id=model_id,
        auth_actor_id=auth_actor_id,
        auth_roles=auth_roles,
    )
    if decision.decision != "allow":
        raise HindcastError(
            decision.reason_code,
            decision.reason,
            {"model_id": model_id, "policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )
    return decision


def _require_supersede_cli_policy(
    supersede_model_id: str,
    *,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision:
    decision = _require_cli_policy_decision(
        "models.supersede",
        target_type="model_instance",
        target_id=supersede_model_id,
        auth_actor_id=auth_actor_id,
        auth_roles=auth_roles,
    )
    if decision.decision != "allow":
        raise FrequencyFitError(
            decision.reason,
            error_code=decision.reason_code,
            details={
                "model_id": supersede_model_id,
                "policy_decision": decision.to_dict(),
                "no_mutation_expected": True,
            },
        )
    return decision


def _add_argparse_auth_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auth-actor-id")
    parser.add_argument("--auth-role", action="append", default=[])


def _years_from_run_ids(run_ids: list[str]) -> list[int]:
    years: list[int] = []
    for run_id in run_ids:
        try:
            years.append(int(run_id.rsplit("_", maxsplit=1)[1]))
        except (IndexError, ValueError):
            continue
    return years


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("hindcast-submit")
    @click.option("--model-id", required=True)
    @click.option("--source-id", default="ERA5", show_default=True)
    @click.option("--start-time", required=True)
    @click.option("--end-time", required=True)
    @click.option("--purpose", default="flood_frequency_sample", show_default=True)
    @click.option("--auth-actor-id", default=None)
    @click.option("--auth-role", multiple=True)
    def hindcast_submit_command(
        model_id: str,
        source_id: str,
        start_time: str,
        end_time: str,
        purpose: str,
        auth_actor_id: str | None,
        auth_role: tuple[str, ...],
    ) -> None:
        try:
            policy_decision = _require_hindcast_submit_cli_policy(
                model_id,
                auth_actor_id=auth_actor_id,
                auth_roles=auth_role,
            )
            click.echo(
                json.dumps(
                    _hindcast_submit(
                        model_id,
                        source_id,
                        start_time,
                        end_time,
                        purpose,
                        policy_decision=policy_decision,
                    ),
                    sort_keys=True,
                )
            )
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("hindcast-year")
    @click.option("--model-id", required=True)
    @click.option("--source-id", default="ERA5", show_default=True)
    @click.option("--year", required=True, type=int)
    def hindcast_year_command(model_id: str, source_id: str, year: int) -> None:
        try:
            click.echo(json.dumps(_hindcast_year(model_id, source_id, year), sort_keys=True))
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("hindcast-status")
    @click.option("--model-id", required=True)
    def hindcast_status_command(model_id: str) -> None:
        try:
            click.echo(json.dumps(_hindcast_status(model_id), sort_keys=True, default=str))
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("fit-curves")
    @click.option("--model-id", required=True)
    @click.option("--segment-id")
    @click.option("--duration", type=click.Choice(["1h", "3h", "6h", "24h", "72h", "7d"]))
    @click.option("--method", type=click.Choice(["P-III", "GEV", "auto"]), default="auto", show_default=True)
    @click.option("--dry-run", is_flag=True)
    @click.option("--supersede-model-id")
    @click.option("--verbose", is_flag=True)
    @click.option("--auth-actor-id", default=None)
    @click.option("--auth-role", multiple=True)
    def fit_curves_command(
        model_id: str,
        segment_id: str | None,
        duration: str | None,
        method: str,
        dry_run: bool,
        supersede_model_id: str | None,
        verbose: bool,
        auth_actor_id: str | None,
        auth_role: tuple[str, ...],
    ) -> None:
        try:
            policy_decision = (
                _require_supersede_cli_policy(
                    supersede_model_id,
                    auth_actor_id=auth_actor_id,
                    auth_roles=auth_role,
                )
                if supersede_model_id and not dry_run
                else None
            )
            click.echo(
                json.dumps(
                    _fit_curves(
                        model_id,
                        segment_id,
                        duration,
                        method,
                        dry_run,
                        supersede_model_id=supersede_model_id,
                        verbose=verbose,
                        policy_decision=policy_decision,
                    ),
                    sort_keys=True,
                )
            )
        except (HindcastError, FrequencyFitError) as error:
            message = getattr(error, "message", str(error))
            code = getattr(error, "error_code", "FREQUENCY_FIT_ERROR")
            click.echo(f"{code}: {message}", err=True)
            raise SystemExit(1) from error

    @cli.command("compute-return-period")
    @click.option("--run-id")
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    def compute_return_period_command(run_id: str | None, manifest_index: str | None, task_id: int | None) -> None:
        try:
            resolved_run_id, quality_contract = _resolve_return_period_request(run_id, manifest_index, task_id)
            click.echo(
                json.dumps(
                    _call_compute_return_period(resolved_run_id, quality_contract),
                    sort_keys=True,
                    default=str,
                )
            )
        except (ManifestValidationError, HindcastError, ReturnPeriodError) as error:
            message = getattr(error, "message", str(error))
            code = getattr(error, "error_code", "RETURN_PERIOD_ERROR")
            click.echo(f"{code}: {message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-flood")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("hindcast-submit")
    submit_parser.add_argument("--model-id", required=True)
    submit_parser.add_argument("--source-id", default="ERA5")
    submit_parser.add_argument("--start-time", required=True)
    submit_parser.add_argument("--end-time", required=True)
    submit_parser.add_argument("--purpose", default="flood_frequency_sample")
    _add_argparse_auth_options(submit_parser)

    year_parser = subparsers.add_parser("hindcast-year")
    year_parser.add_argument("--model-id", required=True)
    year_parser.add_argument("--source-id", default="ERA5")
    year_parser.add_argument("--year", required=True, type=int)

    status_parser = subparsers.add_parser("hindcast-status")
    status_parser.add_argument("--model-id", required=True)

    fit_parser = subparsers.add_parser("fit-curves")
    fit_parser.add_argument("--model-id", required=True)
    fit_parser.add_argument("--segment-id")
    fit_parser.add_argument("--duration", choices=["1h", "3h", "6h", "24h", "72h", "7d"])
    fit_parser.add_argument("--method", choices=["P-III", "GEV", "auto"], default="auto")
    fit_parser.add_argument("--dry-run", action="store_true")
    fit_parser.add_argument("--supersede-model-id")
    fit_parser.add_argument("--verbose", action="store_true")
    _add_argparse_auth_options(fit_parser)

    compute_parser = subparsers.add_parser("compute-return-period")
    compute_parser.add_argument("--run-id")
    compute_parser.add_argument("--manifest-index")
    compute_parser.add_argument("--task-id", type=int, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "hindcast-submit":
            policy_decision = _require_hindcast_submit_cli_policy(
                args.model_id,
                auth_actor_id=args.auth_actor_id,
                auth_roles=args.auth_role,
            )
            result = _hindcast_submit(
                args.model_id,
                args.source_id,
                args.start_time,
                args.end_time,
                args.purpose,
                policy_decision=policy_decision,
            )
            print(json.dumps(result))
            return 0
        if args.command == "hindcast-year":
            print(json.dumps(_hindcast_year(args.model_id, args.source_id, args.year)))
            return 0
        if args.command == "hindcast-status":
            print(json.dumps(_hindcast_status(args.model_id), default=str))
            return 0
        if args.command == "fit-curves":
            policy_decision = (
                _require_supersede_cli_policy(
                    args.supersede_model_id,
                    auth_actor_id=args.auth_actor_id,
                    auth_roles=args.auth_role,
                )
                if args.supersede_model_id and not args.dry_run
                else None
            )
            result = _fit_curves(
                args.model_id,
                args.segment_id,
                args.duration,
                args.method,
                args.dry_run,
                supersede_model_id=args.supersede_model_id,
                verbose=args.verbose,
                policy_decision=policy_decision,
            )
            print(json.dumps(result))
            return 0
        if args.command == "compute-return-period":
            resolved_run_id, quality_contract = _resolve_return_period_request(
                args.run_id,
                args.manifest_index,
                args.task_id,
            )
            print(
                json.dumps(
                    _call_compute_return_period(resolved_run_id, quality_contract),
                    default=str,
                )
            )
            return 0
    except (ManifestValidationError, HindcastError, FrequencyFitError, ReturnPeriodError) as error:
        message = getattr(error, "message", str(error))
        code = getattr(error, "error_code", "FREQUENCY_FIT_ERROR")
        print(f"{code}: {message}", file=sys.stderr)
        return 1
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
