# DECISIONS.md

1. **Indexes.**
   - `attendance_logs (emp_code, date)` is unique. It keeps one record per day, guards punch-in, and serves the monthly summary and both `$lookup`s.
   - `(date -1, emp_code 1)` matches the list sort and the leaderboard's month scan.
   - `(emp_code, punch_in -1)` lets punch-out find the latest punch.
   - On employees: `emp_code` unique, `(department, joined_on)`, and `joined_on` for the summary without a department.
   - I rejected an index on `status`. It only has five values and every query also filters on date.

2. **Punch-in race.** Both requests validate, compute the same date and call `insert_one`. There's no find-first step. MongoDB's unique index accepts the first insert and raises `DuplicateKeyError` on the second, which I return as a 409.

3. **Ties.** `$setWindowFields` with `$rank` gives 1, 1, 3. I filter on `rank <= limit` instead of slicing the rows, so both tied employees come back even if that's more rows than `limit`. Rows are ordered by minutes, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`), not from the logs. `$lookup` pulls each person's month totals, and `$unwind` with `preserveNullAndEmptyArrays` keeps people with no logs.

5. **100x data.** I'd keep a daily per-department rollup updated on every write, so analytics read a few hundred rows instead of scanning raw logs.