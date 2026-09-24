# ClockBook external input validation review

## Global controls

- Pydantic request models inherit strict `extra="forbid"` behavior, so unexpected fields are rejected.
- Request bodies are capped by `CLOCKBOOK_MAX_REQUEST_BYTES`, default 2 MiB, using the actual body size rather than trusting only `Content-Length`.
- High-risk/common string fields now have bounded lengths.
- Large collection payloads such as client imports and template reorder requests have list limits.
- Numeric business fields use ranges where a meaningful bound exists.
- Enum-like request values use constrained/Literal values for roles, notification channel, help direction, inactivity kind and similar inputs.
- IDs exposed in request schemas have practical maximum lengths.

## Areas with endpoint-specific validation

Timer timestamps and recovery timestamps continue to receive additional server-side semantic validation because syntactically valid ISO strings alone are not sufficient for timer integrity.

Template-created task metadata continues to be derived/validated by the server rather than trusting the browser.

## Tests

Regression tests cover unexpected fields, field-length/range rejection and the global body limit behavior. New public endpoints should add negative tests when they introduce nested or high-volume payloads.

## Residual risk

Validation is a continuing boundary, not a one-time closure. When a new request schema is added, it should explicitly answer:

1. Which fields are optional?
2. What is the maximum string/list size?
3. What numeric/date range is valid?
4. Is the value an enum rather than arbitrary text?
5. Is additional semantic validation required after schema parsing?
