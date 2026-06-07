from __future__ import annotations

from pathlib import Path

from .config import load_prompt


TASK_TO_PROMPT = {
    "chat": "chat",
    "summarize": "summarize",
    "code": "code",
    "classify": "classify",
}


def system_prompt_for(task: str) -> str:
    prompt_name = TASK_TO_PROMPT.get(task, "chat")
    return load_prompt(prompt_name)


def build_prompt(task: str, text: str) -> str:
    if task == "summarize":
        return f"Summarize the following text:\n\n{text.strip()}"
    if task == "code":
        return f"Solve this coding task. Return code first.\n\n{text.strip()}"
    if task == "classify":
        return (
            "Classify the following input. Return JSON with keys "
            '"label", "reason", and "confidence". Return raw JSON only with no markdown fences.\n\n'
            f"{text.strip()}"
        )
    return text.strip()


def read_input(input_text: str | None, input_file: str | None) -> str:
    if input_text:
        return input_text
    if input_file:
        return Path(input_file).read_text()
    raise ValueError("either inline text or --file is required")
