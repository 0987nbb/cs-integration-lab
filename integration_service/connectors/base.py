# -*- coding: utf-8 -*-
"""Template shared by every connector.

A connector implements exactly one method, :meth:`BaseConnector.sync`, and
receives a :class:`~integration_service.sync_result.SyncResult` to record
outcomes on. :meth:`BaseConnector.run` wraps it with the behaviour every
provider must exhibit:

* start/end timestamps captured around the whole run;
* an unexpected exception recorded as a **fatal** run rather than a traceback;
* **partial failure continuation** - :meth:`guard` isolates one record so a bad
  record is counted as ``failed`` and the loop proceeds;
* the finished result handed to :class:`~integration_service.sync_logger.SyncLogWriter`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence

from ..config import Settings, get_settings
from ..errors import HttpError, IntegrationError, RecordSyncError
from ..http_client import ResilientHttpClient
from ..odoo_client import OdooClient
from ..sanitize import sanitize
from ..sync_logger import SyncLogWriter
from ..sync_result import SyncResult

LOGGER = logging.getLogger("integration_service.connector")


@dataclass
class ConnectorContext:
    """Everything a connector needs, injected so tests can supply fakes."""

    odoo: OdooClient
    settings: Settings
    http: ResilientHttpClient
    log_writer: Optional[SyncLogWriter] = None

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run

    @property
    def sample_limit(self) -> int:
        return self.settings.sample_limit


def build_context(
    settings: Optional[Settings] = None,
    odoo: Optional[OdooClient] = None,
    http: Optional[ResilientHttpClient] = None,
) -> ConnectorContext:
    """Assemble a context from the environment, reusing anything passed in."""
    settings = settings or get_settings()
    odoo = odoo or OdooClient(dry_run=settings.dry_run, http_settings=settings.http)
    http = http or ResilientHttpClient(settings=settings.http)
    return ConnectorContext(
        odoo=odoo,
        settings=settings,
        http=http,
        log_writer=SyncLogWriter(odoo, settings),
    )


class BaseConnector:
    """Base class for the five provider connectors."""

    #: Must match a value of ``x_integration_sync_log.x_provider``.
    provider: str = ""
    #: Human-readable name used in log lines.
    label: str = ""

    def __init__(self, ctx: ConnectorContext) -> None:
        if not self.provider:
            raise IntegrationError(f"{type(self).__name__} must define a provider.")
        self.ctx = ctx
        self.odoo = ctx.odoo
        self.http = ctx.http
        self.settings = ctx.settings
        self.dry_run = ctx.dry_run
        self.log = logging.getLogger(f"integration_service.{self.provider}")

    # -- template method ----------------------------------------------------

    def run(self, write_log: bool = True) -> SyncResult:
        """Execute the sync and return its accounting record."""
        result = SyncResult(provider=self.provider)
        self.log.info("Starting %s sync (dry_run=%s)", self.label or self.provider, self.dry_run)
        try:
            self.sync(result)
        except IntegrationError as exc:
            result.mark_fatal(f"{self.label or self.provider} sync aborted: {sanitize(exc)}")
            self.log.error("%s sync aborted: %s", self.provider, sanitize(exc))
        except Exception as exc:  # noqa: BLE001 - a connector bug must not kill the batch
            result.mark_fatal(f"Unexpected {type(exc).__name__} in {self.provider} sync: {sanitize(exc)}")
            self.log.exception("Unexpected failure in %s sync", self.provider)
        finally:
            result.finish()

        if write_log and self.ctx.log_writer is not None:
            self.ctx.log_writer.write(result)
        self.log.info("Finished %s", result.summary_line())
        return result

    def sync(self, result: SyncResult) -> None:
        """Provider-specific work. Subclasses must override."""
        raise NotImplementedError

    # -- per-record isolation ------------------------------------------------

    @contextmanager
    def guard(self, result: SyncResult, external_id: Optional[str] = None,
              label: Optional[str] = None) -> Iterator[None]:
        """Isolate one record so its failure does not abort the run.

        >>> for issue in issues:
        ...     with self.guard(result, external_id=eid):
        ...         self.upsert_issue(issue, result)
        """
        try:
            yield
        except RecordSyncError as exc:
            result.record_failed(exc, external_id or exc.external_id)
            self.log.warning("Record failed (%s): %s", external_id or exc.external_id, sanitize(exc))
        except HttpError as exc:
            result.record_failed(f"{label or 'record'}: {sanitize(exc)}", external_id)
            self.log.warning("HTTP failure on %s: %s", external_id, sanitize(exc))
        except IntegrationError as exc:
            result.record_failed(f"{label or 'record'}: {sanitize(exc)}", external_id)
            self.log.warning("Failure on %s: %s", external_id, sanitize(exc))
        except Exception as exc:  # noqa: BLE001
            result.record_failed(f"{label or 'record'}: {type(exc).__name__}: {sanitize(exc)}", external_id)
            self.log.exception("Unexpected failure on record %s", external_id)

    # -- shared helpers ------------------------------------------------------

    def limited(self, items: Sequence[Any], result: Optional[SyncResult] = None,
                label: str = "records") -> List[Any]:
        """Apply ``SYNC_SAMPLE_LIMIT``, noting explicitly what was dropped.

        A cap that is never mentioned reads as full coverage, so the number of
        omitted records is always recorded on the result.
        """
        limit = self.ctx.sample_limit
        items = list(items)
        if limit and limit > 0 and len(items) > limit:
            if result is not None:
                result.add_note(
                    f"Sample limit applied: processed {limit} of {len(items)} {label} "
                    f"(raise SYNC_SAMPLE_LIMIT to process all)."
                )
            return items[:limit]
        return items

    def mock_or_call(self, result: SyncResult, allowed: bool, method: str, url: str,
                     payload: Any = None, send: Any = None) -> Any:
        """Send an outbound write, or record it as ``[MOCK WRITE]``.

        Args:
            allowed: whether the write may actually be sent (credentials present
                and not a dry run).
            send: zero-argument callable performing the real request.
        """
        if not allowed or self.dry_run or send is None:
            entry = result.add_mock_write(method, url, payload)
            self.log.info("%s", entry)
            return None
        return send()


__all__ = ["BaseConnector", "ConnectorContext", "build_context"]
