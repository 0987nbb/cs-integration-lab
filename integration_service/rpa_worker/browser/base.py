# -*- coding: utf-8 -*-
"""
Playwright Browser Manager.
Manages browser/context/page lifecycle, timeouts, screenshot capturing, and exception translation.
"""
import base64
from typing import Optional, Dict, Any, Callable
from ..config import WorkerConfig
from ..exceptions import (
    TransientWorkerError,
    PermanentWorkerError,
    HumanInterventionRequiredError,
)
from ..logging_utils import get_worker_logger

LOGGER = get_worker_logger("rpa_worker.browser")


class PlaywrightBrowserManager:
    """Manages Python Playwright browser context, execution, and cleanup."""

    def __init__(self, config: Optional[WorkerConfig] = None):
        self.config = config or WorkerConfig()

    def run_browser_task(self, task_func: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
        """
        Executes task_func(page) inside a fresh Playwright browser context.
        Handles context cleanup and converts Playwright exceptions into structured WorkerErrors.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
        except ImportError:
            raise PermanentWorkerError(
                "Playwright package is not installed.",
                details="Run '.venv\\Scripts\\python.exe -m pip install playwright' and '.venv\\Scripts\\python.exe -m playwright install chromium'."
            )

        playwright_obj = None
        browser = None
        context = None
        page = None
        screenshot_b64: Optional[str] = None

        try:
            playwright_obj = sync_playwright().start()
            browser_type = getattr(playwright_obj, self.config.browser_type, playwright_obj.chromium)
            browser = browser_type.launch(headless=self.config.headless)
            
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Odoo-RPA-Worker/1.0",
            )
            context.set_default_timeout(self.config.action_timeout_ms)
            context.set_default_navigation_timeout(self.config.navigation_timeout_ms)

            page = context.new_page()
            
            # Execute browser task logic
            result = task_func(page)
            if isinstance(result, dict) and "_screenshot_b64" not in result and page:
                shot = self._capture_screenshot_b64(page)
                if shot:
                    result["_screenshot_b64"] = shot
            return result

        except PlaywrightTimeoutError as exc:
            LOGGER.warning(f"Browser action/navigation timeout: {exc}")
            if page:
                screenshot_b64 = self._capture_screenshot_b64(page)
            raise TransientWorkerError("Browser timeout encountered during automation.", details=str(exc), screenshot_b64=screenshot_b64) from exc

        except PlaywrightError as exc:
            msg = str(exc)
            LOGGER.error(f"Playwright error during execution: {msg}")
            if page:
                screenshot_b64 = self._capture_screenshot_b64(page)
            
            # Check for CAPTCHA / 2FA or human challenge indicators in page or exception
            if "captcha" in msg.lower() or "2fa" in msg.lower() or "otp" in msg.lower() or "challenge" in msg.lower():
                raise HumanInterventionRequiredError("Authentication challenge or CAPTCHA encountered.", details=msg, screenshot_b64=screenshot_b64) from exc
            
            raise PermanentWorkerError(f"Browser execution failed: {msg}", details=msg, screenshot_b64=screenshot_b64) from exc

        except (TransientWorkerError, PermanentWorkerError, HumanInterventionRequiredError) as exc:
            if page and not getattr(exc, "screenshot_b64", None):
                exc.screenshot_b64 = self._capture_screenshot_b64(page)
            raise

        except Exception as exc:
            LOGGER.error(f"Unexpected exception during browser execution: {exc}")
            if page:
                screenshot_b64 = self._capture_screenshot_b64(page)
            raise PermanentWorkerError(f"Unexpected browser automation error: {exc}", details=str(exc), screenshot_b64=screenshot_b64) from exc

        finally:
            # Clean up page, context, browser, and playwright in reverse order
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_obj:
                try:
                    playwright_obj.stop()
                except Exception:
                    pass

    def _capture_screenshot_b64(self, page: Any) -> Optional[str]:
        """Captures page screenshot as base64 encoded string safely."""
        try:
            png_bytes = page.screenshot(type="png", full_page=False)
            return base64.b64encode(png_bytes).decode("utf-8")
        except Exception as exc:
            LOGGER.debug(f"Could not capture failure screenshot: {exc}")
            return None
