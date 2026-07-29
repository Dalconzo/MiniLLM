from __future__ import annotations

import json
import html
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import typer

from .agents.base import render_prompt
from .agents.code_reviewer import (
    build_diff_review_prompt,
    build_file_review_prompt,
    parse_review_response,
    render_review_output,
)
from .agents.function_writer import (
    build_function_writer_prompt,
    parse_function_writer_response,
    render_function_plan,
)
from .agents.log_analyzer import (
    build_log_analysis_prompt,
    parse_log_analysis_response,
    parse_log_text,
    render_log_analysis,
)
from .agents.test_writer import (
    build_test_writer_prompt,
    parse_test_writer_response,
    render_test_plan,
)
from .bake_cam import (
    BakeCamError,
    capture_now as bake_cam_capture_now,
    create_session as bake_cam_create_session,
    health_check as bake_cam_health_check,
    latest_capture as bake_cam_latest_capture,
    list_devices as bake_cam_list_devices,
    list_sessions as bake_cam_list_sessions,
    load_session as bake_cam_load_session,
    schedule_session as bake_cam_schedule_session,
    status_summary as bake_cam_status_summary,
    sync_spooled_captures as bake_cam_sync_spooled_captures,
    write_trace as bake_cam_write_trace,
)
from .config import load_config
from .indexing.repo_indexer import default_db_path, index_repo
from .llm.model_router import route_task
from .llm.ollama_client import OllamaClient, OllamaError
from .logging.run_logger import RunLogger
from .memory.chatgpt_ingest import SCHEMA_VERSION, import_chatgpt_export
from .memory.audit import record_retrieval_event, retrieval_exposures_for_run, tombstone_source
from .memory.analysis import (
    analyze_memory_corpus,
    analyze_memory_patterns,
    render_memory_analysis,
    render_memory_analysis_html,
    render_memory_patterns,
    render_memory_patterns_html,
)
from .memory.candidates import (
    get_candidate_memory,
    list_candidate_memories,
    list_candidate_memories_for_subject,
    list_candidate_subjects,
    update_candidate_review,
)
from .memory.curated import create_memory_record, get_memory_record, list_memory_records, promote_chunk_to_memory_record
from .memory.context_packet import build_context_packet, compact_context_items
from .memory.embeddings import embed_missing_chunks, fallback_model_spec
from .memory.feedback import feedback_summary, list_open_loops, record_memory_feedback
from .memory.eval_checks import run_memory_eval
from .memory.frontdoor import (
    SAFE_MEMORY_ACTIONS,
    build_memory_frontdoor_prompt,
    parse_memory_frontdoor_response,
    render_memory_frontdoor_plan,
)
from .memory.observability import (
    MemoryObservationError,
    MemoryTraceWriter,
    dry_run_chatgpt_ingest,
    list_recent_runs,
    memory_db_path,
    read_memory_trace,
    render_memory_trace,
    summarize_memory_status,
    utc_now,
    validate_memory_state,
)
from .memory.search import search_chatgpt_memory
from .memory.subjects import assign_conversation_subject, init_subject_schema, list_subjects, normalize_subject_slug
from .home_mcp import HomeMCPError, build_home_mcp_server, serve_home_mcp
from .services.home_mcp_launchd import (
    HOME_MCP_HOME_LABEL,
    HOME_MCP_TUNNEL_LABEL,
    install_home_mcp_launchd,
    read_home_mcp_tunnel_url,
    uninstall_home_mcp_launchd,
)
from .tools.file_tools import redact_text
from .tools.git_tools import changed_files_from_diff, git_diff
from .tools.patches import PatchFile, apply_files, build_unified_patch, patch_filename
from .tools.search import fetch_file_chunks, search_index


app = typer.Typer(add_completion=False, help="Local Agent Lab CLI")
home_mcp_app = typer.Typer(add_completion=False, help="Home MCP server and local tools.")
bake_cam_app = typer.Typer(add_completion=False, help="Baking/proofing camera workstation tools.")
app.add_typer(home_mcp_app, name="home-mcp")
app.add_typer(bake_cam_app, name="bake-cam")


def _client_and_logger():
    config = load_config()
    client = OllamaClient(
        host=config.ollama.host,
        timeout_seconds=config.ollama.request_timeout_seconds,
    )
    logger = RunLogger(config.logs_dir)
    return config, client, logger


@app.command()
def health() -> None:
    """Check config, paths, prompt files, and Ollama connectivity."""
    config, client, logger = _client_and_logger()
    run = logger.start("health", {"config": str(config.path)})
    try:
        prompt_files = sorted(str(path.relative_to(config.root_dir)) for path in config.prompts_dir.glob("*.md"))
        status = "ok"
        installed: list[str] = []
        ollama_error = None
        try:
            installed = client.list_models()
        except OllamaError as exc:
            status = "degraded"
            ollama_error = str(exc)
        report = {
            "status": status,
            "config": str(config.path),
            "ollama_host": config.ollama.host,
            "installed_models": len(installed),
            "ollama_error": ollama_error,
            "prompt_files": prompt_files,
            "logs_dir": str(config.logs_dir),
        }
        logger.write_artifact(run, "health.json", json.dumps(report, indent=2))
        logger.finish(run, status=status, result=report)
        typer.echo(json.dumps(report, indent=2))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        raise typer.Exit(code=1) from exc


@app.command()
def models() -> None:
    """Show configured models and whether they are installed in Ollama."""
    config, client, logger = _client_and_logger()
    run = logger.start("models", {})
    try:
        installed = set()
        ollama_error = None
        try:
            installed = set(client.list_models())
        except OllamaError as exc:
            ollama_error = str(exc)
        rows = []
        for profile in config.list_profiles():
            rows.append(
                {
                    "alias": profile.alias,
                    "model": profile.model,
                    "task": profile.task,
                    "routing_label": profile.routing_label,
                    "installed": profile.model in installed,
                    "notes": profile.notes,
                }
            )
        payload = {"models": rows, "ollama_error": ollama_error}
        logger.write_artifact(run, "models.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok" if ollama_error is None else "degraded", result=payload)
        typer.echo(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        raise typer.Exit(code=1) from exc


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question or task for the local model."),
    task: str = typer.Option("chat", "--task", help="Task route to use."),
    model: str | None = typer.Option(None, "--model", help="Override configured model alias."),
    system_file: Path | None = typer.Option(None, "--system-file", exists=True, file_okay=True, dir_okay=False),
    stats: bool = typer.Option(False, "--stats", help="Include routing and token stats."),
) -> None:
    """Run a single prompt through the configured local model."""
    config, client, logger = _client_and_logger()
    run = logger.start(
        "ask",
        {
            "task": task,
            "model": model,
            "question": question,
        },
    )
    try:
        route = route_task(config, task=task, model_alias=model)
        prompt_input = redact_text(question) if config.runtime.redact_before_model else question
        system_prompt_path = system_file or config.prompt_path(task)
        system_prompt = system_prompt_path.read_text(encoding="utf-8").strip()
        prompt = render_prompt(task=task, question=prompt_input)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=system_prompt,
            temperature=route.profile.temperature,
        )
        result = {
            "task": task,
            "routing_label": route.label,
            "model_alias": route.profile.alias,
            "model": route.profile.model,
            "response": response["response"].strip(),
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "total_duration": response.get("total_duration"),
        }
        if config.runtime.save_full_prompts:
            logger.write_artifact(run, "prompt.md", prompt)
        logger.write_artifact(run, "response.md", result["response"])
        logger.write_artifact(run, "result.json", json.dumps(result, indent=2))
        logger.finish(run, status="ok", result=result)
        typer.echo(result["response"])
        if stats:
            typer.echo(json.dumps({k: v for k, v in result.items() if k != "response"}, indent=2))
    except (OllamaError, FileNotFoundError, KeyError) as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("index-repo")
