# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique `emp_code` enforces business ID uniqueness. Compound `(department, emp_code)` backs filtered, sorted pagination. `joined_on` and `(department, joined_on)` accelerate headcount and trend filtering. Rejected `email`: no endpoints query by email.
   - `attendance_logs`: Unique compound `(emp_code, date)` guarantees one daily log and prevents duplicate punch-ins. `(date: -1, emp_code: 1)` optimizes `GET /attendance` sorting. `(emp_code: 1, punch_in: -1)` accelerates punch-out lookups. `(date: 1, late_minutes: 1)` optimizes leaderboard matching.

2. **Punch-in race.**
   Both requests validate payloads, derive the IST calendar date, and concurrently invoke `db.attendance_logs.insert_one(doc)`. WiredTiger's unique index on `(emp_code, date)` allows only one document insert to commit (`201 Created`). The colliding insert raises PyMongo's `DuplicateKeyError`, caught and returned as `409 Conflict`.

3. **Ties.**
   The aggregation pipeline uses `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4), and applies `{ rank: { $lte: limit } }` after ranking. Tied employees share the same rank; thus, ties at rank 2 with `limit=2` return all tied employees (e.g. 3 rows total).

4. **Headcount.**
   The aggregation drives from `employees` matching `joined_on <= period_end`, `$lookup`s `attendance_logs`, and uses `$unwind` with `preserveNullAndEmptyArrays: true`. Employees lacking logs are preserved, and headcount is counted via `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x data.**
   Pre-aggregate monthly attendance summaries asynchronously via change streams and shard `attendance_logs` on a hashed `emp_code` key to eliminate cross-collection `$lookup` overhead across millions of records.
