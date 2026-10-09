# REVIEW.md

I reviewed the existing `app/main.py` against the contract in `openapi.yaml`, the storage rules in `DATA_MODEL.md`, and the business rules R1-R10.

|| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
||---|---|---|---|---|
|| 1 | `build_punch_in_record()` (line 322) | Missing truncation to whole seconds on `punched_at` before storage and calculation (R1 requirement). | A punch-in with microseconds like 09:40:00.900 would store with microseconds instead of truncating to 09:40:00. | Added `truncate_to_seconds()` helper and applied it to `punched_at` in punch-in, punch-out, and regularization. |
|| 2 | `punch_out()` (line 367) | Missing truncation to whole seconds on `punched_at` (R1 requirement). | A punch-out with microseconds would store with microseconds. | Applied `truncate_to_seconds()` to `punched_at` before validation and storage. |
|| 3 | `regularize_attendance()` (line 439) | Missing truncation to whole seconds on `punch_in` and `punch_out` (R1 requirement). Also fetched employee multiple times unnecessarily. | Regularization with millisecond timestamps would store them without truncation. | Applied `truncate_to_seconds()` to punch times and cached employee lookup to avoid redundant queries. |
|| 4 | `department_trend()` (line 679) | Incorrect logic for `attendance_rate` on working days with headcount=0. The code had a redundant condition that could set rate to 0.0 incorrectly. | A working day with no employees (headcount=0) might return 0.0 instead of null for attendance_rate. | Simplified the logic: attendance_rate is null on weekends, null on working days with headcount=0, otherwise calculated. |
|| 5 | `explain_endpoint()` - department_summary (line 801) | The explain pipeline used a different structure than the actual endpoint (used nested lookup instead of the actual aggregation). | The explain output would not accurately represent the query plan used by the real endpoint. | Updated the explain pipeline to match the actual `department_summary()` endpoint's aggregation. |
|| 6 | `late_leaderboard()` (line 647) | Unused variable `match` that was built but never used in the pipeline. | Code clutter, potential for confusion during review. | Removed the unused `match` variable. |

Items already correct:
- Helper functions (`attendance_date_for_punch`, `compute_late_minutes`, `compute_overtime_minutes`, `compute_work_hours`, `compute_half_day`) correctly implement R1-R5.
- Pagination in `list_employees()` and `list_attendance()` is correct.
- Unique indexes for concurrency control are in place.
- All endpoints marked "You" in the problem statement are implemented.
- Analytics aggregations use proper MongoDB operations.
- History tracking in regularization correctly appends entries and guards against concurrent edits.
- Moving average calculation in department trend correctly uses a 7-day window within the requested range.
