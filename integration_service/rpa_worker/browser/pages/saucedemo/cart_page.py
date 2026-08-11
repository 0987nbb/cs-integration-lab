# -*- coding: utf-8 -*-
"""
SauceDemo Cart Page Object.
Handles verifying actual cart items, matching product names, reading item prices & quantities,
and proceeding to checkout.
"""
from typing import Optional, Dict, Any, List
from ..base_page import BasePage


class CartPage(BasePage):
    """Page Object for SauceDemo Shopping Cart Page."""

    CART_CONTAINER = '.cart_list'
    CART_ITEM = '.cart_item'
    ITEM_NAME = '.inventory_item_name'
    ITEM_PRICE = '.inventory_item_price'
    ITEM_QTY = '.cart_quantity'
    CHECKOUT_BUTTON = '[data-test="checkout"]'

    def is_loaded(self) -> bool:
        """Verifies if cart page is loaded."""
        return self.is_visible(self.CART_CONTAINER, timeout_ms=5000)

    def verify_cart_item(self, product_name: str) -> Dict[str, Any]:
        """
        Verifies that requested product_name exists in cart.
        Reads and returns dictionary with actual cart item details.
        Raises ValueError if item is missing from cart.
        """
        self.page.wait_for_selector(self.CART_CONTAINER, state="visible")
        items = self.page.query_selector_all(self.CART_ITEM)
        for item in items:
            name_el = item.query_selector(self.ITEM_NAME)
            if name_el:
                name_text = (name_el.text_content() or "").strip()
                if name_text.lower() == product_name.strip().lower():
                    price_el = item.query_selector(self.ITEM_PRICE)
                    qty_el = item.query_selector(self.ITEM_QTY)
                    price_text = (price_el.text_content() or "").strip() if price_el else ""
                    qty_text = (qty_el.text_content() or "").strip() if qty_el else "1"
                    return {
                        "name": name_text,
                        "price": price_text,
                        "quantity": int(qty_text) if qty_text.isdigit() else 1,
                        "verified": True,
                    }
        raise ValueError(f"Cart verification failed: Product '{product_name}' was not found in cart list.")

    def proceed_to_checkout(self) -> None:
        """Clicks checkout button."""
        self.click_element(self.CHECKOUT_BUTTON)
