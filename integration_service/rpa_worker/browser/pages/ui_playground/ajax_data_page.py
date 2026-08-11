# -*- coding: utf-8 -*-
"""
UI Testing Playground - AJAX Data Page Object.
Demonstrates resilience against asynchronous client-side / AJAX data loading delays (15s delay)
using explicit Playwright condition waits rather than arbitrary sleeps.
"""
from typing import Optional
from ..base_page import BasePage


class AjaxDataPage(BasePage):
    """Page Object for http://uitestingplayground.com/ajax."""

    AJAX_BUTTON = '#ajaxButton'
    AJAX_RESULT_CONTAINER = 'p.bg-success'

    def __init__(self, page, base_url: str = "http://uitestingplayground.com"):
        super().__init__(page, base_url=base_url)

    def navigate(self) -> None:
        """Navigates to AJAX Data test page."""
        self.navigate_to("ajax")

    def trigger_ajax_request(self) -> None:
        """Clicks AJAX trigger button."""
        self.click_element(self.AJAX_BUTTON)

    def wait_for_ajax_result(self, timeout_ms: int = 20000) -> str:
        """Waits explicitly for AJAX data container to render after asynchronous response."""
        self.page.wait_for_selector(self.AJAX_RESULT_CONTAINER, state="visible", timeout=timeout_ms)
        return self.get_text(self.AJAX_RESULT_CONTAINER).strip()
