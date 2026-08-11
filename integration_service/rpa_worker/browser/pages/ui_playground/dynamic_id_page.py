# -*- coding: utf-8 -*-
"""
UI Testing Playground - Dynamic ID Page Object.
Demonstrates resilience against dynamic, randomly generated element IDs (e.g. id="9f21a48c-...")
by utilizing stable, semantic class and text locators instead of brittle ID attributes.
"""
from typing import Optional
from ..base_page import BasePage


class DynamicIdPage(BasePage):
    """Page Object for http://uitestingplayground.com/dynamicid."""

    # Resilient selector strategy avoiding randomly generated id attributes
    RESILIENT_BUTTON_SELECTOR = 'button.btn-primary'
    PAGE_HEADER = 'h3'

    def __init__(self, page, base_url: str = "http://uitestingplayground.com"):
        super().__init__(page, base_url=base_url)

    def navigate(self) -> None:
        """Navigates to the Dynamic ID test page."""
        self.navigate_to("dynamicid")

    def is_loaded(self) -> bool:
        """Verifies page is loaded using explicit wait for button visibility."""
        return self.is_visible(self.RESILIENT_BUTTON_SELECTOR, timeout_ms=10000)

    def get_button_text(self) -> str:
        """Reads text content of the dynamic button using resilient selector."""
        return self.get_text(self.RESILIENT_BUTTON_SELECTOR).strip()

    def click_dynamic_button(self) -> str:
        """Clicks the button using resilient class selector and returns clicked text."""
        self.click_element(self.RESILIENT_BUTTON_SELECTOR)
        return self.get_button_text()
