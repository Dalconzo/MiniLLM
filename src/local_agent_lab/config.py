from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "agent.yaml"


@dataclass(frozen=True)
class ModelProfile:
    alias: str
    model: str
    task: str
    temperature: float
    max_tokens: int
    routing_label: str
    notes: str = ""


@dataclass(frozen=True)
class OllamaSettings:
    host: str
    request_timeout_seconds: int


@dataclass(frozen=True)
class RuntimeSettings:
    default_task: str
    redact_before_model: bool
    save_full_prompts: bool


class AppConfig:
    def __init__(self, path: Path, raw: dict[str, Any]) -> None:
        self.path = path
        self.raw = raw
        self.root_dir = path.parents[1]
        self.app_name = str(raw["app"]["name"])
        self.log_level = str(raw["app"].get("log_level", "info"))
        self.paths = self._resolve_paths(raw["paths"])
        self.ollama = OllamaSettings(**raw["ollama"])
        self.runtime = RuntimeSettings(**raw["runtime"])
        self.routing = raw.get("routing", {})
        self._profiles = {
            alias: ModelProfile(alias=alias, **payload)
            for alias, payload in raw["models"].items()
        }

    def _resolve_paths(self, payload: dict[str, str]) -> dict[str, Path]:
        resolved = {key: (self.root_dir / value).resolve() for key, value in payload.items()}
        for path in resolved.values():
            path.mkdir(parents=True, exist_ok=True)
        return resolved

    @property
    def prompts_dir(self) -> Path:
        return self.root_dir / "config" / "prompts"

    @property
    def logs_dir(self) -> Path:
        return self.paths["logs_dir"]

    @property
    def patches_dir(self) -> Path:
        return self.paths["patches_dir"]

    def list_profiles(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    def get_profile(self, alias: str) -> ModelProfile:
        if alias not in self._profiles:
            raise KeyError(f"unknown model alias: {alias}")
        return self._profiles[alias]

    def resolve_task_alias(self, task: str, model_alias: str | None = None) -> str:
        if model_alias:
            return model_alias
        task_map = self.routing.get("task_map", {})
        alias = task_map.get(task) or task_map.get(self.runtime.default_task)
        if not alias:
            raise KeyError(f"no routing alias configured for task '{task}'")
        return alias

    def prompt_path(self, task: str) -> Path:
        mapping = {
            "chat": "general_chat.md",
            "summarize": "repo_summary.md",
            "code": "small_function.md",
            "write_function": "small_function.md",
            "write_tests": "test_generation.md",
            "review": "code_review.md",
            "search": "narrow_search.md",
            "log": "log_analysis.md",
        }
        filename = mapping.get(task, "task_router.md")
        return self.prompts_dir / filename


def load_config(config_path: str | Path | None = None) -> AppConfig:
    env_override = os.environ.get("LAGENT_CONFIG")
    path = Path(config_path or env_override or DEFAULT_CONFIG_PATH).resolve()
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return AppConfig(path=path, raw=raw)
