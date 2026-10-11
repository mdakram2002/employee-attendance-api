"""Focused contract tests for timestamp normalization, request schemas, and Mongo indexes."""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Isolate integration tests from the developer's configured database before importing the app.
TEST_DB = f"attendance_api_test_{uuid.uuid4().hex}"
os.environ["MONGO_DB"] = TEST_DB

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main

UTC = timezone.utc


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class TimestampAndSchemaTests(unittest.TestCase):
    def test_optional_timestamps_distinguish_omission_from_null(self):
        self.assertIsNone(main.PunchInIn(emp_code="EMP0001").punched_at)
        self.assertIsNone(main.PunchOutIn(emp_code="EMP0001").punched_at)
        self.assertIsNone(main.RegularizeIn(reason="valid reason", regularized_by="manager").punch_in)

        for model, payload in (
            (main.PunchInIn, {"emp_code": "EMP0001", "punched_at": None}),
            (main.PunchOutIn, {"emp_code": "EMP0001", "punched_at": None}),
            (main.RegularizeIn, {"status": None, "reason": "valid reason", "regularized_by": "manager"}),
            (main.RegularizeIn, {"punch_in": None, "reason": "valid reason", "regularized_by": "manager"}),
            (main.RegularizeIn, {"punch_out": None, "reason": "valid reason", "regularized_by": "manager"}),
        ):
            with self.subTest(model=model.__name__, payload=payload), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_datetime_conversion_discards_subseconds(self):
        value = datetime(2026, 10, 10, 4, 30, 12, 987000, tzinfo=UTC)
        self.assertEqual(main.ms_to_dt(epoch_ms(value)).microsecond, 0)
        self.assertEqual(main.as_utc(value).microsecond, 0)
        self.assertEqual(main.dt_to_ms(value) % 1000, 0)

    def test_generated_openapi_has_analytics_response_models_and_nonnullable_optional_inputs(self):
        with TestClient(main.app) as client:
            response = client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        routes = {
            "/analytics/employees/{emp_code}/monthly": ("EmployeeMonthlyResponse", {
                "emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
                "total_late_minutes", "total_overtime_minutes", "attendance_pct",
            }),
            "/analytics/departments/summary": ("DepartmentSummaryResponse", {"month", "items"}),
            "/analytics/leaderboard/late": ("LeaderboardResponse", {"month", "items"}),
            "/analytics/departments/{department}/trend": ("TrendResponse", {"department", "items"}),
        }
        for path, (model_name, fields) in routes.items():
            with self.subTest(path=path):
                response_schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
                self.assertEqual(response_schema["$ref"], f"#/components/schemas/{model_name}")
                model_schema = spec["components"]["schemas"][model_name]
                self.assertTrue(fields <= model_schema["properties"].keys())

        schemas = spec["components"]["schemas"]
        expected_item_fields = {
            "DepartmentSummaryItem": {"department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"},
            "LeaderboardItem": {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"},
            "TrendItem": {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"},
        }
        for model_name, fields in expected_item_fields.items():
            with self.subTest(model_name=model_name):
                self.assertTrue(fields <= schemas[model_name]["properties"].keys())
        for path in ("/analytics/employees/{emp_code}/monthly", "/analytics/departments/{department}/trend"):
            with self.subTest(error_response=path):
                self.assertIn("404", spec["paths"][path]["get"]["responses"])
        for path in routes:
            with self.subTest(validation_response=path):
                self.assertIn("422", spec["paths"][path]["get"]["responses"])

        regularize = spec["components"]["schemas"]["RegularizeIn"]
        for field in ("status", "punch_in", "punch_out"):
            self.assertNotIn("anyOf", regularize["properties"][field])
            self.assertNotIn(field, regularize.get("required", []))
        for model_name in ("PunchInIn", "PunchOutIn"):
            self.assertNotIn("anyOf", spec["components"]["schemas"][model_name]["properties"]["punched_at"])


class MongoBackedComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except Exception as exc:  # MongoDB is an integration-test prerequisite.
            raise unittest.SkipTest(f"MongoDB is unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        main.client.drop_database(TEST_DB)

    def setUp(self):
        main.client.drop_database(TEST_DB)
        main._indexes_ready = False

    def api(self):
        return TestClient(main.app)

    def test_startup_creates_required_indexes_idempotently_on_clean_database(self):
        with self.api() as client:
            employee_indexes = main.db.employees.index_information()
            attendance_indexes = main.db.attendance_logs.index_information()
            self.assertEqual(set(employee_indexes), {"_id_", "emp_code_1", "department_1_emp_code_1", "joined_on_1"})
            self.assertEqual(set(attendance_indexes), {
                "_id_", "emp_code_1_date_1", "date_-1_emp_code_1", "status_1_date_-1_emp_code_1",
            })
            self.assertTrue(employee_indexes["emp_code_1"]["unique"])
            self.assertTrue(attendance_indexes["emp_code_1_date_1"]["unique"])
            main.ensure_indexes()
            self.assertEqual(set(main.db.employees.index_information()), set(employee_indexes))
            self.assertEqual(set(main.db.attendance_logs.index_information()), set(attendance_indexes))

    def test_http_null_validation_and_omitted_timestamp_defaults(self):
        with self.api() as client:
            created = client.post("/employees", json={
                "emp_code": "EMP0001", "name": "Test", "email": "test@example.com",
                "department": "Engineering", "joined_on": "2026-10-10",
            })
            self.assertEqual(created.status_code, 201)

            for path in ("/attendance/punch-in", "/attendance/punch-out"):
                response = client.post(path, json={"emp_code": "EMP0001", "punched_at": None})
                self.assertEqual(response.status_code, 422, response.text)

            start = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
            end = start + timedelta(hours=9)
            with patch.object(main, "now_utc", side_effect=[start, end]):
                punch_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
                punch_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
            self.assertEqual(punch_in.status_code, 201, punch_in.text)
            self.assertEqual(punch_out.status_code, 200, punch_out.text)
            self.assertEqual(punch_in.json()["punch_in"] % 1000, 0)
            self.assertEqual(punch_out.json()["punch_out"] % 1000, 0)

    def test_analytics_success_responses_validate_for_empty_and_no_log_data(self):
        with self.api() as client:
            created = client.post("/employees", json={
                "emp_code": "EMP0001", "name": "Test", "email": "test@example.com",
                "department": "Engineering", "joined_on": "2026-10-05",
            })
            self.assertEqual(created.status_code, 201, created.text)

            monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-10"})
            self.assertEqual(monthly.status_code, 200, monthly.text)
            self.assertEqual(monthly.json()["working_days"], 20)
            self.assertEqual(monthly.json()["present_days"], 0)
            self.assertEqual(monthly.json()["attendance_pct"], 0)

            summary = client.get("/analytics/departments/summary", params={"month": "2026-10"})
            self.assertEqual(summary.status_code, 200, summary.text)
            self.assertEqual(summary.json()["items"], [{
                "department": "Engineering", "headcount": 1, "present_days": 0,
                "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0,
                "leave_count": 0, "on_duty_count": 0,
            }])

            leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2026-10"})
            self.assertEqual(leaderboard.status_code, 200, leaderboard.text)
            self.assertEqual(leaderboard.json()["items"], [])

            trend = client.get("/analytics/departments/Engineering/trend", params={
                "from": "2026-10-05", "to": "2026-10-06",
            })
            self.assertEqual(trend.status_code, 200, trend.text)
            self.assertEqual(len(trend.json()["items"]), 2)
            self.assertEqual([row["attendance_rate"] for row in trend.json()["items"]], [0, 0])

    def test_existing_subsecond_bson_instants_are_normalized_when_reused(self):
        start_with_ms = datetime(2026, 10, 5, 4, 15, 0, 900000, tzinfo=UTC)
        old_history_time = datetime(2026, 10, 5, 4, 16, 0, 900000, tzinfo=UTC)
        main.db.employees.insert_one({
            "emp_code": "EMP0001", "name": "Test", "email": "test@example.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": old_history_time,
        })
        main.db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-10-05", "status": "PRESENT",
            "punch_in": start_with_ms, "punch_out": None, "work_hours": None,
            "late_minutes": 15, "overtime_minutes": 0, "half_day": False,
            "history": [{
                "at": old_history_time, "by": "seed", "reason": "seed record",
                "changes": {"punch_in": {"from": start_with_ms, "to": start_with_ms}},
            }],
        })

        with self.api() as client:
            listed = client.get("/attendance", params={"emp_code": "EMP0001"})
            self.assertEqual(listed.status_code, 200, listed.text)
            listed_record = listed.json()["items"][0]
            self.assertEqual(listed_record["punch_in"] % 1000, 0)
            self.assertEqual(listed_record["history"][0]["at"] % 1000, 0)
            self.assertEqual(listed_record["history"][0]["changes"]["punch_in"]["from"] % 1000, 0)

            response = client.post("/attendance/punch-out", json={
                "emp_code": "EMP0001", "punched_at": epoch_ms(datetime(2026, 10, 5, 13, 0, 0, 900000, tzinfo=UTC)),
            })
            self.assertEqual(response.status_code, 200, response.text)
            record = response.json()
            self.assertEqual(record["punch_in"] % 1000, 0)
            self.assertEqual(record["punch_out"] % 1000, 0)
            self.assertEqual(record["work_hours"], 8.75)

        stored = main.db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-10-05"})
        self.assertEqual(stored["punch_in"].microsecond, 0)
        self.assertEqual(stored["punch_out"].microsecond, 0)
        # Existing audit history remains immutable in storage; serialization returns canonical seconds.
        self.assertEqual(stored["history"][0]["at"].microsecond, 900000)

    def test_created_at_is_stored_and_returned_at_whole_seconds(self):
        with self.api() as client, patch.object(main, "now_utc", return_value=datetime(2026, 10, 10, 4, 30, 12, 987000, tzinfo=UTC)):
            response = client.post("/employees", json={
                "emp_code": "EMP0001", "name": "Test", "email": "test@example.com",
                "department": "Engineering", "joined_on": "2026-10-10",
            })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["created_at"] % 1000, 0)
        stored = main.db.employees.find_one({"emp_code": "EMP0001"})
        self.assertEqual(stored["created_at"].microsecond, 0)


if __name__ == "__main__":
    unittest.main()
