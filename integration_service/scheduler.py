# -*- coding: utf-8 -*-
"""Scheduled synchronisation for the external service.

Odoo Online cannot run this code, so ``ir.cron`` is not an option: the schedule
lives in the service itself. Two execution styles are supported and both use the
same due-time arithmetic:

* **Resident** - ``--schedule`` keeps the process alive and runs each provider on
  its own interval.
* **One-shot** - ``--schedule --once`` runs only the providers that are currently
  due and exits, which is what an OS scheduler (cron, Windows Task Scheduler)
  should invoke.

"Sync Now" is simply ``--provider <name>``, which ignores the schedule entirely.

Interval configuration comes from the environment
(``SCHEDULE_INTERVAL_MINUTES`` plus per-provider ``SCHEDULE_<PROVIDER>_MINUTES``).
When ``x_integration_config`` is readable, its ``x_schedule_enabled`` flag can
disable a provider, and ``x_last_sync_at`` / ``x_next_sync_at`` are kept current
so the schedule is visible from inside Odoo.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import Settings, get_settings
from .errors import OdooError
from .sanitize import sanitize
from .sync_logger import CONFIG_MODEL
from .sync_result import SyncResult, to_odoo_datetime, utcnow

LOGGER = logging.getLogger("integration_service.scheduler")

#: Used when neither a per-provider nor a global interval is configured.
DEFAULT_INTERVAL_MINUTES = 60

#: Longest the resident loop sleeps in one go, so a shutdown signal is noticed
#: promptly even when the next run is hours away.
_SLEEP_SLICE_SECONDS = 5.0


def _interval_minutes(provider: str) -> int:
    """Resolve a provider's interval: per-provider override, else the global one."""
    specific = os.getenv(f"SCHEDULE_{provider.upper()}_MINUTES")
    generic = os.getenv("SCHEDULE_INTERVAL_MINUTES")
    for raw in (specific, generic):
        if raw and raw.strip():
            try:
                value = int(raw.strip())
            except ValueError:
                LOGGER.warning("Ignoring non-numeric schedule interval %r for %s", raw, provider)
                continue
            if value > 0:
                return value
    return DEFAULT_INTERVAL_MINUTES


@dataclass
class ScheduleEntry:
    """One provider's schedule state."""

    provider: str
    interval_minutes: int
    next_run_at: datetime
    enabled: bool = True
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    runs: int = 0

    def is_due(self, now: Optional[datetime] = None) -> bool:
        return self.enabled and (now or utcnow()) >= self.next_run_at

    def reschedule(self, now: Optional[datetime] = None) -> None:
        """Advance to the next slot, skipping any slot already missed.

        Advancing by whole intervals (rather than ``now + interval``) keeps runs
        on their original cadence after a long run or a paused machine, and the
        loop guarantees the result is strictly in the future.
        """
        reference = now or utcnow()
        step = timedelta(minutes=self.interval_minutes)
        if self.next_run_at <= reference:
            missed = int((reference - self.next_run_at) / step) + 1
            self.next_run_at = self.next_run_at + step * missed
        while self.next_run_at <= reference:
            self.next_run_at += step

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "next_run_at": to_odoo_datetime(self.next_run_at),
            "last_run_at": to_odoo_datetime(self.last_run_at),
            "last_status": self.last_status,
            "runs": self.runs,
        }


