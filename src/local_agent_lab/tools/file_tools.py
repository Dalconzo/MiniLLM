from __future__ import annotations

import re


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
ASSIGNMENT_RE = re.compile(
    r"(?im)\b(api[_-]?key|token|password|secret|aws_secret_access_key)\b\s*[:=]\s*([^\s]+)"
)


def redact_text(text: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted
