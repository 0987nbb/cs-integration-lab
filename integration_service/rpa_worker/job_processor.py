# -*- coding: utf-8 -*-
"""
RPA Job Processor.
Validates job input payloads, dispatches browser workflows, and classifies errors into ExecutionResults.
"""
import json
from typing import Optional, Dict, Any, Callable
from .config import WorkerConfig
from .exceptions import (
    WorkerError,
    TransientWorkerError,
    PermanentWorkerError,
    HumanInterventionRequiredError,
)
from .logging_utils import get_worker_logger, sanitize_sensitive_data
from .models import RpaJobRecord, JobPayload, ExecutionResult
from .browser.base import PlaywrightBrowserManager

import os

LOGGER = get_worker_logger("rpa_worker.job_processor")

SUPPORTED_JOB_TYPES = ("saucedemo", "ui_playground")


class OutcomeReconciler:
    """
    Reconciles external action outcomes across worker restarts.
    Prevents duplicate state-changing browser actions when Odoo result write fails.
    """
    def __init__(self, ledger_file: str = "logs/rpa_outcome_ledger.json"):
        self.ledger_file = ledger_file

    def _load_ledger(self) -> Dict[str, Any]:
        if os.path.exists(self.ledger_file):
            try:
                with open(self.ledger_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_outcome(self, idempotency_key: str, outcome_data: Dict[str, Any]) -> None:
        """Persists completed external action outcome keyed by idempotency_key."""
        if not idempotency_key:
            return
        os.makedirs(os.path.dirname(self.ledger_file), exist_ok=True)
        ledger = self._load_ledger()
        ledger[idempotency_key] = outcome_data
        try:
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=2)
        except Exception as exc:
            LOGGER.warning(f"Failed to write outcome ledger: {exc}")

    def get_completed_outcome(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Returns prior completed external action outcome if already executed."""
        if not idempotency_key:
            return None
        ledger = self._load_ledger()
        return ledger.get(idempotency_key)

    def reconcile_prior_outcome(
        self,
        job: RpaJobRecord,
        external_readback_func: Optional[Callable[[RpaJobRecord], Optional[Dict[str, Any]]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Reconciles prior outcome using external read-back verification and local ledger evidence.
        """
        if external_readback_func:
            try:
                ext_data = external_readback_func(job)
                if ext_data:
                    LOGGER.info(f"External Read-Back verified prior completion for job {job.id} ({job.name}).")
                    return ext_data
            except Exception as exc:
                LOGGER.warning(f"External read-back check failed for job {job.id}: {exc}")

        if job.idempotency_key:
            ledger_data = self.get_completed_outcome(job.idempotency_key)
            if ledger_data:
                return ledger_data

        if job.last_successful_step in ("completed", "reconciled_prior_outcome") or job.external_reference:
            return {
                "result_data": {"status": "completed", "external_reference": job.external_reference},
                "external_reference": job.external_reference,
            }

        return None


class JobProcessor:
    """Processes an individual claimed RPA Job record."""

    def __init__(
        self,
        config: Optional[WorkerConfig] = None,
        browser_manager: Optional[PlaywrightBrowserManager] = None,
        reconciler: Optional[OutcomeReconciler] = None,
    ):
        self.config = config or WorkerConfig()
        self.browser_manager = browser_manager or PlaywrightBrowserManager(self.config)
        self.reconciler = reconciler or OutcomeReconciler()

    def validate_job_payload(self, job: RpaJobRecord) -> JobPayload:
        """
        Validates job payload structure, job_type support, and JSON parsing before browser launch.
        Raises PermanentWorkerError on invalid input (never treated as a browser failure).
        """
        if not job.job_type or not job.job_type.strip():
            raise PermanentWorkerError("Missing required job_type parameter.")

        clean_job_type = job.job_type.strip()
        if clean_job_type not in SUPPORTED_JOB_TYPES:
            raise PermanentWorkerError(
                f"Unsupported job_type '{clean_job_type}'. Supported types: {list(SUPPORTED_JOB_TYPES)}"
            )

        if not job.payload_str or not job.payload_str.strip():
            raise PermanentWorkerError("Input payload is missing or empty.")

        try:
            parsed_data = json.loads(job.payload_str)
            if not isinstance(parsed_data, dict):
                raise PermanentWorkerError("Input payload must be a valid JSON dictionary object.")
            
            # Workflow specific validation
            if clean_job_type == "saucedemo":
                prod = str(parsed_data.get("product_name") or parsed_data.get("product") or "").strip()
                if not prod:
                    raise PermanentWorkerError("SauceDemo payload validation failed: Missing required 'product_name' or 'product' parameter.")
                chk = parsed_data.get("checkout")
                if chk is not None and isinstance(chk, dict):
                    fn = str(chk.get("first_name") or "").strip()
                    ln = str(chk.get("last_name") or "").strip()
                    pc = str(chk.get("postal_code") or "").strip()
                    if chk and (not fn or not ln or not pc):
                        raise PermanentWorkerError("SauceDemo payload validation failed: 'checkout' details must include non-empty 'first_name', 'last_name', and 'postal_code'.")
            elif clean_job_type == "ui_playground":
                sc = str(parsed_data.get("scenario") or "dynamic_id").strip().lower()
                if sc and sc not in ("dynamic_id", "load_delay", "ajax_data", "client_side_delay"):
                    raise PermanentWorkerError(f"UI Playground payload validation failed: Unsupported scenario '{sc}'.")

            return JobPayload(raw_payload=job.payload_str, data=parsed_data, job_type=clean_job_type)
        except (ValueError, TypeError) as exc:
            raise PermanentWorkerError(f"Invalid JSON in input payload: {exc}", details=str(exc)) from exc

    def process_job(
        self,
        job: RpaJobRecord,
        custom_task_func: Optional[Callable[[Any, JobPayload], Dict[str, Any]]] = None,
        external_readback_func: Optional[Callable[[RpaJobRecord], Optional[Dict[str, Any]]]] = None,
    ) -> ExecutionResult:
        """
        Processes claimed job: validates payload, reconciles prior external outcome, executes workflow, captures evidence, and returns ExecutionResult.
        """
        LOGGER.info(
            f"Processing job {job.id} ({job.name}) [Type: {job.job_type}, Attempt: {job.attempt_count}]",
            extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "validation"},
        )

        # Step 1: Pre-execution Payload Validation
        try:
            payload = self.validate_job_payload(job)
        except PermanentWorkerError as exc:
            LOGGER.warning(f"Payload validation failed for job {job.id}: {exc.message}")
            return ExecutionResult(
                state="failed",
                error_details=f"Validation Failure: {exc.message}",
                last_successful_step="pre_validation",
            )

        # Step 1.5: Reconciliation Check - External Read-Back & Outcome Verification
        prior_outcome = self.reconciler.reconcile_prior_outcome(job, external_readback_func=external_readback_func)
        if prior_outcome:
            LOGGER.info(
                f"Reconciliation: Prior external action already completed for job {job.id} ({job.name}) with idempotency_key '{job.idempotency_key}'. Skipping duplicate execution.",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "reconciled"},
            )
            return ExecutionResult(
                state="success",
                result_data=prior_outcome.get("result_data"),
                last_successful_step="reconciled_prior_outcome",
                external_reference=str(prior_outcome.get("external_reference") or ""),
                screenshot_base64=prior_outcome.get("screenshot_base64"),
                screenshot_filename=prior_outcome.get("screenshot_filename"),
            )

        # Step 2: Browser Workflow Execution
        try:
            if custom_task_func:
                def task_wrapper(page):
                    return custom_task_func(page, payload)
                try:
                    output = self.browser_manager.run_browser_task(task_wrapper)
                except PermanentWorkerError as p_err:
                    if "Playwright package is not installed" in p_err.message:
                        output = custom_task_func(None, payload)
                    else:
                        raise
            elif payload.job_type == "saucedemo":
                from .workflows.saucedemo_workflow import run_saucedemo_workflow
                def saucedemo_task(page):
                    return run_saucedemo_workflow(page, payload, config=self.config)
                output = self.browser_manager.run_browser_task(saucedemo_task)
            elif payload.job_type == "ui_playground":
                from .workflows.ui_playground_workflow import run_ui_playground_workflow
                def ui_playground_task(page):
                    return run_ui_playground_workflow(page, payload, config=self.config)
                output = self.browser_manager.run_browser_task(ui_playground_task)
            else:
                # Default foundation execution for fallback
                def default_foundation_task(page):
                    page.goto("https://example.com")
                    title = page.title()
                    return {"status": "completed", "page_title": title, "workflow": payload.job_type}

                output = self.browser_manager.run_browser_task(default_foundation_task)

            shot_b64 = output.pop("_screenshot_b64", None) if isinstance(output, dict) else None
            sanitized_output = sanitize_sensitive_data(output)
            LOGGER.info(
                f"Successfully completed job {job.id} ({job.name})",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "completed"},
            )
            exec_result = ExecutionResult(
                state="success",
                result_data=sanitized_output,
                last_successful_step="completed",
                external_reference=str(sanitized_output.get("order_id") or sanitized_output.get("reference") or ""),
                screenshot_base64=shot_b64,
                screenshot_filename=f"evidence_success_{job.id}.png" if shot_b64 else None,
            )

            # Persist completed outcome to reconciliation ledger
            self.reconciler.save_outcome(
                job.idempotency_key,
                {
                    "result_data": sanitized_output,
                    "external_reference": exec_result.external_reference,
                    "screenshot_base64": exec_result.screenshot_base64,
                    "screenshot_filename": exec_result.screenshot_filename,
                }
            )
            return exec_result

        except HumanInterventionRequiredError as exc:
            LOGGER.warning(
                f"Human intervention required for job {job.id}: {exc.message}",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "needs_human"},
            )
            return ExecutionResult(
                state="needs_human",
                error_details=f"Human Intervention Required: {exc.message} | Details: {exc.details}",
                last_successful_step="human_challenge_detected",
                screenshot_base64=exc.screenshot_b64,
                screenshot_filename=f"evidence_challenge_{job.id}.png" if exc.screenshot_b64 else None,
            )

        except TransientWorkerError as exc:
            LOGGER.warning(
                f"Transient worker error for job {job.id}: {exc.message}",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "transient_failure"},
            )
            return ExecutionResult(
                state="failed",
                error_details=f"Transient Failure: {exc.message} | Details: {exc.details}",
                last_successful_step="transient_error",
                screenshot_base64=exc.screenshot_b64,
                screenshot_filename=f"evidence_failure_{job.id}.png" if exc.screenshot_b64 else None,
            )

        except PermanentWorkerError as exc:
            LOGGER.error(
                f"Permanent worker error for job {job.id}: {exc.message}",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "permanent_failure"},
            )
            return ExecutionResult(
                state="failed",
                error_details=f"Permanent Failure: {exc.message} | Details: {exc.details}",
                last_successful_step="permanent_error",
                screenshot_base64=exc.screenshot_b64,
                screenshot_filename=f"evidence_failure_{job.id}.png" if exc.screenshot_b64 else None,
            )

        except Exception as exc:
            LOGGER.error(
                f"Unexpected exception processing job {job.id}: {exc}",
                extra={"job_id": job.id, "job_ref": job.name, "job_type": job.job_type, "step": "unexpected_error"},
            )
            return ExecutionResult(
                state="failed",
                error_details=f"Unexpected Execution Error: {exc}",
                last_successful_step="unexpected_error",
            )
