# -*- coding: utf-8 -*-
"""Persist a :class:`SyncResult` to ``x_integration_sync_log``.

The model is provisioned by :mod:`integration_service.provisioning`, together
with the access rule that makes it reachable. The writer still degrades rather
than lose an audit trail, because a run that cannot record itself is worse than
a run that records itself somewhere else:

1. try ``x_integration_sync_log`` in Odoo;
2. on failure, append the same record as one JSON object per line to
   ``SYNC_LOG_FALLBACK_PATH``.

A fallback write is announced on the result as a note, so a run whose log did
not reach Odoo never looks like one that did.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from .config import Settings, get_settings
from .errors import OdooError
from .sanitize import sanitize, truncate
from .sync_result import PROVIDERS, SyncResult, to_odoo_datetime

LOGGER = logging.getLogger("integration_service.sync_log")

SYNC_LOG_MODEL = "x_integration_sync_log"
CONFIG_MODEL = "x_integration_config"

#: Cap for the free-text error column so one noisy run cannot bloat the record.
MAX_ERROR_DETAILS = 8000


class SyncLogWriter:
    """Writes run records to Odoo, falling back to a local JSONL file."""

    def __init__(
        self,
        client: Any,
        settings: Optional[Settings] = None,
        fallback_path: Optional[str] = None,
    ) -> None:
        self.client = client
        self.settings = settings or get_settings()
        self.fallback_path = fallback_path or self.settings.sync_log_fallback_path
        self._odoo_available: Optional[bool] = None
        self._config_ids: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
        #: True once the operator has been told Odoo logging is unavailable.
        self._warned = False
        #: Why the last Odoo write failed, quoted back on the result.
        self._last_error: Optional[str] = None

    # -- public API ---------------------------------------------------------

    def write(self, result: SyncResult) -> Dict[str, Any]:
        """Persist ``result``. Never raises - logging must not break a sync."""
        record = self._build_record(result)
        target = "none"

        if self._try_odoo(record, result):
            target = SYNC_LOG_MODEL
        elif self._try_file(result):
            target = self.fallback_path

        LOGGER.info("%s | sync log -> %s", result.summary_line(), target)
        return {"target": target, "record": record}

    # -- Odoo ---------------------------------------------------------------

    def _build_record(self, result: SyncResult) -> Dict[str, Any]:
        provider = result.provider if result.provider in PROVIDERS else "github"
        started = to_odoo_datetime(result.started_at)
        reference = f"SYNC/{provider}/{(started or '').replace(' ', 'T')}"
        details = result.error_details()
        if len(details) > MAX_ERROR_DETAILS:
            details = details[:MAX_ERROR_DETAILS] + "\n... [truncated]"

        return {
            "x_name": reference,
            "x_provider": provider,
            "x_start_time": started,
            "x_end_time": to_odoo_datetime(result.ended_at),
            "x_status": result.status,
            "x_created_count": result.created,
            "x_updated_count": result.updated,
            "x_skipped_count": result.skipped,
            "x_failed_count": result.failed,
            # Already sanitised: SyncResult only accepts redacted text.
            "x_error_details": sanitize(details) or False,
        }

    def _try_odoo(self, record: Dict[str, Any], result: SyncResult) -> bool:
        if self.settings.dry_run:
            LOGGER.info("[MOCK WRITE] sync log %s", truncate(record, 300))
            return False
        if self._odoo_available is False:
            return False

        payload = dict(record)
        config_id, config_model = self._config_id_for(record["x_provider"])
        if config_id:
            # Linking the run to its configuration is what makes the Sync Logs
            # list filterable by integration rather than only by provider name.
            payload["x_config_id"] = config_id

        try:
            new_id = self.client.create_one(SYNC_LOG_MODEL, payload)
            if new_id:
                self._odoo_available = True
                if config_id:
                    self._touch_config(config_id, config_model, record["x_end_time"])
                return True
        except OdooError as exc:
            self._last_error = sanitize(exc)

        self._odoo_available = False
        reason = getattr(self, "_last_error", None) or "sync log model is not writable"
        if not self._warned:
            LOGGER.warning(
                "Cannot write to sync log model (%s). Falling back to %s.",
                reason, self.fallback_path,
            )
            self._warned = True
        result.add_note(f"Sync log written to {self.fallback_path} instead of Odoo: {reason}")
        return False

    def _config_id_for(self, provider: str) -> Tuple[Optional[int], Optional[str]]:
        """Resolve the integration config row for a provider, if readable."""
        if provider in self._config_ids:
            return self._config_ids[provider]

        res: Tuple[Optional[int], Optional[str]] = (None, None)
        try:
            rows = self.client.search_read(
                CONFIG_MODEL, [["x_provider", "=", provider]], fields=["id"], limit=1
            )
            if rows:
                res = (rows[0]["id"], CONFIG_MODEL)
        except OdooError:
            pass

        self._config_ids[provider] = res
        return res

    def _touch_config(self, config_id: int, config_model: str, end_time: Any) -> None:
        """Best-effort stamp of last_sync_at on the provider's config row."""
        time_field = "last_sync_at" if config_model == "cs.integration.config" else "x_last_sync_at"
        try:
            self.client.write(config_model, [config_id], {time_field: end_time})
        except OdooError:
            pass


    # -- file fallback ------------------------------------------------------

    def _try_file(self, result: SyncResult) -> bool:
        try:
            directory = os.path.dirname(os.path.abspath(self.fallback_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.fallback_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(result.to_dict(), ensure_ascii=False, default=str) + "\n")
            return True
        except OSError as exc:
            LOGGER.error("Could not write the fallback sync log: %s", sanitize(exc))
            return False


__all__ = ["SyncLogWriter", "SYNC_LOG_MODEL", "CONFIG_MODEL"]
