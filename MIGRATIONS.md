# ClockBook migration framework

ClockBook now contains an Alembic migration graph, but the current production schema is still created/upgraded by the existing backward-compatible startup migration code. This is a deliberate transition state rather than a pretend cut-over.

## Existing production database adoption

1. Take and verify a PostgreSQL backup.
2. Deploy the current hardening release and allow the existing startup migrations to complete.
3. Verify `/health/ready`, startup logs, and critical row counts.
4. From an authorised maintenance environment pointing at that exact database, run:
   `alembic stamp 0001_clockbook_baseline`
5. Confirm `alembic current` reports `0001_clockbook_baseline`.

Do **not** run `alembic upgrade head` on an empty database and expect revision 0001 to create the ClockBook schema. It is a baseline marker for already-existing ClockBook databases.

## Rule for future schema changes

After production is stamped, every new schema mutation should be a reviewed Alembic revision with:

- one clear revision ID and parent;
- upgrade order in source control;
- transactional DDL where PostgreSQL supports it;
- a documented rollback or forward-fix strategy;
- a migration test against a copy of a pre-change database;
- no new `ALTER TABLE` silently added to application startup.

The legacy startup migration functions should be retired gradually only after all supported live databases are stamped and the equivalent historical transitions are no longer needed for deployment compatibility.

## CI

CI validates that the Alembic graph has a single understandable history. A production migration should additionally be tested against a restored non-production copy of the prior production schema before deployment.
