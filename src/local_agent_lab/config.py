from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .security.paths import normalize_relative_path, resolve_authorized_path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONFIG_PATH = SOURCE_ROOT / "config" / "agent.yaml"
INSTALLED_CONFIG_DIR = Path(sys.prefix).resolve() / "share" / "minillm" / "config"
INSTALLED_CONFIG_PATH = INSTALLED_CONFIG_DIR / "agent.yaml"
DEFAULT_CONFIG_PATH = SOURCE_CONFIG_PATH if SOURCE_CONFIG_PATH.is_file() else INSTALLED_CONFIG_PATH


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
    def __init__(self, path: Path, raw: dict[str, Any], *, root_dir: Path, prompts_dir: Path) -> None:
        self.path = path
        self.raw = raw
        self.root_dir = root_dir
        self._prompts_dir = prompts_dir
        self.app_name = str(raw["app"]["name"])
        self.log_level = str(raw["app"].get("log_level", "info"))
        self.paths = self._resolve_paths(raw["paths"])
        self.ollama = OllamaSettings(**raw["ollama"])
        self.runtime = RuntimeSettings(**raw["runtime"])
        self.routing = raw.get("routing", {})
        self._profiles = {alias: ModelProfile(alias=alias, **payload) for alias, payload in raw["models"].items()}

    def _resolve_paths(self, payload: dict[str, str]) -> dict[str, Path]:
        if self.root_dir.is_symlink():
            raise ValueError("runtime root must not be a symlink")
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = self.root_dir.resolve(strict=True)
        resolved: dict[str, Path] = {}
        for key, value in payload.items():
            normalized = normalize_relative_path(value)
            path = resolve_authorized_path(root, normalized)
            path.mkdir(parents=True, exist_ok=True)
            resolved[key] = path
        return resolved

    @property
    def prompts_dir(self) -> Path:
        return self._prompts_dir

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
        if not isinstance(alias, str) or not alias:
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
    path = Path(config_path or env_override or _default_config_path()).resolve()
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    raw: dict[str, Any] = loaded
    return AppConfig(
        path=path,
        raw=raw,
        root_dir=_runtime_root(path),
        prompts_dir=_prompts_dir(path),
    )


def _default_config_path() -> Path:
    if SOURCE_CONFIG_PATH.is_file():
        return SOURCE_CONFIG_PATH
    if INSTALLED_CONFIG_PATH.is_file():
        return INSTALLED_CONFIG_PATH
    raise FileNotFoundError("default configuration is unavailable; reinstall the package or set LAGENT_CONFIG")


def _runtime_root(config_path: Path) -> Path:
    override = os.environ.get("LAGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if config_path == INSTALLED_CONFIG_PATH.resolve():
        return Path.cwd().resolve()
    return config_path.parents[1]


def _prompts_dir(config_path: Path) -> Path:
    adjacent = config_path.parent / "prompts"
    if adjacent.is_dir():
        return adjacent
    for candidate in (SOURCE_CONFIG_PATH.parent / "prompts", INSTALLED_CONFIG_DIR / "prompts"):
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "prompt resources are unavailable; reinstall the package or place prompts beside the config"
    )
