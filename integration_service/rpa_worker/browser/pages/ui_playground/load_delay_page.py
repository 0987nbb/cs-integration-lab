# -*- coding: utf-8 -*-
"""
UI Testing Playground - Load Delay Page Object.
Demonstrates handling server-side rendering delays (5-second load delay) using explicit Playwright
locator waits instead of hard-coded sleeps.
"""
from typing import Optional
from ..base_page import BasePage


class LoadDelayPage(BasePage):
    """Page Object for http://uitestingplayground.com/loaddelay."""

    LOAD_DELAY_BUTTON = 'button.btn-primary'

    def __init__(self, page, base_url: str = "http://uitestingplayground.com"):
        super().__init__(page, base_url=base_url)

    def navigate(self) -> None:
        """Navigates to the Load Delay test page."""
        self.navigate_to("loaddelay")

    def wait_for_button_after_delay(self, timeout_ms: int = 15000) -> str:
        """Waits explicitly for button to become visible after server load delay."""
        self.page.wait_for_selector(self.LOAD_DELAY_BUTTON, state="visible", timeout=timeout_ms)
        return self.get_text(self.LOAD_DELAY_BUTTON).strip()

    def click_delayed_button(self, timeout_ms: int = 15000) -> str:
        """Clicks the button after explicit wait."""
        text = self.wait_for_button_after_delay(timeout_ms=timeout_ms)
        self.click_element(self.LOAD_DELAY_BUTTON)
        return text
