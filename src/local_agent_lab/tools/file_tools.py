from __future__ import annotations

import re


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")
AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\r\n]+)@")
AUTHORIZATION_HEADER_RE = re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*:\s*)(?:bearer\s+)?[^\r\n]+$")
BEARER_TOKEN_RE = re.compile(r"(?im)\bbearer\s+[^\r\n,;}}\]]+")
_ASSIGNMENT_NAME = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|client[_-]?secret|"
    r"token|password|passwd|pwd|secret|aws_secret_access_key"
)
QUOTED_ASSIGNMENT_RE = re.compile(rf"""(?im)(["']?(?:{_ASSIGNMENT_NAME})["']?\s*[:=]\s*)(["'])([^"'\r\n]+)(["'])""")
ASSIGNMENT_RE = re.compile(
    rf"(?im)(\b(?:{_ASSIGNMENT_NAME})\b\s*[:=]\s*)(?![\"'])(.+?)"
    rf"(?=\s+\b(?:{_ASSIGNMENT_NAME})\b\s*[:=]|[\r\n,;}}&]|$)"
)


def redact_text(text: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = OPENAI_KEY_RE.sub("[REDACTED_OPENAI_KEY]", redacted)
    redacted = GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = AWS_ACCESS_KEY_RE.sub("[REDACTED_AWS_ACCESS_KEY]", redacted)
    redacted = SLACK_TOKEN_RE.sub("[REDACTED_SLACK_TOKEN]", redacted)
    redacted = JWT_RE.sub("[REDACTED_JWT]", redacted)
    redacted = URL_USERINFO_RE.sub(lambda match: f"{match.group(1)}[REDACTED]@", redacted)
    redacted = QUOTED_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}",
        redacted,
    )
    redacted = AUTHORIZATION_HEADER_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
    redacted = BEARER_TOKEN_RE.sub("Bearer [REDACTED]", redacted)
    redacted = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
    return redacted
