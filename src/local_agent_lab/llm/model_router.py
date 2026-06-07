from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig, ModelProfile


@dataclass(frozen=True)
class RouteDecision:
    task: str
    profile: ModelProfile
    label: str


def route_task(config: AppConfig, task: str, model_alias: str | None = None) -> RouteDecision:
    alias = config.resolve_task_alias(task, model_alias)
    profile = config.get_profile(alias)
    return RouteDecision(task=task, profile=profile, label=profile.routing_label)
