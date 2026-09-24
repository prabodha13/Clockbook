# ClockBook external monitoring plan

Source support is ready; external account configuration is still required before monitoring can be marked closed.

## Required monitors

- `GET /health/live`: process liveness.
- `GET /health/ready`: application + database readiness.

Recommended cadence: every 1-5 minutes. Alert after at least two consecutive failures to reduce transient noise.

## Alert conditions

- service unavailable;
- repeated readiness failure;
- sustained 5xx rate from Railway/log platform;
- database connectivity failures;
- startup/migration failures;
- repeated integration failures, but optional integration failures must not mark core ClockBook unavailable.

## Acceptance test

Monitoring is only Closed after a real test alert is received by an operator outside ClockBook. Record provider, monitor URL, alert destination, test date, time-to-alert and resolution evidence in `OPERATIONS_RUNBOOK.md`.
