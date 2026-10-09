# uvicorn app.main:app --port 8000
import os
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(s: str) -> date_cls:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_time(s: str) -> datetime.time:
    return datetime.strptime(s, "%H:%M").time()


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def from_epoch_ms(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def decimal_round(value: float | int, digits: int) -> float:
    q = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def month_start_end(month: str) -> tuple[str, str]:
    year, month_no = month.split("-")
    year_int = int(year)
    month_int = int(month_no)
    start = date_cls(year_int, month_int, 1)
    if month_int == 12:
        end = date_cls(year_int + 1, 1, 1) - timedelta(days=1)
    else:
        end = date_cls(year_int, month_int + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def timezone_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    local_dt = timezone_ist(punch_in)
    shift_end_time = parse_time(shift_end)
    if shift_end_time <= parse_time(shift_start):
        if local_dt.time() < shift_end_time:
            return (local_dt - timedelta(days=1)).date().isoformat()
    return local_dt.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    local_dt = timezone_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    start_local = local_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (local_dt - start_local).total_seconds()
    if diff_seconds <= 600:
        return 0
    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    hours = (punch_out - punch_in).total_seconds() / 3600
    return decimal_round(hours, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, shift_start: str, record_date: str) -> int:
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    local_out = timezone_ist(punch_out)
    end_date = parse_date(record_date)
    end_h, end_m = map(int, shift_end.split(":"))
    if shift_end <= shift_start:
        end_date = end_date + timedelta(days=1)
    end_dt = datetime.combine(end_date, datetime.strptime(f"{end_h:02d}:{end_m:02d}", "%H:%M").time(), tzinfo=IST)
    diff = (local_out - end_dt).total_seconds() / 60
    if diff < 0:
        return 0
    minutes = int(diff)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float | None) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.5


def is_present_status(status: str | None) -> bool:
    return status in PRESENT_STATUSES


def date_is_weekday(date_str: str) -> bool:
    return parse_date(date_str).weekday() < 5


def employee_working_days(joined_on: str, month: str) -> int:
    month_start, month_end = month_start_end(month)
    start = max(parse_date(joined_on), parse_date(month_start))
    end = parse_date(month_end)
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    record = {key: value for key, value in doc.items() if key != "_id"}
    if "created_at" in record and record["created_at"] is not None:
        record["created_at"] = to_epoch_ms(record["created_at"])
    return record


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    record = {key: value for key, value in doc.items() if key != "_id"}
    record["punch_in"] = to_epoch_ms(record.get("punch_in"))
    record["punch_out"] = to_epoch_ms(record.get("punch_out"))
    if record.get("work_hours") is not None:
        record["work_hours"] = float(record["work_hours"])
    record["late_minutes"] = int(record.get("late_minutes", 0) or 0)
    record["overtime_minutes"] = int(record.get("overtime_minutes", 0) or 0)
    record["half_day"] = bool(record.get("half_day", False))
    record["history"] = serialize_history(record.get("history") or [])
    return record


def serialize_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        new_item = {
            "at": to_epoch_ms(item.get("at")),
            "by": item.get("by"),
            "reason": item.get("reason"),
            "changes": {},
        }
        for field, change in (item.get("changes") or {}).items():
            new_field = {}
            if "from" in change:
                if field in {"punch_in", "punch_out"}:
                    new_field["from"] = to_epoch_ms(change.get("from"))
                else:
                    new_field["from"] = change.get("from")
            if "to" in change:
                if field in {"punch_in", "punch_out"}:
                    new_field["to"] = to_epoch_ms(change.get("to"))
                else:
                    new_field["to"] = change.get("to")
            new_item["changes"][field] = new_field
        out.append(new_item)
    return out


def iso_weekday(date_value: str) -> int:
    return parse_date(date_value).weekday() + 1


def normalize_record(doc: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(doc)
    if "history" not in normalized:
        normalized["history"] = []
    if "half_day" not in normalized:
        normalized["half_day"] = False
    normalized["late_minutes"] = int(normalized.get("late_minutes", 0) or 0)
    normalized["overtime_minutes"] = int(normalized.get("overtime_minutes", 0) or 0)
    if "status" not in normalized:
        normalized["status"] = "PRESENT"
    if normalized.get("punch_in") is not None and not isinstance(normalized["punch_in"], datetime):
        normalized["punch_in"] = from_epoch_ms(int(normalized["punch_in"]))
    if normalized.get("punch_out") is not None and not isinstance(normalized["punch_out"], datetime):
        normalized["punch_out"] = from_epoch_ms(int(normalized["punch_out"]))
    return normalized


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_start")
    @classmethod
    def validate_shift_start(cls, value: str) -> str:
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_end(cls, value: str) -> str:
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%d")
        return value

    @field_validator("shift_end")
    @classmethod
    def check_shift_diff(cls, value: str, info):
        if info.data.get("shift_start") and value == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY for punch-in")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise ValueError("status must be one of the allowed attendance states")
        return value


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")


@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1), ("punch_out", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    candidate = body.model_dump()
    candidate["created_at"] = datetime.now(timezone.utc)
    try:
        result = db.employees.insert_one(candidate)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    inserted = db.employees.find_one({"_id": result.inserted_id})
    return serialize_employee(inserted)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = []
    for doc in db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size):
        items.append(serialize_employee(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def build_punch_in_record(emp_code: str, punched_at: datetime, status: str) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    punched_at = truncate_to_seconds(punched_at)
    local_punch = timezone_ist(punched_at)
    shift_end = emp["shift_end"]
    shift_start = emp["shift_start"]
    record_date = attendance_date_for_punch(punched_at, shift_start, shift_end)
    late = compute_late_minutes(punched_at, shift_start)
    doc = {
        "emp_code": emp_code,
        "date": record_date,
        "status": status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    return doc


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    doc = build_punch_in_record(body.emp_code, ts, body.status)
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    saved = db.attendance_logs.find_one({"_id": result.inserted_id})
    return serialize_attendance(saved)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    punched_at = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punched_at = truncate_to_seconds(punched_at)

    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_dt = existing["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if punched_at - punch_in_dt > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    status = existing.get("status")
    if status not in PRESENT_STATUSES:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    updated = db.attendance_logs.find_one_and_update(
        {"_id": existing["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": compute_work_hours(punch_in_dt, punched_at),
                "overtime_minutes": compute_overtime_minutes(punched_at, emp["shift_end"], emp["shift_start"], existing["date"]),
                "half_day": compute_half_day(compute_work_hours(punch_in_dt, punched_at)),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="that record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and parse_date(date_from) > parse_date(date_to):
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_attendance(normalize_record(doc)) for doc in cursor]
    total = db.attendance_logs.count_documents(q)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parse_date(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid date") from exc

    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if existing is None:
        if not db.employees.find_one({"emp_code": emp_code}):
            raise HTTPException(status_code=404, detail="unknown employee")
        raise HTTPException(status_code=404, detail="attendance record not found")

    emp = db.employees.find_one({"emp_code": emp_code})
    original = normalize_record(dict(existing))
    original_history = list(original.get("history") or [])
    proposed_status = body.status if body.status is not None else original["status"]
    proposed_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else original.get("punch_in")
    proposed_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else original.get("punch_out")

    if proposed_punch_in is not None:
        proposed_punch_in = truncate_to_seconds(proposed_punch_in)
    if proposed_punch_out is not None:
        proposed_punch_out = truncate_to_seconds(proposed_punch_out)

    if proposed_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot have punch times")
        proposed_punch_in = None
        proposed_punch_out = None
    elif proposed_status in PRESENT_STATUSES:
        if proposed_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if proposed_punch_in is not None:
            proposed_date = attendance_date_for_punch(proposed_punch_in, emp["shift_start"], emp["shift_end"])
            if proposed_date != date:
                raise HTTPException(status_code=422, detail="punch_in must remain on the attendance date")

    if proposed_punch_out is not None:
        if proposed_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if proposed_punch_in.tzinfo is None:
            proposed_punch_in = proposed_punch_in.replace(tzinfo=timezone.utc)
        if proposed_punch_out.tzinfo is None:
            proposed_punch_out = proposed_punch_out.replace(tzinfo=timezone.utc)
        if proposed_punch_out <= proposed_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if proposed_punch_out - proposed_punch_in > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    new_status = proposed_status
    if proposed_status in {"ABSENT", "LEAVE"}:
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = proposed_punch_in
        final_punch_out = proposed_punch_out

    if final_punch_in is not None and final_punch_out is None:
        final_work_hours = None
        final_overtime = 0
        final_half_day = False
    elif final_punch_in is not None and final_punch_out is not None:
        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
        if final_punch_out.tzinfo is None:
            final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
        final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
        final_overtime = compute_overtime_minutes(final_punch_out, emp["shift_end"], emp["shift_start"], date)
        final_half_day = compute_half_day(final_work_hours)
    else:
        final_work_hours = None
        final_overtime = 0
        final_half_day = False

    if final_punch_in is not None and final_punch_in.tzinfo is None:
        final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
    final_late = compute_late_minutes(final_punch_in, emp["shift_start"]) if final_punch_in is not None else 0

    changes: dict[str, dict[str, Any]] = {}
    old_punch_in = original.get("punch_in")
    old_punch_out = original.get("punch_out")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    for field, old_value, new_value in [
        ("status", original.get("status"), new_status),
        ("punch_in", old_punch_in, final_punch_in),
        ("punch_out", old_punch_out, final_punch_out),
        ("work_hours", original.get("work_hours"), final_work_hours),
        ("late_minutes", original.get("late_minutes", 0), final_late),
        ("overtime_minutes", original.get("overtime_minutes", 0), final_overtime),
        ("half_day", original.get("half_day", False), final_half_day),
    ]:
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {field: {"from": to_epoch_ms(change["from"]) if field in {"punch_in", "punch_out"} and change["from"] is not None else change["from"], "to": to_epoch_ms(change["to"]) if field in {"punch_in", "punch_out"} and change["to"] is not None else change["to"]} for field, change in changes.items()},
    }

    update = {
        "$set": {
            "status": new_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": final_work_hours,
            "late_minutes": final_late,
            "overtime_minutes": final_overtime,
            "half_day": final_half_day,
        },
        "$push": {"history": history_entry},
    }
    result = db.attendance_logs.find_one_and_update(
        {"_id": existing["_id"], "history": original_history},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return serialize_attendance(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    try:
        parse_date(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid month") from exc
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    month_start, month_end = month_start_end(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, 5]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    row = next(db.attendance_logs.aggregate(pipeline), None)
    if row is None:
        row = {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    working_days = employee_working_days(employee["joined_on"], month)
    attendance_pct = None if working_days == 0 else decimal_round((row["present_days"] / working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": decimal_round(float(row["present_days"]), 2),
        "leave_days": int(row["leave_days"]),
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "total_overtime_minutes": int(row["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        parse_date(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid month") from exc
    month_start, month_end = month_start_end(month)

    emp_filter: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_filter["department"] = department
    dept_headcount = list(db.employees.aggregate([
        {"$match": emp_filter},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]))
    headcount_map = {item["_id"]: item["headcount"] for item in dept_headcount}

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.joined_on": {"$lte": month_end}}},
        {"$addFields": {"is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "is_weekday": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, 5]}}},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "sum_work_hours": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
            "work_hours_records": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    summary_by_dept = {item["_id"]: item for item in db.attendance_logs.aggregate(pipeline)}
    items = []
    for department_name in sorted(headcount_map.keys()):
        stats = summary_by_dept.get(department_name, {})
        present_days = float(stats.get("present_days", 0) or 0)
        avg_work = None
        if stats.get("work_hours_records", 0):
            avg_work = decimal_round(stats["sum_work_hours"] / stats["work_hours_records"], 2)
        items.append({
            "department": department_name,
            "headcount": headcount_map[department_name],
            "present_days": decimal_round(present_days, 2),
            "avg_work_hours": avg_work,
            "late_count": int(stats.get("late_count", 0) or 0),
            "total_late_minutes": int(stats.get("total_late_minutes", 0) or 0),
            "leave_count": int(stats.get("leave_count", 0) or 0),
            "on_duty_count": int(stats.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    try:
        parse_date(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid month") from exc
    month_start, month_end = month_start_end(month)
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department} if department is not None else {"employee.department": {"$exists": True}}},
        {"$group": {"_id": "$emp_code", "name": {"$first": "$employee.name"}, "department": {"$first": "$employee.department"}, "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1, "_id": 1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = [{
        "rank": int(row["rank"]),
        "emp_code": row["_id"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": int(row["total_late_minutes"]),
        "late_count": int(row["late_count"]),
    } for row in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to: str):
    try:
        start = parse_date(from_date)
        end = parse_date(to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid date range") from exc
    if end < start:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="unknown department")

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$addFields": {"day_date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}},
        {"$group": {
            "_id": "$day_date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [{"$dateFromString": {"dateString": from_date, "timezone": "Asia/Kolkata"}}, {"$dateFromString": {"dateString": to, "timezone": "Asia/Kolkata"}}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "Asia/Kolkata"}}}},
        {"$project": {"_id": 0, "date": 1, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    rows_by_date = {row["date"]: row for row in rows}
    output = []
    current = start
    while current <= end:
        day = current.isoformat()
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": day}})
        row = rows_by_date.get(day, {"present_count": 0, "late_count": 0})
        present_count = float(row["present_count"]) if row else 0.0
        is_working_day = current.weekday() < 5
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = decimal_round((present_count / headcount), 4)
        elif is_working_day and headcount == 0:
            attendance_rate = None
        else:
            attendance_rate = None
        output.append({
            "date": day,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": decimal_round(present_count, 2),
            "late_count": int(row.get("late_count", 0) or 0),
            "attendance_rate": attendance_rate,
        })
        current += timedelta(days=1)
    moving = []
    for idx, item in enumerate(output):
        window = []
        for j in range(max(0, idx - 6), idx + 1):
            val = output[j].get("attendance_rate")
            if val is not None:
                window.append(val)
        if not window:
            item["moving_avg_7d"] = None
        else:
            item["moving_avg_7d"] = decimal_round(sum(window) / len(window), 4)
        moving.append(item)
    return {"department": department, "items": moving}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(status_code=422, detail="unsupported endpoint")

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status
        explain = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        month_start, month_end = month_start_end(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": "$emp_code", "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, 5]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = month_start_end(month)
        emp_filter: dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            emp_filter["department"] = department
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.joined_on": {"$lte": month_end}}},
            {"$addFields": {"is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "is_weekday": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}, 5]}}},
            {"$group": {
                "_id": "$employee.department",
                "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "sum_work_hours": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "work_hours_records": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}},
            }},
            {"$sort": {"_id": 1}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = month_start_end(month)
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department} if department is not None else {"employee.department": {"$exists": True}}},
            {"$group": {"_id": "$emp_code", "name": {"$first": "$employee.name"}, "department": {"$first": "$employee.department"}, "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1, "_id": 1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if department is None or from_date is None or to is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$addFields": {"day_date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}},
            {"$group": {"_id": "$day_date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
            {"$sort": {"_id": 1}},
            {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [{"$dateFromString": {"dateString": from_date, "timezone": "Asia/Kolkata"}}, {"$dateFromString": {"dateString": to, "timezone": "Asia/Kolkata"}}]}}},
            {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "Asia/Kolkata"}}}},
            {"$project": {"_id": 0, "date": 1, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(status_code=422, detail="unsupported endpoint")
