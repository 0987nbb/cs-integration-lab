# -*- coding: utf-8 -*-
"""
UI Testing Playground Page Objects Package.
Provides resilient Page Object implementations for dynamic IDs, load delays, and AJAX data scenarios.
"""
from .dynamic_id_page import DynamicIdPage
from .load_delay_page import LoadDelayPage
from .ajax_data_page import AjaxDataPage

__all__ = ["DynamicIdPage", "LoadDelayPage", "AjaxDataPage"]
