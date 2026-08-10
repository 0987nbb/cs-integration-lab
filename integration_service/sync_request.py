# -*- coding: utf-8 -*-
"""Odoo-initiated "Sync Now" requests, and the worker that fulfils them.

Odoo Online executes server-action code under ``safe_eval``: no ``import``, no
sockets. Odoo therefore *cannot* call GitHub, Frankfurter or Open-Meteo itself,
and a button that reported "Sync Completed" from inside Odoo would be reporting
something it never did.

So the button does the only honest thing it can: it **enqueues**. Clicking Sync
Now flips ``x_sync_state`` to ``requested`` and says "requested", not
"completed". :class:`SyncRequestWorker` is the half that runs outside Odoo - it
claims the request, executes the real connector against the real provider, and
writes the real sync log and the real outcome back onto the config row.

The queue is a state machine on ``x_integration_config``::

    idle ──click Sync Now──▶ requested ──worker claims──▶ running
                                                            │
                                 done ◀──success────────────┤
                                 failed ◀──failure──────────┘

A claim is a compare-and-set: the worker only takes a row it still sees as
``requested``, and re-reads it after writing ``running``, so two workers racing
for the same row cannot both run it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import IntegrationError, OdooError
from .sanitize import sanitize
from .sync_result import SyncResult

LOGGER = logging.getLogger("integration_service.sync_request")

CONFIG_MODEL = "x_integration_config"

#: Queue states. ``requested`` is the only one a worker will pick up.
STATE_IDLE = "idle"
STATE_REQUESTED = "requested"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

SYNC_STATE_SELECTION: List[Tuple[str, str]] = [
    (STATE_IDLE, "Idle"),
    (STATE_REQUESTED, "Sync Requested"),
    (STATE_RUNNING, "Running"),
    (STATE_DONE, "Completed"),
    (STATE_FAILED, "Failed"),
]

#: Columns the queue adds to ``x_integration_config``.
SYNC_REQUEST_FIELD_SPECS: List[Dict[str, Any]] = [
    {
        "name": "x_sync_state",
        "ttype": "selection",
        "field_description": "Sync State",
        "help": "Lifecycle of a Sync Now request: idle -> requested -> running -> done/failed.",
        "selection": SYNC_STATE_SELECTION,
    },
    {
        "name": "x_sync_requested_at",
        "ttype": "datetime",
        "field_description": "Sync Requested At",
        "help": "When Sync Now was last clicked for this integration.",
    },
    {
        "name": "x_sync_requested_by",
        "ttype": "char",
        "field_description": "Sync Requested By",
        "help": "Login of the user who clicked Sync Now.",
    },
    {
        "name": "x_sync_message",
        "ttype": "text",
        "field_description": "Last Sync Result",
        "help": "Outcome reported by the integration worker for the last request.",
    },
]

#: Body of the "Sync Now" server action.
#:
#: This runs inside Odoo under safe_eval, so it contains no import and no I/O.
#: It deliberately reports "requested", never "completed" - the run itself
#: happens in :class:`SyncRequestWorker`, outside Odoo.
SYNC_NOW_ACTION_CODE = """
now_dt = env.cr.now()
targets = records or record
queued = []
busy = []
for rec in targets:
    if rec.x_sync_state in ('requested', 'running'):
        busy.append(rec.x_name)
        continue
    rec.write({
        'x_sync_state': 'requested',
        'x_sync_requested_at': now_dt,
        'x_sync_requested_by': env.user.login,
        'x_sync_message': 'Queued by ' + env.user.name + ' at ' + str(now_dt) + ' UTC. Awaiting the integration worker.',
    })
    queued.append(rec.x_name)
if queued:
    title = 'Sync Requested'
    kind = 'info'
    msg = 'Queued: ' + ', '.join(queued) + '. The integration worker will call the provider API and write a Sync Log record with the real result.'
else:
    title = 'Already Queued'
    kind = 'warning'
    msg = 'A sync is already requested or running for: ' + ', '.join(busy)
