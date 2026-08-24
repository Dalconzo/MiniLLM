from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from .config import ModelProfile


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


class OllamaError(RuntimeError):
    pass


@dataclass
class GenerationResult:
    model: str
    response: str
    total_duration: int | None
    load_duration: int | None
    prompt_eval_count: int | None
    eval_count: int | None


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.URLError as exc:
        raise OllamaError(f"failed to call Ollama at {url}: {exc}") from exc


def _get_json(url: str) -> dict:
    try:
        with request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.URLError as exc:
        raise OllamaError(f"failed to call Ollama at {url}: {exc}") from exc


def generate(profile: ModelProfile, prompt: str, system: str | None = None) -> GenerationResult:
    payload = {
        "model": profile.model,
        "prompt": prompt,
        "system": system or "",
        "stream": False,
        "options": {
            "temperature": profile.temperature,
            "num_ctx": profile.num_ctx,
            "top_p": profile.top_p,
            "repeat_penalty": profile.repeat_penalty,
        },
    }
    data = _post_json(OLLAMA_URL, payload)
    return GenerationResult(
        model=data.get("model", profile.model),
        response=data.get("response", "").strip(),
        total_duration=data.get("total_duration"),
        load_duration=data.get("load_duration"),
        prompt_eval_count=data.get("prompt_eval_count"),
        eval_count=data.get("eval_count"),
    )


def list_models() -> list[dict]:
    data = _get_json(OLLAMA_TAGS_URL)
    return data.get("models", [])
