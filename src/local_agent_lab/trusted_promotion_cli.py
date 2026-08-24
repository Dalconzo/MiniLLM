from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

import typer

from .deployment_gate import check_deployment_candidate
from .promotion_controller import HumanAuthorization
from .trusted_promotion import (
    CommandSpec,
    GitCommittedTree,
    HashChainAuditJournal,
    TrustedPromotionAdapter,
    TrustedPromotionConfig,
    TrustedPromotionError,
    load_deployment_candidate,
    write_human_authorization,
)


app = typer.Typer(add_completion=False, help="Root-controlled MiniLLM promotion adapter.")


def _config(repository_dir: Path) -> TrustedPromotionConfig:
    builder_identity = "_minillm_builder"
    production_identity = "_minillm_prod"
    builder_python = Path("/private/var/db/minillm-builder/venv/bin/python")
    production_python = Path("/Library/Application Support/MiniLLM/python-runtime/bin/python")
    production_config = Path("/private/var/db/minillm-production/config/agent.yaml")
    current_source = Path("/Library/Application Support/MiniLLM/releases/current/src")
    return TrustedPromotionConfig(
        repository_dir=repository_dir.resolve(),
        release_root=Path("/Library/Application Support/MiniLLM/releases"),
        control_root=Path("/Library/Application Support/MiniLLM/deployment-control"),
        audit_root=Path("/Library/Application Support/MiniLLM/deployment-control/audit"),
        service_label="com.dalconzo.minillm.production",
        verifier_identity="trusted-release-verifier",
        executor_identity="trusted-production-executor",
        preflight_commands=(
            CommandSpec(
                "full-pytest",
                (
                    "/usr/bin/sudo",
                    "-n",
                    "-u",
                    builder_identity,
                    "--",
                    "/usr/bin/env",
                    f"PYTHONPATH={repository_dir.resolve() / 'src'}",
                    str(builder_python),
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ),
            ),
        ),
        post_smoke_commands=(
            CommandSpec(
                "home-mcp-smoke",
                (
                    "/usr/bin/sudo",
                    "-n",
                    "-u",
                    production_identity,
                    "--",
                    "/usr/bin/env",
                    f"PYTHONPATH={current_source}",
                    f"LAGENT_CONFIG={production_config}",
                    str(production_python),
                    "-m",
                    "local_agent_lab.cli",
                    "home-mcp",
                    "smoke-test",
                    "--url",
                    "http://127.0.0.1:8765/mcp",
                ),
            ),
        ),
    )


def _verification_payload(verification) -> dict[str, object]:
    return {
        "repository": verification.repository,
        "branch": verification.branch,
        "candidate_commit": verification.candidate_commit,
        "checked_out_commit": verification.checked_out_commit,
        "manifest_commit": verification.manifest_commit,
        "worktree_clean": verification.worktree_clean,
        "commit_object_verified": verification.commit_object_verified,
        "manifest_sha256": verification.manifest_sha256,
        "artifacts": [artifact.to_dict() for artifact in verification.artifacts],
        "verified_by": verification.verified_by,
        "verified_at": verification.verified_at,
    }


