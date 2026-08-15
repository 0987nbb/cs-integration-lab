# Microsoft Graph API Operation Mapping

This document maps all business operations in the **Microsoft 365 Operations Layer** to their exact Microsoft Graph API (v1.0) endpoints, HTTP methods, and required permissions.

---

## 1. Tenant Readiness & Discovery

| Operation | HTTP Method | Graph API Endpoint | Permission Required |
| :--- | :--- | :--- | :--- |
| Discover Identity | `GET` | `/v1.0/organization` | `Organization.Read.All` |
| Discover Domains | `GET` | `/v1.0/domains` | `Organization.Read.All` |
| Discover SKUs & License Capacity | `GET` | `/v1.0/subscribedSkus` | `LicenseAssignment.Read.All` |
| Discover LAB Test Groups | `GET` | `/v1.0/groups` | `GroupMember.ReadWrite.All` |
| Capability Access Checks | `GET` | `/v1.0/users?$top=1`, `/v1.0/deviceManagement/...` | `User.Read.All`, `DeviceManagementManagedDevices.Read.All` |

---

## 2. User 360 Diagnostic (Read-Only)

| Operation | HTTP Method | Graph API Endpoint | Permission Required |
| :--- | :--- | :--- | :--- |
| Get User Profile | `GET` | `/v1.0/users/{upn}` | `User.Read.All` |
| Get User Manager | `GET` | `/v1.0/users/{upn}/manager` | `User.Read.All` |
| Get Direct Group Memberships | `GET` | `/v1.0/users/{upn}/memberOf` | `Directory.Read.All` |
| Get Transitive Group Memberships | `GET` | `/v1.0/users/{upn}/transitiveMemberOf` | `Directory.Read.All` |
| Get Auth Methods | `GET` | `/v1.0/users/{upn}/authentication/methods` | `UserAuthenticationMethod.Read.All` (or graceful fallback) |
| Get Registered Devices | `GET` | `/v1.0/users/{upn}/registeredDevices` | `User.Read.All` |

---

## 3. Synthetic Employee Onboarding (`LAB-*`)

| Operation | HTTP Method | Graph API Endpoint | Permission Required |
| :--- | :--- | :--- | :--- |
| Create Test User | `POST` | `/v1.0/users` | `User.Create`, `User.ReadWrite.All` |
| Assign Manager | `PUT` | `/v1.0/users/{id}/manager/$ref` | `User.ReadWrite.All` |
| Assign License | `POST` | `/v1.0/users/{id}/assignLicense` | `LicenseAssignment.ReadWrite.All` |
| Add to Demo Group | `POST` | `/v1.0/groups/{group_id}/members/$ref` | `GroupMember.ReadWrite.All` |
| Post-Onboarding Verification | `GET` | `/v1.0/users/{id}`, `/v1.0/users/{id}/memberOf` | `User.Read.All` |

---

## 4. Helpdesk Remediation Actions

| Remediation Action | HTTP Method | Graph API Endpoint | Permission Required |
| :--- | :--- | :--- | :--- |
| Block Sign-in | `PATCH` | `/v1.0/users/{id}` (`accountEnabled: false`) | `User.EnableDisableAccount.All` + `User.Read.All` |
| Unblock Sign-in | `PATCH` | `/v1.0/users/{id}` (`accountEnabled: true`) | `User.EnableDisableAccount.All` + `User.Read.All` |
| Revoke Sign-in Sessions | `POST` | `/v1.0/users/{id}/revokeSignInSessions` | `User.RevokeSessions.All` |
| Reset Temporary Password | `PATCH` | `/v1.0/users/{id}` (`passwordProfile`) | `User-PasswordProfile.ReadWrite.All` |
| Add to Approved Group | `POST` | `/v1.0/groups/{group_id}/members/$ref` | `GroupMember.ReadWrite.All` |
| Remove from Approved Group | `DELETE` | `/v1.0/groups/{group_id}/members/{user_id}/$ref` | `GroupMember.ReadWrite.All` |
| Assign License | `POST` | `/v1.0/users/{id}/assignLicense` | `LicenseAssignment.ReadWrite.All` |
| Remove License | `POST` | `/v1.0/users/{id}/assignLicense` | `LicenseAssignment.ReadWrite.All` |

---

## 5. Synthetic Employee Offboarding (`LAB-*`)

| Operation | HTTP Method | Graph API Endpoint | Permission Required |
| :--- | :--- | :--- | :--- |
| Capture Pre-State | `GET` | `/v1.0/users/{id}`, `/v1.0/users/{id}/memberOf` | `User.Read.All` + `Directory.Read.All` |
| Revoke Active Sessions | `POST` | `/v1.0/users/{id}/revokeSignInSessions` | `User.RevokeSessions.All` |
| Disable Sign-in | `PATCH` | `/v1.0/users/{id}` (`accountEnabled: false`) | `User.EnableDisableAccount.All` + `User.Read.All` |
| Remove Group Memberships | `DELETE` | `/v1.0/groups/{group_id}/members/{user_id}/$ref` | `GroupMember.ReadWrite.All` |
| Remove Assigned Licenses | `POST` | `/v1.0/users/{id}/assignLicense` | `LicenseAssignment.ReadWrite.All` |
| Final State Verification | `GET` | `/v1.0/users/{id}`, `/v1.0/users/{id}/memberOf` | `User.Read.All` |
