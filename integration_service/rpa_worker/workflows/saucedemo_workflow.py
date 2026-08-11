# -*- coding: utf-8 -*-
"""
SauceDemo Browser Automation Workflow.
Executes end-to-end SauceDemo login, product selection, cart verification, customer checkout,
and final order confirmation page state verification.
"""
from typing import Dict, Any, Optional
from ..config import WorkerConfig
from ..exceptions import (
    PermanentWorkerError,
    TransientWorkerError,
    HumanInterventionRequiredError,
)
from ..logging_utils import get_worker_logger, sanitize_sensitive_data
from ..models import JobPayload
from ..browser.pages.saucedemo import LoginPage, InventoryPage, CartPage, CheckoutPage

LOGGER = get_worker_logger("rpa_worker.workflow.saucedemo")


def run_saucedemo_workflow(
    page: Any,
    payload: JobPayload,
    config: Optional[WorkerConfig] = None,
) -> Dict[str, Any]:
    """
    Executes SauceDemo checkout workflow using Page Objects.
    Verifies actual browser page state at every milestone.
    """
    cfg = config or WorkerConfig()
    data = payload.data or {}

    # Extract & validate input parameters
    product_name = str(data.get("product_name") or data.get("product") or "").strip()
    if not product_name:
        raise PermanentWorkerError("Missing required 'product_name' parameter in payload.")

    checkout_data = data.get("checkout") or {}
    if not isinstance(checkout_data, dict):
        raise PermanentWorkerError("Payload parameter 'checkout' must be a JSON dictionary.")

    first_name = str(checkout_data.get("first_name") or "Ali").strip()
    last_name = str(checkout_data.get("last_name") or "Raza").strip()
    postal_code = str(checkout_data.get("postal_code") or "46000").strip()

    if not first_name or not last_name or not postal_code:
        raise PermanentWorkerError(
            "Missing required checkout information: 'first_name', 'last_name', and 'postal_code' must be provided."
        )

    if not cfg.saucedemo_username or not cfg.saucedemo_password:
        raise PermanentWorkerError(
            "Missing required SauceDemo credentials (SAUCEDEMO_USERNAME / SAUCEDEMO_PASSWORD). Please configure them in environment / .env."
        )

    LOGGER.info(
        f"Starting SauceDemo Workflow [Product: '{product_name}', Customer: '{first_name} {last_name}']",
        extra={"step": "login_started"},
    )

    # Initialize Page Objects
    login_page = LoginPage(page, base_url=cfg.saucedemo_url)
    inventory_page = InventoryPage(page)
    cart_page = CartPage(page)
    checkout_page = CheckoutPage(page)

    # Step 1: Navigate & Authenticate
    login_page.navigate()
    login_page.login(cfg.saucedemo_username, cfg.saucedemo_password)

    if login_page.has_human_challenge():
        raise HumanInterventionRequiredError(
            "Authentication challenge / CAPTCHA encountered on SauceDemo login page."
        )

    if not login_page.is_logged_in():
        err_msg = login_page.get_error_message()
        if err_msg:
            raise PermanentWorkerError(f"SauceDemo authentication failed: {err_msg}")
        raise PermanentWorkerError("SauceDemo authentication failed: Inventory page was not reached after login.")

    LOGGER.info("SauceDemo authentication verified successfully.", extra={"step": "login_verified"})

    # Step 2: Inventory Search & Add to Cart
    if not inventory_page.is_loaded():
        raise PermanentWorkerError("Inventory page failed to load after login.")

    product_info = inventory_page.find_product(product_name)
    if not product_info:
        raise PermanentWorkerError(f"Requested product '{product_name}' was not found in SauceDemo inventory.")

    LOGGER.info(f"Found product '{product_name}' (Price: {product_info['price']}). Adding to cart...", extra={"step": "product_found"})

    inventory_page.add_product_to_cart(product_name)
    badge_count = inventory_page.get_cart_badge_count()
    if badge_count < 1:
        raise PermanentWorkerError("Cart badge count did not update after clicking Add to Cart.")

    LOGGER.info("Product added to cart. Badge count verified.", extra={"step": "product_added"})

    # Step 3: Open Cart & Verify Contents
    inventory_page.open_cart()
    if not cart_page.is_loaded():
        raise PermanentWorkerError("Cart page failed to load after clicking cart icon.")

    cart_details = cart_page.verify_cart_item(product_name)
    LOGGER.info(f"Cart contents verified for product '{product_name}' (Qty: {cart_details['quantity']}).", extra={"step": "cart_verified"})

    # Step 4: Checkout Information Submission
    cart_page.proceed_to_checkout()
    checkout_page.fill_checkout_information(first_name, last_name, postal_code)

    if not checkout_page.is_overview_loaded():
        raise PermanentWorkerError("Checkout Overview page failed to load after submitting information.")

    LOGGER.info("Checkout overview loaded and verified. Submitting order...", extra={"step": "checkout_submitted"})

    # Step 5: Complete Order & Verify Confirmation State
    checkout_page.finish_checkout()

    if not checkout_page.is_confirmation_loaded():
        raise PermanentWorkerError("SauceDemo order confirmation page state was not detected after clicking finish.")

    confirmation_header = checkout_page.get_confirmation_header()
    LOGGER.info(f"SauceDemo Order Confirmation Verified: '{confirmation_header}'", extra={"step": "order_confirmation_verified"})

    output_res = {
        "status": "completed",
        "workflow": "saucedemo_checkout",
        "product_name": product_name,
        "product_price": product_info.get("price", ""),
        "cart_verified": True,
        "checkout_verified": True,
        "order_confirmation_verified": True,
        "confirmation_header": confirmation_header,
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "postal_code": postal_code,
        },
    }

    return sanitize_sensitive_data(output_res)
