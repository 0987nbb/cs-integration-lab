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

LOGGER = get_worker_logger("rpa_worker.job_processor")

SUPPORTED_JOB_TYPES = ("saucedemo", "ui_playground")


class JobProcessor:
    """Processes an individual claimed RPA Job record."""

    def __init__(
        self,
        config: Optional[WorkerConfig] = None,
        browser_manager: Optional[PlaywrightBrowserManager] = None,
    ):
        self.config = config or WorkerConfig()
        self.browser_manager = browser_manager or PlaywrightBrowserManager(self.config)

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
    ) -> ExecutionResult:
        """
        Processes claimed job: validates payload, executes workflow, captures evidence, and returns ExecutionResult.
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
            return ExecutionResult(
                state="success",
                result_data=sanitized_output,
                last_successful_step="completed",
                external_reference=str(sanitized_output.get("order_id") or sanitized_output.get("reference") or ""),
                screenshot_base64=shot_b64,
                screenshot_filename=f"evidence_success_{job.id}.png" if shot_b64 else None,
            )

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
