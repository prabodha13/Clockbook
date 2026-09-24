# Historical secret exposure review

Current ClockBook code is designed to keep integration secrets and credentials server-side and to exclude them from audit payloads and structured request logging. That does not prove that older history never contained secrets.

## Required operational review

Before declaring historical secret exposure closed, review:

- Git history, including deleted files and old `.env` examples;
- Railway/current and superseded deployment variables;
- GitHub Actions/CI logs and artifacts;
- Railway application/deployment logs;
- old debug output/screenshots shared during development;
- exported database snapshots and local development copies.

Search for credential prefixes and names relevant to ClockBook, including Google client secrets/tokens, Slack tokens, Calamari keys, Karbon access keys, Resend keys, database URLs and `CLOCKBOOK_ENCRYPTION_KEY`.

## Response to uncertainty

If a credential may have appeared historically, rotate it rather than attempting to prove a negative from incomplete records. Rotation should include invalidating the old credential and confirming ClockBook still operates with the new one.

## Current source-tree check

A source-tree pattern scan should be performed before release, but source-tree cleanliness alone is **not** evidence that Git/deployment/log history is clean.

### Current working-tree result

On 17 September 2026, the delivered source tree was scanned for common literal secret/key patterns (including Resend, Slack, Google API/private-key and credential-bearing PostgreSQL URL forms). No matching literal credentials were found in the current source tree. This result does **not** cover Git history, Railway history, CI logs or previously shared screenshots/output.
