# -*- coding: utf-8 -*-
"""
SauceDemo Checkout Page Object.
Handles Step 1 information entry (first_name, last_name, postal_code),
Step 2 order overview verification, checkout submission, and final order confirmation verification.
"""
from typing import Optional, Dict, Any
from ..base_page import BasePage


class CheckoutPage(BasePage):
    """Page Object for SauceDemo Checkout Flow & Order Confirmation."""

    FIRST_NAME_INPUT = '[data-test="firstName"]'
    LAST_NAME_INPUT = '[data-test="lastName"]'
    POSTAL_CODE_INPUT = '[data-test="postalCode"]'
    CONTINUE_BUTTON = '[data-test="continue"]'
    FINISH_BUTTON = '[data-test="finish"]'
    
    PAGE_TITLE = '.title'
    COMPLETE_CONTAINER = '#checkout_complete_container'
    COMPLETE_HEADER = '.complete-header'

    def fill_checkout_information(self, first_name: str, last_name: str, postal_code: str) -> None:
        """Fills checkout step 1 customer information and clicks continue."""
        self.fill_input(self.FIRST_NAME_INPUT, first_name)
        self.fill_input(self.LAST_NAME_INPUT, last_name)
        self.fill_input(self.POSTAL_CODE_INPUT, postal_code)
        self.click_element(self.CONTINUE_BUTTON)

    def is_overview_loaded(self) -> bool:
        """Verifies if checkout step 2 overview page is loaded."""
        if self.is_visible(self.FINISH_BUTTON, timeout_ms=5000):
            return True
        if self.is_visible(self.PAGE_TITLE, timeout_ms=3000):
            text = self.get_text(self.PAGE_TITLE)
            return "overview" in text.lower()
        return False

    def finish_checkout(self) -> None:
        """Clicks finish button on overview page."""
        self.click_element(self.FINISH_BUTTON)

    def is_confirmation_loaded(self) -> bool:
        """
        Verifies actual SauceDemo order confirmation page state.
        Returns True if complete header ('Thank you for your order!') or complete container is visible.
        """
        if self.is_visible(self.COMPLETE_HEADER, timeout_ms=5000):
            text = self.get_text(self.COMPLETE_HEADER).strip()
            return "thank you" in text.lower()
        if self.is_visible(self.COMPLETE_CONTAINER, timeout_ms=3000):
            return True
        return False

    def get_confirmation_header(self) -> str:
        """Returns order confirmation text."""
        if self.is_visible(self.COMPLETE_HEADER, timeout_ms=3000):
            return self.get_text(self.COMPLETE_HEADER).strip()
        return ""
