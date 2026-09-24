# ClockBook performance baseline plan

Performance is not considered proven from source inspection alone. Establish a repeatable baseline against PostgreSQL with production-like indexes and representative data.

## Representative scale

Target at least:

- 100 staff;
- 10,000 clients;
- large active/historical task set;
- large timer-segment history;
- several years of submitted entries.

## Measure

Capture p50/p95 response time, database query count and payload size for:

- dashboard load;
- Insights load;
- exports;
- Super Admin reports;
- staff list;
- client search/list;
- Calendar-related queries when enabled.

## Investigate

- N+1 query patterns;
- sequential integration calls;
- missing indexes;
- unbounded result sets;
- oversized JSON payloads;
- repeated expensive calculations;
- endpoints that should paginate.

## Acceptance approach

Do not optimize from intuition. Save a dated baseline before changing a query, repeat the same dataset/test after the change, and retain the comparison. Production-like PostgreSQL results are authoritative; SQLite developer timings are not.
