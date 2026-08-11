# -*- coding: utf-8 -*-
"""
SauceDemo Page Objects Package.
Provides centralized, stable Page Object implementations for SauceDemo authentication,
inventory navigation, cart verification, and checkout completion.
"""
from .login_page import LoginPage
from .inventory_page import InventoryPage
from .cart_page import CartPage
from .checkout_page import CheckoutPage

__all__ = ["LoginPage", "InventoryPage", "CartPage", "CheckoutPage"]
