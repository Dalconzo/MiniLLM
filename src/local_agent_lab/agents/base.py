from __future__ import annotations


def render_prompt(task: str, question: str) -> str:
    return f"Task: {task}\n\nUser request:\n{question}\n"
