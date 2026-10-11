"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded, but real environment variables win)

Layout of this file:
  1. configuration & database      5. serialisation (Mongo -> JSON)
  2. validation types              6. employees / attendance endpoints
  3. time & business-rule helpers  7. aggregation pipeline builders
  4. indexes                       8. analytics + explain endpoints
"""
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pydantic.json_schema import SkipJsonSchema
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration & database
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENT_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_PUNCH_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92
MAX_PATCH_ATTEMPTS = 5

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup, idempotent)
# No custom index names: MongoDB's default name is derived from the keys, so re-creating an index that
# already exists (even one made by an older version of this code) is a harmless no-op, not a name conflict.
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health reports 503 and retries index creation once Mongo answers.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Validation types
# --------------------------------------------------------------------------- #
def _check_date(value: str) -> str:
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date") from None
    return value


def _check_month(value: str) -> str:
    _check_date(value + "-01")
    return value


# [0-9] instead of \d: \d would also match non-ASCII digits.
DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_check_month)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict=True: "1783312500000", 1783312500000.0 and true are all rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageNo = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class ErrorResponse(BaseModel):
    detail: str


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: MonthStr
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: MonthStr
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: MonthStr
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: date_cls
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class TrendResponse(BaseModel):
    department: str
    items: list[TrendItem]


def invalid(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body, for rules a type annotation cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def reject_explicit_nulls(values: Any, fields: tuple[str, ...]) -> Any:
    """Keep omitted optional values valid while rejecting explicit JSON nulls."""
    if isinstance(values, dict):
        for field in fields:
            if field in values and values[field] is None:
                raise ValueError(f"{field} cannot be null; omit it to leave it unchanged")
    return values


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs | SkipJsonSchema[None] = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("punched_at",))


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("punched_at",))


class RegularizeIn(BaseModel):
    # Omitted values mean "unchanged"; the validator below rejects explicit nulls.
    status: Status | SkipJsonSchema[None] = None
    punch_in: EpochMs | SkipJsonSchema[None] = None
    punch_out: EpochMs | SkipJsonSchema[None] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, values: Any) -> Any:
        return reject_explicit_nulls(values, ("status", "punch_in", "punch_out"))


# --------------------------------------------------------------------------- #
# 3. Time & business-rule helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Normalize any datetime reused by the API to UTC whole-second precision (R1)."""
    normalized = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return normalized.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    """Integer arithmetic (no float timestamp()) so the result is exact."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(emp: dict) -> bool:
    return hhmm_to_minutes(emp["shift_end"]) <= hhmm_to_minutes(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the day before."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
    if is_overnight(emp) and seconds_into_day < hhmm_to_minutes(emp["shift_end"]) * 60:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_moment(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    """A shift boundary as an IST-aware datetime on the record's attendance date."""
    day = date_cls.fromisoformat(date_str) + timedelta(days=plus_days)
    minutes = hhmm_to_minutes(hhmm)
    return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, emp: dict, date_str: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((as_utc(punch_in) - shift_moment(date_str, emp["shift_start"])).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, emp: dict, date_str: str) -> int:
    """R3: whole minutes after shift end, counted only when >= 30. Overnight shifts end on the next day."""
    shift_end = shift_moment(date_str, emp["shift_end"], plus_days=1 if is_overnight(emp) else 0)
    seconds = int((as_utc(punch_out) - shift_end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded half-up to 2 decimals, in exact integer arithmetic."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return ((seconds * 100 + 1800) // 3600) / 100


def half_up(numerator: int, denominator: int, places: int) -> float:
    """R8: round numerator/denominator half-up to `places` decimals (non-negative values, exact)."""
    scale = 10 ** places
    return ((2 * numerator * scale + denominator) // (2 * denominator)) / scale


def derive_fields(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """Every stored field that depends on the punch times, in one place (punch-in, punch-out and PATCH all use it)."""
    fields = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
              "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENT_STATUSES and punch_in is not None:
        fields["late_minutes"] = compute_late_minutes(punch_in, emp, date_str)
        if punch_out is not None:
            fields["work_hours"] = compute_work_hours(punch_in, punch_out)
            fields["overtime_minutes"] = compute_overtime_minutes(punch_out, emp, date_str)
            fields["half_day"] = fields["work_hours"] < HALF_DAY_HOURS  # R5: on the rounded value
    return fields


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:])
    first = date_cls(year, mon, 1)
    nxt = date_cls(year + (mon == 12), mon % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """Mon-Fri days in [start, end]; a calendar computation (at most 31 iterations), not a data loop."""
    count, day = 0, start
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# 5. Serialisation (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def _json_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": _json_value(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    return {
        "at": _json_value(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"),
        "changes": {field: {"from": _json_value(c.get("from")), "to": _json_value(c.get("to"))}
                    for field, c in (entry.get("changes") or {}).items()},
    }


def serialize_record(doc: dict) -> dict:
    """Missing legacy fields (history, half_day, ...) get their documented defaults."""
    work_hours = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": _json_value(doc.get("punch_in")), "punch_out": _json_value(doc.get("punch_out")),
        "work_hours": None if work_hours is None else float(work_hours),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def paged(collection, query: dict, sort: list, page: int, page_size: int, serializer) -> dict:
    total = collection.count_documents(query)
    skip = (page - 1) * page_size
    items = []
    if skip < total:  # also keeps an absurd `page` from overflowing Mongo's skip
        items = [serializer(d) for d in collection.find(query, {"_id": 0}).sort(sort).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    return emp


# --------------------------------------------------------------------------- #
# 6. Employees & attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = as_utc(now_utc())
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageNo = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    return paged(db.employees, query, [("emp_code", 1)], page, page_size, serialize_employee)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(punched_at, emp)
    doc = {"emp_code": body.emp_code, "date": date_str, **derive_fields(emp, date_str, body.status, punched_at, None), "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Treat legacy BSON instants in the same second as their R1 whole-second value.
    # The exclusive upper bound includes any fractional BSON punch_in that truncates to punched_at.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code,
                                       "punch_in": {"$lt": punched_at + timedelta(seconds=1)}}, {"_id": 0},
                                      sort=[("date", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_at = as_utc(rec["punch_in"])
    if punched_at <= punch_in_at:
        raise HTTPException(422, "punched_at must be after punch_in")
    if punched_at - punch_in_at > MAX_PUNCH_SPAN:
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")

    new = derive_fields(emp, rec["date"], rec["status"], punch_in_at, punched_at)
    # Compare-and-set: only succeeds while the record is still open and unchanged since we read it.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "status": rec["status"],
         "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {k: new[k] for k in ("punch_in", "punch_out", "work_hours", "overtime_minutes", "half_day")}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out or changed concurrently")
    return serialize_record(updated)


def attendance_find_args(emp_code, date_from, date_to, status, page, page_size):
    """Filter, sort, skip, limit for GET /attendance. Shared with the explain endpoint so both stay identical."""
    if date_from and date_to and date_from > date_to:  # ISO strings compare chronologically
        raise invalid(("query", "date_from"), "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {**({"$gte": date_from} if date_from else {}), **({"$lte": date_to} if date_to else {})}
    if status is not None:
        query["status"] = status
    # With a single emp_code the secondary sort key is constant, so it is dropped to match the (emp_code, date) index.
    sort = [("date", -1)] if emp_code is not None else [("date", -1), ("emp_code", 1)]
    return query, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[DateStr] = None, date_to: Optional[DateStr] = None,
                    status: Optional[Status] = None, page: PageNo = 1, page_size: PageSize = 20):
    query, sort, _, _ = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
    return paged(db.attendance_logs, query, sort, page, page_size, serialize_record)


def plan_regularization(emp: dict, old: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate a correction and return (new stored fields, changes). Pure function: no database access."""
    status = body.status or old["status"]
    if status in NON_PRESENT_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid(("body", "status"), "ABSENT/LEAVE clears the punch times; do not supply punch_in/punch_out")
        punch_in = punch_out = None
    else:
        stored_in, stored_out = old.get("punch_in"), old.get("punch_out")
        punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else (as_utc(stored_in) if stored_in else None)
        punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else (as_utc(stored_out) if stored_out else None)
        if punch_in is None:
            raise invalid(("body", "punch_in"), "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in, emp) != old["date"]:
            raise invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if punch_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if punch_out <= punch_in or punch_out - punch_in > MAX_PUNCH_SPAN:
                raise invalid(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    new = derive_fields(emp, old["date"], status, punch_in, punch_out)
    before = {
        "status": old["status"],
        "punch_in": as_utc(old["punch_in"]) if old.get("punch_in") else None,
        "punch_out": as_utc(old["punch_out"]) if old.get("punch_out") else None,
        "work_hours": None if old.get("work_hours") is None else float(old["work_hours"]),
        "late_minutes": int(old.get("late_minutes") or 0),
        "overtime_minutes": int(old.get("overtime_minutes") or 0),
        "half_day": bool(old.get("half_day", False)),
    }
    changes = {f: {"from": before[f], "to": new[f]} for f in before if before[f] != new[f]}
    if not changes:
        raise invalid(("body",), "the request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee(emp_code)
    for _ in range(MAX_PATCH_ATTEMPTS):
        old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
        if old is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(emp, old, body)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history = old.get("history")
        # Optimistic concurrency: write only if the record is exactly as we read it (including the history
        # length), so two concurrent corrections can never overwrite each other's history entry.
        snapshot = {"emp_code": emp_code, "date": date, "status": old["status"],
                    "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
                    "history": {"$size": len(history)} if history is not None else {"$exists": False}}
        updated = db.attendance_logs.find_one_and_update(
            snapshot, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return serialize_record(updated)
        # someone else changed the record first: re-read and re-plan
    raise HTTPException(409, "record is being modified concurrently, retry")


# --------------------------------------------------------------------------- #
# 7. Aggregation pipeline builders (used by BOTH the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
# Expression fragments. "present units" are half-days: a full day = 2, a half day = 1, so sums stay exact integers.
def _present_units(prefix: str = "$", weekdays_only: bool = True) -> dict:
    is_present = {"$in": [f"{prefix}status", list(PRESENT_STATUSES)]}
    condition = {"$and": [is_present, _is_weekday(f"{prefix}date")]} if weekdays_only else is_present
    return {"$cond": [condition, {"$cond": [{"$ifNull": [f"{prefix}half_day", False]}, 1, 2]}, 0]}


def _is_weekday(date_field: str) -> dict:
    """Mon-Fri for a 'YYYY-MM-DD' string field. Parsed and evaluated in UTC so the calendar date cannot shift."""
    parsed = {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": parsed}, 5]}


def _late(prefix: str = "$") -> dict:
    return {"$ifNull": [f"{prefix}late_minutes", 0]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_units": {"$sum": _present_units()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _late()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so people with zero logs still count (R9); logs are joined in per employee."""
    first, last = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$isNumber": "$work_hours"}]}
    logs_of_employee = [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}},
        {"$group": {
            "_id": None,
            "present_units": {"$sum": _present_units()},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
            "late_minutes": {"$sum": _late()},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            # work_hours has 2 decimals: summing integer hundredths avoids float drift.
            "hours_hundredths": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "hours_records": {"$sum": {"$cond": [has_hours, 1, 0]}},
        }},
    ]
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": logs_of_employee, "as": "s"}},
        {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {"$literal": {}}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_units": {"$sum": "$s.present_units"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
            "hours_hundredths": {"$sum": "$s.hours_hundredths"},
            "hours_records": {"$sum": "$s.hours_records"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # before ranking: rank inside the department
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). It must see ONLY total_late_minutes in sortBy,
        # otherwise a tie-breaker field would split the ties.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, start: date_cls, end: date_cls) -> list:
    """Runs on `employees` (the department's people), joins their logs, then builds one row per calendar day."""
    first, last = start.isoformat(), end.isoformat()
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    logs_of_employee = [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}},
        {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
    ]
    calendar = {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "i", "in": {
        "$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}}}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": logs_of_employee, "as": "logs"}},
        {"$project": {"_id": 0, "joined_on": 1, "logs": 1}},
        # Two small summaries of the same input: counts per day, and employees per joining date.
        {"$facet": {
            "per_day": [{"$unwind": "$logs"},
                        {"$group": {"_id": "$logs.date",
                                    "units": {"$sum": _present_units("$logs.", weekdays_only=False)},
                                    "late": {"$sum": {"$cond": [{"$gt": [_late("$logs."), 0]}, 1, 0]}}}}],
            "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
        }},
        # Gap filling: generate every calendar day inside the database, whether or not it has records.
        {"$set": {"day": calendar}},
        {"$unwind": "$day"},
        {"$set": {
            "stat": {"$arrayElemAt": [{"$filter": {"input": "$per_day", "as": "p", "cond": {"$eq": ["$$p._id", "$day"]}}}, 0]},
            "headcount": {"$reduce": {"input": "$joins", "initialValue": 0,
                                      "in": {"$cond": [{"$lte": ["$$this._id", "$day"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]}}},
        }},
        {"$set": {"present_units": {"$ifNull": ["$stat.units", 0]}, "late_count": {"$ifNull": ["$stat.late", 0]},
                  "is_working_day": _is_weekday("$day")}},
        # rate in 1/10000 units, half-up, using integers only: floor(units*10000/(2*headcount) + 1/2)
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_units", 10000]}, "$headcount"]}, {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"avg_int": {"$avg": "$rate_int", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1, "present_units": 1,
                      "late_count": 1, "rate_int": 1, "avg_int": 1}},
    ]


def validate_trend_range(start: str, end: str) -> tuple[date_cls, date_cls]:
    d_from, d_to = date_cls.fromisoformat(start), date_cls.fromisoformat(end)
    if d_to < d_from:
        raise invalid(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise invalid(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


# --------------------------------------------------------------------------- #
# 8. Analytics & explain endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyResponse,
         responses={404: {"model": ErrorResponse, "description": "Unknown employee"}})
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    working_days = count_weekdays(max(date_cls.fromisoformat(first), date_cls.fromisoformat(emp["joined_on"])),
                                  date_cls.fromisoformat(last))
    row = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), {})
    units = int(row.get("present_units", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": units / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(units * 50, working_days, 2) if working_days else None,  # units/2/working*100
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryResponse)
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(summary_pipeline(month, department)):
        records = int(row.get("hours_records", 0))
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": int(row.get("present_units", 0)) / 2,
            "avg_work_hours": half_up(int(row["hours_hundredths"]), records * 100, 2) if records else None,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardResponse)
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]}


