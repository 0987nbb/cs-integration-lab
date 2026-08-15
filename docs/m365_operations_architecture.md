# Microsoft 365 Operations Layer — Architecture & Multi-Tenant Guide

## Overview

The **Microsoft 365 Operations Layer** provides an enterprise-ready, multi-tenant capable, resilient Python service integrated with Odoo (`https://ai-demo-company.odoo.com`).

It serves as the technical foundation for:
1. **Tenant Readiness & Discovery**: Dynamic environment discovery and capacity tracking.
2. **User 360 Diagnostic**: Helpdesk diagnostic snapshots with graceful degradation.
3. **Synthetic Employee Onboarding**: Two-stage (Plan/Dry-Run -> Approved Execution & Read-Back Verification).
4. **Helpdesk Remediation Actions**: 8 state-changing actions (Block/Unblock Sign-in, Revoke Sessions, Password Reset, Group Add/Remove, License Assign/Remove).
5. **Synthetic Employee Offboarding**: Idempotent offboarding preserving protected groups.

---

## Multi-Tenant Architecture & Authentication Decoupling

```
+-------------------------------------------------------------------+
|                        GRAPH BUSINESS OPERATIONS                  |
|  TenantReadiness | User360 | Onboarding | Remediation | Offboarding|
+---------------------------------+---------------------------------+
                                  | Uses explicit TenantContext
                                  v
+-------------------------------------------------------------------+
|                     AUTHENTICATION LAYER                          |
|  TenantContext  <--->  TokenProvider (Abstract Interface)        |
|                              |                                    |
|             +----------------+----------------+                   |
|             |                                 |                   |
|             v                                 v                   |
|   MSALTokenProvider               (Future Provider: e.g.          |
|   (App-Only Client Creds)          Delegated / Customer Tenant)   |
+-------------------------------------------------------------------+
```

### Key Architectural Guarantee:
- **Decoupled Business Logic**: `TenantReadinessService`, `User360Service`, `OnboardingService`, `RemediationService`, and `OffboardingService` do NOT perform OAuth HTTP token requests directly.
- **Explicit `TenantContext`**: Every operation receives an explicit `TenantContext` instance containing `tenant_id`, `display_name`, `domain`, and `graph_base_url`.
- **Abstract `TokenProvider`**: Authentication is abstracted via `TokenProvider.get_access_token()`. Supporting customer tenants or alternative authentication flows in the future requires swapping the `TokenProvider` instance without modifying a single line of business operation logic.

---

## Odoo Control Plane Integration

- **Target Instance**: Odoo Online Demo (`https://ai-demo-company.odoo.com`, Database: `ai-demo-company`).
- **Route**: Official Odoo 19 JSON-2 API (`POST {ODOO_URL}/json/2/<model>/<method>`).
- **Authentication**: Bearer API key (`Authorization: Bearer {ODOO_API_KEY}`) and `X-Odoo-Database: ai-demo-company` header.
- **Persistence Model**: `x_integration_config` (for integration state, formatted reports, and JSON payloads) and `x_integration_sync_log` (for audit execution history).
- **Idempotency**: All writes check for existing external/natural keys to perform in-place updates (`OdooClient.write`), preventing duplicate record creation.
