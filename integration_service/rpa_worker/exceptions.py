# -*- coding: utf-8 -*-
"""
Exception hierarchy for the external RPA Worker.
Classifies failures into Transient, Permanent, and Human Intervention required.
"""


class WorkerError(Exception):
    """Base exception class for all RPA worker errors."""
    def __init__(self, message: str, details: str = "", screenshot_b64: str = None):
        super().__init__(message)
        self.message = message
        self.details = details
        self.screenshot_b64 = screenshot_b64

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class TransientWorkerError(WorkerError):
    """
    Transient / temporary failure (e.g. network timeout, browser timeout, temporary unreachable target).
    Candidate for retry according to retry policies.
    """
    pass


class PermanentWorkerError(WorkerError):
    """
    Permanent failure (e.g. invalid payload JSON, missing required workflow parameter, element not found, invalid data).
    Job should be marked failed and not retried automatically without intervention.
    """
    pass


class HumanInterventionRequiredError(WorkerError):
    """
    Challenge requiring human intervention (e.g. CAPTCHA, 2FA, OTP, unexpected auth challenge).
    Job state must be set to 'needs_human'. NEVER attempt to bypass CAPTCHA or 2FA automatically.
    """
    pass
