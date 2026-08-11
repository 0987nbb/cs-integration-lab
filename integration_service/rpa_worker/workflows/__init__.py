# -*- coding: utf-8 -*-
"""Workflows subpackage for RPA Worker."""
from .saucedemo_workflow import run_saucedemo_workflow
from .ui_playground_workflow import run_ui_playground_workflow

__all__ = ["run_saucedemo_workflow", "run_ui_playground_workflow"]
