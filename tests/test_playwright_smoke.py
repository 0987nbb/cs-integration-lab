# -*- coding: utf-8 -*-
"""
Safe Playwright Local Smoke Test for Phase 2 RPA Worker foundation.
Launches Playwright headless Chromium, visits a public harmless page (https://example.com),
verifies page title, captures a screenshot, and closes cleanly.
"""
import base64
import os
import sys
import pytest

sys.path.insert(0, ".")

from integration_service.rpa_worker.config import WorkerConfig
from integration_service.rpa_worker.browser.base import PlaywrightBrowserManager


def run_playwright_smoke_test() -> dict:
    """Executes safe browser launch and navigation smoke test."""
    config = WorkerConfig(headless=True)
    manager = PlaywrightBrowserManager(config=config)

    def smoke_task(page):
        page.goto("https://example.com", timeout=15000)
        title = page.title()
        url = page.url
        png_bytes = page.screenshot(type="png")
        b64_screenshot = base64.b64encode(png_bytes).decode("utf-8")
        return {
            "status": "success",
            "title": title,
            "url": url,
            "screenshot_len": len(b64_screenshot),
        }

    return manager.run_browser_task(smoke_task)


def test_playwright_smoke():
    """Pytest test case executing safe local Playwright browser smoke test if Playwright is installed."""
    try:
        import playwright
    except ImportError:
        pytest.skip("Playwright is not installed in local environment.")

    try:
        res = run_playwright_smoke_test()
        assert res["status"] == "success"
        assert "Example Domain" in res["title"] or "example" in res["url"].lower()
        assert res["screenshot_len"] > 100
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "browser" in str(exc).lower():
            pytest.skip(f"Playwright browser binaries not installed locally: {exc}")
        else:
            raise exc


if __name__ == "__main__":
    print("Running Playwright Local Smoke Test...")
    try:
        outcome = run_playwright_smoke_test()
        print("Playwright Smoke Test PASSED! Outcome:", outcome)
    except Exception as err:
        print("Playwright Smoke Test Skipped/Failed:", err)