action = {
    'type': 'ir.actions.client',
    'tag': 'display_notification',
    'params': {'title': title, 'message': msg, 'type': kind, 'sticky': False},
}
"""


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SyncRequestWorker:
    """Claims ``requested`` config rows and runs the real connector for each.

    Args:
        odoo: the JSON-2 client.
        runner: ``provider -> SyncResult``; injected so the CLI owns connector
            construction and tests can supply a fake.
    """

    def __init__(self, odoo: Any, runner: Callable[[str], SyncResult]) -> None:
        self.odoo = odoo
        self.runner = runner

    # -- queue ---------------------------------------------------------------

    def pending(self) -> List[Dict[str, Any]]:
        """Config rows waiting to be run, oldest request first."""
        try:
            return self.odoo.search_read(
                CONFIG_MODEL,
                [["x_sync_state", "=", STATE_REQUESTED]],
                fields=["id", "x_name", "x_provider", "x_sync_requested_at",
                        "x_sync_requested_by", "x_active"],
                order="x_sync_requested_at asc, id asc",
            )
        except OdooError as exc:
            raise IntegrationError(
                f"Cannot read the sync request queue on {CONFIG_MODEL}: {sanitize(exc)}"
            ) from None

    def _claim(self, row: Dict[str, Any]) -> bool:
        """Compare-and-set ``requested`` -> ``running``.

        Re-reads the row afterwards: if another worker claimed it first, the
        state we read back is not ours to run.
        """
        config_id = row["id"]
        try:
            self.odoo.write(CONFIG_MODEL, [config_id], {
                "x_sync_state": STATE_RUNNING,
                "x_sync_message": f"Claimed by the integration worker at {_utc_now_str()} UTC.",
            })
        except OdooError as exc:
            LOGGER.warning("Could not claim config %s: %s", config_id, sanitize(exc))
            return False

        check = self.odoo.search_read(
            CONFIG_MODEL, [["id", "=", config_id]], fields=["x_sync_state"], limit=1
        )
        return bool(check) and check[0].get("x_sync_state") == STATE_RUNNING

    def _release(self, config_id: int, state: str, message: str,
                 end_time: Optional[str] = None) -> None:
        vals: Dict[str, Any] = {"x_sync_state": state, "x_sync_message": message[:8000]}
        if end_time:
            vals["x_last_sync_at"] = end_time
        try:
            self.odoo.write(CONFIG_MODEL, [config_id], vals)
        except OdooError as exc:
            LOGGER.error("Could not release config %s: %s", config_id, sanitize(exc))

    # -- execution -----------------------------------------------------------

    def drain(self) -> List[Dict[str, Any]]:
        """Run every currently-requested config once. Returns one report per row."""
        reports: List[Dict[str, Any]] = []
        for row in self.pending():
            reports.append(self.run_one(row))
        return reports

    def run_one(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Claim one row and execute its provider for real."""
        config_id = int(row["id"])
        provider = str(row.get("x_provider") or "")
        report: Dict[str, Any] = {
            "config_id": config_id,
            "name": row.get("x_name"),
            "provider": provider,
            "requested_by": row.get("x_sync_requested_by"),
            "claimed": False,
            "status": None,
        }

        if not self._claim(row):
            report["skipped"] = "not claimed (another worker took it)"
            return report
        report["claimed"] = True

        if row.get("x_active") is False:
            self._release(config_id, STATE_FAILED,
                          "Integration is archived (x_active = False); nothing was run.")
            report["status"] = "failed"
            report["error"] = "integration archived"
            return report

        LOGGER.info("Running requested sync for %s (config %s)", provider, config_id)
        try:
            result = self.runner(provider)
        except Exception as exc:  # noqa: BLE001 - a worker must survive any connector bug
            message = f"Sync failed to start: {type(exc).__name__}: {sanitize(exc)}"
            self._release(config_id, STATE_FAILED, message)
            report["status"] = "failed"
            report["error"] = message
            LOGGER.exception("Requested sync for %s crashed", provider)
            return report

        summary = result.summary_line()
        message = (
            f"{summary}\n"
            f"Ran at {_utc_now_str()} UTC by the integration worker "
            f"(requested by {row.get('x_sync_requested_by') or 'unknown'})."
        )
        errors = result.error_details()
        if errors:
            message += f"\n{errors}"

        state = STATE_DONE if result.status != "failed" else STATE_FAILED
        end_time = None
        if result.ended_at is not None:
            end_time = result.ended_at.strftime("%Y-%m-%d %H:%M:%S")
        self._release(config_id, state, message, end_time=end_time)

        report.update({
            "status": result.status,
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "failed": result.failed,
            "summary": summary,
        })
        return report

    def serve_forever(self, poll_seconds: int = 10,
                      should_stop: Optional[Callable[[], bool]] = None) -> None:
        """Poll the queue until interrupted. Intended for a long-lived process."""
        LOGGER.info("Sync request worker polling %s every %ss", CONFIG_MODEL, poll_seconds)
        while not (should_stop and should_stop()):
            try:
                for report in self.drain():
                    LOGGER.info("Request handled: %s", report)
            except IntegrationError as exc:
                LOGGER.error("Queue poll failed: %s", sanitize(exc))
            for _ in range(max(1, poll_seconds)):
                if should_stop and should_stop():
                    return
                time.sleep(1)


__all__ = [
    "CONFIG_MODEL",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_REQUESTED",
    "STATE_RUNNING",
    "SYNC_NOW_ACTION_CODE",
    "SYNC_REQUEST_FIELD_SPECS",
    "SYNC_STATE_SELECTION",
    "SyncRequestWorker",
]