@dataclass
class Scheduler:
    """Drives provider runs on a per-provider interval.

    Args:
        providers: provider names, in run order.
        runner: called as ``runner(provider) -> SyncResult``; injected so the CLI
            owns connector construction and tests can supply a stub.
        odoo: optional client used to mirror schedule state into Odoo.
        sleep: injected for tests.
        clock: injected for tests.
    """

    providers: List[str]
    runner: Callable[[str], SyncResult]
    settings: Optional[Settings] = None
    odoo: Optional[Any] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = utcnow
    entries: Dict[str, ScheduleEntry] = field(default_factory=dict)
    _stopping: bool = False

    def __post_init__(self) -> None:
        self.settings = self.settings or get_settings()
        if not self.entries:
            self.entries = self._build_entries()

    # -- setup ---------------------------------------------------------------

    def _build_entries(self) -> Dict[str, ScheduleEntry]:
        """Start every provider due immediately, then honour its interval."""
        now = self.clock()
        overrides = self._odoo_schedule_flags()
        entries: Dict[str, ScheduleEntry] = {}
        for provider in self.providers:
            entries[provider] = ScheduleEntry(
                provider=provider,
                interval_minutes=_interval_minutes(provider),
                next_run_at=now,
                enabled=overrides.get(provider, True),
            )
        return entries

    def _odoo_schedule_flags(self) -> Dict[str, bool]:
        """Read ``x_schedule_enabled`` per provider, if the model is readable.

        The custom models are access-denied on the current database, so a failure
        here is expected and simply leaves every provider enabled.
        """
        if self.odoo is None:
            return {}
        try:
            rows = self.odoo.search_read(
                CONFIG_MODEL, [], fields=["x_provider", "x_schedule_enabled", "x_active"]
            )
        except OdooError as exc:
            LOGGER.debug("Schedule flags unavailable from %s: %s", CONFIG_MODEL, sanitize(exc))
            return {}
        flags: Dict[str, bool] = {}
        for row in rows or []:
            provider = row.get("x_provider")
            if provider:
                flags[provider] = bool(row.get("x_schedule_enabled")) and bool(row.get("x_active", True))
        return flags

    # -- execution -----------------------------------------------------------

    def due_providers(self, now: Optional[datetime] = None) -> List[str]:
        reference = now or self.clock()
        return [p for p in self.providers if self.entries[p].is_due(reference)]

    def run_due(self) -> List[SyncResult]:
        """Run every provider that is currently due, in configured order."""
        results: List[SyncResult] = []
        for provider in self.due_providers():
            results.append(self._run_one(provider))
        return results

    def _run_one(self, provider: str) -> SyncResult:
        entry = self.entries[provider]
        started = self.clock()
        LOGGER.info("Scheduled run: %s (every %dm)", provider, entry.interval_minutes)
        try:
            result = self.runner(provider)
        except Exception as exc:  # noqa: BLE001 - one provider must not kill the loop
            LOGGER.exception("Scheduled run of %s raised", provider)
            result = SyncResult(provider=provider)
            result.mark_fatal(f"Scheduled run raised {type(exc).__name__}: {sanitize(exc)}")
            result.finish()

        entry.last_run_at = started
        entry.last_status = result.status
        entry.runs += 1
        entry.reschedule(self.clock())
        self._publish(entry)
        return result

    def _publish(self, entry: ScheduleEntry) -> None:
        """Mirror last/next run onto ``x_integration_config`` when writable."""
        if self.odoo is None or (self.settings and self.settings.dry_run):
            return
        try:
            rows = self.odoo.search_read(
                CONFIG_MODEL, [["x_provider", "=", entry.provider]], fields=["id"], limit=1
            )
            if not rows:
                return
            self.odoo.write(CONFIG_MODEL, [rows[0]["id"]], {
                "x_last_sync_at": to_odoo_datetime(entry.last_run_at),
                "x_next_sync_at": to_odoo_datetime(entry.next_run_at),
            })
        except OdooError as exc:
            LOGGER.debug("Could not publish schedule state for %s: %s", entry.provider, sanitize(exc))

    def seconds_until_next(self) -> float:
        active = [e for e in self.entries.values() if e.enabled]
        if not active:
            return float(DEFAULT_INTERVAL_MINUTES * 60)
        soonest = min(e.next_run_at for e in active)
        return max(0.0, (soonest - self.clock()).total_seconds())

    def stop(self, *_signal_args: Any) -> None:
        """Request a graceful shutdown of :meth:`run_forever`."""
        if not self._stopping:
            LOGGER.info("Shutdown requested; finishing the current cycle.")
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # not the main thread, or unsupported
                continue

    def run_forever(self, max_cycles: Optional[int] = None) -> List[SyncResult]:
        """Run due providers, sleep until the next slot, repeat.

        Args:
            max_cycles: stop after this many cycles. Used by the tests; ``None``
                runs until a shutdown signal arrives.
        """
        all_results: List[SyncResult] = []
        cycles = 0
        LOGGER.info(
            "Scheduler started: %s",
            ", ".join(f"{e.provider} every {e.interval_minutes}m"
                      for e in self.entries.values() if e.enabled) or "nothing enabled",
        )
        while not self._stopping:
            all_results.extend(self.run_due())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._sleep_until_due()
        LOGGER.info("Scheduler stopped after %d cycle(s).", cycles)
        return all_results

    def _sleep_until_due(self) -> None:
        """Sleep in slices so a shutdown signal is acted on quickly."""
        remaining = self.seconds_until_next()
        while remaining > 0 and not self._stopping:
            slice_seconds = min(_SLEEP_SLICE_SECONDS, remaining)
            self.sleep(slice_seconds)
            remaining -= slice_seconds

    def describe(self) -> List[Dict[str, Any]]:
        return [self.entries[p].to_dict() for p in self.providers]


__all__ = ["DEFAULT_INTERVAL_MINUTES", "ScheduleEntry", "Scheduler"]
