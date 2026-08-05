# -*- coding: utf-8 -*-
"""Secret redaction.

Nothing this service logs, stores in ``x_integration_sync_log.x_error_details``
or raises as an exception message may contain a credential. Two mechanisms are
combined:

* **Registered values** - concrete secrets loaded from ``.env`` are registered at
  startup via :func:`register_secret` and replaced wherever they appear.
* **Structural patterns** - ``Authorization: Bearer ...`` headers, ``token=...``
  query parameters, ``"api_key": "..."`` JSON fragments and bare GitHub token
  literals are redacted even if the value was never registered.

:func:`sanitize` is deliberately cheap and total: it never raises, and it
accepts any object (non-strings are coerced with ``str``).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Set

REDACTED = "[REDACTED]"

#: Secrets registered at runtime. Longest first so that a token that contains
#: another registered value as a prefix is still fully masked.
_SECRETS: Set[str] = set()

#: Minimum length for a registered secret. Short values ("1", "true") would
#: otherwise corrupt unrelated log text.
_MIN_SECRET_LEN = 8

_PATTERNS = (
    # Authorization: Bearer <token>  /  Authorization: Basic <blob>
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|basic|token)?\s*\S+"),
     lambda m: f"{m.group(1)}{(m.group(2) or '')} {REDACTED}".replace("  ", " ")),
    # ?token=... &access_token=... &api_key=... &apikey=... &key=... &password=...
    (re.compile(r"(?i)([?&](?:access_token|api[_-]?key|apikey|token|password|secret|passwd|pwd)=)[^&\s\"']+"),
     lambda m: f"{m.group(1)}{REDACTED}"),
    # JSON / dict fragments: "api_key": "..."  'token': '...'
    (re.compile(r"""(?i)(["']?(?:access_token|api[_-]?key|apikey|token|password|secret|client[_-]?secret)["']?\s*[:=]\s*)(["'])[^"']*(\2)"""),
     lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(3)}"),
    # Bare GitHub credentials, which are self-identifying by prefix.
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b"), lambda m: REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), lambda m: REDACTED),
    # Odoo/basic-auth credentials embedded in a URL: https://user:pass@host
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), lambda m: f"{m.group(1)}{REDACTED}@"),
)


def register_secret(value: Optional[str]) -> None:
    """Register a concrete secret so it is masked wherever it appears.

    Values shorter than 8 characters and obvious placeholders are ignored, so
    that an unset or example credential cannot blank out unrelated log output.
    """
    if not value or not isinstance(value, str):
        return
    stripped = value.strip()
    if len(stripped) < _MIN_SECRET_LEN:
        return
    if stripped.lower() in {
        "your_api_key", "your_token", "changeme", "placeholder",
        "your_database", "none", "null", "false", "true",
    }:
        return
    _SECRETS.add(stripped)


def register_secrets(values: Iterable[Optional[str]]) -> None:
    """Register several secrets at once."""
    for value in values:
        register_secret(value)


def clear_secrets() -> None:
    """Drop every registered secret. Used by the test-suite for isolation."""
    _SECRETS.clear()


def sanitize(value: Any) -> str:
    """Return ``value`` as a string with every known credential redacted.

    Never raises: an object whose ``__str__`` explodes degrades to a marker
    string rather than masking the original error with a new one.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - defensive
        return "[unprintable value]"

    # Registered literals first, longest first.
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)

    for pattern, repl in _PATTERNS:
        try:
            text = pattern.sub(repl, text)
        except Exception:  # pragma: no cover - defensive
            continue
    return text


def sanitize_headers(headers: Optional[dict]) -> dict:
    """Return a copy of ``headers`` safe to log.

    Sensitive header values are replaced wholesale rather than pattern-matched,
    because a header value is a credential in its entirety.
    """
    if not headers:
        return {}
    sensitive = {
        "authorization", "x-api-key", "api-key", "x-odoo-database",
        "cookie", "set-cookie", "proxy-authorization", "x-auth-token",
    }
    return {
        key: (REDACTED if key.lower() in sensitive else sanitize(val))
        for key, val in headers.items()
    }


def truncate(text: Any, limit: int = 500) -> str:
    """Sanitise ``text`` and clip it to ``limit`` characters."""
    out = sanitize(text)
    if len(out) <= limit:
        return out
    return f"{out[:limit]}... [truncated {len(out) - limit} chars]"
