# Microsoft Graph Least-Privilege Permissions Matrix

This document defines the Microsoft Graph **application permissions** needed for
the Microsoft 365 Graph Operations Layer. Authentication is app-only OAuth 2.0
client credentials against the demo Microsoft 365 tenant.

Sources used:

- Microsoft Learn: Create user (`POST /users`)
- Microsoft Learn: Update user (`PATCH /users/{id | userPrincipalName}`)
- Microsoft Learn: List users (`GET /users`)
- Microsoft Learn: List user direct memberships (`GET /users/{id}/memberOf`)
- Microsoft Learn: List subscribed SKUs (`GET /subscribedSkUs`)
- Microsoft Learn: Add group members (`POST /groups/{id}/members/$ref`)
- Microsoft Learn: Assign license (`POST /users/{id}/assignLicense`)
- Microsoft Learn: Revoke sign-in sessions (`POST /users/{id}/revokeSignInSessions`)
- Microsoft Learn: Microsoft Graph permissions reference

## Required Application Permissions

| Permission | Operations | Why it is needed | Broader permission avoided |
| :--- | :--- | :--- | :--- |
| `Organization.Read.All` | Tenant readiness: organization profile, tenant ID, verified/default domains | Least-privileged app permission for organization metadata used by readiness discovery | Avoids `Directory.Read.All` where only organization/domain metadata is needed |
| `LicenseAssignment.Read.All` | Tenant readiness: `/subscribedSkus`, purchased/consumed/available license capacity | Least-privileged app permission for subscribed SKU/license capacity read | Avoids `Directory.Read.All` for SKU discovery |
| `User.Read.All` | User 360 profile reads, manager reads, onboarding/offboarding read-back verification, user sampling | Least-privileged app permission to read users in daemon mode | Avoids `Directory.Read.All` for ordinary user reads |
| `Directory.Read.All` | User direct/transitive membership details where Graph requires directory-object relationship reads | Microsoft Graph app-only permissions for another user's `memberOf` require `Directory.Read.All`; it is justified only for membership diagnostics/verification | Avoids `Directory.ReadWrite.All`; use read-only directory permission |
| `User.Create` | Synthetic onboarding user creation | Least-privileged app permission for `POST /users` | Avoids `User.ReadWrite.All` solely for creation |
| `User.ReadUpdate.All` | Update normal user profile fields where supported | Least-privileged app permission for general user updates | Avoids `Directory.ReadWrite.All` |
| `User.EnableDisableAccount.All` plus `User.Read.All` | Block/unblock sign-in and offboarding account disablement (`accountEnabled`) | Microsoft Graph documents this as the least-privileged combination for updating `accountEnabled` | Avoids using broad `User.ReadWrite.All` only for account status |
| `User-PasswordProfile.ReadWrite.All` | Set/reset temporary password where supported | Least-privileged app permission for updating `passwordProfile` | Avoids broad `User.ReadWrite.All` or `Directory.ReadWrite.All` for password reset |
| `User.RevokeSessions.All` | Revoke active sign-in sessions | Least-privileged app permission for `revokeSignInSessions` | Avoids `Directory.ReadWrite.All`; `User.ReadWrite.All` is not the least-privileged app permission for this action |
| `GroupMember.ReadWrite.All` | Add/remove approved group memberships; discover LAB groups basic membership context | Least-privileged app permission for user membership mutation | Avoids `Group.ReadWrite.All`, which can modify group properties and create/delete groups |
| `LicenseAssignment.ReadWrite.All` | Assign/remove approved Microsoft 365 licenses | Least-privileged app permission for `assignLicense` | Avoids `Directory.ReadWrite.All` and broad user write permissions for licensing |

## Optional Diagnostic Permissions

| Permission | Operations | Behavior if missing |
| :--- | :--- | :--- |
| `UserAuthenticationMethod.Read.All` | User 360 registered authentication methods | Show `Not available`; do not fail the whole diagnostic |
| `Device.Read.All` | User registered devices where available | Show `Not available`; do not fail the whole diagnostic |
| `DeviceManagementManagedDevices.Read.All` | Intune managed devices where available | Show `Not available`; do not fail the whole diagnostic |

## Permissions Not Requested

| Permission | Reason |
| :--- | :--- |
| `Directory.ReadWrite.All` | Too broad for this assignment. It grants broad directory write capability and is not required for the scoped user, group membership, license, and session actions above. |
| `Group.ReadWrite.All` | Broader than required because the assignment manages group membership only, not group metadata or group lifecycle. |
| `Application.ReadWrite.All` or `AppRoleAssignment.ReadWrite.All` | Not needed. The assignment does not automate Entra app registration, admin consent, or Partner Center/GDAP setup. |
| Delegated permissions such as `User.Read` | This service uses app-only client credentials, so delegated permissions are not used by the runtime. |

## Credential Handling

- `M365_TENANT_ID`, `M365_CLIENT_ID`, and `M365_CLIENT_SECRET` are read from environment variables.
- `.env` is ignored by Git.
- `.env.example` must contain placeholders only.
- Client secrets and access tokens are registered with the sanitizer and redacted from logs, exceptions, and audit persistence.
