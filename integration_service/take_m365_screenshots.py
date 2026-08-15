# -*- coding: utf-8 -*-
"""Take screenshots of live Odoo UI views for M365 models.

Authentication strategy: inject the API key via Authorization header in
every Playwright request.  Odoo 17+ /odoo routes honour Bearer tokens for
JSON-RPC calls but the web client itself still needs a session cookie to
render pages.  We therefore:

1. Obtain a real session cookie by POST-ing to /web/session/authenticate
   with (db, login, api_key) — on Odoo.sh / Odoo Online the API key IS
   accepted as the password field when sent from the same trusted IP range.
   If that fails we fall back to fetching /web/dataset/call_kw with the
   Bearer token to extract the session_id cookie.
2. Load that cookie into the Playwright browser context.
3. Navigate directly to each action URL and screenshot.
"""
import asyncio
import os
import logging
import requests
from playwright.async_api import async_playwright
from integration_service.config import get_settings
from integration_service.odoo_client import OdooClient

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("screenshots")

ARTIFACT_DIR = r"C:\Users\Ali Raza\.gemini\antigravity\brain\5b47cc81-5807-4648-9b61-84f98ae790d4"
NAV_TIMEOUT = 60_000


def get_session_cookie(odoo_url: str, database: str, api_key: str) -> dict | None:
    """Obtain an Odoo session cookie via the Bearer-token authenticate endpoint."""
    # Strategy A: /web/session/authenticate with api_key as password
    for login in ["m365_admin@demo.com", "aalirazamughal71@gmail.com"]:
        try:
            resp = requests.post(
                f"{odoo_url}/web/session/authenticate",
                json={
                    "jsonrpc": "2.0",
                    "method": "call",
                    "id": 1,
                    "params": {"db": database, "login": login, "password": api_key},
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            data = resp.json()
            uid = data.get("result", {}).get("uid")
            if uid:
                session_id = resp.cookies.get("session_id")
                LOGGER.info("Session authenticated as uid=%s login=%s", uid, login)
                return {"name": "session_id", "value": session_id,
                        "domain": odoo_url.replace("https://", "").replace("http://", ""),
                        "path": "/"}
        except Exception as exc:
            LOGGER.warning("Auth attempt failed for %s: %s", login, exc)

    # Strategy B: use JSON-RPC call with Bearer token and grab any set-cookie
    try:
        resp = requests.post(
            f"{odoo_url}/json/2/res.users/read",
            json={"params": {"ids": [2], "fields": ["name", "login"]}},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        session_id = resp.cookies.get("session_id")
        if session_id:
            LOGGER.info("Obtained session_id via JSON/2 Bearer call")
            return {"name": "session_id", "value": session_id,
                    "domain": odoo_url.replace("https://", "").replace("http://", ""),
                    "path": "/"}
    except Exception as exc:
        LOGGER.warning("Strategy B failed: %s", exc)

    return None


async def _goto(page, url: str) -> None:
    """Navigate tolerating Odoo's perpetual background polling."""
    try:
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(5000)


async def capture_views():
    settings = get_settings()
    odoo_url = settings.odoo.url.rstrip("/")

    # Look up window action IDs
    oc = OdooClient()
    acts_tenant = oc.search_read("ir.actions.act_window", [["res_model", "=", "x_m365_tenant"]], fields=["id"])
    acts_snap   = oc.search_read("ir.actions.act_window", [["res_model", "=", "x_m365_user_snapshot"]], fields=["id"])
    acts_op     = oc.search_read("ir.actions.act_window", [["res_model", "=", "x_m365_operation"]], fields=["id"])
    acts_audit  = oc.search_read("ir.actions.act_window", [["res_model", "=", "x_m365_graph_audit_log"]], fields=["id"])
    acts_diff   = oc.search_read("ir.actions.act_window", [["res_model", "=", "x_m365_snapshot_diff"]], fields=["id"])

    act_tenant_id = acts_tenant[0]["id"] if acts_tenant else 905
    act_snap_id   = acts_snap[0]["id"]   if acts_snap   else 906
    act_op_id     = acts_op[0]["id"]     if acts_op     else 907
    act_audit_id  = acts_audit[0]["id"]  if acts_audit  else 908
    act_diff_id   = acts_diff[0]["id"]   if acts_diff   else 910

    LOGGER.info("Action IDs — tenant:%s snap:%s op:%s audit:%s diff:%s",
                act_tenant_id, act_snap_id, act_op_id, act_audit_id, act_diff_id)

    # Get session cookie
    cookie = get_session_cookie(odoo_url, settings.odoo.database, settings.odoo.api_key)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
            extra_http_headers={"Authorization": f"Bearer {settings.odoo.api_key}"},
        )

        # Inject session cookie if obtained
        if cookie:
            await context.add_cookies([{
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie["domain"],
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }])
            LOGGER.info("Session cookie injected into Playwright context.")
        else:
            LOGGER.warning("No session cookie obtained — pages may show login screen.")

        page = await context.new_page()
        page.set_default_timeout(NAV_TIMEOUT)

        # Navigate to backend home first
        LOGGER.info("Opening Odoo backend home...")
        await _goto(page, f"{odoo_url}/odoo?debug=1")
        LOGGER.info("Current URL: %s", page.url)

        # If still showing login page, try to detect and fill form using web_login/web_password
        if "/web/login" in page.url or await page.locator("input[name='login']").count() > 0:
            LOGGER.info("Login page detected, attempting form login with web_login/web_password...")
            if settings.odoo.web_login and settings.odoo.web_password:
                await page.fill("input[name='login']", settings.odoo.web_login)
                await page.fill("input[name='password']", settings.odoo.web_password)
                await page.click("button[type='submit']")
                await page.wait_for_timeout(6000)
                LOGGER.info("Post-submit URL: %s", page.url)
            else:
                LOGGER.error("No web credentials available. Cannot authenticate.")

        async def capture(action_id: int, label: str, filename: str) -> None:
            url = f"{odoo_url}/odoo/action-{action_id}?debug=1"
            LOGGER.info("Capturing %s (action %s) → %s", label, action_id, url)
            await _goto(page, url)
            LOGGER.info("  Page URL after nav: %s", page.url)
            out = os.path.join(ARTIFACT_DIR, filename)
            await page.screenshot(path=out, full_page=False)
            LOGGER.info("  Saved → %s", out)

        await capture(act_tenant_id, "x_m365_tenant",          "m365_tenant_list_view.png")
        await capture(act_snap_id,   "x_m365_user_snapshot",   "m365_user_snapshot_list_view.png")
        await capture(act_op_id,     "x_m365_operation",       "m365_operation_list_view.png")
        await capture(act_audit_id,  "x_m365_graph_audit_log", "m365_audit_log_list_view.png")
        await capture(act_diff_id,   "x_m365_snapshot_diff",   "m365_snapshot_diff_list_view.png")

        await browser.close()
        LOGGER.info("All screenshots captured.")


if __name__ == "__main__":
    asyncio.run(capture_views())
