from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise OllamaError(f"unable to reach Ollama at {self.host}: {exc}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.host}{path}", timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
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
        return self._post(
            "/api/generate",
            {
                "model": model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"temperature": temperature},
            },
        )

    def embed(self, *, model: str, text: str) -> list[float]:
        try:
            payload = self._post("/api/embed", {"model": model, "input": text})
            embeddings = payload.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                return [float(value) for value in embeddings[0]]
        except OllamaError:
            payload = self._post("/api/embeddings", {"model": model, "prompt": text})
            embedding = payload.get("embedding")
            if isinstance(embedding, list):
                return [float(value) for value in embedding]
            raise

        raise OllamaError(f"Ollama returned no embedding for model {model}")
