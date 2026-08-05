# -*- coding: utf-8 -*-
"""Resilient HTTP layer shared by all five connectors.

Responsibilities kept in one place so no connector has to re-implement them:

* connect/read **timeouts** on every request;
* **retry with exponential backoff** plus jitter for transient failures;
* **HTTP 429** handling that honours ``Retry-After`` (delta-seconds or HTTP-date);
* **HTTP 5xx** handling as a retryable class, 4xx as terminal;
* **pagination** over ``Link: rel="next"`` and over page/offset query parameters;
* **sanitised** errors - URLs, headers and bodies pass through
  :mod:`integration_service.sanitize` before they appear in a message or a log.
"""
from __future__ import annotations

import email.utils
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

import requests

from .config import HttpSettings
from .errors import (
    ClientError,
    HttpError,
    InvalidPayloadError,
    RateLimitError,
    ServerError,
    TimeoutError_,
)
from .sanitize import sanitize, sanitize_headers, truncate

LOGGER = logging.getLogger("integration_service.http")

#: Statuses worth retrying. 429 is handled separately so Retry-After can win.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Longest a single ``Retry-After`` may park the run for, in seconds.
MAX_RETRY_AFTER = 120.0


@dataclass
class HttpResponse:
    """A successful HTTP response, already parsed where applicable."""

    status_code: int
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    data: Any = None
    text: str = ""

    @property
    def is_empty(self) -> bool:
        """True for ``204 No Content`` and for an empty body.

        Nager.Date answers 204 for a country it has no data for, which is a
        legitimate "nothing to import" outcome rather than a failure.
        """
        return self.status_code == 204 or (self.data is None and not self.text.strip())

    def link(self, rel: str) -> Optional[str]:
        """Return the URL for ``rel`` from an RFC-8288 ``Link`` header."""
        return parse_link_header(self.headers.get("Link") or self.headers.get("link")).get(rel)


def parse_link_header(value: Optional[str]) -> Dict[str, str]:
    """Parse ``Link: <url>; rel="next", <url>; rel="last"`` into ``{rel: url}``."""
    links: Dict[str, str] = {}
    if not value:
        return links
    for part in value.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip()
        if not (url.startswith("<") and url.endswith(">")):
            continue
        url = url[1:-1]
        for param in section[1:]:
            if "=" not in param:
                continue
            key, _, val = param.partition("=")
            if key.strip().lower() == "rel":
                links[val.strip().strip('"\'')] = url
    return links


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Interpret a ``Retry-After`` header as a non-negative delay in seconds.

    Accepts both forms allowed by RFC 9110: delta-seconds and an HTTP-date.
    Returns ``None`` when the header is absent or unparseable, and clamps the
    result to :data:`MAX_RETRY_AFTER` so a hostile header cannot stall a run.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    target = parsed.timestamp()
    reference = time.time() if now is None else now
    return max(0.0, min(target - reference, MAX_RETRY_AFTER))


