# ClockBook final hardening review — current build

Date: 2026-09-24

This review applies to the current ClockBook application after the final UI/feature work was merged forward into the production-hardening branch. The older hardening snapshot was treated as a source of security/operational controls only; current ClockBook application behavior was not rolled back to the older snapshot.

## Automated verification completed

- Python syntax compilation passed for `main.py`, `models.py`, `schemas.py`, and `database.py`.
- Local automated suite: **29 passed, 1 skipped**.
- The single skipped test is the Hypothesis property suite because Hypothesis is not installed in this isolated runtime. `requirements-ci.txt` pins Hypothesis, so CI is configured to run it where dependencies can be installed.
- The focused hardening/migration/invariant/current-feature suites all passed: **29 passed**.
- A high-confidence source scan found no literal OpenAI-style keys, AWS access keys, or private-key blocks.
- Current application preservation check found no missing top-level Python functions/classes relative to the latest pre-hardening build.
- Current feature markers were verified in the final frontend/API, including the redesigned login, Helping/Training, Manual Overrides, net login-to-shutdown reporting, Add & Start, Karbon Team auto-refresh, and presence heartbeat.

## Current-feature regression coverage added

`tests/test_current_features_regression.py` protects the newer behavior that did not exist in the original hardening snapshot:

- Helping/Training requires another staff member and stores the relationship.
- Presence heartbeat creates/updates the current daily presence record without depending on a timer.
- Task deletion removes task-derived clock/help records while preserving and detaching inactivity history.
- Activity reporting exposes gross login-to-shutdown, inactivity, and net login-to-shutdown fields.

## Hardening controls carried forward

- Tenant-scoped ORM reads and cross-tenant write protection.
- Optimistic concurrency/version checks for mutable admin configuration.
- Append-only application audit ledger with secret-value suppression.
- Request body size enforcement.
- Strict request schemas and bounded high-risk inputs.
- Session-revocation endpoints for workspace and platform emergencies.
- Integration configuration revisions to prevent stale credential/config updates.
- Read-only invariant diagnostics.
- Serialized PostgreSQL startup migrations during rolling deploys.
- Backward-safe startup migration handling for versioned and legacy schemas.
- Alembic baseline/migration documentation, operations scripts, rollback guidance, incident response, monitoring, retention, security-history, accessibility/browser, and performance checklists.
- CI security/regression workflow and dependency-update configuration.

## Latest application behavior deliberately preserved

Hardening was merged into the latest application instead of replacing it with the older hardened application files. In particular, the final package retains:

- Daily presence heartbeat and shutdown inference.
- Gross/inactivity/net login-to-shutdown reporting.
- Super-admin-only Karbon net span comparison.
- Karbon Team view and automatic comparison refresh.
- Add & Start.
- Helping/Training built-in non-billable task type and helped/trained-person requirement.
- Manual Overrides reporting and filters.
- Robust task-delete cleanup.
- Current login, Dashboard, Settings, and searchable-person UI refinements.

## Important operational verification still required before calling a production deployment fully proven

No source-code review can honestly prove external production infrastructure from an isolated development runtime. The following items remain deployment/operator checks and are documented in the included runbooks/checklists:

1. Perform a real PostgreSQL backup and restore drill against the production/staging infrastructure.
2. Verify external uptime/error monitoring and alert delivery end to end.
3. Run the browser/accessibility checklist on the supported production browsers.
4. Run representative production/staging performance/load checks.
5. Run the historical repository/secret-history review against the real source-control history.
6. Run dependency vulnerability auditing in CI/production build infrastructure (`pip-audit`) where dependencies and network access are available.
7. Run the complete CI suite with `requirements-ci.txt`, including the Hypothesis property suite.

## Known non-blocking technical debt

The test run reports Python deprecation warnings for naive `datetime.utcnow()` usage. These are not current functional test failures, but migrating to timezone-aware UTC datetimes should be scheduled separately rather than mixed into this hardening release.

## Release assessment

The application-level hardening merge is complete and the automated checks available in this environment pass. Production release should still be gated by the operational checks above, especially backup/restore and external monitoring verification.
