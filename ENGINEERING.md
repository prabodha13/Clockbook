# ClockBook engineering overview

## Architecture

ClockBook is a React frontend backed by FastAPI, SQLAlchemy and PostgreSQL in production. The backend is authoritative for identity, authorization, tenant scope, timer state and business validation.

## Tenancy

`Tenant` is the workspace boundary. Global `User` identity is separated from tenant-specific `Member` membership. Tenant-owned models inherit `TenantScopedMixin`; SQLAlchemy automatically scopes ORM reads once authentication establishes `Session.info['tenant_id']`, and a flush guard blocks cross-tenant inserts/updates/deletes.

## Authorization

A membership has `member`, `admin` or `super_admin` authority inside one tenant. Object and pod checks are performed server-side. Frontend view simulation changes presentation only and must never be treated as authority.

## Timer state machine

Normal states are `todo -> running -> paused -> running -> submitted`, with reset returning active work to `todo`. Start is server-authoritative and serialized per owner in PostgreSQL. A partial unique index enforces at most one running timer per owner. Pause, reset and submit are idempotent for retries. Segments are the durable duration source; submitted work may carry an explicit adjusted duration.

## Task/template lifecycle

Template-created tasks snapshot relevant template metadata at creation. Later template edits do not rewrite historical task metadata. Period/metric requirements are validated server-side.

## Integrations

Google Calendar is per member. Karbon and Calamari are tenant-specific optional integrations. Core time tracking must continue when optional integrations are disconnected/unavailable. Secrets stay server-side and sensitive integration values are excluded from audit logs.

## Audit architecture

Authenticated tenant-scoped business mutations emit `AuditEvent` rows. Audit events are application-level append-only and sensitive fields are excluded. Database administrators remain capable of direct database modification, so production DB access must be separately controlled/audited.

## Operational controls

- `/health/live`, `/health/ready`
- structured request IDs/logging
- login rate limiting
- backup/restore scripts and drill template
- invariant diagnostics
- emergency workspace/global session revocation
- CI regression/security workflow
- `X-ClockBook-Version`

## Migration transition

The existing startup migration system remains for backward compatibility. Alembic is scaffolded as the future versioned migration source of truth; see `MIGRATIONS.md` before adopting it on production.

## Testing

`tests/test_hardening.py`, `tests/test_invariants.py` and `tests/test_properties.py` cover high-risk authorization, tenancy, concurrency, idempotency, duration, export, template, capacity, integration and audit invariants. External restore, monitoring and browser tests remain operational activities.
