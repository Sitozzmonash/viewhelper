"""Logging setup with mandatory secret redaction (PRD 22 / 32.12).

Nothing in this module may print an API key, the relay secret or a full base64
screenshot. :func:`redact` is applied to every record through a logging filter so
that even accidental ``logger.info("key=%s", api_key)`` calls stay safe.
"""

from __future__ import annotations

import logging
import re
import sys

__all__ = ["redact", "RedactingFilter", "setup_logging", "get_logger"]

_MASK = "***REDACTED***"

# data:image/png;base64,iVBORw0....  ->  keep the mime prefix only
_DATA_URL_RE = re.compile(
    r"data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<body>[A-Za-z0-9+/=\s]{64,})"
)
# Bare base64 blobs (screenshots that were logged without the data-url prefix).
_LONG_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{4096,}={0,2}(?![A-Za-z0-9+/])")
# sk-abc123... / key-abc123... style provider keys: keep a short prefix.
_PROVIDER_KEY_RE = re.compile(r"\b(?P<prefix>(?:sk|pk|rk|key|ak)-)[A-Za-z0-9_\-]{8,}\b")
# key=value / "key": "value" pairs for known secret-ish names.
_SECRET_KV_RE = re.compile(
    r"(?P<name>api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth(?:orization)?"
    r"|token|secret|password|passwd|relay_secret|llm_api_key|vision_api_key)"
    r"(?P<sep>\s*[:=]\s*|\s*=\s*)(?P<quote>[\"']?)(?P<value>[^\"',;\s\}\]]{4,})",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{6,}")


def redact(text: str) -> str:
    """Return *text* with secrets and oversized base64 payloads masked."""
    if not text:
        return text

    def _data_url(match: re.Match[str]) -> str:
        body = re.sub(r"\s", "", match.group("body"))
        return f"data:{match.group('mime')};base64,<{len(body)} chars { _MASK }>"

    def _kv(match: re.Match[str]) -> str:
        value = match.group("value")
        hint = f"{value[:2]}…({len(value)} chars)" if len(value) > 4 else _MASK
        return f"{match.group('name')}{match.group('sep')}{match.group('quote')}{hint}"

    text = _DATA_URL_RE.sub(_data_url, text)
    text = _LONG_BASE64_RE.sub(f"<base64 blob { _MASK }>", text)
    text = _PROVIDER_KEY_RE.sub(lambda m: f"{m.group('prefix')}{ _MASK }", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{ _MASK }", text)
    text = _SECRET_KV_RE.sub(_kv, text)
    return text


class RedactingFilter(logging.Filter):
    """Applies :func:`redact` to the rendered message of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken args must not kill logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def setup_logging(level: str = "INFO", *, stream: object | None = None) -> logging.Logger:
    """Configure the root logger once and return it."""
    root = logging.getLogger()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    if not isinstance(numeric, int):
        numeric = logging.INFO
    root.setLevel(numeric)

    handler_stream = stream if stream is not None else sys.stderr
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(handler_stream)  # type: ignore[arg-type]
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    return root


def get_logger(name: str) -> logging.Logger:
    """Logger for *name* with the redaction filter attached defensively."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
