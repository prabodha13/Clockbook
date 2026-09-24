# External error monitoring preparation

ClockBook already emits structured request logs with request IDs and exposes health endpoints. External exception aggregation/alerting still requires a real provider/account configuration before this area can be marked Closed.

## Provider requirements

The selected service should:

- capture unhandled FastAPI/backend exceptions;
- capture important React/frontend failures;
- attach or search by ClockBook request ID where practical;
- redact request bodies, authorization headers, passwords, tokens and integration secrets;
- minimize/pseudonymize user and tenant identifiers where practical;
- alert on new/high-frequency production exceptions.

Typical implementation choices include Sentry or an equivalent Railway-compatible error-monitoring service. Do not add a production SDK until the actual provider, data-processing expectations and environment variables are agreed.

## Acceptance test

1. Connect the production/staging app to the provider.
2. Trigger a controlled non-sensitive test exception in non-production.
3. Confirm it appears with the ClockBook release/request identifier.
4. Confirm no secret/request credential data is present.
5. Confirm the intended alert recipient receives the test alert.

Until this acceptance test is completed, status remains Operational action required.
