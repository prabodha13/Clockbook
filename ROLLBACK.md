# ClockBook deployment rollback procedure

## Identify the running version

Use the `X-ClockBook-Version` response header and Railway deployment metadata. The application uses `CLOCKBOOK_VERSION` or `RAILWAY_GIT_COMMIT_SHA` when available.

## Application-only rollback

A Railway application rollback is normally safe when the failed release did not make a backwards-incompatible database change. After rollback:

1. confirm the previous deployment is serving traffic;
2. check `/health/live`;
3. check `/health/ready`;
4. verify one login and one timer flow;
5. verify tenant isolation with a representative workspace switch if applicable.

## When the database schema already changed

Do not blindly roll the application back. First determine whether the previous application version can operate against the new schema.

- Additive nullable columns/indexes are often backward-compatible.
- Dropped/renamed columns, changed constraints or transformed data may not be.
- If compatibility is uncertain, freeze the deployment and use a forward fix against the current schema.

Database rollback is only appropriate when a tested downgrade/restore plan exists and the loss window is understood.

## Verification after rollback/forward-fix

Run the regression suite, invariant diagnostics and representative record checks. Record the deployed SHA/version and the decision taken.
