# -*- coding: utf-8 -*-
"""
Data structures and DTOs for the RPA Worker.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union


@dataclass
class JobPayload:
    """Parsed and validated RPA job payload."""
    raw_payload: str
    data: Dict[str, Any]
    job_type: str


@dataclass
class RpaJobRecord:
    """Representation of an Odoo x_rpa_job or cs.rpa.job record."""
    id: int
    name: str
    job_type: str
    payload_str: str
    state: str
    idempotency_key: str
    attempt_count: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_successful_step: Optional[str] = None
    result: Optional[str] = None
    error_details: Optional[str] = None
    screenshot: Optional[Union[str, bytes]] = None
    screenshot_filename: Optional[str] = None
    external_reference: Optional[str] = None

    @classmethod
    def from_odoo_dict(cls, data: Dict[str, Any]) -> "RpaJobRecord":
        """Factory method to construct an RpaJobRecord from Odoo API dictionary."""
        return cls(
            id=int(data.get("id", 0)),
            name=str(data.get("name") or data.get("x_name") or "New"),
            job_type=str(data.get("job_type") or data.get("x_job_type") or ""),
            payload_str=str(data.get("payload") or data.get("x_payload") or ""),
            state=str(data.get("state") or data.get("x_state") or "draft"),
            idempotency_key=str(data.get("idempotency_key") or data.get("x_idempotency_key") or ""),
            attempt_count=int(data.get("attempt_count") or data.get("x_attempt_count") or 0),
            started_at=data.get("started_at") or data.get("x_started_at"),
            finished_at=data.get("finished_at") or data.get("x_finished_at"),
            last_successful_step=data.get("last_successful_step") or data.get("x_last_successful_step"),
            result=data.get("result") or data.get("x_result"),
            error_details=data.get("error_details") or data.get("x_error_details"),
            screenshot=data.get("screenshot") or data.get("x_screenshot"),
            screenshot_filename=data.get("screenshot_filename") or data.get("x_screenshot_filename"),
            external_reference=data.get("external_reference") or data.get("x_external_reference"),
        )


@dataclass
class ExecutionResult:
    """Execution outcome produced by JobProcessor and Playwright workflows."""
    state: str  # 'success', 'failed', 'needs_human'
    result_data: Optional[Dict[str, Any]] = None
    error_details: Optional[str] = None
    last_successful_step: Optional[str] = None
    external_reference: Optional[str] = None
    screenshot_base64: Optional[str] = None
    screenshot_filename: Optional[str] = None
