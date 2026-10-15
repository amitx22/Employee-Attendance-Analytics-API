"""
Employee Attendance & Analytics API

FastAPI + MongoDB implementation adhering strictly to openapi.yaml and DATA_MODEL.md.
"""
from __future__ import annotations

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Ensure environment variables from .env are loaded if not already present
load_dotenv()

# IST timezone: UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))

# MongoDB client & database instances
client: Optional[MongoClient] = None
db: Optional[pymongo.database.Database] = None


def get_db() -> pymongo.database.Database:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
    return db


def get_client() -> MongoClient:
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
    return client


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | None, decimals: int) -> float | None:
    """R8: Rounding of reported numbers half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def parse_time_str(t_str: str) -> time:
    h, m = map(int, t_str.split(":"))
    return time(h, m, 0)


def instant_to_epoch_ms(dt: datetime | None) -> int | None:
    """Convert a UTC BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Truncate to whole seconds before calculation, return UTC datetime."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def calculate_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    start_t = parse_time_str(shift_start)
    end_t = parse_time_str(shift_end)

    if end_t <= start_t:
        # Overnight shift
        if punch_in_ist.time() < end_t:
            return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R1, R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = date.fromisoformat(attendance_date_str)
    start_t = parse_time_str(shift_start)
    shift_start_dt = datetime.combine(d, start_t).replace(tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    # strictly more than 10 minutes (600 seconds)
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    """
    R1, R3: Whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Overnight shift end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    d = date.fromisoformat(attendance_date_str)
    start_t = parse_time_str(shift_start)
    end_t = parse_time_str(shift_end)

    if end_t <= start_t:
        end_date = d + timedelta(days=1)
    else:
        end_date = d

    shift_end_dt = datetime.combine(end_date, end_t).replace(tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # 30 minutes = 1800 seconds
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = Decimal(int((punch_out_utc - punch_in_utc).total_seconds()))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners."""
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    joined_date = date.fromisoformat(joined_on_str)
    first_of_month = date(year, month, 1)
    last_of_month = date(year, month, num_days)

    if joined_date > last_of_month:
        return 0

    cur = max(first_of_month, joined_date)
    count = 0
    while cur <= last_of_month:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def sanitize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert BSON datetimes in attendance document to epoch milliseconds according to schema."""
    doc.pop("_id", None)
    doc.pop("id", None)
    doc["punch_in"] = instant_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = instant_to_epoch_ms(doc.get("punch_out"))
    doc["late_minutes"] = int(doc.get("late_minutes", 0))
    doc["overtime_minutes"] = int(doc.get("overtime_minutes", 0))
    doc["half_day"] = bool(doc.get("half_day", False))

    raw_history = doc.get("history", [])
    sanitized_history = []
    for h in raw_history:
        entry = {
            "at": instant_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for field, change in h.get("changes", {}).items():
            f_from = change.get("from")
            f_to = change.get("to")
            if field in ("punch_in", "punch_out"):
                entry["changes"][field] = {
                    "from": instant_to_epoch_ms(f_from) if isinstance(f_from, datetime) else f_from,
                    "to": instant_to_epoch_ms(f_to) if isinstance(f_to, datetime) else f_to,
                }
            else:
                entry["changes"][field] = {"from": f_from, "to": f_to}
        sanitized_history.append(entry)
    doc["history"] = sanitized_history
    return doc


def clean_bson_obj(obj: Any) -> Any:
    """Recursively convert BSON/MongoDB objects into JSON-compliant structures."""
    if isinstance(obj, dict):
        return {k: clean_bson_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_bson_obj(x) for x in obj]
    if isinstance(obj, datetime):
        return instant_to_epoch_ms(obj)
    if hasattr(obj, "__str__") and type(obj).__module__.startswith("bson"):
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_difference(cls, v: str, info):
        start = info.data.get("shift_start", "09:30")
        if v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid joined_on date")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Lifespan: DB Initialization & Idempotent Index Creation
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    mongo_db_name = os.environ.get("MONGO_DB", "attendance_db")

    client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[mongo_db_name]

    # Create all required indexes idempotently
    try:
        # employees
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        # attendance_logs
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except Exception as e:
        # If DB connection failed at startup, it will be caught in /health
        pass

    yield

    if client:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    c = get_client()
    try:
        c.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB ping failed",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn):
    """Create an employee. Enforced by unique index on emp_code."""
    database = get_db()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = instant_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    database = get_db()
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = database.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        emp["created_at"] = instant_to_epoch_ms(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn):
    """Record a punch-in. Race-safe via unique compound index on (emp_code, date)."""
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_in_utc = epoch_ms_to_datetime_utc(int(now_dt.timestamp() * 1000))

    attendance_date = calculate_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return sanitize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes most recent record where punch_in <= punched_at."""
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_out_utc = epoch_ms_to_datetime_utc(int(now_dt.timestamp() * 1000))

    # Find the employee's most recent attendance record with punch_in <= punch_out_utc
    record = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in",
        )
    if diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_mins = compute_overtime(punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = bool(work_hours < 4.50)

    # Atomic update ensuring concurrency safety
    updated = database.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    return sanitize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, then emp_code asc."""
    database = get_db()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = database.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)

    items = [sanitize_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=status.HTTP_200_OK)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """Regularize (manually correct) one day's record. Recomputes derived fields and appends to history."""
    database = get_db()
    date_str = date
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    # Determine target values
    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="supplying punch_in or punch_out with ABSENT or LEAVE is not allowed",
            )
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")
            if target_punch_in and target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="presence status requires a punch_in",
            )

        calc_date = calculate_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")
            if target_punch_out and target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if diff_sec > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be within 24 hours of punch_in",
                )
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], date_str, emp["shift_start"]
            )
            target_half_day = bool(target_work_hours < 4.50)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_str)

    # Detect actual changes
    changes: Dict[str, Any] = {}

    def get_epoch_or_none(dt: datetime | None) -> int | None:
        return instant_to_epoch_ms(dt) if dt else None

    # punch_in
    if get_epoch_or_none(record.get("punch_in")) != get_epoch_or_none(target_punch_in):
        changes["punch_in"] = {
            "from": record.get("punch_in"),
            "to": target_punch_in,
        }

    # punch_out
    if get_epoch_or_none(record.get("punch_out")) != get_epoch_or_none(target_punch_out):
        changes["punch_out"] = {
            "from": record.get("punch_out"),
            "to": target_punch_out,
        }

    # status
    if record["status"] != target_status:
        changes["status"] = {"from": record["status"], "to": target_status}

    # work_hours
    if record.get("work_hours") != target_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": target_work_hours}

    # late_minutes
    if int(record.get("late_minutes", 0)) != target_late_minutes:
        changes["late_minutes"] = {"from": int(record.get("late_minutes", 0)), "to": target_late_minutes}

    # overtime_minutes
    if int(record.get("overtime_minutes", 0)) != target_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": int(record.get("overtime_minutes", 0)),
            "to": target_overtime_minutes,
        }

    # half_day
    if bool(record.get("half_day", False)) != target_half_day:
        changes["half_day"] = {"from": bool(record.get("half_day", False)), "to": target_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request that changes nothing is 422",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history array length to guarantee race-safety
    history_len = len(record.get("history", []))
    updated = database.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected. Please re-read and retry.",
        )

    return sanitize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = compute_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "isoDayOfWeek": {
                    "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$isoDayOfWeek", 5]},
                                ]
                            },
                            {
                                "$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(database.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = round_half_up(data.get("present_days", 0.0), 2)
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month via MongoDB cross-collection aggregation."""
    database = get_db()
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "isoDayOfWeek": {
                                "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": ["$$l.isoDayOfWeek", 5]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {
                                "$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wl",
                        "in": "$$wl.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_list"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$avg": {
                        "$reduce": {
                            "input": "$all_work_hours",
                            "initialValue": [],
                            "in": {"$concatArrays": ["$$value", "$$this"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    res = list(database.employees.aggregate(pipeline))
    items = []
    for r in res:
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": round_half_up(r["present_days"], 2),
                "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month using standard competition ranking ($rank)."""
    database = get_db()
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(database.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date_str: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average."""
    database = get_db()
    # Check if department exists (has any employees)
    if database.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown department '{department}'",
        )

    try:
        from_d = date.fromisoformat(from_date_str)
        to_d = date.fromisoformat(to_date_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be >= 'from' date",
        )

    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    from_dt = datetime.combine(from_d, time.min).replace(tzinfo=timezone.utc)
    to_dt_plus_one = datetime.combine(to_d + timedelta(days=1), time.min).replace(tzinfo=timezone.utc)

    pipeline: List[Dict[str, Any]] = [
        # Match date range on attendance_logs backed by index
        {"$match": {"date": {"$gte": from_date_str, "$lte": to_date_str}}},
        # Join employee to filter department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Gap-filling stage
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # Headcount on each day
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    raw_items = list(database.attendance_logs.aggregate(pipeline))
    items = []
    for row in raw_items:
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row["present_count"], 2),
                "late_count": int(row["late_count"]),
                "attendance_rate": round_half_up(row.get("attendance_rate"), 4),
                "moving_avg_7d": round_half_up(row.get("moving_avg_7d"), 4),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    from_date_str: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """MongoDB explain for an endpoint's main query or aggregation pipeline."""
    database = get_db()

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        explain_cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = database.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson_obj(res)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$addFields": {
                    "isoDayOfWeek": {
                        "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$lte": ["$isoDayOfWeek", 5]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = database.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson_obj(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        },
                        {
                            "$addFields": {
                                "isoDayOfWeek": {
                                    "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                                }
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "l",
                                        "cond": {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": ["$$l.isoDayOfWeek", 5]},
                                            ]
                                        },
                                    }
                                },
                                "as": "pl",
                                "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                            }
                        }
                    },
                    "work_hours_list": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "wl",
                            "in": "$$wl.work_hours",
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                            }
                        }
                    },
                    "total_late_minutes": {"$sum": "$logs.late_minutes"},
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                    "all_work_hours": {"$push": "$work_hours_list"},
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$avg": {
                            "$reduce": {
                                "input": "$all_work_hours",
                                "initialValue": [],
                                "in": {"$concatArrays": ["$$value", "$$this"]},
                            }
                        }
                    },
                }
            },
            {"$sort": {"department": 1}},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = database.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_bson_obj(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$emp.name",
                        "department": "$emp.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = database.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson_obj(res)}

    elif endpoint == "department_trend":
        if not department or not from_date_str or not to_date_str:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )
        try:
            from_d = date.fromisoformat(from_date_str)
            to_d = date.fromisoformat(to_date_str)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if to_d < from_d:
            raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Range cannot exceed 92 days")

        from_dt = datetime.combine(from_d, time.min).replace(tzinfo=timezone.utc)
        to_dt_plus_one = datetime.combine(to_d + timedelta(days=1), time.min).replace(tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"date": {"$gte": from_date_str, "$lte": to_date_str}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$date"}}}},
            {
                "$group": {
                    "_id": "$date_dt",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$densify": {
                    "field": "_id",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_plus_one],
                    },
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                    "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"current_day": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$current_day"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}}},
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = database.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson_obj(res)}
