# REVIEW.md

Line numbers refer to the original starter `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` L36-37 | Grace check runs on floored minutes (`> 10`), so 10 min 1 s counts as 10 and isn't late | Shift 09:30, punch 09:40:01 → 0, expected 10 | Compare elapsed **seconds** `> 600`, then floor to minutes |
| 2 | `compute_late_minutes` L35 | `punch_in.replace(hour, minute)` keeps the punch's own tz, so a UTC datetime is compared to 09:30 UTC | 10:05 IST as an aware UTC datetime (what PyMongo returns) → 0, expected 35 | Build shift start as an IST datetime on the attendance date (`shift_window`) |
| 3 | `compute_late_minutes` | Overnight shifts ignored; shift start is assumed to be on the punch's own day | Shift 22:00, punch 00:15 next day → 0, expected 135 | Shift start comes from the attendance date (R1), not the punch's calendar day |
| 4 | `compute_work_hours` L41 | `round()` is float/banker's rounding, not half-up | 1 h 0 m 54 s = 1.015 → 1.01, expected 1.02 | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP` |
| 5 | `compute_overtime` L47 | No 30-minute minimum | Punch-out 18:35 on an 18:30 shift → 5, expected 0 | Return 0 when overtime `< 30` |
| 6 | `compute_overtime` L46 | Overnight shift end not moved to the next day | Sample EMP0005 (22:00-06:00, out 06:40) → 1480, expected 40 | `shift_window` adds a day to the end when `shift_end <= shift_start` |
| 7 | `compute_overtime` L46-47 | Subtracts an aware datetime from a naive one | Naive `punch_out` from PyMongo → `TypeError` → 500 | Client uses `tz_aware=True`; all datetimes are aware UTC |
| 8 | All helpers | No truncation to whole seconds (R1) | 09:40:00.900 must count as 09:40:00 | `from_ms` does `ms // 1000` before any calculation; truncated value is stored |
| 9 | L22 `MongoClient(...)` | Default naive datetimes and a 30 s server-selection timeout | Naive values break tz maths (#7); `/health` would hang 30 s on a dead DB | `tz_aware=True`, `serverSelectionTimeoutMS=5000` |
| 10 | `EmployeeIn` L53-60 | No validation at all | `emp_code="garbage"`, `email="nope"`, `shift_start="25:99"`, `joined_on="yesterday"` → 201 | Field patterns/lengths from the contract, real-date check, `shift_start != shift_end` |
| 11 | `PunchInIn.punched_at` L65 | `Optional[int]` accepts seconds, numeric strings, floats | `punched_at: 1783312500` (seconds) → 201 with date `1970-01-21` | `StrictInt` with bounds `1e11 .. 4102444800000` → 422 |
| 12 | `PunchInIn.status` L66 | Free `str` | `"BANANA"` or `"ABSENT"` at punch-in → 201 | `Literal["PRESENT", "WFH", "ON_DUTY"]` |
| 13 | `health` L72-74 | Never touches MongoDB | Returns `ok` with the database down | `ping`; 503 on failure (also retries index creation) |
| 14 | `create_employee` L79-80 | Check-then-insert, no unique index | Two parallel creates of the same code → both 201 | Unique index on `emp_code`; catch `DuplicateKeyError` → 409 |
| 15 | `create_employee` L82-85 | `datetime.now()` is naive local time (PyMongo stores it as if UTC) and is returned as an ISO string | On an IST machine `created_at` is 5.5 h off; response isn't epoch ms | Store `datetime.now(UTC)`, return epoch ms |
| 16 | `list_employees` L93 | `skip = page * page_size` | `page=1` skips the first page | `(page - 1) * page_size` |
| 17 | `list_employees` L94 | `total` ignores the department filter | `department=Eng` → total of all employees | `count_documents(q)` |
| 18 | `list_employees` L95 | No sort | Order follows insertion, not `emp_code` | `.sort("emp_code", 1)` |
| 19 | `list_employees`, `list_attendance` | `page`/`page_size` unvalidated | `page=0` → 200; `page_size=0` → no limit; negative page → 500 | `Query(ge=1)`, `page_size` 1-100 → 422 |
| 20 | `punch_in` L101, L113 | Unknown employee not handled | `emp_code="EMP9999"` → `TypeError` on `None` → 500 | `get_employee` → 404 |
| 21 | `punch_in` L102-103 | `fromtimestamp()` / `now()` use the server's local zone; date is that zone's date | Punch at 00:30 IST on 8 Jul on a UTC server → date `2026-07-07` | Instants in UTC; date from the IST wall clock (R1), overnight rule included |
| 22 | `punch_in` L104 | Check-then-insert race, no unique index | Two simultaneous punch-ins → both 201 | Unique `(emp_code, date)` index; `DuplicateKeyError` → 409 |
| 23 | `punch_in` L118-121 | Leaks MongoDB `_id` as `id`; `punch_in` returned as a datetime, not epoch ms | Response has `"id"` and an ISO string | `record_out` serializer: no `_id`, instants as epoch ms |
| 24 | `list_attendance` L144-147 | Loads every match into memory and paginates in Python | Slow / memory blow-up on 100k records | DB `sort` + `skip` + `limit`, `count_documents` for total, backed by `(date -1, emp_code 1)` |
| 25 | `list_attendance` L145 | No secondary sort | Same-date rows come back as EMP0003, EMP0001 | Sort `date` desc, `emp_code` asc |
| 26 | `list_attendance` L148-150 | Raw documents returned | `_id` leaked as `id`, datetimes not epoch ms, legacy records lack `half_day`/`history` | `record_out` fills defaults (`false`, `[]`, `0`) |
| 27 | `list_attendance` params | No validation of dates, status or range | `date_from > date_to`, `date_from=garbage`, `status=BANANA` → 200 | Date validator, `Status` literal, range check → 422 |
| 28 | Module level | No indexes created at all | Every query is a `COLLSCAN`; uniqueness not enforced | `ensure_indexes()` at startup, idempotent |

## Looked at and decided were not defects

- **`load_dotenv()`** doesn't override variables that are already set, so real env vars win as required.
- **`IST = timezone(timedelta(hours=5, minutes=30))`** is correct; IST has no DST.
- **Punch-in defaults** (`punch_out: None`, `work_hours: None`, `overtime_minutes: 0`, `half_day: False`, `history: []`) match the data model. I kept them and now compute them through one `derived_fields()` function.
- **`doc.pop("_id")` after `insert_one`** is fine. The fix was to stop exposing `id`, not this line.
- **String comparison for `date_from` / `date_to`** works because `YYYY-MM-DD` sorts lexicographically, and it uses the index. I kept it.
- **Reusing the helpers:** the signatures were the problem (no attendance date, no shift context), so I changed them to take the attendance date and the employee and kept the same names.