@app.command("verify")
def verify(
    manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False, readable=True),
    repository_dir: Path = typer.Option(Path.cwd(), "--repository-dir", exists=True, file_okay=False),
) -> None:
    """Inventory a clean committed candidate without changing the host."""

    try:
        candidate = load_deployment_candidate(manifest)
        verification = GitCommittedTree(repository_dir).verify(
            candidate,
            verified_by="trusted-release-verifier",
            verified_at=datetime.now(timezone.utc).isoformat(),
        )
        gate = check_deployment_candidate(candidate)
        status = "ok" if verification.worktree_clean and verification.commit_object_verified and gate.promotable else "blocked"
        typer.echo(
            json.dumps(
                {
                    "status": status,
                    "check_only": True,
                    "host_changes_performed": False,
                    "verification": _verification_payload(verification),
                    "gate": gate.to_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        if status != "ok":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (OSError, ValueError, TrustedPromotionError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc


@app.command("authorize")
def authorize(
    manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False, readable=True),
    repository_dir: Path = typer.Option(Path.cwd(), "--repository-dir", exists=True, file_okay=False),
    action: str = typer.Option("promote", "--action", help="Exact action: promote or rollback."),
    reason: str = typer.Option(..., "--reason", help="Human review rationale."),
    approval_reference: str = typer.Option(..., "--approval-reference", help="Opaque ticket/change reference."),
    expires_minutes: int = typer.Option(30, "--expires-minutes", min=5, max=240),
    yes: bool = typer.Option(False, "--yes", help="Confirm non-interactively; root invocation is still required."),
) -> None:
    """Create a root-owned, build-bound, one-time human authorization."""

    try:
        if action not in {"promote", "rollback"}:
            raise ValueError("action must be promote or rollback")
        if any(token in approval_reference for token in (";", "&&", "||", "|", "`", "$(", "\n", "\r")):
            raise ValueError("approval reference must be opaque data")
        candidate = load_deployment_candidate(manifest)
        verification = GitCommittedTree(repository_dir).verify(
            candidate,
            verified_by="trusted-release-verifier",
            verified_at=datetime.now(timezone.utc).isoformat(),
        )
        gate = check_deployment_candidate(candidate)
        if not verification.worktree_clean or not verification.commit_object_verified or not gate.promotable:
            raise TrustedPromotionError("authorization refused: candidate is not a clean, verified, gate-approved commit")
        if candidate.rollback is None:
            raise TrustedPromotionError("authorization refused: rollback metadata is required")
        if not yes:
            typer.confirm(
                f"Authorize {action} of {candidate.candidate_commit} over {candidate.rollback.previous_build}?",
                abort=True,
            )
        now = datetime.now(timezone.utc)
        authorization = HumanAuthorization(
            authorization_id=f"human-{action}-{uuid.uuid4().hex}",
            actor=os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown-human",
            actor_type="human",
            decision="approve",
            action=action,  # type: ignore[arg-type]
            candidate_commit=candidate.candidate_commit,
            previous_build=candidate.rollback.previous_build,
            manifest_sha256=verification.manifest_sha256,
            authorized_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=expires_minutes)).isoformat(),
            reason=reason,
            approval_reference=approval_reference,
        )
        control_root = Path("/Library/Application Support/MiniLLM/deployment-control")
        path = write_human_authorization(control_root, authorization)
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "authorization_id": authorization.authorization_id,
                    "action": action,
                    "candidate_commit": candidate.candidate_commit,
                    "manifest_sha256": verification.manifest_sha256,
                    "expires_at": authorization.expires_at,
                    "stored_at": str(path),
                    "one_time": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
    except typer.Abort:
        raise typer.Exit(code=1)
    except (OSError, ValueError, TrustedPromotionError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc


@app.command("apply")
def apply(
    manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False, readable=True),
    authorization_id: str = typer.Option(..., "--authorization-id"),
    repository_dir: Path = typer.Option(Path.cwd(), "--repository-dir", exists=True, file_okay=False),
) -> None:
    """Apply one ready promotion under the trusted root adapter."""

    try:
        candidate = load_deployment_candidate(manifest)
        adapter = TrustedPromotionAdapter(_config(repository_dir))
        result = adapter.execute_promotion(candidate, authorization_id=authorization_id)
        payload = {
            "status": result.outcome.status if result.outcome is not None else result.plan.status,
            "operation_id": result.operation_id,
            "candidate_build": result.plan.candidate_build,
            "previous_build": result.plan.previous_build,
            "manifest_sha256": result.plan.manifest_sha256,
            "approval_consumed": result.approval_consumed,
            "release_path": str(result.release_path) if result.release_path else None,
            "rollback_required": result.outcome.rollback_required if result.outcome else False,
            "reasons": [reason.to_dict() for reason in (result.outcome.reasons if result.outcome else result.plan.reasons)],
            "audit_record_sha256": (result.outcome.audit if result.outcome else result.plan.audit).record_sha256,
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if payload["status"] != "completed":
            raise typer.Exit(code=2 if payload["rollback_required"] else 1)
    except typer.Exit:
        raise
    except (OSError, ValueError, TrustedPromotionError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc


@app.command("rollback")
def rollback(
    operation_id: str = typer.Option(..., "--operation-id"),
    authorization_id: str = typer.Option(..., "--authorization-id"),
    repository_dir: Path = typer.Option(Path.cwd(), "--repository-dir", exists=True, file_okay=False),
) -> None:
    """Execute a separately authorized rollback from persisted trusted state."""

    try:
        adapter = TrustedPromotionAdapter(_config(repository_dir))
        promotion = adapter.load_promotion_result(operation_id)
        if promotion.plan.builder_identity != "_minillm_builder" or promotion.plan.production_identity != "_minillm_prod":
            raise TrustedPromotionError("persisted operation identities do not match the production service contract")
        result = adapter.execute_rollback(promotion, authorization_id=authorization_id)
        payload = {
            "status": "completed" if result.completed else result.assessment.status,
            "operation_id": operation_id,
            "target_build": result.target_build,
            "completed": result.completed,
            "restart_result": result.restart_result.to_dict() if result.restart_result else None,
            "smoke_results": [item.to_dict() for item in result.smoke_results],
            "audit_record_sha256": result.assessment.audit.record_sha256,
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if not result.completed:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (OSError, ValueError, TrustedPromotionError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc


@app.command("audit-verify")
def audit_verify(
    audit_root: Path = typer.Option(
        Path("/Library/Application Support/MiniLLM/deployment-control/audit"),
        "--audit-root",
        exists=True,
        file_okay=False,
    ),
) -> None:
    """Verify every link in the trusted append-only promotion journal."""

    try:
        journal = HashChainAuditJournal(audit_root, expected_owner_uid=0)
        records = journal.read_all()
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "records": len(records),
                    "last_record_sha256": records[-1]["record_sha256"] if records else None,
                    "audit_path": str(journal.path),
                },
                indent=2,
                sort_keys=True,
            )
        )
    except (OSError, TrustedPromotionError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2), err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
