# -*- coding: utf-8 -*-
"""
RPA Worker Polling Loop Orchestrator.
Polls Odoo for queued jobs, safely claims, executes, reports outcomes, and recovers stale running jobs.
"""
import signal
import time
from typing import Optional
from .config import WorkerConfig
from .logging_utils import get_worker_logger
from .odoo_claim import OdooJobClaimer
from .job_processor import JobProcessor

LOGGER = get_worker_logger("rpa_worker.main")


class RpaWorker:
    """Main RPA Worker polling loop and process orchestrator."""

    def __init__(
        self,
        config: Optional[WorkerConfig] = None,
        claimer: Optional[OdooJobClaimer] = None,
        processor: Optional[JobProcessor] = None,
    ):
        self.config = config or WorkerConfig()
        self.claimer = claimer or OdooJobClaimer(config=self.config)
        self.processor = processor or JobProcessor(config=self.config)
        self._running = False

    def process_next_job(self) -> bool:
        """
        Polls Odoo for 1 queued job, claims it safely, executes it, and writes the outcome back to Odoo.
        Returns True if a job was processed, False if queue was empty or claim failed.
        """
        # Run stale running recovery check
        self.claimer.recover_stale_running_jobs()

        queued_jobs = self.claimer.fetch_queued_jobs(limit=1)
        if not queued_jobs:
            LOGGER.debug("Queue is empty. No jobs to process.")
            return False

        candidate = queued_jobs[0]
        LOGGER.info(f"Discovered queued job {candidate.id} ({candidate.name}). Attempting to claim...")

        # Attempt safe compare-and-update claim
        claimed_job = self.claimer.claim_job(candidate)
        if not claimed_job:
            LOGGER.info(f"Could not claim job {candidate.id}. Another worker may have claimed it.")
            return False

        # Execute claimed job
        try:
            execution_result = self.processor.process_job(claimed_job)
            self.claimer.write_result(claimed_job.id, execution_result)
            return True
        except Exception as exc:
            LOGGER.error(f"Unhandled exception during job execution: {exc}")
            # Ensure job does not remain stuck in running
            from .models import ExecutionResult
            fallback_result = ExecutionResult(
                state="failed",
                error_details=f"Unhandled Worker Error: {exc}",
                last_successful_step="crashed",
            )
            self.claimer.write_result(claimed_job.id, fallback_result)
            return True

    def start(self) -> None:
        """Starts continuous polling loop until stopped or interrupted."""
        self._running = True
        LOGGER.info(f"Starting RPA Worker loop with config: {self.config}")

        # Register signal handlers for graceful shutdown if in main thread
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, AttributeError):
            pass

        while self._running:
            try:
                processed = self.process_next_job()
                if not processed and self._running:
                    time.sleep(self.config.poll_interval_seconds)
            except Exception as exc:
                LOGGER.error(f"Error in worker main loop: {exc}")
                if self._running:
                    time.sleep(self.config.poll_interval_seconds)

        LOGGER.info("RPA Worker loop stopped cleanly.")

    def stop(self) -> None:
        """Stops the worker polling loop."""
        LOGGER.info("Stopping RPA Worker...")
        self._running = False

    def _handle_signal(self, signum, frame):
        LOGGER.info(f"Received signal {signum}. Initiating graceful shutdown...")
        self.stop()
