from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

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
from .config import load_config
from .indexing.repo_indexer import default_db_path, index_repo
from .llm.model_router import route_task
from .llm.ollama_client import OllamaClient, OllamaError
from .logging.run_logger import RunLogger
from .memory.chatgpt_ingest import SCHEMA_VERSION, import_chatgpt_export
from .memory.embeddings import embed_missing_chunks, fallback_model_spec
from .memory.observability import (
    MemoryObservationError,
    MemoryTraceWriter,
    dry_run_chatgpt_ingest,
    memory_db_path,
    read_memory_trace,
    render_memory_trace,
    validate_memory_state,
)
from .memory.search import search_chatgpt_memory
from .memory.subjects import assign_conversation_subject, init_subject_schema, list_subjects
from .tools.file_tools import redact_text
from .tools.git_tools import changed_files_from_diff, git_diff
from .tools.patches import PatchFile, apply_files, build_unified_patch, patch_filename
from .tools.search import fetch_file_chunks, search_index


app = typer.Typer(add_completion=False, help="Local Agent Lab CLI")


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
    except Exception as exc:
        error = {"message": str(exc), "stage": "validate_state", "error_code": "invariant_failed", "source_ref": None}
        memory_trace.trace("validate_state", str(exc), level="error", details={"error_code": "invariant_failed"})
        memory_trace.finish(status="error", result={"error": error}, error=error)
        typer.echo(json.dumps({"run_id": run.run_id, "artifact_dir": str(run.run_dir), "error": error}, indent=2), err=True)
        raise typer.Exit(code=1)


@app.command("memory-search")
def memory_search(
    query: str = typer.Argument(..., help="Query to run against ChatGPT memory."),
    limit: int = typer.Option(8, "--limit", min=1, max=50, help="Maximum number of hits to return."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject filter."),
    title: str | None = typer.Option(None, "--title", help="Optional conversation title filter."),
    date_from: str | None = typer.Option(None, "--date-from", help="Inclusive ISO timestamp/date lower bound."),
    date_to: str | None = typer.Option(None, "--date-to", help="Inclusive ISO timestamp/date upper bound."),
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
        )
        payload["run_id"] = run.run_id
        memory_trace.trace(
            "rank_results",
            "Ranked ChatGPT memory results.",
            details=payload["candidate_counts"],
        )
        memory_trace.trace(
            "apply_disclosure",
            "Applied default medium disclosure tier.",
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
        memory_trace.write_json("embedding_report.json", report)
        memory_trace.finish(status="ok", result=report)
        if json_output:
            typer.echo(json.dumps(report, indent=2, sort_keys=True))
        else:
            typer.echo(_render_memory_embed(report))
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


def _render_memory_search(payload: dict[str, object]) -> str:
    lines = [
        f"Run ID: {payload['run_id']}",
        f"Results: {payload['count']}",
    ]
    for result in payload["results"]:
        lines.append(
            f"{result['rank']}. {result['title']} [{result['role']}] "
            f"{result['chunk_id']} score={result['score']:.4f}"
        )
        lines.append(f"   {result['snippet']}")
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


def _render_memory_subjects(payload: dict[str, object]) -> str:
    lines = [f"Run ID: {payload['run_id']}", f"Subjects: {payload['count']}"]
    for subject in payload["subjects"]:
        lines.append(
            f"- {subject['kind']}:{subject['slug']} {subject['name']} "
            f"conversations={subject['conversation_count']} chunks={subject['chunk_count']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    app()
