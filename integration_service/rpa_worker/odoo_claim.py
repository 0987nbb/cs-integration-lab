# -*- coding: utf-8 -*-
"""
Odoo Job Claimer & Result Writer.
Provides safe job claiming (optimistic compare-and-update), stale-running crash recovery,
and writing worker outcomes back to Odoo.
"""
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from integration_service.odoo_client import OdooClient, OdooClientError as OdooError
from .config import WorkerConfig
from .logging_utils import get_worker_logger
from .models import RpaJobRecord, ExecutionResult

LOGGER = get_worker_logger("rpa_worker.odoo_claim")


def utc_now_iso() -> str:
    """Returns current UTC timestamp in Odoo-compatible ISO format (YYYY-MM-DD HH:MM:SS)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class OdooJobClaimer:
    """Handles polling, safe compare-and-update claiming, result reporting, and crash recovery."""

    def __init__(self, client: Optional[OdooClient] = None, config: Optional[WorkerConfig] = None):
        self.config = config or WorkerConfig()
        self.client = client or OdooClient()
        self.model = self.config.model_name

    def _f(self, field_name: str) -> str:
        """Helper to resolve field name depending on model prefix."""
        if self.model.startswith("x_") and not field_name.startswith("x_") and field_name not in ("id", "create_date", "write_date"):
            return f"x_{field_name}"
        return field_name

    def fetch_queued_jobs(self, limit: int = 5) -> List[RpaJobRecord]:
        """Poll Odoo for RPA jobs in 'queued' state."""
        domain = [[self._f("state"), "=", "queued"]]
        try:
            records = self.client.search_read(self.model, domain, limit=limit, order="id desc")
            return [RpaJobRecord.from_odoo_dict(rec) for rec in records]
        except OdooError as exc:
            LOGGER.error(f"Error polling queued jobs from Odoo: {exc}")
            return []

    def claim_job(self, job: RpaJobRecord) -> Optional[RpaJobRecord]:
        """
        Safely claims a queued job using atomic Compare-and-Update.
        Verifies that state is still 'queued' atomically before transitioning to 'running'.
        Returns updated RpaJobRecord if successful, or None if another worker claimed it first.
        """
        now_str = utc_now_iso()
        update_vals = {
            self._f("state"): "running",
            self._f("started_at"): now_str,
            self._f("attempt_count"): (job.attempt_count or 0) + 1,
        }
        try:
            if hasattr(self.client, "claim_job_atomic") and callable(getattr(self.client, "claim_job_atomic")):
                claimed_dict = self.client.claim_job_atomic(self.model, job.id, update_vals)
            else:
                claim_domain = [["id", "=", job.id], [self._f("state"), "=", "queued"]]
                matching_recs = self.client.search_read(self.model, claim_domain, fields=["id"])
                if not matching_recs:
                    return None
                self.client.write(self.model, [job.id], update_vals)
                updated_recs = self.client.search_read(self.model, [["id", "=", job.id]])
                claimed_dict = updated_recs[0] if updated_recs else None

            if claimed_dict:
                claimed = RpaJobRecord.from_odoo_dict(claimed_dict)
                LOGGER.info(f"Successfully claimed job {claimed.id} ({claimed.name}), state set to 'running'.")
                return claimed
            LOGGER.info(f"Job {job.id} ({job.name}) was already claimed by another worker or state changed.")
            return None
        except Exception as exc:
            LOGGER.warning(f"Failed to claim job {job.id} ({job.name}): {exc}")
            return None

    def write_result(self, job_id: int, result: ExecutionResult, max_retries: int = 3) -> bool:
        """Writes execution outcome (success, failed, or needs_human) and evidence back to Odoo with automatic retries on transient network errors."""
        now_str = utc_now_iso()
        vals: Dict[str, Any] = {
            self._f("state"): result.state,
            self._f("finished_at"): now_str,
        }
        if result.result_data is not None:
            import json
            vals[self._f("result")] = json.dumps(result.result_data)
        if result.error_details is not None:
            vals[self._f("error_details")] = result.error_details
        if result.last_successful_step is not None:
            vals[self._f("last_successful_step")] = result.last_successful_step
        if result.external_reference is not None:
            vals[self._f("external_reference")] = result.external_reference
        if result.screenshot_base64:
            vals[self._f("screenshot")] = result.screenshot_base64
            vals[self._f("screenshot_filename")] = result.screenshot_filename or f"evidence_{job_id}.png"

        import time
        for attempt in range(1, max_retries + 1):
            try:
                self.client.write(self.model, [job_id], vals)
                LOGGER.info(f"Wrote result for job {job_id}: state={result.state}")
                return True
            except Exception as exc:
                LOGGER.warning(f"Attempt {attempt}/{max_retries} to write result for job {job_id} failed: {exc}")
                if attempt < max_retries:
                    time.sleep(1.0)
                else:
                    LOGGER.error(f"Failed to write result for job {job_id} after {max_retries} attempts.")
                    return False

    def recover_stale_running_jobs(self) -> int:
        """
        Scans for stale jobs left stuck in 'running' state (e.g. from a crashed worker).
        If started_at is older than stale_running_timeout_seconds, marks job as failed.
        """
        timeout_sec = self.config.stale_running_timeout_seconds
        domain = [[self._f("state"), "=", "running"]]
        recovered_count = 0
        try:
            records = self.client.search_read(self.model, domain)
            now = datetime.now(timezone.utc)
            for r in records:
                started_str = r.get(self._f("started_at")) or r.get("started_at")
                if not started_str:
                    continue
                try:
                    # Parse Odoo datetime string (YYYY-MM-DD HH:MM:SS)
                    started_dt = datetime.strptime(started_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    elapsed = (now - started_dt).total_seconds()
                    if elapsed > timeout_sec:
                        job_id = r["id"]
                        recovery_vals = {
                            self._f("state"): "failed",
                            self._f("finished_at"): utc_now_iso(),
                            self._f("error_details"): f"Stale running recovery: Worker timeout or crash after {elapsed:.0f} seconds in running state.",
                        }
                        if hasattr(self.client, "recover_stale_job_atomic") and callable(getattr(self.client, "recover_stale_job_atomic")):
                            ok = self.client.recover_stale_job_atomic(self.model, job_id, timeout_sec, recovery_vals)
                        else:
                            ok = self.client.write(self.model, [job_id], recovery_vals)
                        if ok:
                            LOGGER.warning(f"Recovering stale running job {job_id} (elapsed {elapsed:.1f}s > timeout {timeout_sec}s)")
                            recovered_count += 1
                except Exception as parse_err:
                    LOGGER.debug(f"Could not parse started_at '{started_str}' for job {r.get('id')}: {parse_err}")
        except OdooError as exc:
            LOGGER.error(f"Error during stale running recovery scan: {exc}")
        return recovered_count
