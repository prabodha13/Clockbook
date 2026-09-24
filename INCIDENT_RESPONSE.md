# ClockBook incident response

This playbook is for security, data-integrity and availability incidents. Preserve evidence before destructive repair where possible.

## First 15 minutes

1. Assign one incident lead and record the UTC start time.
2. Identify the deployed build using `X-ClockBook-Version` and Railway deployment history.
3. If cross-tenant access, stolen sessions or compromised admin credentials are suspected, revoke sessions immediately:
   - tenant: `POST /api/admin/sessions/revoke-workspace`
   - platform-wide break glass: `POST /api/platform/sessions/revoke-all`
4. Rotate a leaked integration credential at the provider first, then update/disconnect ClockBook.
5. Preserve relevant request IDs, application logs, Railway deployment logs and audit events.
6. If database corruption is suspected, stop non-essential writes before attempting repair.

## First hour

### Suspected cross-tenant data access
- revoke affected sessions;
- identify tenant, actor, request IDs and object IDs;
- query the append-only audit ledger;
- run `/api/admin/diagnostics/invariants` in each affected tenant;
- preserve database/log snapshots before fixing data;
- determine whether access was read-only or included writes.

### Compromised admin account
- disable/reset the identity;
- revoke workspace or platform sessions depending on blast radius;
- rotate credentials that account could reach;
- review role, permission, integration and tenant changes in audit events.

### Leaked integration token
- revoke/rotate at Google/Karbon/Calamari/Slack first;
- disconnect or replace ClockBook configuration;
- check logs/CI history for historical exposure;
- do not paste replacement secrets into tickets or chat logs.

### Database failure
- verify Railway/PostgreSQL health independently of the application;
- do not run a restore over production as a first response;
- restore the latest known-good backup to a fresh environment and verify it before cutover.

### Failed deployment/migration
- stop further deployments;
- determine whether schema changes committed;
- use the rollback decision tree in `ROLLBACK.md`;
- prefer a forward fix when application rollback would be incompatible with the already-changed schema.

### Suspected timer corruption
- run invariant diagnostics;
- use detect -> report -> explicit repair;
- avoid direct database edits unless repair tooling cannot safely express the correction.

## Recovery

- verify `/health/live` and `/health/ready`;
- run the regression/invariant suite;
- verify representative staff login, timer, client/task, export and tenant-switch flows;
- compare critical row counts after any restore;
- re-enable integrations one at a time if they were isolated.

## Post-incident review

Record timeline, affected tenants/users, root cause, data exposure/loss, corrective actions, why existing controls did or did not catch it, and a regression test that prevents recurrence where practical.
