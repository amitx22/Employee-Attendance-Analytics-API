# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` enforces business-key uniqueness and backs foreign-key lookups. Compound index on `(department, joined_on)` accelerates department filtering and headcount queries without collection scans.
   - `attendance_logs`: Unique compound index on `(emp_code, date)` guarantees at most one record per employee per attendance day and prevents race conditions. Compound index on `(date, emp_code)` powers `GET /attendance` sorting and pagination. Compound index on `(date, late_minutes)` optimizes the leaderboard aggregation.
   - *Considered and rejected:* A standalone index on `status` was rejected due to low cardinality (only five values), making index scans inefficient compared to date-anchored compound indexes.

2. **Punch-in race.**
   Both requests compute the same attendance date (R1) and issue an `insert_one`. MongoDB's storage engine enforces the unique index on `(emp_code, date)`. The first request completes successfully (HTTP 201), while the concurrent insert raises a `DuplicateKeyError`. The application catches this exception and returns HTTP 409 Conflict, avoiding check-then-insert race conditions.

3. **Ties.**
   The leaderboard uses MongoDB's `$setWindowFields` with the `$rank` function to apply standard competition ranking (1, 2, 2, 4). Because `limit` filters ranks (`rank <= limit`) rather than row counts, all employees sharing a rank at or below the cutoff are returned. Tied rows are ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   The `department_summary` aggregation initiates from the `employees` collection rather than `attendance_logs`. It matches employees who joined on or before the month's end (`joined_on <= last_day`). Each employee contributes to department headcount, and their attendance records are joined via `$lookup`. Employees with zero logs simply have an empty logs array, preserving their headcount count.

5. **One thing you would change for 100x data.**
   At 100x data (~10 million attendance records), we would maintain pre-aggregated daily rollups via MongoDB change streams to eliminate real-time multi-stage aggregations, and shard `attendance_logs` on a compound shard key `(emp_code, date)`.
