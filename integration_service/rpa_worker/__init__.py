# -*- coding: utf-8 -*-
"""
RPA Worker Package for Odoo Integration Lab.
Provides Playwright browser automation worker, job polling, safe claim mechanism,
stale-running recovery, structured logging, and Page Object foundation.
"""

from .config import WorkerConfig
from .exceptions import (
    WorkerError,
    TransientWorkerError,
    PermanentWorkerError,
    HumanInterventionRequiredError,
)
from .logging_utils import get_worker_logger, sanitize_sensitive_data
from .models import RpaJobRecord, JobPayload, ExecutionResult
from .odoo_claim import OdooJobClaimer
from .job_processor import JobProcessor
from .worker import RpaWorker

__all__ = [
    "WorkerConfig",
    "WorkerError",
    "TransientWorkerError",
    "PermanentWorkerError",
    "HumanInterventionRequiredError",
    "get_worker_logger",
    "sanitize_sensitive_data",
    "RpaJobRecord",
    "JobPayload",
    "ExecutionResult",
    "OdooJobClaimer",
    "JobProcessor",
    "RpaWorker",
]
