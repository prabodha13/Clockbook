# ClockBook production hardening runbook

## Backup and restore proof

ClockBook now includes `ops/backup_postgres.sh` and `ops/restore_postgres.sh`. A backup is not considered proven until a restore drill has completed successfully.

Recommended monthly drill:

1. Create a fresh non-production PostgreSQL database.
2. Set `DATABASE_URL` to production and run `ops/backup_postgres.sh` from an authorised maintenance environment.
3. Set `RESTORE_DATABASE_URL` to the fresh non-production database and run `ops/restore_postgres.sh <backup.dump>`.
4. Start the current ClockBook release against the restored database.
5. Verify `/health/ready` returns 200.
6. Verify user, tenant, client, task, timer-segment, template and audit-event counts against the source database.
7. Run the automated regression suite.
8. Record drill date, backup timestamp, restore duration, row-count comparison and tester.

Do not run a destructive restore against production as a drill.

## External monitoring

The application exposes:

- `/health/live` for process liveness.
- `/health/ready` for application/database readiness and deployed version.

Configure an external uptime monitor against `/health/ready`, ideally every 1-5 minutes, alerting after at least two consecutive failures. Configure Railway/log monitoring for repeated `request_error`, readiness failures, startup failures and integration failures. Keep alert recipients outside the ClockBook application itself so an application outage cannot suppress the alert.

## Audit ledger

Authenticated business mutations now create tenant-scoped append-only `audit_events` records. Sensitive credential/token fields and bulky timer segment payloads are excluded. Super Admins can retrieve recent events from `/api/audit/changes`.

The audit ledger is application-level append-only: there is no edit/delete API for audit events. Database owners can still alter database rows directly, so production DB access must remain tightly controlled and independently logged by the hosting platform.

## Concurrent editing

Mutable business/configuration tables use SQLAlchemy optimistic versioning. If two requests load the same row and one commits first, the stale writer receives HTTP 409 rather than silently overwriting the first change. The user should refresh and retry.

## Request validation

Pydantic request models reject unknown fields. High-risk/common text fields now have explicit length limits, and request bodies are capped by `CLOCKBOOK_MAX_REQUEST_BYTES` (default 2 MiB). Increase this only when there is a documented feature need.

## Dependency scanning

`.github/workflows/security-regression.yml` runs regression tests plus dependency audits. For the Python audit to be authoritative, the production repository must contain a pinned `backend/requirements.txt` or `backend/pyproject.toml`. For Node, commit `frontend/package-lock.json` (or `frontend/npm-shrinkwrap.json`). The workflow intentionally fails when a Python dependency manifest is absent rather than pretending dependencies were scanned.

## Release safety

Before production deployment:

1. CI regression/security workflow must pass.
2. Review schema changes against a realistic database copy for high-risk migrations.
3. Confirm a recent backup exists.
4. Deploy one release.
5. Check `/health/ready`, startup logs and one normal login/timer workflow.
6. Keep the previous Railway deployment available for rollback until smoke checks pass.

## Restore drill record template

Copy this section for every real restore drill. Do not mark backup/recovery Closed until at least one successful production-like restore is recorded.

```text
Restore drill date:
Tester:
Source environment:
Source backup timestamp:
Backup artifact/checksum:
Target non-production PostgreSQL:
ClockBook version / Git SHA:

Restore started:
Restore completed:
Restore duration:

/health/live: PASS / FAIL
/health/ready: PASS / FAIL
Regression suite: PASS / FAIL
Representative records manually verified: PASS / FAIL

Critical row counts
- tenants/workspaces: source=    restored=
- users: source=    restored=
- members: source=    restored=
- clients: source=    restored=
- tasks: source=    restored=
- submitted tasks/entries: source=    restored=
- templates: source=    restored=
- template tasks: source=    restored=
- tenant settings/integration metadata: source=    restored=
- audit events: source=    restored=

Issues found:
Resolution:
Final result: PASS / FAIL
Follow-up owner/date:
```

For timer segments, ClockBook currently stores segments on task records rather than in a standalone timer-segment table; verify representative task segment arrays and submitted durations as part of the manual/automated checks.

## Incident response and emergency session revocation

See `INCIDENT_RESPONSE.md`. ClockBook now provides two break-glass operations:

- `POST /api/admin/sessions/revoke-workspace` — current tenant Super Admin, revokes every session in that workspace, including the caller.
- `POST /api/platform/sessions/revoke-all` — platform administrator, revokes every ClockBook session globally.

Both operations intentionally require re-authentication afterwards. Use global revocation only for a genuine platform-wide security event.

## Migration transition

See `MIGRATIONS.md`. Alembic is now scaffolded with a schema-neutral baseline marker so existing databases can be brought under versioned migration history after backup and verification. Existing startup compatibility migrations remain active during the transition; future schema changes should move to explicit Alembic revisions rather than expanding silent startup DDL indefinitely.

## Full tenant data portability

`ops/export_tenant.py` exports a workspace's non-secret business data to JSON for migration/retirement purposes. It deliberately excludes sessions, OAuth state, rate-limit/login-event operational records, password hashes, refresh tokens and integration credentials. Treat this as a portability export, not a substitute for PostgreSQL backup.
