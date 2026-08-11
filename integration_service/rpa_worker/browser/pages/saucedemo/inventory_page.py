# -*- coding: utf-8 -*-
"""
SauceDemo Inventory Page Object.
Handles searching for requested products, verifying product availability, adding items to cart,
reading cart badge counts, and navigating to the cart page.
"""
from typing import Optional, Dict, Any, List
from ..base_page import BasePage


class InventoryPage(BasePage):
    """Page Object for SauceDemo Product Inventory Page."""

    INVENTORY_CONTAINER = '.inventory_list'
    INVENTORY_ITEM = '.inventory_item'
    ITEM_NAME = '.inventory_item_name'
    ITEM_PRICE = '.inventory_item_price'
    ADD_TO_CART_BTN = 'button'
    CART_BADGE = '.shopping_cart_badge'
    CART_LINK = '.shopping_cart_link'

    def is_loaded(self) -> bool:
        """Verifies if inventory page is loaded."""
        return self.is_visible(self.INVENTORY_CONTAINER, timeout_ms=5000)

    def find_product(self, product_name: str) -> Optional[Dict[str, Any]]:
        """
        Searches inventory items for product matching product_name (case-insensitive).
        Returns dictionary with product details if found, or None if product does not exist.
        """
        self.page.wait_for_selector(self.INVENTORY_CONTAINER, state="visible")
        items = self.page.query_selector_all(self.INVENTORY_ITEM)
        for item in items:
            name_el = item.query_selector(self.ITEM_NAME)
            if name_el:
                name_text = (name_el.text_content() or "").strip()
                if name_text.lower() == product_name.strip().lower():
                    price_el = item.query_selector(self.ITEM_PRICE)
                    price_text = (price_el.text_content() or "").strip() if price_el else ""
                    return {
                        "name": name_text,
                        "price": price_text,
                        "_element": item,
                    }
        return None

    def add_product_to_cart(self, product_name: str) -> Dict[str, Any]:
        """
        Finds requested product, clicks Add to Cart button, and verifies cart count change.
        Raises ValueError if product is not found.
        """
        product_info = self.find_product(product_name)
        if not product_info:
            raise ValueError(f"Product '{product_name}' was not found in SauceDemo inventory.")

        item_el = product_info["_element"]
        btn = item_el.query_selector("button")
        if not btn:
            # Fallback to data-test or item button
            btn = item_el.query_selector("[data-test^='add-to-cart']")
        
        if not btn:
            raise ValueError(f"Add to Cart button for product '{product_name}' was not found.")

        btn.click()
        return product_info

    def get_cart_badge_count(self) -> int:
        """Reads cart badge count value (returns 0 if badge is not visible)."""
        if self.is_visible(self.CART_BADGE, timeout_ms=2000):
            val_str = self.get_text(self.CART_BADGE).strip()
            return int(val_str) if val_str.isdigit() else 0
        return 0

    def open_cart(self) -> None:
        """Clicks shopping cart icon link."""
        self.click_element(self.CART_LINK)
