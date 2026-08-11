# -*- coding: utf-8 -*-
"""
Browser automation foundation built on Python Playwright.
Provides reusable browser lifecycle management, Page Object base classes, and screenshot capture.
"""
from .base import PlaywrightBrowserManager
from .pages.base_page import BasePage

__all__ = ["PlaywrightBrowserManager", "BasePage"]
