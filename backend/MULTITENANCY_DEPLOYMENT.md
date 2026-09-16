# ClockBook multi-tenancy

## Existing Around Finance deployment

No tenant setup is required before deployment. On first startup after this version:

- ClockBook creates the `Around Finance` tenant using the stable id `tenant_around_finance`.
- Existing members, clients, tasks, pods, templates, settings, integrations and audit/time records are backfilled into that tenant.
- Existing member IDs, task IDs, client IDs and login credentials are preserved.
- Existing login identities are promoted into the new global `users` table and linked back to their Around Finance membership.
- Existing sessions are linked to Around Finance so a deployment does not intentionally force a logout.

The migration runs under the existing PostgreSQL startup advisory lock and is idempotent.

## Tenant isolation

Tenant-owned ORM models inherit `TenantScopedMixin`. After authentication resolves the session's tenant, SQLAlchemy automatically applies a tenant criterion to ORM SELECT/UPDATE/DELETE statements. A flush guard also rejects cross-tenant inserts, updates and deletes. Endpoint authorization still remains in place as a second layer.

Tenant-owned uniqueness is scoped by tenant. For example, client code `ABC123` may exist in two different tenants but cannot be duplicated inside one tenant.

## Authentication model

`User` is the global login identity. `Member` is the tenant membership/profile. A user can therefore belong to more than one tenant without duplicating login credentials.

Current Around Finance users continue to log in normally. Their default tenant is Around Finance.

Multi-workspace users are supported through:

- `GET /api/auth/workspaces`
- `POST /api/auth/switch-workspace/{tenant_id}`

ClockBook shows the active workspace in the top bar. If the signed-in user belongs to more than one workspace, that label becomes a workspace switcher. Switching is blocked while the user has a running timer, and a successful switch reloads the application with a new tenant-bound session so tenant-owned UI state cannot leak between workspaces.

## Platform administration

Creating a new tenant is deliberately separate from a tenant Super Admin. To enable platform-level tenant creation, set:

`CLOCKBOOK_PLATFORM_ADMIN_EMAILS=email1@example.com,email2@example.com`

Those identities can use:

- `GET /api/platform/tenants`
- `POST /api/platform/tenants`

They also see **Settings > Platform administration > Workspaces**, where a tenant can be created without manually calling the API. Creating a tenant seeds its standard internal client, default template, roles, task types and tracked metrics, gives the creating platform admin a Super Admin membership in the new tenant, and makes the new workspace available in the top-bar switcher.

If `CLOCKBOOK_PLATFORM_ADMIN_EMAILS` is not set, platform tenant creation is unavailable; normal Around Finance usage is unaffected.

## Integrations

Karbon, Calamari, inactivity settings and future tenant settings are stored per tenant. Existing Around Finance integration settings are migrated automatically. Environment-based Karbon and Slack fallback credentials are retained only for Around Finance so a future tenant cannot inherit Around Finance credentials accidentally. Google Calendar remains per member.

## Deployment verification

After deployment, verify:

1. Existing staff can log in using the same credentials.
2. Existing clients/tasks/pods/templates are present.
3. A running timer can start, pause and complete normally.
4. Existing Karbon/Calamari/Google connections behave as before.
5. `GET /api/auth/workspaces` reports Around Finance as the active tenant for an existing user.

For PostgreSQL the migration also removes the old global uniqueness constraints and replaces them with tenant-scoped indexes. Fresh SQLite databases use the multi-tenant schema directly; upgraded legacy SQLite databases are supported for the Around Finance migration, while PostgreSQL remains the production target for operating multiple tenants.

## Tenant user invitations

Tenant membership is managed separately from global login identity. Admins and Super Admins can create secure workspace invitations from the Staff page instead of choosing passwords for other people.

Invitation flow:

- The invitation is bound to the current tenant, email address and requested tenant role.
- Invitation links expire after 7 days.
- Only a SHA-256 hash of the invitation token is stored in the database; the raw token is shown only when the invitation is created or regenerated.
- Pending invitations can be regenerated or revoked from the Staff page.
- A regular Admin can invite Staff or Admin users. Only a Super Admin can invite another Super Admin.
- If the invited email already has a ClockBook login in another tenant, the person verifies that existing password and ClockBook creates only a new membership. Their existing password is not changed.
- If the email is new to ClockBook, the invitee chooses their own password while accepting the invitation.
- Accepting an invitation creates a tenant-bound session and opens the invited workspace immediately.
- An email address that already belongs to the current tenant cannot be invited again.

ClockBook currently generates a secure invitation link for the administrator to send through their normal communication channel. No outbound email provider is required for the invitation system itself.
