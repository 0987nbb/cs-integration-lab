# -*- coding: utf-8 -*-
"""
SauceDemo Login Page Object.
Handles authentication, credential input, login verification, error message extraction,
and human intervention challenge detection.
"""
from typing import Tuple, Optional
from ..base_page import BasePage


class LoginPage(BasePage):
    """Page Object for SauceDemo Login Page (https://www.saucedemo.com)."""

    # Centralized stable selectors
    USERNAME_INPUT = '[data-test="username"]'
    PASSWORD_INPUT = '[data-test="password"]'
    LOGIN_BUTTON = '[data-test="login-button"]'
    ERROR_MESSAGE = '[data-test="error"]'
    INVENTORY_CONTAINER = '.inventory_list'
    PAGE_TITLE = '.title'

    def __init__(self, page, base_url: str = "https://www.saucedemo.com"):
        super().__init__(page, base_url=base_url)

    def navigate(self) -> None:
        """Navigates to SauceDemo home page."""
        self.navigate_to("")

    def login(self, username: str, password: str) -> None:
        """Fills login form and clicks login button."""
        self.fill_input(self.USERNAME_INPUT, username)
        self.fill_input(self.PASSWORD_INPUT, password)
        self.click_element(self.LOGIN_BUTTON)

    def is_logged_in(self) -> bool:
        """Verifies if login succeeded by reading page state (inventory list or title visible)."""
        if self.is_visible(self.INVENTORY_CONTAINER, timeout_ms=5000):
            return True
        if self.is_visible(self.PAGE_TITLE, timeout_ms=3000):
            text = self.get_text(self.PAGE_TITLE)
            return "products" in text.lower()
        return False

    def get_error_message(self) -> str:
        """Returns login error message text if visible."""
        if self.is_visible(self.ERROR_MESSAGE, timeout_ms=3000):
            return self.get_text(self.ERROR_MESSAGE)
        return ""

    def has_human_challenge(self) -> bool:
        """Checks for CAPTCHA, 2FA, OTP or unexpected auth challenges."""
        try:
            content = self.page.content().lower()
            return any(term in content for term in ("captcha", "g-recaptcha", "hcaptcha", "2fa", "otp_code", "human verification"))
        except Exception:
            return False
