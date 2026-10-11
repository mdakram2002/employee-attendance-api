# Current compliance review

I compared the current implementation with `PROBLEM_STATEMENT.docx`, `openapi.yaml`, and `DATA_MODEL.md`. The rows below record the confirmed gaps corrected in this revision.

| # | Where | What was wrong | How it was detected | Fix |
|---|---|---|---|---|
| 1 | `app/main.py`: `create_employee`, `as_utc`, `dt_to_ms`, `punch_out` | `created_at` retained milliseconds. Reused BSON punch times could also retain sub-second precision in calculations and responses, and a sub-second punch-in could be excluded by the punch-out lookup at an exact second boundary. | Check that API epoch values and newly stored timestamps are divisible by 1,000; seed a BSON datetime with non-zero milliseconds and exercise GET and punch-out. | Normalize datetimes to UTC whole seconds, create `created_at` with `now_utc`, normalize punch-in when punch-out updates a record, and use an exclusive next-second lookup bound for legacy fractional values. Existing history entries remain immutable in storage; serialization normalizes their instants. |
| 2 | `PunchInIn`, `PunchOutIn`, `RegularizeIn` | Optional fields treated explicit JSON `null` like omission, and the generated schema advertised those fields as nullable. | Validate omitted and explicit-null bodies separately; inspect `app.openapi()`. | Reject supplied nulls with before validators, keep omitted fields optional, and use `SkipJsonSchema[None]` so nullability is not advertised. |
| 3 | Four analytics route decorators | Runtime OpenAPI had empty success schemas for analytics responses. | Inspect each route's generated `/openapi.json` `200` schema. | Add response models for monthly, department summary, leaderboard, and trend responses, plus documented 404 bodies where applicable. |
| 4 | `DECISIONS.md` versus `ensure_indexes()` | The decisions file claimed an `(emp_code, punch_in, punch_out)` index that startup did not create. | Compare the documented index list with `ensure_indexes()` on a clean database. | Document the actual indexes and their query roles. The unique `(emp_code, date)` index narrows the punch-out employee lookup and supports its date ordering; no undocumented index is claimed. |

## Checked and retained

- IST attendance dates, overnight date assignment, late/overtime rules, rounded work hours, half-day calculation, weekday presence, and report rounding are implemented in the time helpers and analytics pipelines.
- Punch-in uses the unique `(emp_code, date)` key. Punch-out uses a conditional update. Regularization uses an atomic history-length condition and append.
- Summary starts from employees so employees without logs contribute to headcount. Leaderboard uses `$rank`, applies the cutoff after ranking, and sorts ties by employee code. Trend gap filling and its moving average run inside MongoDB aggregation.
- Explain builders reuse the corresponding endpoint filters and pipeline builders and request `executionStats`.

## Latest verification

- `moving_avg_7d` was reviewed against `openapi.yaml`. No defect was found and no code change was required: the calendar rows use the current day and up to six preceding rows in range, MongoDB `$avg` ignores null rates, and half-day attendance contributes `0.5` to `present_count`.
- `python -m pytest tests -q -p no:cacheprovider` passed: 8 tests and 18 subtests. `git diff --check` passed.
- `python -m pytest -q` encountered a pytest-cache permission error during collection at the repository root. This was a test discovery/cache access issue, not an application test failure; the explicit test-suite run above passed.
- The confirmed low-severity documentation issue that `DECISIONS.md` exceeded the requested word count has been resolved; it now contains 199 whitespace-separated words.

## Pending verification

Concurrency under simultaneous requests and explain-plan verification on a clean 100,000-record MongoDB 6.0 dataset still require dedicated integration testing. These behaviors have not been verified.
