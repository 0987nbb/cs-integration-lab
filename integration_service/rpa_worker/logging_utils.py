# -*- coding: utf-8 -*-
"""
Structured Logging utility with automatic sensitive data redaction.
Prevents logging API keys, passwords, bearer tokens, or secrets.
"""
import json
import logging
import re
import sys
from typing import Any, Dict


# Sensitive key pattern matchers
SENSITIVE_KEYS_RE = re.compile(
    r"(api_key|apikey|password|passwd|secret|token|bearer|authorization|pwd)",
    re.IGNORECASE,
)

# Sensitive value pattern matchers (e.g. bearer tokens, long hex keys)
SENSITIVE_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(api_key=)[A-Za-z0-9\-\._~]+", re.IGNORECASE), r"\1[REDACTED]"),
]


def sanitize_sensitive_data(val: Any) -> Any:
    """Recursively scrub dictionaries, lists, or strings of sensitive credentials."""
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            if SENSITIVE_KEYS_RE.search(str(k)):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = sanitize_sensitive_data(v)
        return cleaned
    elif isinstance(val, list):
        return [sanitize_sensitive_data(item) for item in val]
    elif isinstance(val, str):
        res = val
        for pat, repl in SENSITIVE_PATTERNS:
            res = pat.sub(repl, res)
        return res
    return val


class RedactingJsonFormatter(logging.Formatter):
    """Logging formatter that outputs structured JSON and scrubs sensitive fields."""

    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "job_id"):
            data["job_id"] = getattr(record, "job_id")
        if hasattr(record, "job_ref"):
            data["job_ref"] = getattr(record, "job_ref")
        if hasattr(record, "job_type"):
            data["job_type"] = getattr(record, "job_type")
        if hasattr(record, "step"):
            data["step"] = getattr(record, "step")

        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)

        sanitized = sanitize_sensitive_data(data)
        return json.dumps(sanitized)


def get_worker_logger(name: str = "rpa_worker") -> logging.Logger:
    """Configures and returns a structured logger with credential redaction."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(RedactingJsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger
