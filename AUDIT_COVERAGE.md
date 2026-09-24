# ClockBook mutation audit coverage

ClockBook's `audit_events` ledger is tenant-scoped and append-only at the application layer. The SQLAlchemy session hook records create/update/delete mutations for tenant-owned ORM records unless the model or field is explicitly excluded.

## Coverage review

| Mutation area | Coverage | Notes |
| --- | --- | --- |
| Clients | Covered | Create/update/delete recorded with changed non-sensitive fields. |
| Templates / template tasks | Covered | Configuration changes are recorded; historical task snapshots remain separate. |
| Members | Covered | Member profile, role, pod, capacity and permission field changes are recorded. |
| Roles / task types / tracked metrics | Covered | Create/update/delete recorded. |
| Pods | Covered | Create/update/delete recorded. |
| Permission changes | Covered | Stored member permission fields are audited through Member mutation events. |
| Tenant settings | Covered safely | The secret/raw `value` field is never logged. A safe `setting_value_changed` marker records whether configuration changed. |
| Integration configuration | Covered safely | Karbon/Calamari configuration is persisted through tenant settings. Configuration changes are visible without exposing access keys/API keys. |
| Integration connect/disconnect | Covered safely | Setting mutations plus structured operational logs show the configuration transition. Secrets remain excluded. |
| Task reassignment / submitted-time changes | Operationally logged by existing task workflows | Timer/task state-machine records are intentionally not placed under generic optimistic versioning. Where a destructive operation exists, the existing task/activity controls remain authoritative. A future audit review should keep task events concise rather than copy raw segments. |
| Capacity configuration | Covered | Member capacity and effective-from changes are Member mutations. |
| Workspace branding/settings | Covered safely | Tenant setting mutation is captured; logo payload itself is excluded. |
| Tenant invitations | Covered | Invitation record lifecycle is tenant-scoped; token hashes are excluded. |

## Deliberate exclusions

Never place these in audit event payloads:

- passwords or password hashes;
- access/refresh tokens;
- API keys or integration secrets;
- invitation/session tokens;
- raw timer segment arrays unless required for a specific incident;
- workspace logo image payloads;
- sensitive free-text notes/context by default.

## Append-only guarantee

Ordinary application code cannot update or delete an `AuditEvent`. The database owner can still alter records directly, so database administrative access must be independently restricted and logged by the hosting platform.

## Verification

Regression tests assert that:

- business mutations automatically create audit events;
- secret tenant-setting values are not copied into audit payloads;
- normal ORM mutation/deletion of audit records is rejected.