class ResilientHttpClient:
    """A ``requests`` session wrapper implementing the reliability contract.

    Args:
        settings: timeout / retry / backoff budget.
        default_headers: merged into every request (e.g. a provider Accept type).
        sleep: injected for tests so backoff does not consume wall-clock time.
        jitter: disable to make backoff delays deterministic in tests.
        session: injected ``requests.Session`` (tests, connection pooling).
    """

    def __init__(
        self,
        settings: Optional[HttpSettings] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: bool = True,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.settings = settings or HttpSettings()
        self._sleep = sleep
        self._jitter = jitter
        self.session = session or requests.Session()
        self.default_headers: Dict[str, str] = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json",
        }
        if default_headers:
            self.default_headers.update(default_headers)
        #: Populated for observability; values are never logged verbatim.
        self.last_status: Optional[int] = None

    # -- public API ---------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("PUT", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        expect_json: bool = True,
        max_retries: Optional[int] = None,
    ) -> HttpResponse:
        """Perform a request, retrying transient failures.

        Raises:
            TimeoutError_: the timeout budget was exhausted on every attempt.
            RateLimitError: HTTP 429 persisted past the retry budget.
            ServerError: HTTP 5xx persisted past the retry budget.
            ClientError: HTTP 4xx (terminal, never retried).
            InvalidPayloadError: ``expect_json`` and the body is not JSON.
        """
        attempts = (self.settings.max_retries if max_retries is None else max_retries) + 1
        request_headers = dict(self.default_headers)
        if headers:
            request_headers.update(headers)
        effective_timeout = self.settings.timeout if timeout is None else timeout
        safe_url = sanitize(url)
        last_error: Optional[HttpError] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json,
                    headers=request_headers,
                    timeout=effective_timeout,
                )
            except requests.exceptions.Timeout:
                last_error = TimeoutError_(
                    f"{method.upper()} {safe_url} timed out after {effective_timeout}s "
                    f"(attempt {attempt}/{attempts})",
                    url=safe_url,
                )
            except requests.exceptions.ConnectionError as exc:
                last_error = HttpError(
                    f"{method.upper()} {safe_url} connection error "
                    f"(attempt {attempt}/{attempts}): {truncate(exc, 200)}",
                    url=safe_url,
                    retryable=True,
                )
            except requests.exceptions.RequestException as exc:
                # Malformed URL, too many redirects, ... - not worth retrying.
                raise HttpError(
                    f"{method.upper()} {safe_url} failed: {truncate(exc, 200)}",
                    url=safe_url,
                    retryable=False,
                ) from None
            else:
                self.last_status = response.status_code
                outcome = self._evaluate(method, safe_url, response, attempt, attempts, expect_json)
                if isinstance(outcome, HttpResponse):
                    return outcome
                last_error = outcome

            if attempt >= attempts or not (last_error and last_error.retryable):
                break
            delay = self._delay_for(attempt, last_error)
            LOGGER.warning(
                "Retrying %s %s in %.2fs (attempt %d/%d): %s",
                method.upper(), safe_url, delay, attempt, attempts, sanitize(last_error),
            )
            self._sleep(delay)

        assert last_error is not None  # loop always assigns before breaking
        raise last_error

    def paginate(
        self,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        max_pages: int = 100,
        **kwargs: Any,
    ) -> Iterator[HttpResponse]:
        """Yield each page of a ``Link``-header paginated collection.

        The first request carries ``params``; subsequent requests follow the
        absolute ``rel="next"`` URL, which already embeds its own query string.
        ``max_pages`` bounds the walk so a malformed or cyclic ``Link`` chain
        cannot loop forever.
        """
        next_url: Optional[str] = url
        next_params = dict(params) if params else None
        seen: set = set()
        pages = 0

        while next_url and pages < max_pages:
            if next_url in seen:
                LOGGER.warning("Pagination cycle detected at %s; stopping.", sanitize(next_url))
                break
            seen.add(next_url)
            response = self.get(next_url, params=next_params, **kwargs)
            yield response
            pages += 1
            next_url = response.link("next")
            next_params = None  # the rel="next" URL is already fully qualified

        if pages >= max_pages and next_url:
            LOGGER.warning(
                "Stopped paginating %s after %d pages (max_pages reached).",
                sanitize(url), max_pages,
            )

    # -- internals ----------------------------------------------------------

    def _evaluate(
        self,
        method: str,
        safe_url: str,
        response: requests.Response,
        attempt: int,
        attempts: int,
        expect_json: bool,
    ) -> Any:
        """Turn a raw response into an :class:`HttpResponse` or an error to retry."""
        status = response.status_code
        headers = dict(response.headers)

        if status == 429:
            retry_after = parse_retry_after(
                headers.get("Retry-After") or headers.get("retry-after")
            )
            if retry_after is None:
                retry_after = self._reset_header_delay(headers)
            return RateLimitError(
                f"{method} {safe_url} rate limited (HTTP 429, attempt {attempt}/{attempts})"
                + (f"; Retry-After={retry_after:.0f}s" if retry_after is not None else ""),
                url=safe_url,
                retry_after=retry_after,
                body=truncate(response.text, 200),
            )

        if status in RETRYABLE_STATUSES or status >= 500:
            return ServerError(
                f"{method} {safe_url} failed with HTTP {status} "
                f"(attempt {attempt}/{attempts}): {truncate(response.text, 200)}",
                status_code=status,
                url=safe_url,
                body=truncate(response.text, 200),
            )

        if 400 <= status < 500:
            raise ClientError(
                f"{method} {safe_url} failed with HTTP {status}: {truncate(response.text, 300)}",
                status_code=status,
                url=safe_url,
                body=truncate(response.text, 300),
            )

        body = response.text or ""
        data: Any = None
        if status != 204 and body.strip():
            try:
                data = response.json()
            except ValueError:
                if expect_json:
                    raise InvalidPayloadError(
                        f"{method} {safe_url} returned HTTP {status} with a non-JSON body: "
                        f"{truncate(body, 200)}"
                    ) from None
        return HttpResponse(status_code=status, url=safe_url, headers=headers, data=data, text=body)

    @staticmethod
    def _reset_header_delay(headers: Mapping[str, str]) -> Optional[float]:
        """Derive a delay from GitHub's ``X-RateLimit-Reset`` epoch header."""
        raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        if not raw:
            return None
        try:
            return max(0.0, min(float(raw) - time.time(), MAX_RETRY_AFTER))
        except ValueError:
            return None

    def _delay_for(self, attempt: int, error: Optional[HttpError]) -> float:
        """Exponential backoff, overridden by a server-advised ``Retry-After``."""
        if isinstance(error, RateLimitError) and error.retry_after is not None:
            return float(error.retry_after)
        delay = self.settings.backoff_factor * (2 ** (attempt - 1))
        delay = min(delay, self.settings.backoff_max)
        if self._jitter:
            # Full-jitter smooths retry storms when several connectors overlap.
            delay = random.uniform(delay / 2.0, delay)
        return delay

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "ResilientHttpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def redacted_request_log(method: str, url: str, headers: Optional[Mapping[str, str]] = None) -> str:
    """Build a one-line, credential-free description of an outbound request."""
    return f"{method.upper()} {sanitize(url)} headers={sanitize_headers(dict(headers or {}))}"


__all__ = [
    "HttpResponse",
    "ResilientHttpClient",
    "RETRYABLE_STATUSES",
    "parse_link_header",
    "parse_retry_after",
    "redacted_request_log",
]
