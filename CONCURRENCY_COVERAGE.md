# ClockBook concurrency coverage

ClockBook intentionally uses two different concurrency mechanisms depending on the kind of record.

## Transactional/state-machine records

Timers and task state transitions use database transactions, row/advisory locking and the database-level one-running-timer invariant. They are deliberately **not** given generic optimistic version fields because stale-version checks can interfere with heartbeat/start/pause/complete semantics.

Covered high-risk behavior includes:

- one running timer per user at database level;
- concurrent Start requests cannot leave two running tasks;
- Pause/Reset/Complete are idempotent where appropriate;
- server-authoritative normal timer timestamps;
- duplicate Quick Meeting creation protection.

## Shared administrative configuration

Optimistic version locking is used where stale edits could silently overwrite another administrator's work. Current candidates covered include:

- tenants/workspaces;
- tenant settings;
- pods;
- members, including role/pod/capacity/permission configuration;
- tenant invitations;
- clients and bank accounts;
- roles;
- task-type options;
- tracked metrics;
- templates/template tasks;
- Karbon/Calamari configuration through a tenant-specific integration revision.

The frontend sends the version it loaded. A stale save receives HTTP 409 and must refresh before retrying.

## Deliberately not version-locked

- TaskInstance timer/state-machine rows;
- Session/OAuth/rate-limit ephemeral records;
- append-only AuditEvent rows.

## Residual risk

A stale-write review should be repeated whenever a new administrator-managed configuration record is introduced. The rule is: use optimistic locking for shared human-edited configuration, and transactional locking/idempotency for state machines.
