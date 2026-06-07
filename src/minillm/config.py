from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "models.json"
PROMPTS_DIR = ROOT / "prompts"


@dataclass(frozen=True)
class ModelProfile:
    alias: str
    provider: str
    model: str
    temperature: float
    num_ctx: int
    top_p: float
    repeat_penalty: float


class Settings:
    def __init__(self, data: dict):
        self._data = data

    @property
    def default_model(self) -> str:
        return self._data["default_model"]

    @property
    def task_defaults(self) -> dict[str, str]:
        return self._data.get("task_defaults", {})

    def get_profile(self, alias: str) -> ModelProfile:
        raw = self._data["models"][alias]
        return ModelProfile(alias=alias, **raw)

    def resolve_alias(self, task: str | None, model: str | None) -> str:
        if model:
            return model
        if task and task in self.task_defaults:
            return self.task_defaults[task]
        return self.default_model

    def list_profiles(self) -> list[ModelProfile]:
        return [self.get_profile(alias) for alias in self._data["models"]]


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    return Settings(json.loads(config_path.read_text()))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}_system.txt").read_text().strip()
