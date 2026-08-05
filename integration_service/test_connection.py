# -*- coding: utf-8 -*-
"""
Harmless connection test script for Odoo 19 JSON-2 API.
Performs a read-only search_read on res.partner model (limit 3).
"""
import os
import sys

# Ensure integration_service directory is in python path
sys.path.insert(0, os.path.dirname(__file__))

from odoo_client import OdooClient, OdooClientError


def main():
    print("Initializing Odoo 19 JSON-2 API Client...")
    try:
        client = OdooClient()
    except OdooClientError as e:
        print(f"Initialization Error: {e}")
        sys.exit(1)

    print("Executing harmless READ test on model 'res.partner' (limit: 3)...")
    try:
        partners = client.search_read(
            model="res.partner",
            domain=[],
            fields=["id", "name", "email"],
            limit=3
        )
        print("READ Request Succeeded!")
        print(f"Number of records returned: {len(partners)}")
        for idx, partner in enumerate(partners, start=1):
            partner_id = partner.get("id")
            name = partner.get("name")
            email = partner.get("email") or "N/A"
            print(f"  Record #{idx}: ID={partner_id}, Name='{name}', Email='{email}'")
    except OdooClientError as e:
        print(f"Odoo READ Request Failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
