# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` guarantees business identifier uniqueness and idempotency. Compound index on `(department, emp_code)` backs filtered, sorted pagination in `GET /employees`. Indexes on `joined_on` and `(department, joined_on)` accelerate period headcount filtering in department summary and trend. Considered and rejected: index on `email`, as no endpoint queries by email.
   - `attendance_logs`: Unique compound index on `(emp_code, date)` prevents duplicate daily logs under concurrent punch-ins. Compound index `(date: -1, emp_code: 1)` serves `GET /attendance` sorting without in-memory sort stages. `(emp_code: 1, punch_in: -1)` backs `POST /attendance/punch-out` lookups. Compound `(date: 1, late_minutes: 1)` optimizes leaderboard matching.

2. **Punch-in race.**
   Both concurrent requests validate payloads and derive the IST calendar date. Both execute `insert_one`. MongoDB's unique compound index `(emp_code, date)` serializes insertion at the WiredTiger storage engine level: exactly one write commits (201 Created), while the conflicting write fails with `DuplicateKeyError`, caught and translated to HTTP 409 Conflict.

3. **Ties.**
   The aggregation pipeline uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). Filtering `{ rank: { $lte: limit } }` occurs strictly after ranking. If two employees tie on late minutes at rank 2 with `limit=2`, both are returned (returning 3 rows total), preserving fairness and specification requirements.

4. **Headcount.**
   The pipeline queries `employees` as the driving collection, matching `joined_on <= period_end`. It performs a `$lookup` to `attendance_logs` and `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with no logs are retained, and headcount is measured via `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x data.**
   Pre-aggregate attendance rollups asynchronously (e.g. daily/monthly summary buckets or incremental MongoDB materialized views via change streams) and shard `attendance_logs` on a hashed `emp_code` partition key to avoid cross-collection lookup overhead across millions of documents.
