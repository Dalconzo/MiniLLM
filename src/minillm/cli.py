from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_settings
from .ollama_client import OllamaError, generate, list_models
from .tasks import build_prompt, read_input, system_prompt_for


def _normalize_response(task: str, text: str) -> str:
    cleaned = text.strip()
    if task == "classify" and cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            cleaned = "\n".join(lines[1:-1]).strip()
    return cleaned


def _format_stats(result) -> str:
    stats = {
        "model": result.model,
        "prompt_tokens": result.prompt_eval_count,
        "response_tokens": result.eval_count,
        "load_duration_ns": result.load_duration,
        "total_duration_ns": result.total_duration,
    }
    return json.dumps(stats, indent=2)


def cmd_list_models(args: argparse.Namespace) -> int:
    settings = load_settings()
    configured = {profile.alias: profile.model for profile in settings.list_profiles()}
    installed = {item["name"] for item in list_models()}

    for alias, model in configured.items():
        marker = "installed" if model in installed else "missing"
        print(f"{alias:8} {model:20} {marker}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    settings = load_settings()
    alias = settings.resolve_alias(args.task, args.model)
    profile = settings.get_profile(alias)
    text = read_input(args.text, args.file)
    prompt = build_prompt(args.task, text)
    result = generate(profile, prompt=prompt, system=system_prompt_for(args.task))
    print(_normalize_response(args.task, result.response))
    if args.stats:
        print("\n---")
        print(_format_stats(result))
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    args.task = "summarize"
    return cmd_ask(args)


def cmd_eval(args: argparse.Namespace) -> int:
    settings = load_settings()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(args.input, "r", encoding="utf-8") as infile, open(
        out_path, "w", encoding="utf-8"
    ) as outfile:
        for line in infile:
            if not line.strip():
                continue
            row = json.loads(line)
            task = row.get("task", "chat")
            alias = settings.resolve_alias(task, args.model)
            profile = settings.get_profile(alias)
            prompt = build_prompt(task, row["input"])
            result = generate(profile, prompt=prompt, system=system_prompt_for(task))
            record = {
                "task": task,
                "model_alias": alias,
                "model": result.model,
                "input": row["input"],
                "response": _normalize_response(task, result.response),
                "prompt_tokens": result.prompt_eval_count,
                "response_tokens": result.eval_count,
                "total_duration_ns": result.total_duration,
            }
            outfile.write(json.dumps(record) + "\n")

    print(f"wrote eval results to {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minillm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-models", help="show configured models")
    list_parser.set_defaults(func=cmd_list_models)

    ask_parser = subparsers.add_parser("ask", help="run a task against Ollama")
    ask_parser.add_argument("text", nargs="?")
    ask_parser.add_argument("--file")
    ask_parser.add_argument("--task", default="chat", choices=["chat", "summarize", "code", "classify"])
    ask_parser.add_argument("--model", help="configured model alias override")
    ask_parser.add_argument("--stats", action="store_true")
    ask_parser.set_defaults(func=cmd_ask)

    summarize_parser = subparsers.add_parser("summarize", help="summarize a file or inline text")
    summarize_parser.add_argument("text", nargs="?")
    summarize_parser.add_argument("--file")
    summarize_parser.add_argument("--model", help="configured model alias override")
    summarize_parser.add_argument("--stats", action="store_true")
    summarize_parser.set_defaults(func=cmd_summarize)

    eval_parser = subparsers.add_parser("eval", help="run batch prompts from jsonl")
    eval_parser.add_argument("--input", required=True)
    eval_parser.add_argument("--output", required=True)
    eval_parser.add_argument("--model", help="configured model alias override")
    eval_parser.set_defaults(func=cmd_eval)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, KeyError, OllamaError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
