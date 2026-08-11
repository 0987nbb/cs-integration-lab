# -*- coding: utf-8 -*-
"""
UI Testing Playground - Automation Resilience Workflow.
Executes dynamic/brittle UI automation scenarios (Dynamic ID, Load Delay, AJAX Data)
using explicit Playwright waits and resilient selector strategies without hard-coded sleeps.
"""
from typing import Dict, Any, Optional
from ..config import WorkerConfig
from ..exceptions import (
    PermanentWorkerError,
    TransientWorkerError,
    HumanInterventionRequiredError,
)
from ..logging_utils import get_worker_logger, sanitize_sensitive_data
from ..models import JobPayload
from ..browser.pages.ui_playground import DynamicIdPage, LoadDelayPage, AjaxDataPage

LOGGER = get_worker_logger("rpa_worker.workflow.ui_playground")

SUPPORTED_SCENARIOS = ("dynamic_id", "load_delay", "ajax_data", "client_side_delay")


def run_ui_playground_workflow(
    page: Any,
    payload: JobPayload,
    config: Optional[WorkerConfig] = None,
) -> Dict[str, Any]:
    """
    Executes UI Testing Playground resilience workflow based on payload scenario.
    Demonstrates resilient selector strategies and explicit Playwright condition waits.
    """
    cfg = config or WorkerConfig()
    data = payload.data or {}

    scenario = str(data.get("scenario") or "dynamic_id").strip().lower()
    if scenario not in SUPPORTED_SCENARIOS:
        raise PermanentWorkerError(
            f"Unsupported UI Playground scenario '{scenario}'. Supported scenarios: {list(SUPPORTED_SCENARIOS)}"
        )

    base_url = "http://uitestingplayground.com"
    LOGGER.info(f"Starting UI Testing Playground Resilience Workflow [Scenario: '{scenario}']", extra={"step": "workflow_started"})

    if scenario == "dynamic_id":
        expected_text = str(data.get("expected_text") or "Button with Dynamic ID").strip()
        dynamic_page = DynamicIdPage(page, base_url=base_url)
        
        dynamic_page.navigate()
        if not dynamic_page.is_loaded():
            raise PermanentWorkerError("Dynamic ID test page failed to load button within timeout.")
        
        LOGGER.info("Dynamic ID page loaded. Locating element via resilient class selector...", extra={"step": "element_located"})
        actual_text = dynamic_page.click_dynamic_button()
        
        if expected_text and expected_text.lower() not in actual_text.lower():
            raise PermanentWorkerError(
                f"Dynamic ID verification failed: Expected text '{expected_text}' not found in actual text '{actual_text}'."
            )
        
        LOGGER.info(f"Successfully clicked dynamic button and verified DOM text: '{actual_text}'.", extra={"step": "result_verified"})
        
        return sanitize_sensitive_data({
            "status": "completed",
            "workflow": "ui_playground",
            "scenario": "dynamic_id",
            "verified": True,
            "expected_text": expected_text,
            "actual_text": actual_text,
            "resilient_selector_used": "button.btn-primary",
        })

    elif scenario == "load_delay":
        expected_text = str(data.get("expected_button_text") or "Button Appearing After Delay").strip()
        load_page = LoadDelayPage(page, base_url=base_url)
        
        load_page.navigate()
        LOGGER.info("Navigated to Load Delay page. Waiting explicitly for 5s server load delay...", extra={"step": "waiting_load_delay"})
        
        actual_text = load_page.click_delayed_button(timeout_ms=20000)
        if expected_text and expected_text.lower() not in actual_text.lower():
            raise PermanentWorkerError(
                f"Load Delay verification failed: Expected button text '{expected_text}' not found in '{actual_text}'."
            )
        
        LOGGER.info(f"Load Delay explicitly resolved. Button clicked and verified text: '{actual_text}'.", extra={"step": "result_verified"})
        
        return sanitize_sensitive_data({
            "status": "completed",
            "workflow": "ui_playground",
            "scenario": "load_delay",
            "verified": True,
            "expected_text": expected_text,
            "actual_text": actual_text,
            "explicit_wait_used": True,
        })

    elif scenario in ("ajax_data", "client_side_delay"):
        expected_text = str(data.get("expected_text") or "Data loaded with AJAX get request.").strip()
        ajax_page = AjaxDataPage(page, base_url=base_url)
        
        ajax_page.navigate()
        LOGGER.info("Navigated to AJAX Data page. Triggering asynchronous request...", extra={"step": "ajax_triggered"})
        
        ajax_page.trigger_ajax_request()
        LOGGER.info("Triggered AJAX request. Waiting explicitly for result container...", extra={"step": "waiting_ajax_result"})
        
        actual_text = ajax_page.wait_for_ajax_result(timeout_ms=25000)
        if expected_text and expected_text.lower() not in actual_text.lower():
            raise PermanentWorkerError(
                f"AJAX Data verification failed: Expected text '{expected_text}' not found in actual text '{actual_text}'."
            )
        
        LOGGER.info(f"AJAX Data loaded asynchronously. Verified DOM content: '{actual_text}'.", extra={"step": "result_verified"})
        
        return sanitize_sensitive_data({
            "status": "completed",
            "workflow": "ui_playground",
            "scenario": scenario,
            "verified": True,
            "expected_text": expected_text,
            "actual_text": actual_text,
            "explicit_wait_used": True,
        })

    raise PermanentWorkerError(f"Unhandled scenario '{scenario}'.")
