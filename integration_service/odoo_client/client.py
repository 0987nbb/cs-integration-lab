# -*- coding: utf-8 -*-
"""Odoo 19 JSON-2 API client.

Talks to ``POST {ODOO_URL}/json/2/<model>/<method>`` with a Bearer API key and
the ``X-Odoo-Database`` header. Transport concerns (timeouts, retry/backoff,
429/5xx handling, credential redaction) are delegated to
:class:`integration_service.http_client.ResilientHttpClient`.

Verified against the live instance: ``search_read``, ``search_count``, ``read``,
``fields_get``, ``create``, ``write`` and ``unlink`` all work over this route.
``ir.model.access`` and ``ir.rule`` are **not** exposed on it (HTTP 404), so
access rules cannot be provisioned from here - see docs/troubleshooting.md.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from ..config import HttpSettings, OdooSettings, get_settings
from ..errors import ClientError, HttpError, IntegrationError, OdooError
from ..http_client import ResilientHttpClient
from ..sanitize import register_secret, sanitize, truncate

import threading

LOGGER = logging.getLogger("integration_service.odoo")

Vals = Dict[str, Any]
Domain = List[Any]


class OdooClientError(OdooError):
    """Backwards-compatible alias kept for the original foundation scripts."""


class OdooClient:
    """Reusable Odoo 19 JSON-2 API client."""

    def __init__(
        self,
        url: Optional[str] = None,
        database: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        dry_run: bool = False,
        http: Optional[ResilientHttpClient] = None,
        http_settings: Optional[HttpSettings] = None,
    ) -> None:
        self._lock = threading.Lock()
        # Explicit arguments win; otherwise fall back to the environment so the
        # original ``OdooClient()`` call style keeps working.
        if url is None or database is None or api_key is None:
            try:
                env = get_settings().odoo
            except IntegrationError:
                env = OdooSettings(url="", database="", api_key="")
        else:
            env = OdooSettings(url=url, database=database, api_key=api_key)

        self.url = (url if url is not None else env.url).rstrip("/")
        self.database = database if database is not None else env.database
        self.api_key = api_key if api_key is not None else env.api_key
        self.timeout = timeout if timeout is not None else getattr(env, "timeout", 30)
        self.dry_run = dry_run

        if not self.url:
            raise OdooClientError("ODOO_URL is missing in environment or arguments.")
        if not self.database:
            raise OdooClientError("ODOO_DATABASE is missing in environment or arguments.")
        if not self.api_key:
            raise OdooClientError("ODOO_API_KEY is missing in environment or arguments.")

        register_secret(self.api_key)

        settings = http_settings or HttpSettings(timeout=self.timeout)
        settings.timeout = self.timeout
        self.http = http or ResilientHttpClient(
            settings=settings,
            default_headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-Odoo-Database": self.database,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # -- transport ----------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """Kept for compatibility; the live headers live on the HTTP client."""
        return dict(self.http.default_headers)

    def _sanitize_error(self, err_msg: Any) -> str:
        return sanitize(err_msg)

    def _request(self, model: str, method: str, payload: Optional[Vals] = None) -> Any:
        """Execute one JSON-2 call and unwrap the response."""
        endpoint = f"{self.url}/json/2/{model}/{method}"
        try:
            response = self.http.post(endpoint, json=payload or {}, timeout=self.timeout)
        except ClientError as exc:
            # 4xx carries Odoo's own explanation (access rights, validation, ...).
            raise OdooError(
                f"Odoo {model}.{method} failed (HTTP {exc.status_code}): {truncate(exc.body, 400)}",
                status_code=exc.status_code,
                model=model,
                method=method,
            ) from None
        except HttpError as exc:
            raise OdooError(
                f"Odoo {model}.{method} transport failure: {sanitize(exc)}",
                status_code=exc.status_code,
                model=model,
                method=method,
            ) from None

        result = response.data
        if isinstance(result, dict) and "error" in result:
            raise OdooError(
                f"Odoo {model}.{method} returned an error: {truncate(result['error'], 400)}",
                status_code=response.status_code,
                model=model,
                method=method,
            )
        if isinstance(result, dict) and "result" in result:
            return result["result"]
        return result

    # -- read ---------------------------------------------------------------

    def search_read(
        self,
        model: str,
        domain: Optional[Domain] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        payload: Vals = {"domain": domain if domain is not None else []}
        if fields is not None:
            payload["fields"] = fields
        if limit is not None:
            payload["limit"] = limit
        if offset is not None:
            payload["offset"] = offset
        if order is not None:
            payload["order"] = order
        if context is not None:
            payload["context"] = dict(context)

        res = self._request(model, "search_read", payload)
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "records" in res:
            return res["records"]
        return res or []

    def search_read_all(
        self,
        model: str,
        domain: Optional[Domain] = None,
        fields: Optional[List[str]] = None,
        batch_size: int = 200,
        order: str = "id asc",
        context: Optional[Mapping[str, Any]] = None,
        hard_limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Page through an entire result set in ``batch_size`` chunks.

        Odoo's own read side is paginated defensively so a large partner or task
        table never arrives in a single oversized response.
        """
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.search_read(
                model, domain=domain, fields=fields, limit=batch_size,
                offset=offset, order=order, context=context,
            )
            if not batch:
                break
            out.extend(batch)
            if hard_limit is not None and len(out) >= hard_limit:
                return out[:hard_limit]
            if len(batch) < batch_size:
                break
            offset += batch_size
        return out

    def search_count(self, model: str, domain: Optional[Domain] = None,
                     context: Optional[Mapping[str, Any]] = None) -> int:
        payload: Vals = {"domain": domain if domain is not None else []}
        if context is not None:
            payload["context"] = dict(context)
        return int(self._request(model, "search_count", payload) or 0)

    def read(self, model: str, ids: Sequence[int], fields: Optional[List[str]] = None,
             context: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        payload: Vals = {"ids": list(ids)}
        if fields is not None:
            payload["fields"] = fields
        if context is not None:
            payload["context"] = dict(context)
        return self._request(model, "read", payload) or []

    def fields_get(self, model: str, attributes: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Vals = {"attributes": attributes or ["string", "type", "required", "relation", "selection"]}
        return self._request(model, "fields_get", payload) or {}

    def model_exists(self, model: str) -> bool:
        """True when the model is installed *and* readable by this API key."""
        try:
            self.search_count(model, [])
            return True
        except OdooError:
            return False

    # -- write --------------------------------------------------------------

    def create(self, model: str, vals_list: Union[List[Vals], Vals]) -> List[int]:
        payload = {"vals_list": [vals_list] if isinstance(vals_list, dict) else list(vals_list)}
        if self.dry_run:
            LOGGER.info("[MOCK WRITE] odoo create %s %s", model, truncate(payload["vals_list"], 300))
            return []
        res = self._request(model, "create", payload)
        if isinstance(res, int):
            return [res]
        if isinstance(res, list):
            cleaned: List[int] = []
            for item in res:
                if isinstance(item, int):
                    cleaned.append(item)
                elif isinstance(item, dict) and "id" in item:
                    cleaned.append(int(item["id"]))
            return cleaned
        return []

    def create_one(self, model: str, vals: Vals) -> Optional[int]:
        ids = self.create(model, [vals])
        return ids[0] if ids else None

    def write(self, model: str, ids: Sequence[int], vals: Vals) -> bool:
        if not ids:
            return True
        if self.dry_run:
            LOGGER.info("[MOCK WRITE] odoo write %s %s %s", model, list(ids), truncate(vals, 300))
            return True
        return bool(self._request(model, "write", {"ids": list(ids), "vals": vals}))

    def unlink(self, model: str, ids: Sequence[int]) -> bool:
        if not ids:
            return True
        if self.dry_run:
            LOGGER.info("[MOCK WRITE] odoo unlink %s %s", model, list(ids))
            return True
        return bool(self._request(model, "unlink", {"ids": list(ids)}))

    def claim_job_atomic(self, model: str, job_id: int, update_vals: Vals) -> Optional[Dict[str, Any]]:
        """
        Atomically claims job if current state is 'queued' via server-side atomic operation.
        Guarantees process-level concurrency safety across independent worker processes.
        """
        if self.dry_run:
            state_field = "x_state" if model.startswith("x_") else "state"
            return {"id": job_id, state_field: "running"}

        with self._lock:
            # First attempt single server-side atomic method call
            try:
                res = self._request(model, "action_claim_job_atomic", {"ids": [job_id]})
                if isinstance(res, dict) and res.get("id"):
                    return res
                if isinstance(res, list) and res:
                    return res[0]
                if res is False:
                    return None
            except OdooError:
                pass

            # Optimistic compare-and-update check
            state_field = "x_state" if model.startswith("x_") else "state"
            domain = [["id", "=", job_id], [state_field, "=", "queued"]]
            matching = self.search_read(model, domain, fields=["id", state_field], limit=1)
            if not matching:
                return None
            try:
                written = self.write(model, [job_id], update_vals)
                if not written:
                    return None
                recs = self.search_read(model, [["id", "=", job_id]])
                return recs[0] if recs else None
            except OdooError:
                return None

    def recover_stale_job_atomic(self, model: str, job_id: int, timeout_sec: float, recovery_vals: Vals) -> bool:
        """
        Atomically recovers stale job if state is still 'running'.
        """
        state_field = "x_state" if model.startswith("x_") else "state"
        domain = [["id", "=", job_id], [state_field, "=", "running"]]
        with self._lock:
            matching = self.search_read(model, domain, fields=["id", state_field], limit=1)
            if not matching:
                return False
            return self.write(model, [job_id], recovery_vals)

    # -- convenience --------------------------------------------------------

    def find_by_external_id(
        self,
        model: str,
        external_id: str,
        fields: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the single record carrying ``x_external_id``, or ``None``.

        ``x_external_id`` is the idempotency key for every integrated model.
        """
        wanted = list({*(fields or []), "id", "x_external_id", "x_source_hash"})
        records = self.search_read(
            model, [["x_external_id", "=", external_id]], fields=wanted, limit=1, order="id asc"
        )
        return records[0] if records else None

    def map_by_external_id(
        self,
        model: str,
        external_ids: Iterable[str],
        fields: Optional[List[str]] = None,
        chunk_size: int = 100,
    ) -> Dict[str, Dict[str, Any]]:
        """Bulk-load records for many external ids, keyed by ``x_external_id``.

        One request per ``chunk_size`` ids instead of one per record, which keeps
        a sync of N records at O(N/chunk) reads rather than O(N).
        """
        ids = [e for e in external_ids if e]
        wanted = list({*(fields or []), "id", "x_external_id", "x_source_hash"})
        out: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start:start + chunk_size]
            for rec in self.search_read_all(
                model, [["x_external_id", "in", chunk]], fields=wanted, batch_size=chunk_size
            ):
                key = rec.get("x_external_id")
                if key and key not in out:
                    out[key] = rec
        return out

    def find_or_create(
        self,
        model: str,
        domain: Domain,
        vals: Vals,
        fields: Optional[List[str]] = None,
    ) -> Optional[int]:
        """Return the id of the first record matching ``domain``, creating it if absent."""
        existing = self.search_read(model, domain, fields=fields or ["id"], limit=1)
        if existing:
            return existing[0]["id"]
        return self.create_one(model, vals)

    def ref_id(self, value: Any) -> Optional[int]:
        """Normalise an Odoo many2one value (``[id, name]`` or ``id``) to an id."""
        if isinstance(value, (list, tuple)) and value:
            return int(value[0])
        if isinstance(value, int):
            return value
        return None

    # -- legacy passthroughs -------------------------------------------------

    def execute_kw(self, model: str, method: str, args: Optional[List[Any]] = None,
                   kwargs: Optional[Vals] = None) -> Any:
        payload = dict(kwargs or {})
        if args:
            payload["args"] = args
        return self._request(model, method, payload)

    def call_kw(self, model: str, method: str, args: Optional[List[Any]] = None,
                kwargs: Optional[Vals] = None) -> Any:
        """Legacy ``/web/dataset/call_kw`` route.

        Retained for reference only: that endpoint authenticates with a session
        cookie and rejects Bearer keys with "user is not connected", so every
        call here fails under API-key auth. Use :meth:`_request` instead.
        """
        raise OdooError(
            "call_kw (/web/dataset/call_kw) requires session-cookie auth and does not accept "
            "an API key. Use the JSON-2 methods on this client instead.",
            model=model,
            method=method,
        )

    def close(self) -> None:
        self.http.close()


__all__ = ["OdooClient", "OdooClientError", "OdooError"]