@app.get("/analytics/departments/{department}/trend", response_model=TrendResponse,
         responses={404: {"model": ErrorResponse, "description": "Unknown department"}})
def department_trend(department: str, from_: DateStr = Query(..., alias="from"), to: DateStr = Query(...)):
    start, end = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, start, end)):
        rate = None if r.get("rate_int") is None else int(r["rate_int"]) / 10000
        avg = None if r.get("avg_int") is None else math.floor(r["avg_int"] + 0.5) / 10000  # half-up
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
            "present_count": int(r["present_units"]) / 2, "late_count": int(r["late_count"]),
            "attendance_rate": rate, "moving_avg_7d": avg,
        })
    return {"department": department, "items": items}


def run_explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    # Explain output can hold BSON types (Int64, Timestamp, ...); convert to plain JSON.
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**params) -> None:
    for name, value in params.items():
        if value is None:
            raise invalid(("query", name), f"{name} is required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Optional[MonthStr] = None, department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None, date_to: Optional[DateStr] = None, status: Optional[Status] = None,
    from_: Optional[DateStr] = Query(None, alias="from"), to: Optional[DateStr] = None,
    page: PageNo = 1, page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        query, sort, skip, lim = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
        command = {"find": "attendance_logs", "filter": query, "sort": dict(sort), "skip": min(skip, 10**12), "limit": lim}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        assert emp_code is not None and month is not None
        command = {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, month), "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        _require(month=month)
        assert month is not None
        command = {"aggregate": "employees", "pipeline": summary_pipeline(month, department), "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        _require(month=month)
        assert month is not None
        command = {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}
        collection = "attendance_logs"
    else:  # department_trend
        _require(department=department, **{"from": from_, "to": to})
        assert department is not None and from_ is not None and to is not None
        start, end = validate_trend_range(from_, to)
        command = {"aggregate": "employees", "pipeline": trend_pipeline(department, start, end), "cursor": {}}
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
