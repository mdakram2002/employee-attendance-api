# Implementation decisions

1. **Indexes.** Startup creates a unique `employees.emp_code` index, `(department, emp_code)` for department-filtered employee lists, and `joined_on` for headcount selection. Attendance indexes are unique `(emp_code, date)` for one record per employee-day and the punch-in race, `(date descending, emp_code ascending)` for attendance listing, and `(status, date descending, emp_code ascending)` for status-filtered lists. Punch-out filters by employee and uses the natural-key index for date ordering; `punch_in` remains a residual condition. These match `ensure_indexes()`.

2. **Punch-in race.** Simultaneous inserts for the same employee and date contend on the unique `(emp_code, date)` index. One succeeds; duplicate-key errors return 409.

3. **Leaderboard ties.** MongoDB `$rank` uses total late minutes alone, so ties share a rank and the next rank is skipped. Filtering `rank <= limit` after ranking keeps all ties at the cutoff. Employee code orders tied rows deterministically.

4. **Department headcount.** The summary begins with employees joined by the period end, then looks up their logs. Employees without logs count; later joiners and departments with zero headcount are omitted.

5. **100x data.** I would measure aggregation plans and latency on a representative dataset. If live aggregation misses the response target, I would consider pre-aggregated reporting collections for trend and leaderboard queries.
