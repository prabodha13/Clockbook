# ClockBook employee-data retention review

This document defines a starting policy for business approval; it does not silently delete production data.

| Data category | Purpose | Typical access | Proposed retention | Action |
|---|---|---|---|---|
| Submitted time / task history | Accounting, reporting, client support | Staff own data; scoped Admin; Super Admin | Align with business/accounting retention policy | Retain/archive |
| Timer segments | Reconstruct tracked duration and investigate disputes | Scoped application/admin | Same as submitted time where needed for reconstruction | Review annually |
| Inactivity / lock / sleep events | Explain automatic pause/recovery and operational anomalies | Restricted admin/audit roles | Shorter period, e.g. 90-180 days unless an investigation requires longer | Business/privacy approval required |
| Help/support records | Team support accounting/insights | Scoped users/admin | 12-24 months unless needed for longer reporting | Review |
| Audit events | Security and change investigation | Super Admin / operations | At least 12-24 months; longer if required by policy | Append-only then archive |
| Login / clock-start operational events | Operational audit/troubleshooting | Restricted admin | 90-180 days unless needed for investigation | Add scheduled cleanup after approval |

Before automatic deletion is enabled, confirm legal/employment/accounting requirements for every jurisdiction in which ClockBook is used. Highly granular activity data should not be retained indefinitely merely because storage is inexpensive.
