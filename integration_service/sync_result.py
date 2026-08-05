# -*- coding: utf-8 -*-
"""Per-run accounting: counters, timing, errors and the resulting status.

One :class:`SyncResult` is produced per connector run and is what
:mod:`integration_service.sync_logger` persists into ``x_integration_sync_log``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .sanitize import sanitize, truncate

#: Values accepted by ``x_integration_sync_log.x_status`` in Odoo.
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: Values accepted by ``x_integration_sync_log.x_provider`` in Odoo.
PROVIDERS = ("github", "jsonplaceholder", "frankfurter", "open_meteo", "nager_date")

#: Odoo stores naive datetimes in UTC.
ODOO_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def to_odoo_datetime(value: Optional[datetime]) -> Optional[str]:
    """Render a datetime the way Odoo expects it: naive UTC, second precision."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime(ODOO_DATETIME_FMT)


@dataclass
class SyncResult:
    """Mutable accumulator for one connector run.

    Counter semantics, applied uniformly by every connector:

    * ``created``  - a new Odoo record was written for an unseen external id;
    * ``updated``  - an existing record's source hash changed and was rewritten;
    * ``skipped``  - the record already matched (idempotent no-op), or the
      upstream deliberately produced nothing to import (e.g. HTTP 204), or a
      write was withheld pending approval;
    * ``failed``   - this record could not be synced; the run continues.
    """

    provider: str
    started_at: datetime = field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    mock_writes: List[str] = field(default_factory=list)
    #: Free-form per-entity breakdown, e.g. {"users": {...}, "posts": {...}}.
    details: Dict[str, Any] = field(default_factory=dict)
    #: Set when the run could not proceed at all (auth failure, unreachable host).
    fatal: bool = False

    # -- counters -----------------------------------------------------------

    def record_created(self, count: int = 1) -> None:
        self.created += count

    def record_updated(self, count: int = 1) -> None:
        self.updated += count

    def record_skipped(self, count: int = 1, reason: Optional[str] = None) -> None:
        self.skipped += count
        if reason:
            self.add_note(reason)

    def record_failed(self, message: Any, external_id: Optional[str] = None, count: int = 1) -> None:
        self.failed += count
        prefix = f"[{external_id}] " if external_id else ""
        self.errors.append(f"{prefix}{truncate(message, 400)}")

    def add_note(self, message: Any) -> None:
        note = sanitize(message)
        if note and note not in self.notes:
            self.notes.append(note)

    def add_mock_write(self, method: str, url: str, payload: Any = None) -> str:
        """Record a write that was described but deliberately not sent.

        Used for JSONPlaceholder (whose writes are simulated server-side) and
        for GitHub when no token is configured.
        """
        entry = f"[MOCK WRITE] {method.upper()} {sanitize(url)}"
        if payload is not None:
            entry += f" payload={truncate(payload, 300)}"
        self.mock_writes.append(entry)
        return entry

    def mark_fatal(self, message: Any) -> None:
        """Flag a run-level failure; the run's status becomes ``failed``."""
        self.fatal = True
        self.errors.append(truncate(message, 600))

    def finish(self) -> "SyncResult":
        self.ended_at = utcnow()
        return self

    # -- derived ------------------------------------------------------------

    @property
    def processed(self) -> int:
        return self.created + self.updated + self.skipped + self.failed

    @property
    def succeeded(self) -> int:
        return self.created + self.updated + self.skipped

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def status(self) -> str:
        """Collapse the counters into one of Odoo's status values.

        ``failed``  - the run aborted, or every attempted record failed.
        ``partial`` - some records synced and at least one failed.
        ``skipped`` - the run completed with nothing to do.
        ``success`` - everything attempted succeeded.
        """
        if self.fatal:
            return STATUS_FAILED
        if self.failed and self.succeeded == 0:
            return STATUS_FAILED
        if self.failed:
            return STATUS_PARTIAL
        if self.processed == 0:
            return STATUS_SKIPPED
        return STATUS_SUCCESS

    def error_details(self) -> str:
        """Human-readable, credential-free summary for ``x_error_details``."""
        blocks: List[str] = []
        if self.errors:
            blocks.append("ERRORS:\n" + "\n".join(f"  - {e}" for e in self.errors[:50]))
            if len(self.errors) > 50:
                blocks.append(f"  ... and {len(self.errors) - 50} more errors")
        if self.mock_writes:
            blocks.append("MOCK WRITES:\n" + "\n".join(f"  - {m}" for m in self.mock_writes[:50]))
            if len(self.mock_writes) > 50:
                blocks.append(f"  ... and {len(self.mock_writes) - 50} more mock writes")
        if self.notes:
            blocks.append("NOTES:\n" + "\n".join(f"  - {n}" for n in self.notes[:50]))
        return "\n\n".join(blocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "start_time": to_odoo_datetime(self.started_at),
            "end_time": to_odoo_datetime(self.ended_at),
            "duration_seconds": round(self.duration_seconds, 3),
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "notes": self.notes,
            "mock_writes": self.mock_writes,
            "details": self.details,
        }

    def summary_line(self) -> str:
        return (
            f"{self.provider:<16} {self.status:<8} "
            f"created={self.created} updated={self.updated} "
            f"skipped={self.skipped} failed={self.failed} "
            f"({self.duration_seconds:.1f}s)"
        )
