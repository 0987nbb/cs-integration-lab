# -*- coding: utf-8 -*-
"""Idempotent upsert against Odoo, keyed on ``x_external_id``.

Every integrated Odoo model (``res.partner``, ``project.task``,
``helpdesk.ticket``, ``calendar.event``) carries the same three custom fields:

``x_external_id``
    Stable, globally unique key of the upstream record, built by
    :func:`make_external_id` as ``"<provider>:<entity>:<id>"``.
``x_source_hash``
    SHA-256 over the *mapped* Odoo values. Re-running a sync whose upstream data
    has not moved recomputes the same hash, so the record is skipped rather than
    rewritten - this is what makes repeated runs cheap and non-destructive.
``x_external_updated_at``
    The upstream ``updated_at``, used to decide direction in two-way sync.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .errors import OdooError, RecordSyncError
from .sanitize import sanitize
from .sync_result import SyncResult, to_odoo_datetime

#: Outcome of a single upsert.
CREATED = "created"
UPDATED = "updated"
SKIPPED = "skipped"

#: Fields excluded from the source hash: they are bookkeeping, not source data,
#: and including them would make every hash differ from the previous run.
_HASH_EXCLUDED = frozenset({"x_source_hash", "x_external_id", "x_external_updated_at"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: Any, max_length: int = 60) -> str:
    """Lowercase ASCII slug, safe to embed in an external id."""
    text = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return text[:max_length] or "unknown"


def make_external_id(provider: str, entity: str, *parts: Any) -> str:
    """Build the canonical ``x_external_id`` for an upstream record.

    >>> make_external_id("github", "issue", 4711)
    'github:issue:4711'
    >>> make_external_id("nager", "holiday", "US", "2026-01-01", "New Year's Day")
    'nager:holiday:US:2026-01-01:new-year-s-day'
    """
    tail = [str(p).strip() if _is_opaque(p) else slugify(p) for p in parts]
    return ":".join([provider, entity, *tail])


def _is_opaque(value: Any) -> bool:
    """True for values that must be preserved verbatim (ids, dates, codes)."""
    if isinstance(value, (int, float)):
        return True
    text = str(value or "")
    return bool(re.fullmatch(r"[A-Za-z0-9._\-]+", text))


def compute_source_hash(payload: Mapping[str, Any], exclude: Iterable[str] = ()) -> str:
    """SHA-256 over a canonical JSON rendering of ``payload``.

    Keys are sorted and non-JSON values are stringified so the digest is stable
    across runs and across Python processes.
    """
    excluded = _HASH_EXCLUDED | set(exclude)
    material = {k: v for k, v in dict(payload).items() if k not in excluded}
    serialized = json.dumps(material, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (including a trailing ``Z``) to aware UTC."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Upserter:
    """Applies the create / update / skip decision for one Odoo model.

    Existing records are pre-loaded in bulk with :meth:`preload` so a run of N
    upstream records costs O(N/chunk) reads instead of N.
    """

    def __init__(self, client: Any, model: str, result: SyncResult, dry_run: bool = False) -> None:
        self.client = client
        self.model = model
        self.result = result
        self.dry_run = dry_run
        self._cache: Dict[str, Dict[str, Any]] = {}

    def preload(self, external_ids: Iterable[str], fields: Optional[list] = None) -> None:
        """Bulk-load the records for ``external_ids`` into the local cache."""
        ids = [e for e in external_ids if e]
        if not ids:
            return
        try:
            self._cache.update(self.client.map_by_external_id(self.model, ids, fields=fields))
        except OdooError as exc:
            # Not fatal: fall back to per-record lookups.
            self.result.add_note(f"Bulk preload of {self.model} failed, using per-record lookups: {sanitize(exc)}")

    def find(self, external_id: str, fields: Optional[list] = None) -> Optional[Dict[str, Any]]:
        if external_id in self._cache:
            return self._cache[external_id]
        record = self.client.find_by_external_id(self.model, external_id, fields=fields)
        if record:
            self._cache[external_id] = record
        return record

    def upsert(
        self,
        external_id: str,
        vals: Dict[str, Any],
        external_updated_at: Optional[datetime] = None,
        extra_hash_input: Optional[Mapping[str, Any]] = None,
        create_only_vals: Optional[Dict[str, Any]] = None,
        count: bool = True,
    ) -> Tuple[str, Optional[int]]:
        """Create, update or skip one record.

        Args:
            external_id: value written to ``x_external_id``.
            vals: mapped Odoo values (without the three bookkeeping fields).
            external_updated_at: upstream modification time.
            extra_hash_input: additional material folded into the hash - use it
                when meaningful upstream data (comments, labels) does not itself
                land in ``vals``.
            create_only_vals: values applied on create but never on update, so a
                later manual edit in Odoo is not overwritten (e.g. ``project_id``).
            count: when False the outcome is returned without touching the
                run counters, letting the caller aggregate differently.

        Returns:
            ``(outcome, record_id)`` where outcome is ``created``/``updated``/``skipped``.
        """
        if not external_id:
            raise RecordSyncError("Refusing to upsert a record without an external id.")

        hash_material = dict(vals)
        if extra_hash_input:
            hash_material.update({f"__extra__{k}": v for k, v in extra_hash_input.items()})
        source_hash = compute_source_hash(hash_material)

        existing = self.find(external_id)
        payload = dict(vals)
        payload["x_external_id"] = external_id
        payload["x_source_hash"] = source_hash
        if external_updated_at is not None:
            payload["x_external_updated_at"] = to_odoo_datetime(external_updated_at)

        if existing is None:
            if create_only_vals:
                payload.update(create_only_vals)
            if self.dry_run:
                self.result.add_mock_write("CREATE", f"odoo://{self.model}", payload)
                if count:
                    self.result.record_created()
                return CREATED, None
            try:
                record_id = self.client.create_one(self.model, payload)
            except OdooError as exc:
                raise RecordSyncError(f"create on {self.model} failed: {sanitize(exc)}", external_id) from None
            if record_id:
                self._cache[external_id] = {
                    "id": record_id, "x_external_id": external_id, "x_source_hash": source_hash,
                }
            if count:
                self.result.record_created()
            return CREATED, record_id

        if existing.get("x_source_hash") == source_hash:
            if count:
                self.result.record_skipped()
            return SKIPPED, existing.get("id")

        record_id = existing.get("id")
        if self.dry_run:
            self.result.add_mock_write("WRITE", f"odoo://{self.model}/{record_id}", payload)
            if count:
                self.result.record_updated()
            return UPDATED, record_id
        try:
            self.client.write(self.model, [record_id], payload)
        except OdooError as exc:
            raise RecordSyncError(f"write on {self.model}#{record_id} failed: {sanitize(exc)}", external_id) from None
        self._cache[external_id] = {
            "id": record_id, "x_external_id": external_id, "x_source_hash": source_hash,
        }
        if count:
            self.result.record_updated()
        return UPDATED, record_id


__all__ = [
    "CREATED",
    "UPDATED",
    "SKIPPED",
    "Upserter",
    "compute_source_hash",
    "make_external_id",
    "parse_iso_datetime",
    "slugify",
]
