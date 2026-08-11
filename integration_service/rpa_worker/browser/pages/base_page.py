# -*- coding: utf-8 -*-
"""
Reusable Page Object pattern base class for Playwright.
Encapsulates selector navigation, actions, and assertions.
"""
from typing import Optional, Any


class BasePage:
    """Base class for all Page Object implementations."""

    def __init__(self, page: Any, base_url: str = ""):
        self.page = page
        self.base_url = base_url

    def navigate_to(self, url_or_path: str = "") -> None:
        """Navigates to URL or path."""
        target_url = url_or_path if url_or_path.startswith("http") else f"{self.base_url.rstrip('/')}/{url_or_path.lstrip('/')}"
        self.page.goto(target_url)

    def fill_input(self, selector: str, text: str) -> None:
        """Fills an input field after waiting for selector visibility."""
        self.page.wait_for_selector(selector, state="visible")
        self.page.fill(selector, text)

    def click_element(self, selector: str) -> None:
        """Clicks an element after waiting for selector visibility."""
        self.page.wait_for_selector(selector, state="visible")
        self.page.click(selector)

    def get_text(self, selector: str) -> str:
        """Returns text content of element."""
        self.page.wait_for_selector(selector, state="visible")
        return self.page.text_content(selector) or ""

    def is_visible(self, selector: str, timeout_ms: int = 5000) -> bool:
        """Returns True if selector is visible within timeout_ms."""
        try:
            if hasattr(self.page, "is_visible") and callable(getattr(self.page, "is_visible")):
                res = self.page.is_visible(selector)
                if isinstance(res, bool):
                    return res
            self.page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False
