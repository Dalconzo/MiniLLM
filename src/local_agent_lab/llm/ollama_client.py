from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..tools.file_tools import redact_text


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, timeout_seconds: int = 180) -> None:
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return _decode_object(response.read())
        except urllib.error.URLError as exc:
            raise OllamaError(f"unable to reach Ollama at {self.host}: {exc}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.host}{path}", timeout=self.timeout_seconds) as response:
                return _decode_object(response.read())
        except urllib.error.URLError as exc:
            raise OllamaError(f"unable to reach Ollama at {self.host}: {exc}") from exc

    def list_models(self) -> list[str]:
        payload = self._get("/api/tags")
        return [item["name"] for item in payload.get("models", [])]

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        system: str,
        temperature: float,
    ) -> dict[str, Any]:
        # This is the final process boundary; callers cannot bypass egress redaction.
        safe_prompt = redact_text(prompt)
        safe_system = redact_text(system)
        return self._post(
            "/api/generate",
            {
                "model": model,
                "prompt": safe_prompt,
                "system": safe_system,
                "stream": False,
                "options": {"temperature": temperature},
            },
        )

    def embed(self, *, model: str, text: str) -> list[float]:
        safe_text = redact_text(text)
        try:
            payload = self._post("/api/embed", {"model": model, "input": safe_text})
            embeddings = payload.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                return [float(value) for value in embeddings[0]]
        except OllamaError:
            payload = self._post("/api/embeddings", {"model": model, "prompt": safe_text})
            embedding = payload.get("embedding")
            if isinstance(embedding, list):
                return [float(value) for value in embedding]
            raise

        raise OllamaError(f"Ollama returned no embedding for model {model}")


def _decode_object(payload: bytes) -> dict[str, Any]:
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise OllamaError("Ollama returned a non-object JSON response")
    return parsed