def index_repo_command(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True),
) -> None:
    """Build or refresh a SQLite search index for a repository."""
    config, _client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo)
    run = logger.start("index-repo", {"repo": str(repo), "db_path": str(db_path)})
    try:
        summary = index_repo(repo, db_path)
        payload = summary.to_dict()
        logger.write_artifact(run, "index_repo.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command()
def search(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    query: str = typer.Argument(..., help="Lexical search query to run against the local index."),
    limit: int = typer.Option(8, "--limit", min=1, max=50, help="Maximum number of hits to return."),
) -> None:
    """Search a previously indexed repository using SQLite FTS."""
    config, _client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo)
    run = logger.start("search", {"repo": str(repo), "query": query, "limit": limit, "db_path": str(db_path)})
    try:
        payload = search_index(repo, query, db_path=db_path, limit=limit)
        logger.write_artifact(run, "search.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command()
def review(
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    diff: bool = typer.Option(False, "--diff", help="Review the current git diff for the repository."),
    file: str | None = typer.Option(None, "--file", help="Review a single file relative to the repository root."),
    revision: str | None = typer.Option(None, "--revision", help="Optional git revision/range to review with --diff."),
    stats: bool = typer.Option(False, "--stats", help="Include routing and finding counts."),
) -> None:
    """Review a git diff or a single file using local retrieval and the coding model."""
    if diff == bool(file):
        typer.echo("choose exactly one of --diff or --file", err=True)
        raise typer.Exit(code=1)

    config, client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo)
    run = logger.start(
        "review",
        {"repo": str(repo), "diff": diff, "file": file, "revision": revision, "db_path": str(db_path)},
    )
    try:
        if not db_path.exists():
            index_summary = index_repo(repo, db_path)
            logger.write_artifact(run, "index_repo.json", json.dumps(index_summary.to_dict(), indent=2))

        route = route_task(config, task="review")
        system_prompt = config.prompt_path("review").read_text(encoding="utf-8").strip()

        if diff:
            diff_text = git_diff(repo, revision=revision)
            if not diff_text.strip():
                payload = {
                    "summary": "No diff to review.",
                    "findings": [],
                    "raw_response": "",
                    "routing_label": route.label,
                    "model_alias": route.profile.alias,
                    "model": route.profile.model,
                }
                logger.write_artifact(run, "result.json", json.dumps(payload, indent=2))
                logger.finish(run, status="ok", result=payload)
                typer.echo("No diff to review.")
                return
            changed_files = changed_files_from_diff(diff_text)
            retrieved_context = _collect_review_context(repo, db_path, changed_files)
            review_input = build_diff_review_prompt(
                repo=repo,
                diff_text=redact_text(diff_text) if config.runtime.redact_before_model else diff_text,
                retrieved_context=retrieved_context,
            )
            logger.write_artifact(run, "review.diff", diff_text)
        else:
            relative_path = file or ""
            target = (repo / relative_path).resolve()
            if not target.exists() or not target.is_file():
                raise FileNotFoundError(f"target file does not exist: {target}")
            file_content = target.read_text(encoding="utf-8")
            retrieved_context = _collect_review_context(repo, db_path, [Path(relative_path).as_posix()])
            review_input = build_file_review_prompt(
                repo=repo,
                relative_path=Path(relative_path).as_posix(),
                file_content=redact_text(file_content) if config.runtime.redact_before_model else file_content,
                retrieved_context=retrieved_context,
            )
            logger.write_artifact(run, "review_file.txt", file_content)

        prompt = render_prompt(task="review", question=review_input)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=system_prompt,
            temperature=route.profile.temperature,
        )
        parsed = parse_review_response(response["response"].strip())
        payload = {
            **parsed.to_dict(),
            "routing_label": route.label,
            "model_alias": route.profile.alias,
            "model": route.profile.model,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "total_duration": response.get("total_duration"),
            "retrieved_context": retrieved_context,
        }
        if config.runtime.save_full_prompts:
            logger.write_artifact(run, "prompt.md", prompt)
        logger.write_artifact(run, "retrieved_context.json", json.dumps(retrieved_context, indent=2))
        logger.write_artifact(run, "raw_response.md", response["response"].strip())
        logger.write_artifact(run, "result.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(render_review_output(parsed))
        if stats:
            typer.echo(
                json.dumps(
                    {
                        "routing_label": route.label,
                        "model_alias": route.profile.alias,
                        "model": route.profile.model,
                        "findings": len(parsed.findings),
                        "prompt_eval_count": response.get("prompt_eval_count"),
                        "eval_count": response.get("eval_count"),
                        "total_duration": response.get("total_duration"),
                    },
                    indent=2,
                )
            )
    except (OllamaError, FileNotFoundError, KeyError, RuntimeError, UnicodeDecodeError) as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("write-function")
def write_function(
    spec: Path = typer.Option(..., "--spec", exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    apply: bool = typer.Option(False, "--apply", help="Apply generated files after writing the patch file."),
    stats: bool = typer.Option(False, "--stats", help="Include routing and generation stats."),
) -> None:
    """Generate a small implementation patch and tests from a spec file."""
    config, client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo)
    run = logger.start("write-function", {"repo": str(repo), "spec": str(spec), "db_path": str(db_path), "apply": apply})
    try:
        if not db_path.exists():
            index_summary = index_repo(repo, db_path)
            logger.write_artifact(run, "index_repo.json", json.dumps(index_summary.to_dict(), indent=2))

        spec_text = spec.read_text(encoding="utf-8")
        route = route_task(config, task="write_function")
        system_prompt = config.prompt_path("write_function").read_text(encoding="utf-8").strip()
        retrieved_context = _collect_generation_context(repo, db_path, spec_text)
        prompt_input = build_function_writer_prompt(
            repo=repo,
            spec_text=redact_text(spec_text) if config.runtime.redact_before_model else spec_text,
            retrieved_context=retrieved_context,
        )
        prompt = render_prompt(task="write_function", question=prompt_input)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=system_prompt,
            temperature=route.profile.temperature,
        )
        logger.write_artifact(run, "raw_response.md", response["response"].strip())
        plan = parse_function_writer_response(response["response"].strip())
        files = _validate_generated_files(
            [
                PatchFile(relative_path=plan.target_file, content=plan.implementation),
                PatchFile(relative_path=plan.test_target_file, content=plan.tests),
            ]
        )
        patch_text = build_unified_patch(repo, files)
        patch_path = config.patches_dir / patch_filename("write-function", run.run_id)
        patch_path.write_text(patch_text, encoding="utf-8")
        applied_files = apply_files(repo, files) if apply else []
        payload = {
            **plan.to_dict(),
            "routing_label": route.label,
            "model_alias": route.profile.alias,
            "model": route.profile.model,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "total_duration": response.get("total_duration"),
            "retrieved_context": retrieved_context,
            "patch_path": str(patch_path),
            "applied": apply,
            "applied_files": applied_files,
        }
        if config.runtime.save_full_prompts:
            logger.write_artifact(run, "prompt.md", prompt)
        logger.write_artifact(run, "spec.md", spec_text)
        logger.write_artifact(run, "retrieved_context.json", json.dumps(retrieved_context, indent=2))
        logger.write_artifact(run, "generated.patch", patch_text)
        logger.write_artifact(run, "result.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(render_function_plan(plan, patch_path=patch_path, applied=apply))
        if stats:
            typer.echo(
                json.dumps(
                    {
                        "routing_label": route.label,
                        "model_alias": route.profile.alias,
                        "model": route.profile.model,
                        "patch_path": str(patch_path),
                        "applied": apply,
                        "prompt_eval_count": response.get("prompt_eval_count"),
                        "eval_count": response.get("eval_count"),
                        "total_duration": response.get("total_duration"),
                    },
                    indent=2,
                )
            )
    except (OllamaError, FileNotFoundError, KeyError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("write-tests")
def write_tests(
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    target: str = typer.Option(..., "--target", help="Target file relative to the repository root."),
    apply: bool = typer.Option(False, "--apply", help="Apply generated tests after writing the patch file."),
    stats: bool = typer.Option(False, "--stats", help="Include routing and generation stats."),
) -> None:
    """Generate test patches for a target file."""
    config, client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo)
    run = logger.start("write-tests", {"repo": str(repo), "target": target, "db_path": str(db_path), "apply": apply})
    try:
        if not db_path.exists():
            index_summary = index_repo(repo, db_path)
            logger.write_artifact(run, "index_repo.json", json.dumps(index_summary.to_dict(), indent=2))

        target_path = (repo / target).resolve()
        if not target_path.exists() or not target_path.is_file():
            raise FileNotFoundError(f"target file does not exist: {target_path}")
        file_content = target_path.read_text(encoding="utf-8")
        route = route_task(config, task="write_tests")
        system_prompt = config.prompt_path("write_tests").read_text(encoding="utf-8").strip()
        retrieved_context = _collect_generation_context(repo, db_path, f"{target}\n{file_content}", target_file=target)
        prompt_input = build_test_writer_prompt(
            repo=repo,
            target_file=Path(target).as_posix(),
            file_content=redact_text(file_content) if config.runtime.redact_before_model else file_content,
            retrieved_context=retrieved_context,
        )
        prompt = render_prompt(task="write_tests", question=prompt_input)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=system_prompt,
            temperature=route.profile.temperature,
        )
        logger.write_artifact(run, "raw_response.md", response["response"].strip())
        plan = parse_test_writer_response(response["response"].strip())
        files = _validate_generated_files([PatchFile(relative_path=plan.test_target_file, content=plan.tests)])
        patch_text = build_unified_patch(repo, files)
        patch_path = config.patches_dir / patch_filename("write-tests", run.run_id)
        patch_path.write_text(patch_text, encoding="utf-8")
        applied_files = apply_files(repo, files) if apply else []
        payload = {
            **plan.to_dict(),
            "routing_label": route.label,
            "model_alias": route.profile.alias,
            "model": route.profile.model,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "total_duration": response.get("total_duration"),
            "retrieved_context": retrieved_context,
            "patch_path": str(patch_path),
            "applied": apply,
            "applied_files": applied_files,
        }
        if config.runtime.save_full_prompts:
            logger.write_artifact(run, "prompt.md", prompt)
        logger.write_artifact(run, "target_file.txt", file_content)
        logger.write_artifact(run, "retrieved_context.json", json.dumps(retrieved_context, indent=2))
        logger.write_artifact(run, "generated.patch", patch_text)
        logger.write_artifact(run, "result.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(render_test_plan(plan, patch_path=patch_path, applied=apply))
        if stats:
            typer.echo(
                json.dumps(
                    {
                        "routing_label": route.label,
                        "model_alias": route.profile.alias,
                        "model": route.profile.model,
                        "patch_path": str(patch_path),
                        "applied": apply,
                        "prompt_eval_count": response.get("prompt_eval_count"),
                        "eval_count": response.get("eval_count"),
                        "total_duration": response.get("total_duration"),
                    },
                    indent=2,
                )
            )
    except (OllamaError, FileNotFoundError, KeyError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("explain-log")
def explain_log(
    file: Path = typer.Option(..., "--file", exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    repo: Path | None = typer.Option(
        None, "--repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True, help="Optional repo for context lookup."
    ),
    stats: bool = typer.Option(False, "--stats", help="Include routing and retrieval stats."),
) -> None:
    """Explain a log file or traceback using local models and optional repo retrieval."""
    config, client, logger = _client_and_logger()
    db_path = default_db_path(config.paths["indexes_dir"], repo) if repo is not None else None
    run = logger.start(
        "explain-log",
        {"file": str(file), "repo": str(repo) if repo is not None else None, "db_path": str(db_path) if db_path else None},
    )
    try:
        if repo is not None and db_path is not None and not db_path.exists():
            index_summary = index_repo(repo, db_path)
            logger.write_artifact(run, "index_repo.json", json.dumps(index_summary.to_dict(), indent=2))

        log_text = file.read_text(encoding="utf-8")
        parsed_log = parse_log_text(log_text)
        retrieved_context = _collect_log_context(repo, db_path, parsed_log) if repo is not None and db_path is not None else []
        route = route_task(config, task="log")
        system_prompt = config.prompt_path("log").read_text(encoding="utf-8").strip()
        prompt_input = build_log_analysis_prompt(
            log_file=file,
            log_text=redact_text(log_text) if config.runtime.redact_before_model else log_text,
            parsed_log=parsed_log,
            retrieved_context=retrieved_context,
            repo=repo,
        )
        prompt = render_prompt(task="log", question=prompt_input)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=system_prompt,
            temperature=route.profile.temperature,
        )
        logger.write_artifact(run, "raw_response.md", response["response"].strip())
        analysis = parse_log_analysis_response(response["response"].strip())
        payload = {
            **analysis.to_dict(),
            "parsed_log": parsed_log.to_dict(),
            "routing_label": route.label,
            "model_alias": route.profile.alias,
            "model": route.profile.model,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "total_duration": response.get("total_duration"),
            "retrieved_context": retrieved_context,
        }
        if config.runtime.save_full_prompts:
            logger.write_artifact(run, "prompt.md", prompt)
        logger.write_artifact(run, "log.txt", log_text)
        logger.write_artifact(run, "parsed_log.json", json.dumps(parsed_log.to_dict(), indent=2))
        logger.write_artifact(run, "retrieved_context.json", json.dumps(retrieved_context, indent=2))
        logger.write_artifact(run, "result.json", json.dumps(payload, indent=2))
        logger.finish(run, status="ok", result=payload)
        typer.echo(render_log_analysis(analysis))
        if stats:
            typer.echo(
                json.dumps(
                    {
                        "routing_label": route.label,
                        "model_alias": route.profile.alias,
                        "model": route.profile.model,
                        "frames": len(parsed_log.frames),
                        "retrieved_context": len(retrieved_context),
                        "prompt_eval_count": response.get("prompt_eval_count"),
                        "eval_count": response.get("eval_count"),
                        "total_duration": response.get("total_duration"),
                    },
                    indent=2,
                )
            )
    except (OllamaError, FileNotFoundError, KeyError, RuntimeError, UnicodeDecodeError) as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("ingest-chatgpt")
def ingest_chatgpt(
    input_path: Path = typer.Option(..., "--input", file_okay=True, dir_okay=True, resolve_path=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Inspect export shape and planned writes without importing."),
    trace: bool = typer.Option(False, "--trace", help="Print run_id and trace artifact paths."),
) -> None:
    """Trace-first ChatGPT export ingestion entrypoint."""
    config, _client, logger = _client_and_logger()
    run = logger.start("ingest-chatgpt", {"input": str(input_path), "dry_run": dry_run, "trace": trace})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="ingest-chatgpt",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=memory_db_path(config.paths["memory_dir"]),
        input_paths=[str(input_path)],
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace("discover_input", "Inspecting ChatGPT export input.", source_kind="path", source_ref=str(input_path))
        if not dry_run:
            memory_trace.schema_version = SCHEMA_VERSION
            memory_trace.trace("parse_export", "Parsing ChatGPT export.", source_kind="path", source_ref=str(input_path))
            memory_trace.trace("normalize_conversations", "Normalizing conversations and messages.")
            memory_trace.trace("chunk_messages", "Chunking normalized message text.")
            memory_trace.trace("write_jsonl", "Writing normalized JSONL artifacts.")
            memory_trace.trace("migrate_sqlite", "Ensuring ChatGPT memory SQLite schema.")
            memory_trace.trace("write_sqlite", "Writing normalized records to SQLite.")
            memory_trace.trace("refresh_fts", "Refreshing SQLite FTS rows for imported chunks.")
            report = import_chatgpt_export(
                input_path=input_path,
                data_dir=config.paths["data_dir"],
                memory_dir=config.paths["memory_dir"],
            )
            memory_trace.trace(
                "write_audit",
                "Writing import report.",
                details={"import_id": report["import_id"], **report["summary"]},
            )
            memory_trace.write_json("import_report.json", report)
            for path in report.get("written_files", []):
                if str(path) not in memory_trace.output_paths:
                    memory_trace.output_paths.append(str(path))
            memory_trace.finish(status="ok", result=report)
            output = {"run_id": run.run_id, **report}
            if trace:
                output["trace_path"] = str(run.run_dir / "trace.jsonl")
                output["artifact_dir"] = str(run.run_dir)
            typer.echo(json.dumps(output, indent=2, sort_keys=True))
            return
        report = dry_run_chatgpt_ingest(input_path)
        memory_trace.trace(
            "parse_export",
            "Completed dry-run export inspection.",
            source_kind="path",
            source_ref=str(input_path),
            details=report["summary"],
        )
        memory_trace.trace("write_audit", "Writing dry-run import report.", details={"artifact": "import_report.json"})
        memory_trace.write_json("import_report.json", report)
        status = "ok" if report["status"] == "ok" else "error"
        memory_trace.finish(status=status, result=report, error=_first_report_error(report))
        output = {"run_id": run.run_id, **report}
        if trace:
            output["trace_path"] = str(run.run_dir / "trace.jsonl")
            output["artifact_dir"] = str(run.run_dir)
        typer.echo(json.dumps(output, indent=2, sort_keys=True))
        if status == "error":
            raise typer.Exit(code=1)
    except MemoryObservationError as exc:
        memory_trace.trace(
            exc.stage,
            str(exc),
            level="error",
            source_kind="path",
            source_ref=exc.source_ref,
            details={"error_code": exc.error_code},
        )
        error = exc.to_dict()
        memory_trace.write_json("import_report.json", {"status": "error", "dry_run": dry_run, "error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(
            json.dumps({"run_id": run.run_id, "artifact_dir": str(run.run_dir), "error": error}, indent=2, sort_keys=True),
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("memory-check")
def memory_check(
    json_output: bool = typer.Option(False, "--json", help="Emit the validation report as JSON."),
) -> None:
    """Validate local ChatGPT memory state and write an inspectable report."""
    config, _client, logger = _client_and_logger()
    run = logger.start("memory-check", {})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-check",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=memory_db_path(config.paths["memory_dir"]),
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace("validate_state", "Validating ChatGPT memory state.")
        report = validate_memory_state(data_dir=config.paths["data_dir"], memory_dir=config.paths["memory_dir"])
        memory_trace.write_json("validation_report.json", report)
        memory_trace.trace("write_audit", "Wrote validation report.", details={"status": report["status"]})
        memory_trace.finish(status=report["status"], result=report)
        payload = {"run_id": run.run_id, **report}
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_check(payload))
        if report["status"] == "error":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        error = {"message": str(exc), "stage": "validate_state", "error_code": "invariant_failed", "source_ref": None}
        memory_trace.trace("validate_state", str(exc), level="error", details={"error_code": "invariant_failed"})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "artifact_dir": str(run.run_dir), "error": error}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("memory-status")
def memory_status(
    json_output: bool = typer.Option(False, "--json", help="Emit the status report as JSON."),
    recent_limit: int = typer.Option(5, "--recent-limit", min=1, max=20, help="Number of recent runs to show."),
) -> None:
    """Show a compact local memory health and activity summary."""
    config, _client, logger = _client_and_logger()
    run = logger.start("memory-status", {"recent_limit": recent_limit})
    db_path = memory_db_path(config.paths["memory_dir"])
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-status",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "validate_state",
            "Validating memory status and recent runs.",
            details={
                "memory_dir": str(config.paths["memory_dir"]),
                "logs_dir": str(config.logs_dir),
                "recent_limit": recent_limit,
            },
        )
        report = summarize_memory_status(
            data_dir=config.paths["data_dir"],
            memory_dir=config.paths["memory_dir"],
            logs_dir=config.logs_dir,
            recent_limit=recent_limit,
        )
        payload = {"run_id": run.run_id, **report}
        memory_trace.trace("write_artifact", "Writing memory status artifact.", details={"artifact": "memory_status.json"})
        memory_trace.write_json("memory_status.json", payload)
        memory_trace.finish(status=report["status"], result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_status(payload))
        if report["status"] == "error":
            raise typer.Exit(code=1)
    except Exception as exc:
        memory_trace.trace("status_failed", str(exc), level="error", source_ref=str(db_path), details={"error": str(exc)})
        memory_trace.finish(status="error", result={"error": str(exc)}, error={"message": str(exc), "stage": "status_failed", "source_ref": str(db_path)})
        raise typer.Exit(code=1) from exc


@app.command("memory-analyze")
def memory_analyze(
    json_output: bool = typer.Option(False, "--json", help="Emit the analysis report as JSON."),
    subject_limit: int = typer.Option(10, "--subject-limit", min=1, max=50, help="Number of subjects to surface."),
    recent_limit: int = typer.Option(5, "--recent-limit", min=1, max=20, help="Number of recent runs to show."),
) -> None:
    """Summarize corpus shape and write a browsable HTML dashboard."""
    config, _client, logger = _client_and_logger()
    run = logger.start(
        "memory-analyze",
        {"subject_limit": subject_limit, "recent_limit": recent_limit},
    )
    db_path = memory_db_path(config.paths["memory_dir"])
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-analyze",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "analyze_corpus",
            "Analyzing memory corpus.",
            details={"subject_limit": subject_limit, "recent_limit": recent_limit, "memory_dir": str(config.paths["memory_dir"])},
        )
        report = analyze_memory_corpus(
            data_dir=config.paths["data_dir"],
            memory_dir=config.paths["memory_dir"],
            logs_dir=config.logs_dir,
            subject_limit=subject_limit,
            recent_limit=recent_limit,
        )
        payload = {"run_id": run.run_id, **report}
        memory_trace.trace("write_artifact", "Writing memory analysis artifacts.", details={"artifacts": ["memory_analysis.json", "memory_analysis.html"]})
        memory_trace.write_json("memory_analysis.json", payload)
        memory_trace.logger.write_artifact(memory_trace.run, "memory_analysis.html", render_memory_analysis_html(payload))
        memory_trace.output_paths.append(str(memory_trace.run.run_dir / "memory_analysis.html"))
        memory_trace.finish(status=report["status"], result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(render_memory_analysis(payload))
            typer.echo(f"\nHTML report: {run.run_dir / 'memory_analysis.html'}")
        if report["status"] == "error":
            raise typer.Exit(code=1)
    except Exception as exc:
        memory_trace.trace("analysis_failed", str(exc), level="error", source_ref=str(db_path), details={"error": str(exc)})
        memory_trace.finish(status="error", result={"error": str(exc)}, error={"message": str(exc), "stage": "analysis_failed", "source_ref": str(db_path)})
        raise typer.Exit(code=1) from exc


@app.command("memory-patterns")
def memory_patterns(
    focus: str = typer.Option("all", "--focus", help="Pattern focus: recipes, projects, or all."),
    source_role: str = typer.Option("user", "--source-role", help="Source role to analyze."),
    limit: int = typer.Option(2000, "--limit", min=1, help="Maximum candidates to inspect per slice."),
    category_limit: int = typer.Option(6, "--category-limit", min=1, max=20, help="Maximum natural categories to surface per slice."),
    title_limit: int = typer.Option(20, "--title-limit", min=1, max=50, help="Maximum titles to surface per slice."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Mine candidate memories for natural category patterns."""
    config, _client, logger = _client_and_logger()
    run = logger.start(
        "memory-patterns",
        {
            "focus": focus,
            "source_role": source_role,
            "limit": limit,
            "category_limit": category_limit,
            "title_limit": title_limit,
        },
    )
    db_path = memory_db_path(config.paths["memory_dir"])
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-patterns",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="analyze_patterns",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "analyze_patterns",
            "Mining natural category patterns.",
            details={
                "focus": focus,
                "source_role": source_role,
                "limit": limit,
                "category_limit": category_limit,
                "title_limit": title_limit,
            },
        )
        report = analyze_memory_patterns(
            data_dir=config.paths["data_dir"],
            memory_dir=config.paths["memory_dir"],
            logs_dir=config.logs_dir,
            focus=focus,
            source_role=source_role,
            limit=limit,
            category_limit=category_limit,
            title_limit=title_limit,
        )
        payload = {"run_id": run.run_id, **report}
        memory_trace.trace("render_output", "Rendering memory pattern report.", details={"focus": focus, "pattern_sets": len(report.get("pattern_sets", []))})
        memory_trace.trace("write_artifact", "Writing memory pattern artifacts.", details={"artifacts": ["memory_patterns.json", "memory_patterns.html"]})
        memory_trace.write_json("memory_patterns.json", payload)
        memory_trace.logger.write_artifact(memory_trace.run, "memory_patterns.html", render_memory_patterns_html(payload))
        memory_trace.output_paths.append(str(memory_trace.run.run_dir / "memory_patterns.html"))
        memory_trace.finish(status=report["status"], result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(render_memory_patterns(payload))
            typer.echo(f"\nHTML report: {run.run_dir / 'memory_patterns.html'}")
        if report["status"] == "error":
            raise typer.Exit(code=1)
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "analyze_patterns", "error_code": "memory_patterns_failed", "source_ref": str(db_path)}
            stage = "analyze_patterns"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-assist")
def memory_assist(
    request: str = typer.Argument(..., help="Natural-language request to route through the AI frontdoor."),
    execute: bool = typer.Option(False, "--execute", help="Execute the selected safe action after planning."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
    model: str | None = typer.Option(None, "--model", help="Override configured model alias."),
) -> None:
    """Use the local model to choose a memory command, then optionally execute it."""
    config, client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-assist",
        {"request": request, "execute": execute, "model": model, "db_path": str(db_path)},
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-assist",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        state = _memory_frontdoor_state(config, logger, recent_limit=5, subject_limit=8)
        memory_trace.trace(
            "collect_state",
            "Collected memory corpus snapshot for routing.",
            details={
                "counts": state.get("counts", {}),
                "top_subjects": len(state.get("top_subjects", [])),
                "candidate_subjects": len(state.get("candidate_subjects", [])),
            },
        )
        route = route_task(config, task="chat", model_alias=model)
        prompt = build_memory_frontdoor_prompt(user_request=request, corpus_state=state)
        response = client.generate(
            model=route.profile.model,
            prompt=prompt,
            system=(
                "You are the front door for the local memory system. "
                "Choose one safe action and return strict JSON only."
            ),
            temperature=0.1,
        )
        plan = parse_memory_frontdoor_response(response["response"].strip())
        memory_trace.trace(
            "plan_request",
            "AI selected a memory action.",
            details={"action": plan.action, "confidence": plan.confidence, "needs_confirmation": plan.needs_confirmation},
        )

        execution: dict[str, object] | None = None
        if execute:
            memory_trace.trace("execute_action", "Executing planned memory action.", details={"action": plan.action})
            execution = _execute_memory_assist_plan(plan, config=config, logger=logger, memory_trace=memory_trace)
        else:
            memory_trace.trace("execute_action", "Execution skipped; planning only.", details={"action": plan.action})

        payload = {
            "status": "ok",
            "run_id": run.run_id,
            "request": request,
            "plan": plan.to_dict(),
            "executed": execute,
            "execution": execution,
            "state": state,
            "model": {
                "alias": route.profile.alias,
                "name": route.profile.model,
                "routing_label": route.label,
            },
        }
        memory_trace.trace("render_output", "Rendering memory frontdoor output.", details={"action": plan.action, "executed": execute})
        memory_trace.write_json("memory_assist.json", payload)
        memory_trace.logger.write_artifact(memory_trace.run, "memory_assist.html", _render_memory_assist_html(payload))
        memory_trace.output_paths.append(str(memory_trace.run.run_dir / "memory_assist.html"))
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_assist(payload))
            typer.echo(f"\nHTML report: {run.run_dir / 'memory_assist.html'}")
    except (MemoryObservationError, OllamaError, sqlite3.Error, ValueError, KeyError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "plan_request", "error_code": "memory_assist_failed", "source_ref": str(db_path)}
            stage = "plan_request"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-runs")
def memory_runs(
    json_output: bool = typer.Option(False, "--json", help="Emit recent runs as JSON."),
    limit: int = typer.Option(10, "--limit", min=1, max=50, help="Number of recent runs to show."),
) -> None:
    """List recent local agent runs from the trace directory."""
    config, _client, logger = _client_and_logger()
    run = logger.start("memory-runs", {"limit": limit})
    try:
        runs = list_recent_runs(config.logs_dir, limit=limit)
        payload = {"status": "ok", "run_id": run.run_id, "count": len(runs), "recent_runs": runs}
        logger.write_artifact(run, "recent_runs.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_recent_runs(payload))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        raise typer.Exit(code=1) from exc


@app.command("memory-search")
def memory_search(
    query: str = typer.Argument(..., help="Query to run against ChatGPT memory."),
    limit: int = typer.Option(8, "--limit", min=1, max=50, help="Maximum number of hits to return."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject filter."),
    title: str | None = typer.Option(None, "--title", help="Optional conversation title filter."),
    date_from: str | None = typer.Option(None, "--date-from", help="Inclusive ISO timestamp/date lower bound."),
    date_to: str | None = typer.Option(None, "--date-to", help="Inclusive ISO timestamp/date upper bound."),
    exclude_source: str | None = typer.Option(None, "--exclude-source", help="Comma-separated conversation/message/chunk IDs to exclude."),
    exclude_subject: str | None = typer.Option(None, "--exclude-subject", help="Comma-separated subject names/slugs to exclude."),
    depth: str = typer.Option("medium", "--depth", help="Disclosure depth: far, medium, close, or full."),
    effort: int = typer.Option(2, "--effort", min=1, max=5, help="Retrieval effort level, 1-5."),
    allow_cross_domain: bool = typer.Option(False, "--allow-cross-domain", help="Allow explicit cross-domain expansion."),
    explain: bool = typer.Option(False, "--explain", help="Write score/candidate explanation artifacts."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Trace-aware ChatGPT memory search entrypoint."""
    config, _client, logger = _client_and_logger()
    run = logger.start("memory-search", {"query": query, "explain": explain})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-search",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=memory_db_path(config.paths["memory_dir"]),
    )
    try:
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "retrieve_candidates",
            "Searching ChatGPT memory FTS index.",
            details={
                "query": query,
                "limit": limit,
                "subject": subject,
                "title": title,
                "date_from": date_from,
                "date_to": date_to,
                "exclude_source": exclude_source,
                "exclude_subject": exclude_subject,
                "depth": depth,
                "effort": effort,
                "allow_cross_domain": allow_cross_domain,
            },
        )
        payload = search_chatgpt_memory(
            memory_dir=config.paths["memory_dir"],
            query=query,
            limit=limit,
            subject=subject,
            title=title,
            date_from=date_from,
            date_to=date_to,
            exclude_source_ids=_comma_values(exclude_source),
            exclude_subjects=_comma_values(exclude_subject),
            depth=depth,
            effort=effort,
            allow_cross_domain=allow_cross_domain,
        )
        payload["run_id"] = run.run_id
        with sqlite3.connect(memory_db_path(config.paths["memory_dir"])) as connection:
            audit = record_retrieval_event(
                connection,
                run_id=run.run_id,
                query=query,
                command="memory-search",
                filters=payload["filters_applied"],
                ranking_profile=payload["ranking_profile"],
                disclosure_depth=depth,
                results=payload["results"],
            )
        payload["retrieval_event_id"] = audit["retrieval_event_id"]
        memory_trace.trace(
            "rank_results",
            "Ranked ChatGPT memory results.",
            details=payload["candidate_counts"],
        )
        memory_trace.trace(
            "apply_disclosure",
            "Applied requested disclosure depth.",
            details={"results": payload["count"]},
        )
        memory_trace.write_json("search.json", payload)
        if explain:
            memory_trace.write_json("search_explain.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output or explain:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_search(payload))
    except MemoryObservationError as exc:
        memory_trace.trace(
            exc.stage,
            str(exc),
            level="error",
            source_ref=exc.source_ref,
            details={"error_code": exc.error_code},
        )
        error = exc.to_dict()
        payload = {"status": "error", "query": query, "run_id": run.run_id, "error": error}
        if explain:
            memory_trace.write_json("search_explain.json", payload)
        memory_trace.finish(status="error", result=payload, error=error)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)
    except ValueError as exc:
        error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "invalid_depth", "source_ref": None}
        memory_trace.trace("retrieve_candidates", str(exc), level="error", details={"error": error})
        payload = {"status": "error", "query": query, "run_id": run.run_id, "error": error}
        if explain:
            memory_trace.write_json("search_explain.json", payload)
        memory_trace.finish(status="error", result=payload, error=error)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-context")
def memory_context(
    query: str = typer.Argument(..., help="Task or question to build a memory context pack for."),
    depth: str = typer.Option("medium", "--depth", help="Disclosure depth: far, medium, close, or full."),
    limit: int = typer.Option(6, "--limit", min=1, max=20, help="Maximum context items."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject filter."),
    effort: int = typer.Option(2, "--effort", min=1, max=5, help="Retrieval effort level, 1-5."),
    allow_cross_domain: bool = typer.Option(False, "--allow-cross-domain", help="Allow explicit cross-domain expansion."),
    explain: bool = typer.Option(False, "--explain", help="Write context/ranking explanation artifacts."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Build an agent-facing memory context pack with provenance and controlled disclosure."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-context",
        {
            "query": query,
            "depth": depth,
            "limit": limit,
            "subject": subject,
            "effort": effort,
            "allow_cross_domain": allow_cross_domain,
            "explain": explain,
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-context",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        memory_trace.trace("retrieve_candidates", "Retrieving memory context candidates.", details={"query": query})
        result = search_chatgpt_memory(
            memory_dir=config.paths["memory_dir"],
            query=query,
            limit=limit,
            subject=subject,
            depth=depth,
            effort=effort,
            allow_cross_domain=allow_cross_domain,
        )
        context_items = compact_context_items(result["results"])
        with sqlite3.connect(db_path) as connection:
            audit = record_retrieval_event(
                connection,
                run_id=run.run_id,
                query=query,
                command="memory-context",
                filters=result["filters_applied"],
                ranking_profile=result["ranking_profile"],
                disclosure_depth=depth,
                results=result["results"],
            )
        context_packet = build_context_packet(
            query=query,
            retrieval_event_id=audit["retrieval_event_id"],
            search_result=result,
            context_items=context_items,
        )
        payload = {
            "status": "ok",
            "run_id": run.run_id,
            "retrieval_event_id": audit["retrieval_event_id"],
            "context_packet_id": context_packet["context_packet_id"],
            "query": query,
            "depth": depth,
            "ranking_profile": result["ranking_profile"],
            "domain_detection": result["domain_detection"],
            "filters_applied": result["filters_applied"],
            "governance": result["governance"],
            "lenses": result["lenses"],
            "candidate_counts": result["candidate_counts"],
            "context_items": context_items,
            "context_packet": context_packet,
        }
        memory_trace.write_json("context_pack.json", payload)
        if explain:
            memory_trace.write_json("context_explain.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output or explain:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_context(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "memory_context_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        payload = {"status": "error", "query": query, "run_id": run.run_id, "error": error}
        if explain:
            memory_trace.write_json("context_explain.json", payload)
        memory_trace.finish(status="error", result=payload, error=error)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-trace")
def memory_trace_command(
    run_id: str = typer.Argument(..., help="Run ID to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Emit trace as JSON."),
) -> None:
    """Show command metadata, artifacts, and trace timeline for a memory run."""
    config, _client, _logger = _client_and_logger()
    try:
        trace = read_memory_trace(config.logs_dir, run_id)
        if json_output:
            typer.echo(json.dumps(trace, indent=2, sort_keys=True))
        else:
            typer.echo(render_memory_trace(trace))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@app.command("memory-embed")
def memory_embed(
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum chunks to embed."),
    dimension: int = typer.Option(64, "--dimension", min=1, max=4096, help="Fallback embedding dimension."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Build deterministic local embeddings for imported ChatGPT chunks."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-embed", {"limit": limit, "dimension": dimension, "db_path": str(db_path)})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-embed",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="embed_chunks",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace("embed_chunks", "Embedding chunks that are missing or stale.", details={"limit": limit})
        with sqlite3.connect(db_path) as connection:
            report = embed_missing_chunks(connection, spec=fallback_model_spec(dimension=dimension), limit=limit)
        report["run_id"] = run.run_id
        report["sqlite_path"] = str(db_path)
        output_report = _compact_memory_embed_report(report)
        memory_trace.write_json("embedding_report.json", output_report)
        memory_trace.finish(status="ok", result=output_report)
        if json_output:
            typer.echo(json.dumps(output_report, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_embed(output_report))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "embed_chunks", "error_code": "embedding_failed", "source_ref": str(db_path)}
            stage = "embed_chunks"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-subjects")
def memory_subjects(
    kind: str | None = typer.Option(None, "--kind", help="Optional kind: subject, project, or workflow."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum subjects to list."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List ChatGPT memory subjects with counts and recency."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-subjects", {"kind": kind, "limit": limit, "db_path": str(db_path)})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-subjects",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="validate_state",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace("retrieve_candidates", "Listing memory subjects.", details={"kind": kind, "limit": limit})
        with sqlite3.connect(db_path) as connection:
            subjects = [summary.to_dict() for summary in list_subjects(connection, kind=kind, limit=limit)]
        payload = {"status": "ok", "run_id": run.run_id, "count": len(subjects), "subjects": subjects}
        memory_trace.write_json("subjects.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_subjects(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "subject_listing_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-assign-subject")
def memory_assign_subject(
    conversation_id: str = typer.Argument(..., help="Conversation ID to label."),
    subject_name: str = typer.Argument(..., help="Subject, project, or workflow name."),
    kind: str = typer.Option("subject", "--kind", help="subject, project, or workflow."),
    include_chunks: bool = typer.Option(True, "--include-chunks/--no-include-chunks", help="Also label existing chunks."),
    confidence: float = typer.Option(1.0, "--confidence", min=0.0, max=1.0, help="Assignment confidence."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Assign or correct a subject label for an imported conversation."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-assign-subject",
        {
            "conversation_id": conversation_id,
            "subject_name": subject_name,
            "kind": kind,
            "include_chunks": include_chunks,
            "confidence": confidence,
            "db_path": str(db_path),
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-assign-subject",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="write_sqlite",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "write_sqlite",
            "Assigning subject to conversation.",
            record_id=conversation_id,
            details={"subject": subject_name, "kind": kind, "include_chunks": include_chunks},
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            init_subject_schema(connection)
            row = connection.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row is None:
                raise MemoryObservationError(
                    f"conversation not found: {conversation_id}",
                    stage="write_sqlite",
                    error_code="conversation_not_found",
                    source_ref=conversation_id,
                )
            subject = assign_conversation_subject(
                connection,
                conversation_id,
                subject_name,
                kind=kind,
                confidence=confidence,
                source="manual",
                include_chunks=include_chunks,
            )
        payload = {"status": "ok", "run_id": run.run_id, "conversation_id": conversation_id, "subject": subject.to_dict()}
        memory_trace.write_json("subject_assignment.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"Assigned {subject.kind}:{subject.slug} to {conversation_id}\nrun_id: {run.run_id}")
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "write_sqlite", "error_code": "subject_assignment_failed", "source_ref": str(db_path)}
            stage = "write_sqlite"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-block-source")
def memory_block_source(
    source_id: str = typer.Argument(..., help="Conversation, message, or chunk ID to block."),
    source_kind: str = typer.Option("chatgpt_export", "--source-kind", help="Source kind label for the tombstone."),
    reason: str = typer.Option("blocked_by_user", "--reason", help="Reason stored in the tombstone."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Tombstone a source so future memory retrieval excludes it by default."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-block-source", {"source_id": source_id, "source_kind": source_kind, "reason": reason})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-block-source",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="write_sqlite",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("write_sqlite", "Writing source tombstone.", record_id=source_id)
        with sqlite3.connect(db_path) as connection:
            tombstone = tombstone_source(connection, source_kind=source_kind, source_id=source_id, reason=reason)
        payload = {"status": "ok", "run_id": run.run_id, "tombstone": tombstone}
        memory_trace.write_json("tombstone.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"Blocked {source_id}\nrun_id: {run.run_id}")
    except (MemoryObservationError, sqlite3.Error) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "write_sqlite", "error_code": "block_source_failed", "source_ref": str(db_path)}
            stage = "write_sqlite"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-promote")
def memory_promote(
    chunk_id: str = typer.Argument(..., help="Chunk ID to promote into curated memory."),
    record_type: str = typer.Option(..., "--type", help="Curated memory type, e.g. decision, preference, workflow."),
    title: str | None = typer.Option(None, "--title", help="Optional title override."),
    trust_level: str = typer.Option("medium", "--trust", help="low, medium, high, or canonical."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Promote an imported ChatGPT chunk into curated working memory."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-promote", {"chunk_id": chunk_id, "record_type": record_type, "title": title})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-promote",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="write_sqlite",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("write_sqlite", "Promoting chunk to curated memory.", record_id=chunk_id)
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            record = promote_chunk_to_memory_record(
                connection,
                chunk_id,
                record_type=record_type,
                title=title,
                trust_level=trust_level,
                created_by="user",
            )
        payload = {"status": "ok", "run_id": run.run_id, "memory_record": record.to_dict()}
        memory_trace.write_json("memory_record.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"Promoted {chunk_id} -> {record.id}\nrun_id: {run.run_id}")
    except (MemoryObservationError, sqlite3.Error, KeyError, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "write_sqlite", "error_code": "memory_promotion_failed", "source_ref": chunk_id}
            stage = "write_sqlite"
            source_ref = chunk_id
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-show")
def memory_show(
    memory_id: str = typer.Argument(..., help="Curated memory record ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show a curated memory record with provenance."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-show", {"memory_id": memory_id})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-show",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            record = get_memory_record(connection, memory_id)
        payload = {"status": "ok", "run_id": run.run_id, "memory_record": record.to_dict()}
        memory_trace.write_json("memory_record.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_record(payload))
    except (MemoryObservationError, sqlite3.Error, KeyError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "memory_record_not_found", "source_ref": memory_id}
            stage = "retrieve_candidates"
            source_ref = memory_id
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-list")
def memory_list(
    record_type: str | None = typer.Option(None, "--type", help="Optional curated memory type filter."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum records to list."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List active curated memory records."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-list", {"record_type": record_type, "limit": limit})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-list",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "retrieve_candidates",
            "Loading curated memory records.",
            details={"record_type": record_type, "limit": limit},
        )
        with sqlite3.connect(db_path) as connection:
            records = [record.to_dict() for record in list_memory_records(connection, record_type=record_type, limit=limit)]
        payload = {"status": "ok", "run_id": run.run_id, "count": len(records), "memory_records": records}
        memory_trace.trace("render_output", "Rendering curated memory records.", details={"count": len(records)})
        memory_trace.write_json("memory_records.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_records(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "memory_list_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-candidates")
def memory_candidates(
    review_status: str | None = typer.Option("pending", "--status", help="Optional review status filter."),
    domain: str | None = typer.Option(None, "--domain", help="Optional domain filter."),
    source_role: str | None = typer.Option(None, "--source-role", help="Optional source role filter."),
    assistant_only: bool = typer.Option(False, "--assistant-only", help="Only show assistant-suggested candidates."),
    quality_filter: str = typer.Option("all", "--quality-filter", help="Candidate quality filter: all, user_only, or high_signal."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum candidates to list."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List candidate memories extracted from ChatGPT exports."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-candidates",
        {
            "review_status": review_status,
            "domain": domain,
            "source_role": source_role,
            "assistant_only": assistant_only,
            "quality_filter": quality_filter,
            "limit": limit,
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-candidates",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "retrieve_candidates",
            "Loading candidate memories.",
            details={
                "review_status": review_status,
                "domain": domain,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "quality_filter": quality_filter,
                "limit": limit,
            },
        )
        with sqlite3.connect(db_path) as connection:
            candidates = [
                candidate.to_dict()
                for candidate in list_candidate_memories(
                    connection,
                    review_status=review_status,
                    domain=domain,
                    source_role=source_role,
                    assistant_suggestion=True if assistant_only else None,
                    quality_filter=quality_filter,
                    limit=limit,
                )
            ]
        payload = {
            "status": "ok",
            "run_id": run.run_id,
            "count": len(candidates),
            "candidate_memories": candidates,
            "filters": {
                "review_status": review_status,
                "domain": domain,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "quality_filter": quality_filter,
                "limit": limit,
            },
        }
        memory_trace.trace("render_output", "Rendering candidate memories.", details={"count": len(candidates)})
        memory_trace.write_json("candidate_memories.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_candidate_memories(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "candidate_list_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-review")
def memory_review(
    candidate_id: str | None = typer.Option(None, "--candidate-id", help="Specific candidate ID to review."),
    action: str | None = typer.Option(None, "--action", help="Review action: approve, reject, or promote."),
    review_status: str | None = typer.Option("pending", "--status", help="Optional status filter."),
    domain: str | None = typer.Option(None, "--domain", help="Optional domain filter."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject filter."),
    subject_kind: str = typer.Option("subject", "--subject-kind", help="Subject kind: subject, project, or workflow."),
    source_role: str | None = typer.Option(None, "--source-role", help="Optional source role filter."),
    assistant_only: bool = typer.Option(False, "--assistant-only", help="Only show assistant-suggested candidates."),
    quality_filter: str = typer.Option("all", "--quality-filter", help="Candidate quality filter: all, user_only, or high_signal."),
    limit: int | None = typer.Option(20, "--limit", min=1, help="Maximum candidates to list."),
    note: str | None = typer.Option(None, "--note", help="Optional review note."),
    record_type: str | None = typer.Option(None, "--record-type", help="Curated record type to create on promote."),
    title: str | None = typer.Option(None, "--title", help="Override title when promoting."),
    trust_level: str = typer.Option("high", "--trust-level", help="Trust level when promoting."),
    allow_assistant: bool = typer.Option(False, "--allow-assistant", help="Allow promoting assistant suggestions."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Inspect candidate memories and optionally approve, reject, or promote them."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-review",
        {
            "candidate_id": candidate_id,
            "action": action,
            "review_status": review_status,
            "domain": domain,
            "subject": subject,
            "subject_kind": subject_kind,
            "source_role": source_role,
            "assistant_only": assistant_only,
            "quality_filter": quality_filter,
            "limit": limit,
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-review",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )

        memory_trace.trace(
            "load_config",
            "Loaded local agent configuration.",
            details={"config": str(config.path), "subject": subject, "subject_kind": subject_kind},
        )
        memory_trace.trace(
            "retrieve_candidates",
            "Loading candidate queue.",
            details={
                "review_status": review_status,
                "domain": domain,
                "subject": subject,
                "subject_kind": subject_kind,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "quality_filter": quality_filter,
                "limit": limit,
            },
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            if candidate_id is not None:
                memory_trace.trace("load_candidate", "Loading candidate by ID.", record_id=candidate_id)
                candidate = get_candidate_memory(connection, candidate_id)
                selected = [candidate]
            else:
                selected = list_candidate_memories(
                    connection,
                    review_status=review_status,
                    domain=domain,
                    source_role=source_role,
                    assistant_suggestion=True if assistant_only else None,
                    subject=subject,
                    subject_kind=subject_kind,
                    quality_filter=quality_filter,
                    limit=limit,
                )

            if action is None:
                candidates = [candidate.to_dict() for candidate in selected]
                payload = {
                    "status": "ok",
                    "run_id": run.run_id,
                    "count": len(candidates),
                    "candidate_memories": candidates,
                    "filters": {
                        "review_status": review_status,
                        "domain": domain,
                        "subject": subject,
                        "subject_kind": subject_kind,
                        "source_role": source_role,
                        "assistant_only": assistant_only,
                        "quality_filter": quality_filter,
                        "limit": limit,
                    },
                }
            else:
                if candidate_id is None:
                    raise ValueError("--candidate-id is required when using --action")
                candidate = selected[0]
                memory_trace.trace("apply_review_action", "Applying review action.", record_id=candidate.id, details={"action": action})
                if action == "approve":
                    updated = update_candidate_review(
                        connection,
                        candidate.id,
                        review_status="approved",
                        review_notes=note,
                        last_confirmed_at=utc_now(),
                    )
                    payload = {"status": "ok", "run_id": run.run_id, "candidate_memory": updated.to_dict()}
                elif action == "reject":
                    updated = update_candidate_review(
                        connection,
                        candidate.id,
                        review_status="rejected",
                        review_notes=note,
                    )
                    payload = {"status": "ok", "run_id": run.run_id, "candidate_memory": updated.to_dict()}
                elif action == "promote":
                    if candidate.assistant_suggestion and not allow_assistant:
                        raise MemoryObservationError(
                            "assistant suggestions stay separate until explicit confirmation",
                            stage="write_sqlite",
                            error_code="assistant_promotion_blocked",
                            source_ref=candidate.id,
                        )
                    promoted = _promote_candidate_memory(
                        connection,
                        candidate,
                        record_type=record_type,
                        title=title,
                        trust_level=trust_level,
                        note=note,
                    )
                    updated = update_candidate_review(
                        connection,
                        candidate.id,
                        review_status="merged",
                        review_notes=note,
                        last_confirmed_at=utc_now(),
                    )
                    payload = {
                        "status": "ok",
                        "run_id": run.run_id,
                        "candidate_memory": updated.to_dict(),
                        "memory_record": promoted.to_dict(),
                    }
                else:
                    raise ValueError(f"invalid review action: {action}")

        memory_trace.trace(
            "render_output",
            "Rendering candidate review payload.",
            details={"action": action, "count": len(payload.get("candidate_memories", []))},
        )
        memory_trace.write_json("candidate_review.json", payload)
        if action is None:
            memory_trace.write_json("candidate_review_queue.json", payload)
        else:
            memory_trace.write_json("candidate_review_action.json", payload)
        memory_trace.logger.write_artifact(memory_trace.run, "candidate_review.html", _render_candidate_review_html(payload))
        memory_trace.output_paths.append(str(memory_trace.run.run_dir / "candidate_review.html"))
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_candidate_review(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError, KeyError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "candidate_review_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-review-subjects")
def memory_review_subjects(
    subject: str | None = typer.Option(None, "--subject", help="Optional subject to drill into."),
    kind: str = typer.Option("subject", "--kind", help="Subject kind: subject, project, or workflow."),
    review_status: str | None = typer.Option("pending", "--status", help="Optional status filter."),
    source_role: str | None = typer.Option(None, "--source-role", help="Optional source role filter."),
    assistant_only: bool = typer.Option(False, "--assistant-only", help="Only show assistant-suggested candidates."),
    quality_filter: str = typer.Option("high_signal", "--quality-filter", help="Candidate quality filter: all, user_only, or high_signal."),
    subject_limit: int = typer.Option(20, "--subject-limit", min=1, help="Maximum subjects to list."),
    candidate_limit: int = typer.Option(20, "--candidate-limit", min=1, help="Maximum candidates to show for a selected subject."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Browse candidate memories by subject with traceable drill-down."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-review-subjects",
        {
            "subject": subject,
            "kind": kind,
            "review_status": review_status,
            "source_role": source_role,
            "assistant_only": assistant_only,
            "quality_filter": quality_filter,
            "subject_limit": subject_limit,
            "candidate_limit": candidate_limit,
            "db_path": str(db_path),
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-review-subjects",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="validate_state",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        memory_trace.trace("load_config", "Loaded local agent configuration.", details={"config": str(config.path)})
        memory_trace.trace(
            "retrieve_subjects",
            "Loading subject summaries for candidate review.",
            details={
                "subject": subject,
                "kind": kind,
                "review_status": review_status,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "quality_filter": quality_filter,
                "subject_limit": subject_limit,
                "candidate_limit": candidate_limit,
            },
        )
        effective_quality_filter = "all" if assistant_only and quality_filter == "high_signal" else quality_filter
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            subject_summaries = [
                summary
                for summary in list_candidate_subjects(
                    connection,
                    review_status=review_status,
                    source_role=source_role,
                    assistant_suggestion=True if assistant_only else None,
                    quality_filter=effective_quality_filter,
                    kind=kind,
                    limit=subject_limit,
                )
            ]
            selected_subject = None
            candidate_memories: list[dict[str, object]] = []
            if subject is not None:
                memory_trace.trace("retrieve_candidates", "Loading subject-scoped candidate memories.", details={"subject": subject, "kind": kind})
                candidate_memories = [
                    candidate.to_dict()
                    for candidate in list_candidate_memories_for_subject(
                        connection,
                        subject,
                        kind=kind,
                        review_status=review_status,
                        source_role=source_role,
                        assistant_suggestion=True if assistant_only else None,
                        quality_filter=effective_quality_filter,
                        limit=candidate_limit,
                    )
                ]
                selected_subject = next(
                    (
                        summary
                        for summary in subject_summaries
                        if summary["kind"] == kind and summary["slug"] == _subject_slug(subject)
                    ),
                    None,
                )
                if selected_subject is None:
                    selected_subject = {
                        "kind": kind,
                        "slug": _subject_slug(subject),
                        "name": subject,
                        "candidate_count": len(candidate_memories),
                        "pending_count": len(candidate_memories) if review_status == "pending" else 0,
                        "approved_count": 0,
                        "merged_count": 0,
                        "rejected_count": 0,
                        "assistant_count": sum(1 for item in candidate_memories if item["assistant_suggestion"]),
                        "latest_candidate_activity_at": None,
                    }

        payload = {
            "status": "ok",
            "run_id": run.run_id,
            "filters": {
                "subject": subject,
                "kind": kind,
                "review_status": review_status,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "quality_filter": quality_filter,
                "effective_quality_filter": effective_quality_filter,
                "subject_limit": subject_limit,
                "candidate_limit": candidate_limit,
            },
            "count": len(subject_summaries),
            "subject_summaries": subject_summaries,
            "selected_subject": selected_subject,
            "candidate_memories": candidate_memories,
        }
        memory_trace.trace(
            "render_output",
            "Rendering subject review payload.",
            details={"subject": subject, "subject_count": len(subject_summaries), "candidate_count": len(candidate_memories)},
        )
        memory_trace.write_json("subject_review.json", payload)
        memory_trace.logger.write_artifact(memory_trace.run, "subject_review.html", _render_subject_review_html(payload))
        memory_trace.output_paths.append(str(memory_trace.run.run_dir / "subject_review.html"))
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_subject_review(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError, KeyError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_subjects", "error_code": "subject_review_failed", "source_ref": str(db_path)}
            stage = "retrieve_subjects"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-feedback")
def memory_feedback(
    source_kind: str = typer.Option(..., "--source-kind", help="Source kind to annotate."),
    source_id: str = typer.Option(..., "--source-id", help="Source ID to annotate."),
    rating: str = typer.Option(..., "--rating", help="Feedback rating: up, down, saved, ignored, or resolved."),
    memory_record_id: str | None = typer.Option(None, "--memory-record-id", help="Optional curated memory record ID."),
    run_id: str | None = typer.Option(None, "--run-id", help="Optional retrieval run ID."),
    query: str | None = typer.Option(None, "--query", help="Optional query associated with the feedback."),
    note: str | None = typer.Option(None, "--note", help="Optional note."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Record feedback on a retrieved or curated memory item."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start(
        "memory-feedback",
        {
            "source_kind": source_kind,
            "source_id": source_id,
            "rating": rating,
            "memory_record_id": memory_record_id,
            "run_id": run_id,
            "query": query,
        },
    )
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-feedback",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="write_sqlite",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            feedback = record_memory_feedback(
                connection,
                source_kind=source_kind,
                source_id=source_id,
                rating=rating,
                memory_record_id=memory_record_id,
                run_id=run_id,
                query=query,
                note=note,
            )
            payload = {
                "status": "ok",
                "run_id": run.run_id,
                "feedback": feedback.to_dict(),
            }
            if memory_record_id is not None:
                payload["memory_record"] = get_memory_record(connection, memory_record_id).to_dict()
                payload["feedback_summary"] = feedback_summary(connection, memory_record_id=memory_record_id)
        memory_trace.write_json("memory_feedback.json", payload)
        memory_trace.finish(status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_memory_feedback(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "write_sqlite", "error_code": "feedback_failed", "source_ref": str(db_path)}
            stage = "write_sqlite"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-open-loops")
def memory_open_loops(
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum loops to list."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List active open-loop memory records."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-open-loops", {"limit": limit})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-open-loops",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            loops = list_open_loops(connection, limit=limit)
        payload = {"status": "ok", "run_id": run.run_id, "count": len(loops), "open_loops": loops}
        memory_trace.write_json("open_loops.json", payload)
        memory_trace.finish(status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_open_loops(payload))
    except (MemoryObservationError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "open_loop_list_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-audit")
def memory_audit(
    run_id: str = typer.Argument(..., help="Memory retrieval run ID to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show what memory sources were exposed for a retrieval run."""
    config, _client, logger = _client_and_logger()
    db_path = memory_db_path(config.paths["memory_dir"])
    run = logger.start("memory-audit", {"target_run_id": run_id})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-audit",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=db_path,
    )
    try:
        if not db_path.exists():
            raise MemoryObservationError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="retrieve_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            from .memory.audit import init_audit_schema

            init_audit_schema(connection)
            row = connection.execute("SELECT 1 FROM retrieval_events WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise MemoryObservationError(
                    f"retrieval run not found: {run_id}",
                    stage="retrieve_candidates",
                    error_code="retrieval_event_not_found",
                    source_ref=run_id,
                )
            exposures = retrieval_exposures_for_run(connection, run_id)
        payload = {"status": "ok", "run_id": run.run_id, "target_run_id": run_id, "count": len(exposures), "exposures": exposures}
        memory_trace.write_json("audit_exposures.json", payload)
        memory_trace.finish(status="ok", result=payload)
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_audit(payload))
    except (MemoryObservationError, sqlite3.Error) as exc:
        if isinstance(exc, MemoryObservationError):
            error = exc.to_dict()
            stage = exc.stage
            source_ref = exc.source_ref
        else:
            error = {"message": str(exc), "stage": "retrieve_candidates", "error_code": "audit_lookup_failed", "source_ref": str(db_path)}
            stage = "retrieve_candidates"
            source_ref = str(db_path)
        memory_trace.trace(stage, str(exc), level="error", source_ref=source_ref, details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


@app.command("memory-eval")
def memory_eval(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Run synthetic ChatGPT memory quality and privacy checks."""
    config, _client, logger = _client_and_logger()
    run = logger.start("memory-eval", {})
    memory_trace = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-eval",
        argv=sys.argv[1:],
        config_path=config.path,
        sqlite_path=memory_db_path(config.paths["memory_dir"]),
    )
    try:
        memory_trace.trace("validate_state", "Running synthetic memory eval checks.")
        with tempfile.TemporaryDirectory(prefix="lagent-memory-eval-") as temp_dir:
            report = run_memory_eval(Path(temp_dir))
        report["run_id"] = run.run_id
        memory_trace.write_json("memory_eval.json", report)
        status = "ok" if report["status"] == "pass" else "error"
        memory_trace.finish(status=status, result=report)
        if json_output:
            typer.echo(json.dumps(report, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_eval(report))
        if status != "ok":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        error = {"message": str(exc), "stage": "validate_state", "error_code": "memory_eval_failed", "source_ref": None}
        memory_trace.trace("validate_state", str(exc), level="error", details={"error": error})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "error": error}, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1)


def _collect_review_context(repo: Path, db_path: Path, relative_paths: list[str]) -> list[dict[str, object]]:
    snippets: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for relative_path in relative_paths:
        for chunk in fetch_file_chunks(repo, relative_path, db_path=db_path, limit=3):
            key = (str(chunk["relative_path"]), int(chunk["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(
                {
                    "relative_path": chunk["relative_path"],
                    "chunk_index": chunk["chunk_index"],
                    "snippet": str(chunk["content"])[:280],
                }
            )
        basename = Path(relative_path).stem
        if not basename:
            continue
        results = search_index(repo, basename, db_path=db_path, limit=2)
        for hit in results["hits"]:
            key = (str(hit["relative_path"]), int(hit["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(hit)
    return snippets[:8]


def _collect_generation_context(
    repo: Path,
    db_path: Path,
    source_text: str,
    *,
    target_file: str | None = None,
) -> list[dict[str, object]]:
    snippets: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    if target_file:
        for chunk in fetch_file_chunks(repo, Path(target_file).as_posix(), db_path=db_path, limit=3):
            key = (str(chunk["relative_path"]), int(chunk["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(
                {
                    "relative_path": chunk["relative_path"],
                    "chunk_index": chunk["chunk_index"],
                    "snippet": str(chunk["content"])[:320],
                }
            )
    for query in _keyword_queries(source_text):
        results = search_index(repo, query, db_path=db_path, limit=3)
        for hit in results["hits"]:
            key = (str(hit["relative_path"]), int(hit["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(hit)
            if len(snippets) >= 8:
                return snippets
    return snippets


def _collect_log_context(
    repo: Path,
    db_path: Path,
    parsed_log,
) -> list[dict[str, object]]:
    snippets: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()

    for frame in parsed_log.frames[-4:]:
        relative_path = _frame_relative_path(repo, frame.file)
        if relative_path is None:
            continue
        for chunk in fetch_file_chunks(repo, relative_path, db_path=db_path, limit=2):
            key = (str(chunk["relative_path"]), int(chunk["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(
                {
                    "relative_path": chunk["relative_path"],
                    "chunk_index": chunk["chunk_index"],
                    "snippet": str(chunk["content"])[:320],
                }
            )

    query_source = " ".join(
        part
        for part in [
            parsed_log.error_type or "",
            parsed_log.error_message or "",
            " ".join(frame.function for frame in parsed_log.frames[-3:]),
            " ".join(Path(frame.file).stem for frame in parsed_log.frames[-3:]),
        ]
        if part
    )
    for query in _keyword_queries(query_source):
        results = search_index(repo, query, db_path=db_path, limit=2)
        for hit in results["hits"]:
            key = (str(hit["relative_path"]), int(hit["chunk_index"]))
            if key in seen:
                continue
            seen.add(key)
            snippets.append(hit)
            if len(snippets) >= 8:
                return snippets
    return snippets[:8]


def _frame_relative_path(repo: Path, frame_file: str) -> str | None:
    try:
        frame_path = Path(frame_file)
        if frame_path.is_absolute():
            return frame_path.resolve().relative_to(repo.resolve()).as_posix()
        candidate = (repo / frame_path).resolve()
        return candidate.relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _keyword_queries(source_text: str) -> list[str]:
    stopwords = {
        "about",
        "after",
        "before",
        "from",
        "into",
        "that",
        "this",
        "with",
        "write",
        "function",
        "tests",
        "test",
        "should",
        "using",
        "small",
        "spec",
    }
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", source_text)
    seen: set[str] = set()
    queries: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in stopwords or lowered in seen:
            continue
        seen.add(lowered)
        queries.append(token)
        if len(queries) >= 4:
            break
    return queries or ["test"]


def _validate_generated_files(files: list[PatchFile]) -> list[PatchFile]:
    validated: list[PatchFile] = []
    seen_paths: set[str] = set()
    for file in files:
        relative_path = file.relative_path.strip().lstrip("/")
        if not relative_path:
            raise ValueError("model response did not include a target path")
        normalized = Path(relative_path).as_posix()
        if normalized.startswith("../") or normalized == "..":
            raise ValueError(f"refusing to write outside repo: {normalized}")
        if normalized in seen_paths:
            raise ValueError(f"duplicate generated path: {normalized}")
        seen_paths.add(normalized)
        validated.append(PatchFile(relative_path=normalized, content=file.content))
    return validated


def _first_report_error(report: dict[str, object]) -> dict[str, object] | None:
    for item in report.get("conversation_files", []):
        if isinstance(item, dict) and item.get("error"):
            return item["error"]
    return None


def _render_memory_check(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    lines = [
        f"Status: {payload['status']}",
        f"Run ID: {payload['run_id']}",
        f"SQLite: {payload['sqlite_path']}",
        f"Checks: {summary['checks']} ({summary['errors']} errors, {summary['warnings']} warnings)",
        "Results:",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['status']} {check['name']}: {check['message']}")
    return "\n".join(lines)


def _render_memory_status(payload: dict[str, object]) -> str:
    validation = payload["validation"]
    sqlite_info = payload["sqlite"]
    counts = sqlite_info.get("counts", {})
    latest_import = sqlite_info.get("latest_import")
    lines = [
        f"Status: {payload['status']}",
        f"Run ID: {payload['run_id']}",
        f"Checked: {payload['checked_at']}",
        f"Data: {payload['data_dir']}",
        f"Memory DB: {payload['memory_dir']}",
        f"Validation: {validation['summary']['errors']} errors, {validation['summary']['warnings']} warnings",
        "Counts:",
        f"- imports={counts.get('imports', 0)} conversations={counts.get('conversations', 0)} messages={counts.get('messages', 0)} chunks={counts.get('message_chunks', 0)}",
        f"- candidates={counts.get('candidate_memories', 0)} curated={counts.get('memory_records', 0)} subjects={counts.get('subjects', 0)} embeddings={counts.get('chunk_embeddings', 0)}",
        f"- retrievals={counts.get('retrieval_events', 0)} exposures={counts.get('retrieval_exposures', 0)}",
    ]
    embedding_coverage = sqlite_info.get("embedding_coverage")
    if isinstance(embedding_coverage, dict):
        active_model = embedding_coverage.get("active_model")
        model_label = "none"
        if isinstance(active_model, dict):
            model_label = f"{active_model.get('provider')}:{active_model.get('model')} ({active_model.get('dimension')}d)"
        lines.extend(
            [
                "Semantic Embeddings:",
                f"- status={embedding_coverage.get('status')} coverage={float(embedding_coverage.get('coverage_ratio', 0.0)):.1%} embedded={embedding_coverage.get('embedded_chunks', 0)}/{embedding_coverage.get('total_chunks', 0)} missing={embedding_coverage.get('missing_chunks', 0)} stale={embedding_coverage.get('stale_chunks', 0)}",
                f"- active_model={model_label}",
            ]
        )
    if latest_import:
        lines.extend(
            [
                "Latest Import:",
                f"- {latest_import['id']} [{latest_import['status']}] {latest_import['conversation_count']} conversations / {latest_import['message_count']} messages / {latest_import['chunk_count']} chunks",
                f"- source={latest_import['source_root']}",
                f"- imported_at={latest_import['imported_at']}",
            ]
        )
    lines.append("Recent Runs:")
    recent_runs = payload.get("recent_runs", [])
    if not recent_runs:
        lines.append("- none")
    else:
        for item in recent_runs:
            lines.append(
                f"- {item['run_id']} [{item.get('status')}] {item.get('command')} "
                f"started={item.get('started_at')} finished={item.get('finished_at')}"
            )
    return "\n".join(lines)


def _render_recent_runs(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Recent runs: {payload['count']}"]
    for item in payload["recent_runs"]:
        lines.append(
            f"- {item['run_id']} [{item.get('status')}] {item.get('command')} "
            f"started={item.get('started_at')} finished={item.get('finished_at')}"
        )
    return "\n".join(lines)


def _render_memory_search(payload: dict[str, object]) -> str:
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Results: {payload['count']}",
    ]
    for result in payload["results"]:
        role = result.get("role") or result.get("source_role") or result.get("record_type") or "unknown"
        lines.append(
            f"{result['rank']}. {result['title']} [{role}] "
            f"{result['chunk_id']} score={result['score']:.4f}"
        )
        snippet = result.get("snippet")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def _render_memory_feedback(payload: dict[str, object]) -> str:
    feedback = payload["feedback"]
    lines = [
        f"Feedback: {feedback['rating']} for {feedback['source_kind']}:{feedback['source_id']}",
        f"Run ID: {payload['run_id']}",
    ]
    if "memory_record" in payload:
        record = payload["memory_record"]
        lines.append(f"Record: {record['id']} [{record['record_type']}/{record['status']}]")
    return "\n".join(lines)


def _render_open_loops(payload: dict[str, object]) -> str:
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Open loops: {payload['count']}",
    ]
    for loop in payload["open_loops"]:
        lines.append(f"- {loop['id']} [{loop['status']}] {loop['title']}")
    return "\n".join(lines)


def _context_item(result: dict[str, object]) -> dict[str, object]:
    payload = {
        "source_kind": result["source_kind"],
        "source_id": result["chunk_id"],
        "conversation_id": result.get("conversation_id"),
        "message_id": result.get("message_id"),
        "title": result["title"],
        "score": result["score"],
        "score_breakdown": result["score_breakdown"],
        "disclosure_tier": result["disclosure_tier"],
        "exposed_fields": result["exposed_fields"],
    }
    if "snippet" in result:
        payload["snippet"] = result["snippet"]
    return payload


def _render_memory_context(payload: dict[str, object]) -> str:
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Retrieval event: {payload['retrieval_event_id']}",
        f"Depth: {payload['depth']}",
        "Context:",
    ]
    for item in payload["context_items"]:
        lines.append(f"- {item['source_kind']}:{item['source_id']} score={item['score']}")
        snippet = item.get("snippet")
        if snippet:
            lines.append(f"  {item['title']}: {snippet}")
    return "\n".join(lines)


def _render_memory_embed(payload: dict[str, object]) -> str:
    return "\n".join(
        [
            f"Run ID: {payload['run_id']}",
            f"Model: {payload['provider']}/{payload['model']} ({payload['dimension']} dims)",
            f"Chunks considered: {payload['chunks_considered']}",
            f"Embeddings written: {payload['embeddings_written']}",
        ]
    )


def _compact_memory_embed_report(report: dict[str, object], *, sample_size: int = 20) -> dict[str, object]:
    compact = dict(report)
    vector_refs = compact.get("vector_refs")
    if isinstance(vector_refs, list):
        compact["vector_ref_count"] = len(vector_refs)
        if len(vector_refs) > sample_size:
            compact["vector_refs_sample"] = vector_refs[:sample_size]
            compact["vector_refs_truncated"] = True
            compact.pop("vector_refs", None)
        else:
            compact["vector_refs_truncated"] = False
    return compact


def _render_memory_subjects(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Subjects: {payload['count']}"]
    for subject in payload["subjects"]:
        lines.append(
            f"- {subject['kind']}:{subject['slug']} {subject['name']} "
            f"conversations={subject['conversation_count']} chunks={subject['chunk_count']}"
        )
    return "\n".join(lines)


def _render_memory_audit(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Target run: {payload['target_run_id']}", f"Exposures: {payload['count']}"]
    for exposure in payload["exposures"]:
        lines.append(
            f"- {exposure['rank']}. {exposure['source_kind']}:{exposure['source_id']} "
            f"tier={exposure['disclosure_tier']} redacted={exposure['redacted_secret_count']}"
        )
    return "\n".join(lines)


def _render_memory_record(payload: dict[str, object]) -> str:
    record = payload["memory_record"]
    return "\n".join(
        [
            f"Run ID: {payload['run_id']}",
            f"{record['id']} [{record['record_type']}/{record['trust_level']}/{record['status']}]",
            record["title"],
            record["body"],
            f"Source: {record['source_kind']} {record['source_ref']}",
        ]
    )


def _render_memory_records(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Records: {payload['count']}"]
    for record in payload["memory_records"]:
        lines.append(f"- {record['id']} [{record['record_type']}/{record['trust_level']}] {record['title']}")
    return "\n".join(lines)


def _render_candidate_memories(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Candidates: {payload['count']}"]
    for candidate in payload["candidate_memories"]:
        lines.append(
            f"- {candidate['id']} [{candidate['review_status']}] {candidate['domain_primary']} "
            f"{candidate['memory_type']} ({candidate['reason_type']})"
        )
        lines.append(
            f"  source={candidate['source_ref']} role={candidate['source_role']} confidence={candidate['confidence']}"
        )
        lines.append(f"  content={candidate['content'][:160]}")
    return "\n".join(lines)


def _render_candidate_review(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}"]
    if "memory_record" in payload and "candidate_memory" in payload:
        candidate = payload["candidate_memory"]
        record = payload["memory_record"]
        lines.extend(
            [
                f"Promoted: {candidate['id']} -> {record['id']}",
                f"Candidate status: {candidate['review_status']}",
                f"Record: {record['record_type']} [{record['status']}] {record['title']}",
            ]
        )
        return "\n".join(lines)

    lines.append(f"Candidates: {payload['count']}")
    for candidate in payload["candidate_memories"]:
        lines.append(
            f"- {candidate['id']} [{candidate['review_status']}] {candidate['domain_primary']} "
            f"{candidate['memory_type']} score={candidate['confidence']:.2f}"
        )
        lines.append(f"  {candidate['content']}")
    return "\n".join(lines)


def _render_candidate_review_html(payload: dict[str, object]) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    cards = []
    if "memory_record" in payload and "candidate_memory" in payload:
        candidate = payload["candidate_memory"]
        record = payload["memory_record"]
        cards.append(
            f"<div class='card'><h2>Promoted</h2><p><code>{esc(candidate['id'])}</code> -> <code>{esc(record['id'])}</code></p>"
            f"<p>Status: {esc(candidate['review_status'])}</p><p>Record: {esc(record['record_type'])} / {esc(record['status'])}</p>"
            f"<pre>{esc(record['body'])}</pre></div>"
        )
    else:
        for candidate in payload.get("candidate_memories", []):
            cards.append(
                f"<div class='card'><h2>{esc(candidate['id'])}</h2>"
                f"<p>{esc(candidate['review_status'])} | {esc(candidate['domain_primary'])} | {esc(candidate['memory_type'])}</p>"
                f"<p class='muted'>score {candidate['confidence']:.2f} | source {esc(candidate['source_ref'])}</p>"
                f"<pre>{esc(candidate['content'])}</pre></div>"
            )
    return f"""
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1' />
        <title>Memory Review</title>
        <style>
          body {{ margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f2ea; color: #1d1a16; }}
          .card {{ background: #fffdf8; border: 1px solid #d7cdbf; border-radius: 14px; padding: 16px; margin-bottom: 14px; box-shadow: 0 10px 20px rgba(68, 52, 35, 0.05); }}
          h1, h2 {{ margin: 0 0 8px; }}
          .muted {{ color: #6b6257; }}
          code, pre {{ background: #f1e7d9; border-radius: 8px; }}
          pre {{ padding: 12px; white-space: pre-wrap; }}
        </style>
      </head>
      <body>
        <h1>Memory Review</h1>
        <p class='muted'>Run {esc(payload['run_id'])}</p>
        <p>Count: {esc(payload.get('count', 0))}</p>
        {''.join(cards) if cards else '<p>No candidates.</p>'}
      </body>
    </html>
    """.strip()


def _render_subject_review(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}"]
    filters = payload.get("filters", {})
    filter_bits = [
        f"{key}={filters.get(key)}"
        for key in ("subject", "kind", "review_status", "source_role", "assistant_only")
        if filters.get(key) is not None
    ]
    if filter_bits:
        lines.append("Filters: " + ", ".join(filter_bits))
    subject = payload.get("selected_subject")
    if subject:
        lines.extend(
            [
                f"Subject: {subject['kind']}:{subject['slug']} {subject['name']}",
                f"Candidates: {subject.get('candidate_count', 0)} pending={subject.get('pending_count', 0)} assistant={subject.get('assistant_count', 0)}",
            ]
        )
    lines.append(f"Subjects: {payload.get('count', 0)}")
    for item in payload.get("subject_summaries", []):
        lines.append(
            f"- {item['kind']}:{item['slug']} {item['name']} "
            f"candidates={item.get('candidate_count', 0)} pending={item.get('pending_count', 0)} "
            f"approved={item.get('approved_count', 0)} merged={item.get('merged_count', 0)} rejected={item.get('rejected_count', 0)}"
        )
    candidates = payload.get("candidate_memories", [])
    if candidates:
        lines.append("Candidates:")
        for candidate in candidates:
            lines.append(
                f"- {candidate['id']} [{candidate['review_status']}] {candidate['domain_primary']} {candidate['memory_type']} score={candidate['confidence']:.2f}"
            )
            lines.append(f"  source={candidate['source_ref']} role={candidate['source_role']}")
            lines.append(f"  content={candidate['content'][:160]}")
    elif subject:
        lines.append("Candidates: 0")
    return "\n".join(lines)


def _render_subject_review_html(payload: dict[str, object]) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    filters = payload.get("filters", {})
    subject = payload.get("selected_subject")
    subject_cards = []
    for item in payload.get("subject_summaries", []):
        subject_cards.append(
            f"<div class='card'><h2>{esc(item['kind'])}:{esc(item['slug'])}</h2>"
            f"<p>{esc(item['name'])}</p>"
            f"<p class='muted'>candidates {item.get('candidate_count', 0)} | pending {item.get('pending_count', 0)} | approved {item.get('approved_count', 0)} | merged {item.get('merged_count', 0)} | rejected {item.get('rejected_count', 0)}</p>"
            f"</div>"
        )
    candidate_cards = []
    for candidate in payload.get("candidate_memories", []):
        candidate_cards.append(
            f"<div class='card'><h2>{esc(candidate['id'])}</h2>"
            f"<p>{esc(candidate['review_status'])} | {esc(candidate['domain_primary'])} | {esc(candidate['memory_type'])}</p>"
            f"<p class='muted'>score {candidate['confidence']:.2f} | source {esc(candidate['source_ref'])}</p>"
            f"<pre>{esc(candidate['content'])}</pre></div>"
        )
    selected_subject_html = ""
    if subject:
        selected_subject_html = (
            f"<div class='card'><h2>{esc(subject['kind'])}:{esc(subject['slug'])}</h2>"
            f"<p>{esc(subject['name'])}</p>"
            f"<p class='muted'>candidates {subject.get('candidate_count', 0)} | pending {subject.get('pending_count', 0)} | assistant {subject.get('assistant_count', 0)}</p>"
            f"</div>"
        )
    return f"""
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1' />
        <title>Memory Subject Review</title>
        <style>
          body {{ margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f2ea; color: #1d1a16; }}
          .card {{ background: #fffdf8; border: 1px solid #d7cdbf; border-radius: 14px; padding: 16px; margin-bottom: 14px; box-shadow: 0 10px 20px rgba(68, 52, 35, 0.05); }}
          h1, h2 {{ margin: 0 0 8px; }}
          .muted {{ color: #6b6257; }}
          code, pre {{ background: #f1e7d9; border-radius: 8px; }}
          pre {{ padding: 12px; white-space: pre-wrap; }}
        </style>
      </head>
      <body>
        <h1>Memory Subject Review</h1>
        <p class='muted'>Run {esc(payload['run_id'])}</p>
        <p>Filters: {esc(filters)}</p>
        {selected_subject_html}
        <h2>Subjects</h2>
        {''.join(subject_cards) if subject_cards else '<p>No subjects found.</p>'}
        <h2>Candidates</h2>
        {''.join(candidate_cards) if candidate_cards else '<p>No candidates.</p>'}
      </body>
    </html>
    """.strip()


def _promote_candidate_memory(
    connection: sqlite3.Connection,
    candidate,
    *,
    record_type: str | None,
    title: str | None,
    trust_level: str,
    note: str | None,
):
    curated_record_type = record_type or _default_curated_record_type(candidate.memory_type)
    provenance = {
        "source": candidate.source_links,
        "candidate_memory_id": candidate.id,
        "candidate_review_status": candidate.review_status,
        "candidate_reason_type": candidate.reason_type,
        "candidate_domains": candidate.domains,
        "assistant_suggestion": candidate.assistant_suggestion,
    }
    record_title = title or _candidate_title(candidate)
    return create_memory_record(
        connection,
        record_type=curated_record_type,
        title=record_title,
        body=candidate.content,
        trust_level=trust_level,
        source_kind="chatgpt_candidate",
        source_ref=candidate.id,
        provenance=provenance,
        metadata={
            "candidate_memory_id": candidate.id,
            "candidate_review_status": candidate.review_status,
            "review_note": note,
            "source_role": candidate.source_links.get("source_role"),
        },
        created_by="user",
    )


def _default_curated_record_type(memory_type: str) -> str:
    mapping = {
        "decision": "decision",
        "preference": "preference",
        "procedure": "workflow",
        "workaround": "lesson",
        "failure": "lesson",
        "open_loop": "open_loop",
        "episodic": "research_note",
        "skill": "workflow",
        "analogy": "lesson",
        "project": "project",
        "semantic_fact": "research_note",
        "source_note": "research_note",
        "constraint": "lesson",
        "risk": "research_note",
        "relationship": "contact_note",
        "health_note": "research_note",
        "financial_note": "research_note",
    }
    return mapping.get(memory_type, "research_note")


def _candidate_title(candidate) -> str:
    text = candidate.content.strip()
    if len(text) <= 80:
        return text
    return text[:77].rstrip() + "..."


def _subject_slug(value: str) -> str:
    return normalize_subject_slug(value)


def _memory_frontdoor_state(config, logger, *, recent_limit: int, subject_limit: int) -> dict[str, object]:
    status = summarize_memory_status(
        data_dir=config.paths["data_dir"],
        memory_dir=config.paths["memory_dir"],
        logs_dir=config.logs_dir,
        recent_limit=recent_limit,
    )
    analyze = analyze_memory_corpus(
        data_dir=config.paths["data_dir"],
        memory_dir=config.paths["memory_dir"],
        logs_dir=config.logs_dir,
        subject_limit=subject_limit,
        recent_limit=recent_limit,
    )
    db_path = memory_db_path(config.paths["memory_dir"])
    with sqlite3.connect(db_path) as connection:
        subjects = [summary.to_dict() for summary in list_subjects(connection, limit=subject_limit)]
        candidate_subjects = list_candidate_subjects(connection, review_status="pending", limit=subject_limit)
    return {
        "status": {
            "status": status["status"],
            "counts": status["sqlite"].get("counts", {}),
            "latest_import": status["sqlite"].get("latest_import"),
            "recent_runs": status.get("recent_runs", []),
        },
        "analysis": {
            "candidate_stats": analyze.get("candidate_stats", {}),
            "codex": analyze.get("codex", {}),
            "token_stats": analyze.get("token_stats", {}),
        },
        "top_subjects": subjects,
        "candidate_subjects": candidate_subjects,
    }


def _execute_memory_assist_plan(
    plan,
    *,
    config,
    logger,
    memory_trace: MemoryTraceWriter,
) -> dict[str, object]:
    db_path = memory_db_path(config.paths["memory_dir"])
    action = plan.action
    arguments = plan.arguments or {}
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if action == "memory-status":
            recent_limit = int(arguments.get("recent_limit", 5))
            result = summarize_memory_status(
                data_dir=config.paths["data_dir"],
                memory_dir=config.paths["memory_dir"],
                logs_dir=config.logs_dir,
                recent_limit=recent_limit,
            )
            result["run_id"] = memory_trace.run.run_id
            return {"command": action, "status": result["status"], "result": result}
        if action == "memory-analyze":
            result = analyze_memory_corpus(
                data_dir=config.paths["data_dir"],
                memory_dir=config.paths["memory_dir"],
                logs_dir=config.logs_dir,
                subject_limit=int(arguments.get("subject_limit", 10)),
                recent_limit=int(arguments.get("recent_limit", 5)),
            )
            result["run_id"] = memory_trace.run.run_id
            return {"command": action, "status": result["status"], "result": result}
        if action == "memory-patterns":
            result = analyze_memory_patterns(
                data_dir=config.paths["data_dir"],
                memory_dir=config.paths["memory_dir"],
                logs_dir=config.logs_dir,
                focus=str(arguments.get("focus", "all")),
                source_role=str(arguments.get("source_role", "user")),
                limit=int(arguments.get("limit", 2000)),
                category_limit=int(arguments.get("category_limit", 6)),
                title_limit=int(arguments.get("title_limit", 20)),
            )
            result["run_id"] = memory_trace.run.run_id
            return {"command": action, "status": result["status"], "result": result}
        if action == "memory-runs":
            runs = list_recent_runs(config.logs_dir, limit=int(arguments.get("limit", 10)))
            result = {"status": "ok", "run_id": memory_trace.run.run_id, "count": len(runs), "recent_runs": runs}
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-subjects":
            subjects = [summary.to_dict() for summary in list_subjects(connection, kind=arguments.get("kind"), limit=arguments.get("limit"))]
            result = {"status": "ok", "run_id": memory_trace.run.run_id, "count": len(subjects), "subjects": subjects}
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-review-subjects":
            subject = str(arguments.get("subject", "")).strip()
            if not subject:
                raise ValueError("memory-review-subjects requires a subject argument")
            kind = str(arguments.get("kind", "subject")).strip() or "subject"
            review_status = arguments.get("review_status", "pending")
            source_role = arguments.get("source_role")
            assistant_only = bool(arguments.get("assistant_only", False))
            subject_limit = int(arguments.get("subject_limit", 20))
            candidate_limit = int(arguments.get("candidate_limit", 20))
            subject_summaries = list_candidate_subjects(
                connection,
                review_status=review_status,
                source_role=source_role,
                assistant_suggestion=True if assistant_only else None,
                kind=kind,
                limit=subject_limit,
            )
            candidate_memories = [
                candidate.to_dict()
                for candidate in list_candidate_memories_for_subject(
                    connection,
                    subject,
                    kind=kind,
                    review_status=review_status,
                    source_role=source_role,
                    assistant_suggestion=True if assistant_only else None,
                    limit=candidate_limit,
                )
            ]
            selected_subject = next(
                (item for item in subject_summaries if item["kind"] == kind and item["slug"] == normalize_subject_slug(subject)),
                None,
            )
            if selected_subject is None:
                selected_subject = {
                    "kind": kind,
                    "slug": normalize_subject_slug(subject),
                    "name": subject,
                    "candidate_count": len(candidate_memories),
                    "pending_count": len(candidate_memories) if review_status == "pending" else 0,
                    "approved_count": 0,
                    "merged_count": 0,
                    "rejected_count": 0,
                    "assistant_count": sum(1 for item in candidate_memories if item["assistant_suggestion"]),
                    "latest_candidate_activity_at": None,
                }
            result = {
                "status": "ok",
                "run_id": memory_trace.run.run_id,
                "filters": {
                    "subject": subject,
                    "kind": kind,
                    "review_status": review_status,
                    "source_role": source_role,
                    "assistant_only": assistant_only,
                    "subject_limit": subject_limit,
                    "candidate_limit": candidate_limit,
                },
                "count": len(subject_summaries),
                "subject_summaries": subject_summaries,
                "selected_subject": selected_subject,
                "candidate_memories": candidate_memories,
            }
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-search":
            query = str(arguments.get("query", "")).strip()
            if not query:
                raise ValueError("memory-search requires a query argument")
            result = search_chatgpt_memory(
                memory_dir=config.paths["memory_dir"],
                query=query,
                limit=int(arguments.get("limit", 8)),
                subject=arguments.get("subject"),
                title=arguments.get("title"),
                date_from=arguments.get("date_from"),
                date_to=arguments.get("date_to"),
                exclude_source_ids=_comma_values(arguments.get("exclude_source")),
                exclude_subjects=_comma_values(arguments.get("exclude_subject")),
                depth=str(arguments.get("depth", "medium")),
                effort=int(arguments.get("effort", 2)),
                allow_cross_domain=bool(arguments.get("allow_cross_domain", False)),
            )
            return {"command": action, "status": result["status"], "result": result}
        if action == "memory-candidates":
            result = {
                "status": "ok",
                "run_id": memory_trace.run.run_id,
                "count": 0,
                "candidate_memories": [
                    candidate.to_dict()
                    for candidate in list_candidate_memories(
                        connection,
                        review_status=arguments.get("review_status", "pending"),
                        domain=arguments.get("domain"),
                        source_role=arguments.get("source_role"),
                        assistant_suggestion=True if arguments.get("assistant_only") else None,
                        limit=arguments.get("limit"),
                    )
                ],
            }
            result["count"] = len(result["candidate_memories"])
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-list":
            result = {
                "status": "ok",
                "run_id": memory_trace.run.run_id,
                "count": 0,
                "memory_records": [record.to_dict() for record in list_memory_records(connection, record_type=arguments.get("record_type"), limit=arguments.get("limit"))],
            }
            result["count"] = len(result["memory_records"])
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-open-loops":
            result = {"status": "ok", "run_id": memory_trace.run.run_id, "count": 0, "open_loops": list_open_loops(connection, limit=int(arguments.get("limit", 20)))}
            result["count"] = len(result["open_loops"])
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-audit":
            target_run_id = str(arguments.get("run_id", "")).strip()
            if not target_run_id:
                raise ValueError("memory-audit requires a run_id argument")
            exposures = retrieval_exposures_for_run(connection, target_run_id)
            result = {
                "status": "ok",
                "run_id": memory_trace.run.run_id,
                "target_run_id": target_run_id,
                "count": len(exposures),
                "exposures": exposures,
            }
            return {"command": action, "status": "ok", "result": result}
        if action == "memory-trace":
            target_run_id = str(arguments.get("run_id", "")).strip()
            if not target_run_id:
                raise ValueError("memory-trace requires a run_id argument")
            trace = read_memory_trace(config.logs_dir, target_run_id)
            return {"command": action, "status": "ok", "result": trace}
    raise ValueError(f"unsupported memory action: {action}")


def _render_memory_eval(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Status: {payload['status']}",
        f"Checks: {summary['passed']}/{summary['checks']} passed",
        f"Usage: {summary['usage_score']}/{summary['usage_max_score']} points ({summary['usage_score_pct']:.1f}%)",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['status']} {check['name']}")
    usage_summary = payload.get("usage_summary", {})
    by_category = usage_summary.get("by_category", {}) if isinstance(usage_summary, dict) else {}
    if by_category:
        lines.append("Usage categories:")
        for category, category_summary in by_category.items():
            lines.append(
                f"- {category}: {category_summary['score']}/{category_summary['max_score']} "
                f"({category_summary['score_pct']:.1f}%)"
            )
    ab_report = payload.get("ab_report", {})
    if isinstance(ab_report, dict) and ab_report.get("variants"):
        lines.append(f"A/B winner: {ab_report.get('winner')}")
        for variant in ab_report["variants"]:
            metrics = variant.get("metrics", {})
            lines.append(
                f"- {variant['variant']}: {variant['status']} "
                f"quality={metrics.get('answer_quality')} provenance={metrics.get('provenance_correctness')}"
            )
    return "\n".join(lines)


def _render_memory_assist(payload: dict[str, object]) -> str:
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Request: {payload['request']}",
        f"Action: {payload['plan']['action']}",
        f"Executed: {'yes' if payload['executed'] else 'no'}",
        f"Confidence: {payload['plan']['confidence']:.2f}",
        f"Rationale: {payload['plan']['rationale']}",
    ]
    if payload.get("execution"):
        lines.append("")
        lines.append(f"Execution command: {payload['execution']['command']}")
        lines.append(_summarize_memory_execution(payload["execution"]["result"]))
    return "\n".join(lines)


def _render_memory_assist_html(payload: dict[str, object]) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    plan = payload["plan"]
    execution = payload.get("execution")
    execution_html = ""
    if execution:
        execution_html = f"<div class='card'><h2>Execution</h2><p>{esc(execution['command'])}</p><pre>{esc(_summarize_memory_execution(execution['result']))}</pre></div>"
    return f"""
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1' />
        <title>Memory Assist</title>
        <style>
          body {{ margin: 0; padding: 24px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f2ea; color: #1d1a16; }}
          .card {{ background: #fffdf8; border: 1px solid #d7cdbf; border-radius: 14px; padding: 16px; margin-bottom: 14px; box-shadow: 0 10px 20px rgba(68, 52, 35, 0.05); }}
          h1, h2 {{ margin: 0 0 8px; }}
          .muted {{ color: #6b6257; }}
          code, pre {{ background: #f1e7d9; border-radius: 8px; }}
          pre {{ padding: 12px; white-space: pre-wrap; }}
        </style>
      </head>
      <body>
        <h1>Memory Assist</h1>
        <p class='muted'>Run {esc(payload['run_id'])}</p>
        <div class='card'>
          <h2>Plan</h2>
          <p><code>{esc(plan['action'])}</code></p>
          <p>{esc(plan['summary'])}</p>
          <p class='muted'>confidence {plan['confidence']:.2f} | needs confirmation {esc(plan['needs_confirmation'])}</p>
          <pre>{esc(plan['rationale'])}</pre>
        </div>
        {execution_html}
      </body>
    </html>
    """.strip()


def _summarize_memory_execution(result: dict[str, object]) -> str:
    if "subjects" in result:
        return f"subjects={len(result.get('subjects', []))}"
    if "candidate_memories" in result:
        return f"candidates={len(result.get('candidate_memories', []))}"
    if "memory_records" in result:
        return f"records={len(result.get('memory_records', []))}"
    if "results" in result:
        return f"results={len(result.get('results', []))}"
    if "recent_runs" in result:
        return f"recent_runs={len(result.get('recent_runs', []))}"
    if "open_loops" in result:
        return f"open_loops={len(result.get('open_loops', []))}"
    if "exposures" in result:
        return f"exposures={len(result.get('exposures', []))}"
    return json.dumps(result, indent=2, sort_keys=True)[:600]


def _comma_values(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@bake_cam_app.command("devices")
def bake_cam_devices(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List configured baking camera workstations."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:devices", {})
    try:
        payload = {"status": "ok", "run_id": run.run_id, "devices": bake_cam_list_devices(config.paths["data_dir"])}
        logger.write_artifact(run, "bake_cam_devices.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_devices(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("start-session")
def bake_cam_start_session(
    session_type: str = typer.Option(..., "--type", help="Session type: starter_feeding, bulk_fermentation, final_proof, bake, misc."),
    name: str = typer.Option(..., "--name", help="Human-readable session name."),
    recipe_id: str | None = typer.Option(None, "--recipe-id", help="Optional recipe ID to attach."),
    batch_id: str | None = typer.Option(None, "--batch-id", help="Optional dough/bake batch ID."),
    feeding_id: str | None = typer.Option(None, "--feeding-id", help="Optional starter feeding ID."),
    started_at: str | None = typer.Option(None, "--started-at", help="Optional ISO timestamp for the session start/feeding time."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Create a baking observation session."""
    config, _client, logger = _client_and_logger()
    run = logger.start(
        "bake-cam:start-session",
        {
            "type": session_type,
            "name": name,
            "recipe_id": recipe_id,
            "batch_id": batch_id,
            "feeding_id": feeding_id,
            "started_at": started_at,
        },
    )
    try:
        session = bake_cam_create_session(
            config.paths["data_dir"],
            session_type=session_type,
            name=name,
            recipe_id=recipe_id,
            batch_id=batch_id,
            feeding_id=feeding_id,
            started_at=started_at,
        )
        payload = {"status": "ok", "run_id": run.run_id, "session": session}
        logger.write_artifact(run, "bake_cam_session.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_session(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("list-sessions")
def bake_cam_list_sessions_command(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum sessions to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List recent baking observation sessions."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:list-sessions", {"limit": limit})
    try:
        payload = {"status": "ok", "run_id": run.run_id, "sessions": bake_cam_list_sessions(config.paths["data_dir"], limit=limit)}
        logger.write_artifact(run, "bake_cam_sessions.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_sessions(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("show-session")
def bake_cam_show_session(
    session_id: str = typer.Argument(..., help="Session ID to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show one baking observation session."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:show-session", {"session_id": session_id})
    try:
        session = bake_cam_load_session(config.paths["data_dir"], session_id)
        payload = {"status": "ok", "run_id": run.run_id, "session": session}
        logger.write_artifact(run, "bake_cam_session.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_session(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("health")
def bake_cam_health(
    device: str = typer.Option("DavesDev", "--device", help="Device ID to probe."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Check SSH, camera tooling, disk, and time for a camera workstation."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:health", {"device": device})
    try:
        payload = {"run_id": run.run_id, **bake_cam_health_check(config.paths["data_dir"], device_id=device)}
        logger.write_artifact(run, "bake_cam_health.json", json.dumps(payload, indent=2, sort_keys=True))
        bake_cam_write_trace(run.run_dir / "trace.jsonl", payload["trace"])
        logger.finish(run, status=payload["status"], result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_health(payload))
        if payload["status"] == "error":
            raise typer.Exit(code=1)
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("schedule")
def bake_cam_schedule(
    session_id: str = typer.Option(..., "--session", help="Session ID to schedule."),
    every: str | None = typer.Option(None, "--every", help="Interval like 30m, 2h, 1d. Requires --until."),
    until: str | None = typer.Option(None, "--until", help="End offset like 12h. Requires --every."),
    at: str | None = typer.Option(None, "--at", help="Comma-separated offsets like 0h,2h,4h,8h."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Attach a deterministic t-plus capture plan to a session."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:schedule", {"session_id": session_id, "every": every, "until": until, "at": at})
    try:
        schedule = bake_cam_schedule_session(config.paths["data_dir"], session_id=session_id, every=every, until=until, at=at)
        payload = {"status": "ok", "run_id": run.run_id, **schedule}
        logger.write_artifact(run, "bake_cam_schedule.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_schedule(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("capture-now")
def bake_cam_capture_now_command(
    session_id: str = typer.Option(..., "--session", help="Session ID to attach capture to."),
    device: str = typer.Option("DavesDev", "--device", help="Device ID to capture from."),
    camera: str = typer.Option("main", "--camera", help="Camera ID label."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Capture one still image from a baking camera workstation."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:capture-now", {"session_id": session_id, "device": device, "camera": camera})
    try:
        payload = {"run_id": run.run_id, **bake_cam_capture_now(config.paths["data_dir"], session_id=session_id, device_id=device, camera_id=camera)}
        logger.write_artifact(run, "bake_cam_capture.json", json.dumps(payload, indent=2, sort_keys=True))
        bake_cam_write_trace(run.run_dir / "trace.jsonl", payload["trace"])
        logger.finish(run, status=payload["status"], result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_capture(payload))
        if payload["status"] == "error":
            raise typer.Exit(code=1)
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("latest")
def bake_cam_latest(
    session_id: str | None = typer.Option(None, "--session", help="Optional session ID filter."),
    camera: str | None = typer.Option(None, "--camera", help="Optional camera ID filter."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show the latest captured still image metadata."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:latest", {"session_id": session_id, "camera": camera})
    try:
        capture = bake_cam_latest_capture(config.paths["data_dir"], session_id=session_id, camera_id=camera)
        payload = {"status": "ok", "run_id": run.run_id, "capture": capture}
        logger.write_artifact(run, "bake_cam_latest.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_latest(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("sync")
def bake_cam_sync(
    session_id: str | None = typer.Option(None, "--session", help="Optional session ID to sync."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Retry copy-back for remote captures left in the device spool."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:sync", {"session_id": session_id})
    try:
        payload = {"run_id": run.run_id, **bake_cam_sync_spooled_captures(config.paths["data_dir"], session_id=session_id)}
        logger.write_artifact(run, "bake_cam_sync.json", json.dumps(payload, indent=2, sort_keys=True))
        bake_cam_write_trace(run.run_dir / "trace.jsonl", payload["trace"])
        logger.finish(run, status=payload["status"], result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_sync(payload))
        if payload["status"] == "error":
            raise typer.Exit(code=1)
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


@bake_cam_app.command("status")
def bake_cam_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show device, session, latest capture, and failure status."""
    config, _client, logger = _client_and_logger()
    run = logger.start("bake-cam:status", {})
    try:
        payload = {"run_id": run.run_id, **bake_cam_status_summary(config.paths["data_dir"])}
        logger.write_artifact(run, "bake_cam_status.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else _render_bake_cam_status(payload))
    except BakeCamError as exc:
        _finish_bake_cam_error(logger, run, exc)


def _finish_bake_cam_error(logger: RunLogger, run, exc: BakeCamError) -> None:
    payload = {"status": "error", "run_id": run.run_id, "error": exc.to_dict()}
    logger.write_artifact(run, "bake_cam_error.json", json.dumps(payload, indent=2, sort_keys=True))
    logger.finish(run, status="error", result=payload)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
    raise typer.Exit(code=1)


def _render_bake_cam_devices(payload: dict) -> str:
    lines = [f"run_id: {payload['run_id']}", "devices:"]
    for device in payload["devices"]:
        lines.append(f"- {device['device_id']} -> {device['ssh_target']} ({device.get('role', 'camera')})")
    return "\n".join(lines)


def _render_bake_cam_session(payload: dict) -> str:
    session = payload["session"]
    return "\n".join(
        [
            f"run_id: {payload['run_id']}",
            f"session_id: {session['session_id']}",
            f"name: {session['name']}",
            f"type: {session['activity_type']}",
            f"status: {session['status']}",
            f"captures: {session['capture_count']}",
            f"last_error: {session.get('last_error')}",
        ]
    )


def _render_bake_cam_sessions(payload: dict) -> str:
    lines = [f"run_id: {payload['run_id']}", f"sessions: {len(payload['sessions'])}"]
    for session in payload["sessions"]:
        lines.append(
            f"- {session['session_id']} | {session['activity_type']} | {session['name']} | captures={session.get('capture_count', 0)}"
        )
    return "\n".join(lines)


def _render_bake_cam_health(payload: dict) -> str:
    probe = payload.get("probe", {})
    lines = [
        f"run_id: {payload['run_id']}",
        f"status: {payload['status']}",
        f"device: {payload['device']['device_id']}",
        f"ssh_ok: {payload['ssh_ok']}",
        f"camera_available: {payload['camera_available']}",
        f"hostname: {probe.get('hostname')}",
        f"time: {probe.get('time')}",
        f"disk: {probe.get('disk')}",
        f"camera_tool: {probe.get('camera_tool')}",
        f"camera_probe: {probe.get('camera_probe')}",
        f"video_devices: {probe.get('video_devices')}",
    ]
    failed_events = [event for event in payload.get("trace", []) if event.get("status") == "error"]
    for event in failed_events:
        details = event.get("details", {})
        lines.append(f"failed_stage: {event.get('stage')}")
        lines.append(f"error: {details.get('stderr') or event.get('message')}")
    return "\n".join(lines)


def _render_bake_cam_schedule(payload: dict) -> str:
    lines = [f"run_id: {payload['run_id']}", f"session_id: {payload['session_id']}", "capture_plan:"]
    for item in payload["capture_plan"]:
        lines.append(f"- {item['offset_label']} | {item['status']} | capture_id={item.get('capture_id')}")
    return "\n".join(lines)


def _render_bake_cam_capture(payload: dict) -> str:
    if payload["status"] != "ok":
        return "\n".join(
            [
                f"run_id: {payload['run_id']}",
                "status: error",
                f"stage: {payload['error']['stage']}",
                f"error: {payload['error']['message']}",
            ]
        )
    capture = payload["capture"]
    return "\n".join(
        [
            f"run_id: {payload['run_id']}",
            "status: ok",
            f"capture_id: {capture['capture_id']}",
            f"local_path: {capture['local_path']}",
            f"elapsed_seconds: {capture['elapsed_seconds']}",
        ]
    )


def _render_bake_cam_latest(payload: dict) -> str:
    capture = payload["capture"]
    return "\n".join(
        [
            f"run_id: {payload['run_id']}",
            f"capture_id: {capture['capture_id']}",
            f"session_id: {capture['session_id']}",
            f"camera_id: {capture['camera_id']}",
            f"captured_at: {capture['captured_at']}",
            f"local_path: {capture['local_path']}",
        ]
    )


def _render_bake_cam_sync(payload: dict) -> str:
    lines = [
        f"run_id: {payload['run_id']}",
        f"status: {payload['status']}",
        f"synced: {len(payload['synced'])}",
        f"failed: {len(payload['failed'])}",
    ]
    for item in payload["synced"][:5]:
        lines.append(f"- synced {item['capture_id']} -> {item['local_path']}")
    for item in payload["failed"][:5]:
        lines.append(f"- failed {item['capture_id']} from {item['remote_path']}: {item['error']}")
    return "\n".join(lines)


def _render_bake_cam_status(payload: dict) -> str:
    latest = payload.get("latest_capture") or {}
    lines = [
        f"run_id: {payload['run_id']}",
        f"devices: {len(payload['devices'])}",
        f"active_sessions: {len(payload['active_sessions'])}",
        f"recent_sessions: {len(payload['recent_sessions'])}",
        f"latest_capture: {latest.get('local_path') if latest else None}",
    ]
    for session in payload["active_sessions"][:5]:
        plan = session.get("capture_plan") or []
        pending = len([item for item in plan if item.get("status") == "pending"])
        lines.append(
            f"- {session['session_id']} | {session['activity_type']} | captures={session.get('capture_count', 0)} | pending={pending} | last_error={session.get('last_error')}"
        )
    return "\n".join(lines)


@home_mcp_app.command("serve")
def home_mcp_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind the MCP HTTP server to."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Port to bind the MCP HTTP server to."),
    auth_mode: str = typer.Option("none", "--auth-mode", help="Auth mode for POST /mcp: none, bearer, oauth, or mixed."),
    auth_token: str | None = typer.Option(None, "--auth-token", help="Optional bearer token required for POST /mcp."),
) -> None:
    """Serve the narrow home-mcp JSON-RPC surface over HTTP."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:serve", {"host": host, "port": port, "auth_mode": auth_mode, "auth_token": bool(auth_token)})
    try:
        server = build_home_mcp_server(config, auth_mode=auth_mode, auth_token=auth_token)
        logger.write_artifact(run, "home_mcp_roots.json", json.dumps(server.list_allowed_roots(), indent=2, sort_keys=True))
        httpd = serve_home_mcp(server, host=host, port=port)
        typer.echo(f"home-mcp serving on http://{host}:{port}/mcp")
        typer.echo(f"health endpoint: http://{host}:{port}/health")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
        finally:
            logger.finish(run, status="ok", result={"host": host, "port": port, "roots": len(server.root_specs)})
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("roots")
def home_mcp_roots() -> None:
    """List the allowlisted roots configured for home-mcp."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:roots", {})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.list_allowed_roots()}
        logger.write_artifact(run, "home_mcp_roots.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("list-files")
def home_mcp_list_files(
    root_id: str = typer.Option(..., "--root-id", help="Allowlisted root identifier."),
    glob: str = typer.Option("*", "--glob", help="Glob pattern within the root."),
    recursive: bool = typer.Option(True, "--recursive/--flat", help="Recurse through the root."),
    limit: int = typer.Option(100, "--limit", min=1, max=500, help="Maximum files to return."),
) -> None:
    """List files inside an allowlisted root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:list-files", {"root_id": root_id, "glob": glob, "recursive": recursive, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.list_files(root_id=root_id, glob=glob, recursive=recursive, limit=limit)}
        logger.write_artifact(run, "home_mcp_list_files.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("search-files")
def home_mcp_search_files(
    query: str = typer.Argument(..., help="Text query to search across allowlisted roots."),
    root_id: str | None = typer.Option(None, "--root-id", help="Restrict search to a single root."),
    file_types: str | None = typer.Option(None, "--file-types", help="Comma-separated suffixes, e.g. .md,.txt."),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum hits to return."),
) -> None:
    """Search allowlisted roots for text matches."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:search-files", {"query": query, "root_id": root_id, "file_types": file_types, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        suffixes = _comma_values(file_types) if file_types else None
        payload = {
            "run_id": run.run_id,
            **server.search_files(query=query, root_id=root_id, file_types=suffixes, limit=limit),
        }
        logger.write_artifact(run, "home_mcp_search_files.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("search-notes")
def home_mcp_search_notes(
    query: str = typer.Argument(..., help="Text query to search across Markdown notes."),
    root_id: str | None = typer.Option(None, "--root-id", help="Restrict search to a single root."),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum hits to return."),
) -> None:
    """Search Markdown notes inside allowlisted roots."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:search-notes", {"query": query, "root_id": root_id, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.search_notes(query=query, root_id=root_id, limit=limit)}
        logger.write_artifact(run, "home_mcp_search_notes.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("recent-files")
def home_mcp_recent_files(
    root_id: str = typer.Option(..., "--root-id", help="Allowlisted root identifier."),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum files to return."),
    file_types: str | None = typer.Option(None, "--file-types", help="Comma-separated suffixes, e.g. .md,.txt."),
) -> None:
    """List recently modified files in an allowlisted root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:recent-files", {"root_id": root_id, "limit": limit, "file_types": file_types})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.list_recent_files(root_id=root_id, limit=limit, file_types=_comma_values(file_types) if file_types else None),
        }
        logger.write_artifact(run, "home_mcp_recent_files.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("search-recipes")
def home_mcp_search_recipes(
    query: str | None = typer.Option(None, "--query", help="Optional recipe query text."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tag filters."),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum hits to return."),
) -> None:
    """Search the recipe book for notes and attempts."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:search-recipes", {"query": query, "tags": tags, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.search_recipes(query=query, tags=_comma_values(tags), limit=limit),
        }
        logger.write_artifact(run, "home_mcp_search_recipes.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("get-recipe")
def home_mcp_get_recipe(
    recipe_id: str = typer.Option(..., "--recipe-id", help="Recipe file identifier."),
) -> None:
    """Read one recipe card with parsed structure."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:get-recipe", {"recipe_id": recipe_id})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.get_recipe(recipe_id=recipe_id)}
        logger.write_artifact(run, "home_mcp_get_recipe.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("browse-recipes")
def home_mcp_browse_recipes(
    query: str | None = typer.Option(None, "--query", help="Optional recipe query text."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tag filters."),
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum hits to return."),
) -> None:
    """Browse standardized recipe cards in the recipe book."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:browse-recipes", {"query": query, "tags": tags, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.browse_recipes(query=query, tags=_comma_values(tags), limit=limit),
        }
        logger.write_artifact(run, "home_mcp_browse_recipes.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("compare-recipe-attempts")
def home_mcp_compare_recipe_attempts(
    recipe_id: str = typer.Option(..., "--recipe-id", help="Recipe file identifier."),
) -> None:
    """Compare logged attempts for a recipe note."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:compare-recipe-attempts", {"recipe_id": recipe_id})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.compare_recipe_attempts(recipe_id=recipe_id)}
        logger.write_artifact(run, "home_mcp_compare_recipe_attempts.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("draft-recipe-card")
def home_mcp_draft_recipe_card(
    source_text: str | None = typer.Option(None, "--source-text", help="Source text to extract from."),
    file_id: str | None = typer.Option(None, "--file-id", help="File identifier to extract from."),
    title: str | None = typer.Option(None, "--title", help="Optional override title."),
    query: str | None = typer.Option(None, "--query", help="Optional search query context."),
) -> None:
    """Draft a structured recipe card from source text or a source file."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:draft-recipe-card", {"file_id": file_id, "title": title, "query": query})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.draft_recipe_card(source_text=source_text, file_id=file_id, title=title, query=query),
        }
        logger.write_artifact(run, "home_mcp_draft_recipe_card.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("recipe-standard")
def home_mcp_recipe_standard() -> None:
    """Print the canonical recipe card standard."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:recipe-standard", {})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.recipe_standard()}
        logger.write_artifact(run, "home_mcp_recipe_standard.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("read-file")
def home_mcp_read_file(
    file_id: str = typer.Option(..., "--file-id", help="File identifier in root_id:relative/path.md form."),
    start_line: int | None = typer.Option(None, "--start-line", min=1, help="1-based start line."),
    end_line: int | None = typer.Option(None, "--end-line", min=1, help="1-based end line."),
) -> None:
    """Read a text file via a file identifier."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:read-file", {"file_id": file_id, "start_line": start_line, "end_line": end_line})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.read_file(file_id=file_id, start_line=start_line, end_line=end_line)}
        logger.write_artifact(run, "home_mcp_read_file.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("create-note")
def home_mcp_create_note(
    root_id: str = typer.Option(..., "--root-id", help="Allowlisted writable root."),
    title: str = typer.Option(..., "--title", help="Note title."),
    body: str = typer.Option(..., "--body", help="Markdown body."),
    folder: str = typer.Option("", "--folder", help="Optional subfolder under the root."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Create a new Markdown note in a writable root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:create-note", {"root_id": root_id, "title": title, "folder": folder})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.create_markdown_note(
                root_id=root_id,
                folder=folder,
                title=title,
                body=body,
                tags=_comma_values(tags),
            ),
        }
        logger.write_artifact(run, "home_mcp_create_note.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("append-note")
def home_mcp_append_note(
    file_id: str = typer.Option(..., "--file-id", help="Markdown file identifier."),
    entry: str = typer.Option(..., "--entry", help="Entry text to append."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Append a timestamped log entry to a Markdown note."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:append-note", {"file_id": file_id})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.append_markdown_log(file_id=file_id, entry=entry, tags=_comma_values(tags)),
        }
        logger.write_artifact(run, "home_mcp_append_note.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("create-project-note")
def home_mcp_create_project_note(
    project_id: str = typer.Option(..., "--project-id", help="Project identifier used as the folder slug."),
    title: str = typer.Option(..., "--title", help="Project note title."),
    body: str = typer.Option(..., "--body", help="Markdown body."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Create a project note under the projects root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:create-project-note", {"project_id": project_id, "title": title})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.create_project_note(project_id=project_id, title=title, body=body, tags=_comma_values(tags)),
        }
        logger.write_artifact(run, "home_mcp_create_project_note.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("create-recipe")
def home_mcp_create_recipe(
    title: str = typer.Option(..., "--title", help="Recipe title."),
    body: str = typer.Option(..., "--body", help="Recipe body."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Create a recipe note in the recipe book root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:create-recipe", {"title": title})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.create_recipe(title=title, body=body, tags=_comma_values(tags)),
        }
        logger.write_artifact(run, "home_mcp_create_recipe.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("create-recipe-card")
def home_mcp_create_recipe_card(
    title: str = typer.Option(..., "--title", help="Recipe title."),
    body: str | None = typer.Option(None, "--body", help="Recipe body."),
    ingredient: list[str] = typer.Option([], "--ingredient", help="Ingredient line. Repeat to add more."),
    step: list[str] = typer.Option([], "--step", help="Recipe step. Repeat to add more."),
    servings: str | None = typer.Option(None, "--servings", help="Servings or yield."),
    prep_time: str | None = typer.Option(None, "--prep-time", help="Prep time."),
    cook_time: str | None = typer.Option(None, "--cook-time", help="Cook time."),
    total_time: str | None = typer.Option(None, "--total-time", help="Total time."),
    notes: str | None = typer.Option(None, "--notes", help="Optional notes."),
    source_file_id: str | None = typer.Option(None, "--source-file-id", help="Optional source file identifier."),
    source_query: str | None = typer.Option(None, "--source-query", help="Optional source query."),
    summary: str | None = typer.Option(None, "--summary", help="Optional summary."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Create a recipe card note in the recipe book root."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:create-recipe-card", {"title": title})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.create_recipe_card(
                title=title,
                body=body,
                ingredients=ingredient or None,
                steps=step or None,
                servings=servings,
                prep_time=prep_time,
                cook_time=cook_time,
                total_time=total_time,
                notes=notes,
                source_file_id=source_file_id,
                source_query=source_query,
                summary=summary,
                tags=_comma_values(tags),
            ),
        }
        logger.write_artifact(run, "home_mcp_create_recipe_card.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("normalize-recipes")
def home_mcp_normalize_recipes(
    apply: bool = typer.Option(False, "--apply", help="Rewrite recipe files in place."),
    limit: int = typer.Option(500, "--limit", min=1, max=1000, help="Maximum recipe files to inspect."),
) -> None:
    """Normalize recipe notes in the recipe book to the canonical card format."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:normalize-recipes", {"apply": apply, "limit": limit})
    try:
        server = build_home_mcp_server(config)
        payload = {"run_id": run.run_id, **server.normalize_recipe_book(apply=apply, limit=limit)}
        logger.write_artifact(run, "home_mcp_normalize_recipes.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("append-recipe-attempt")
def home_mcp_append_recipe_attempt(
    recipe_id: str = typer.Option(..., "--recipe-id", help="Recipe file identifier."),
    notes: str = typer.Option(..., "--notes", help="Attempt notes."),
    outcome: str | None = typer.Option(None, "--outcome", help="Optional outcome text."),
    next_time: str | None = typer.Option(None, "--next-time", help="Optional next-time text."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Append a recipe attempt to a recipe note."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:append-recipe-attempt", {"recipe_id": recipe_id})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.append_recipe_attempt(
                recipe_id=recipe_id,
                notes=notes,
                outcome=outcome,
                next_time=next_time,
                tags=_comma_values(tags),
            ),
        }
        logger.write_artifact(run, "home_mcp_append_recipe_attempt.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("bridge-recipe-note-to-memory")
def home_mcp_bridge_recipe_note_to_memory(
    file_id: str = typer.Option(..., "--file-id", help="Recipe note file identifier."),
    record_type: str = typer.Option("research_note", "--record-type", help="Curated memory record type."),
    title: str | None = typer.Option(None, "--title", help="Optional override title."),
    trust_level: str = typer.Option("high", "--trust-level", help="Trust level for the curated memory record."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject to assign."),
    subject_kind: str = typer.Option("subject", "--subject-kind", help="Subject kind: subject, project, or workflow."),
) -> None:
    """Promote a recipe note into the curated memory layer."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:bridge-recipe-note-to-memory", {"file_id": file_id, "record_type": record_type, "subject": subject})
    try:
        server = build_home_mcp_server(config)
        payload = {
            "run_id": run.run_id,
            **server.bridge_recipe_note_to_memory(
                file_id=file_id,
                record_type=record_type,
                title=title,
                trust_level=trust_level,
                subject=subject,
                subject_kind=subject_kind,
            ),
        }
        logger.write_artifact(run, "home_mcp_bridge_recipe_note_to_memory.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("install-service")
def home_mcp_install_service(
    auth_mode: str = typer.Option("none", "--auth-mode", help="Auth mode for the home-mcp service."),
    auth_token: str | None = typer.Option(None, "--auth-token", help="Optional bearer token for the service."),
    no_tunnel: bool = typer.Option(False, "--no-tunnel", help="Install only the local launchd agent, not the tunnel."),
) -> None:
    """Install and start the home-mcp launchd services."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:install-service", {"auth_mode": auth_mode, "no_tunnel": no_tunnel})
    try:
        result = install_home_mcp_launchd(config, auth_mode=auth_mode, auth_token=auth_token, with_tunnel=not no_tunnel)
        payload = {
            "run_id": run.run_id,
            "status": "ok",
            "home_plist": str(result.home_plist),
            "tunnel_plist": str(result.tunnel_plist) if result.tunnel_plist else None,
            "launched": result.launched,
        }
        logger.write_artifact(run, "home_mcp_install_service.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("uninstall-service")
def home_mcp_uninstall_service(
    no_tunnel: bool = typer.Option(False, "--no-tunnel", help="Only remove the local launchd agent."),
) -> None:
    """Stop and remove the home-mcp launchd services."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:uninstall-service", {"no_tunnel": no_tunnel})
    try:
        removed = uninstall_home_mcp_launchd(with_tunnel=not no_tunnel)
        payload = {
            "run_id": run.run_id,
            "status": "ok",
            "removed": removed,
            "tunnel_url": read_home_mcp_tunnel_url(config),
        }
        logger.write_artifact(run, "home_mcp_uninstall_service.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("service-status")
def home_mcp_service_status() -> None:
    """Show launchd and tunnel status for home-mcp."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:service-status", {})
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    domain = f"gui/{uid}"
    home_output = subprocess.run(
        ["launchctl", "print", f"{domain}/{HOME_MCP_HOME_LABEL}"],
        text=True,
        capture_output=True,
    )
    tunnel_output = subprocess.run(
        ["launchctl", "print", f"{domain}/{HOME_MCP_TUNNEL_LABEL}"],
        text=True,
        capture_output=True,
    )
    health = _probe_home_mcp_health("http://127.0.0.1:8765/health")
    payload = {
        "run_id": run.run_id,
        "status": "ok",
        "home_mcp_loaded": home_output.returncode == 0,
        "tunnel_loaded": tunnel_output.returncode == 0,
        "health_ok": health["ok"],
        "health_status": health["status"],
        "health_response": health["response"],
        "health_error": health["error"],
        "tunnel_url": read_home_mcp_tunnel_url(config),
        "home_mcp_launchd_stdout": home_output.stdout[-2000:],
        "home_mcp_launchd_stderr": home_output.stderr[-2000:],
        "tunnel_launchd_stdout": tunnel_output.stdout[-2000:],
        "tunnel_launchd_stderr": tunnel_output.stderr[-2000:],
    }
    logger.write_artifact(run, "home_mcp_service_status.json", json.dumps(payload, indent=2, sort_keys=True))
    logger.finish(run, status="ok", result=payload)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@home_mcp_app.command("smoke-test")
def home_mcp_smoke_test(
    url: str = typer.Option("http://127.0.0.1:8765/mcp", "--url", help="Home MCP JSON-RPC endpoint to test."),
    auth_token: str | None = typer.Option(None, "--auth-token", help="Optional bearer token for protected endpoints."),
    write_probe: bool = typer.Option(False, "--write-probe", help="Create a timestamped probe note to verify write access."),
) -> None:
    """Exercise the actual Home MCP JSON-RPC endpoint and report stage-level failures."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:smoke-test", {"url": url, "auth_token": bool(auth_token), "write_probe": write_probe})
    try:
        payload = _run_home_mcp_smoke_test(url=url, auth_token=auth_token, write_probe=write_probe)
        payload["run_id"] = run.run_id
        logger.write_artifact(run, "home_mcp_smoke_test.json", json.dumps(payload, indent=2, sort_keys=True))
        status = "ok" if payload["ok"] else "error"
        logger.finish(run, status=status, result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if not payload["ok"]:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@home_mcp_app.command("service-url")
def home_mcp_service_url() -> None:
    """Print the current Cloudflare tunnel URL, if one is recorded."""
    config, _client, logger = _client_and_logger()
    run = logger.start("home-mcp:service-url", {})
    try:
        payload = {"run_id": run.run_id, "tunnel_url": read_home_mcp_tunnel_url(config)}
        logger.write_artifact(run, "home_mcp_service_url.json", json.dumps(payload, indent=2, sort_keys=True))
        logger.finish(run, status="ok", result=payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        logger.finish(run, status="error", result={"error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _probe_home_mcp_health(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    last_error: str | None = None
    for attempt in range(10):
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            ok = payload.get("status") == "ok"
            return {
                "ok": ok,
                "status": "ok" if ok else "bad_status",
                "response": payload,
                "error": None,
                "attempts": attempt + 1,
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            if attempt < 9:
                import time

                time.sleep(1)
    return {"ok": False, "status": "error", "response": None, "error": last_error, "attempts": 10}


def _post_home_mcp_jsonrpc(url: str, payload: dict[str, object], *, auth_token: str | None = None, timeout: int = 5) -> dict[str, object]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("JSON-RPC response was not an object")
    return parsed


def _smoke_stage(
    stages: list[dict[str, object]],
    *,
    name: str,
    url: str,
    payload: dict[str, object],
    auth_token: str | None,
    expect_result: bool = True,
) -> dict[str, object] | None:
    try:
        response = _post_home_mcp_jsonrpc(url, payload, auth_token=auth_token)
        error = response.get("error")
        result = response.get("result")
        ok = error is None and (result is not None if expect_result else True)
        stage = {
            "name": name,
            "ok": ok,
            "jsonrpc_error": error,
            "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        }
        stages.append(stage)
        return result if isinstance(result, dict) else None
    except Exception as exc:
        stages.append({"name": name, "ok": False, "error": str(exc)})
        return None


def _run_home_mcp_smoke_test(*, url: str, auth_token: str | None = None, write_probe: bool = False) -> dict[str, object]:
    parsed_url = urlparse(url)
    health_path = parsed_url.path.removesuffix("/mcp").rstrip("/") + "/health"
    health_url = urlunparse(parsed_url._replace(path=health_path, query="", fragment=""))
    stages: list[dict[str, object]] = []
    health = _probe_home_mcp_health(health_url)
    stages.append({"name": "health", "ok": bool(health["ok"]), "status": health["status"], "error": health["error"]})

    initialize = _smoke_stage(
        stages,
        name="initialize",
        url=url,
        auth_token=auth_token,
        payload={"jsonrpc": "2.0", "id": "smoke-initialize", "method": "initialize", "params": {}},
    )
    tools = _smoke_stage(
        stages,
        name="tools/list",
        url=url,
        auth_token=auth_token,
        payload={"jsonrpc": "2.0", "id": "smoke-tools", "method": "tools/list", "params": {}},
    )
    roots = _smoke_stage(
        stages,
        name="tool:list_allowed_roots",
        url=url,
        auth_token=auth_token,
        payload={
            "jsonrpc": "2.0",
            "id": "smoke-roots",
            "method": "tools/call",
            "params": {"name": "list_allowed_roots", "arguments": {}},
        },
    )
    recipe_standard = _smoke_stage(
        stages,
        name="tool:recipe_standard",
        url=url,
        auth_token=auth_token,
        payload={
            "jsonrpc": "2.0",
            "id": "smoke-recipe-standard",
            "method": "tools/call",
            "params": {"name": "recipe_standard", "arguments": {}},
        },
    )
    search_recipes = _smoke_stage(
        stages,
        name="tool:search_recipes",
        url=url,
        auth_token=auth_token,
        payload={
            "jsonrpc": "2.0",
            "id": "smoke-search-recipes",
            "method": "tools/call",
            "params": {"name": "search_recipes", "arguments": {"query": "sourdough", "limit": 5}},
        },
    )
    memory_status = _smoke_stage(
        stages,
        name="tool:memory_status",
        url=url,
        auth_token=auth_token,
        payload={
            "jsonrpc": "2.0",
            "id": "smoke-memory-status",
            "method": "tools/call",
            "params": {"name": "memory_status", "arguments": {"recent_limit": 3}},
        },
    )

    write_result: dict[str, object] | None = None
    if write_probe:
        write_result = _smoke_stage(
            stages,
            name="tool:create_markdown_note",
            url=url,
            auth_token=auth_token,
            payload={
                "jsonrpc": "2.0",
                "id": "smoke-write",
                "method": "tools/call",
                "params": {
                    "name": "create_markdown_note",
                    "arguments": {
                        "root_id": "projects",
                        "folder": "_smoke_tests",
                        "title": "Home MCP smoke test",
                        "body": "Automated write probe from `lagent home-mcp smoke-test --write-probe`.",
                        "tags": ["smoke-test", "home-mcp"],
                    },
                },
            },
        )

    tool_names: list[str] = []
    if isinstance(tools, dict):
        raw_tools = tools.get("tools")
        if isinstance(raw_tools, list):
            tool_names = sorted(str(tool.get("name")) for tool in raw_tools if isinstance(tool, dict) and tool.get("name"))

    ok = all(bool(stage.get("ok")) for stage in stages)
    return {
        "ok": ok,
        "url": url,
        "auth_token_used": bool(auth_token),
        "write_probe": write_probe,
        "stage_count": len(stages),
        "stages": stages,
        "server": initialize.get("serverInfo") if isinstance(initialize, dict) else None,
        "tool_count": len(tool_names),
        "required_tools_present": {
            name: name in tool_names
            for name in ["list_allowed_roots", "recipe_standard", "search_recipes", "create_markdown_note", "memory_status", "memory_context"]
        },
        "roots_ok": roots is not None,
        "recipe_standard_ok": recipe_standard is not None,
        "search_recipes_ok": search_recipes is not None,
        "memory_status_ok": memory_status is not None,
        "write_result": write_result,
    }


if __name__ == "__main__":
    app()
