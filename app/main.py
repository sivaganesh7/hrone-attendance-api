"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("uvicorn.error")

# Real environment variables take priority over .env
load_dotenv(override=False)

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Domain Helpers (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """
    R8: Rounds numbers using standard half-up rounding (not round-half-to-even/banker's).
    """
    if value is None or isinstance(value, bool):
        return None
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    factor = Decimal("10") ** -decimals
    return float(d.quantize(factor, rounding=ROUND_HALF_UP))


def to_epoch_ms(val: Any) -> Optional[int]:
    """
    Converts datetime or existing millisecond integer into integer epoch milliseconds.
    Instants are truncated to whole seconds per R1.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        val = val.replace(microsecond=0)
        return int(val.timestamp()) * 1000
    if isinstance(val, (int, float)):
        # Truncate to whole seconds (ms // 1000 * 1000)
        return (int(val) // 1000) * 1000
    return None


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Calculates the IST calendar date for a punch-in.
    Overnight shift rule: if shift_end <= shift_start (e.g. 22:00-06:00),
    a punch-in whose IST time is earlier than shift_end belongs to the shift
    that started the previous day.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_utc = punch_in_utc.replace(microsecond=0)
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
        return (punch_in_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    return punch_in_ist.strftime("%Y-%m-%d")


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600s) after shift_start.
    When late: whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_utc = punch_in_utc.replace(microsecond=0)

    start_h, start_m = map(int, shift_start.split(":"))
    y, m, d = map(int, date_str.split("-"))
    shift_start_ist = datetime(y, m, d, start_h, start_m, 0, tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    elapsed_seconds = int((punch_in_utc - shift_start_utc).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    punch_in_utc = punch_in_utc.replace(microsecond=0)
    punch_out_utc = punch_out_utc.replace(microsecond=0)

    elapsed_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return round_half_up(hours, 2)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on the following calendar day.
    """
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    punch_out_utc = punch_out_utc.replace(microsecond=0)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    y, m, d = map(int, date_str.split("-"))
    att_date = datetime(y, m, d, tzinfo=IST)

    if (end_h, end_m) <= (start_h, start_m):
        shift_end_ist = (att_date + timedelta(days=1)).replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    else:
        shift_end_ist = att_date.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    shift_end_utc = shift_end_ist.astimezone(timezone.utc)
    elapsed_seconds = int((punch_out_utc - shift_end_utc).total_seconds())
    minutes = elapsed_seconds // 60
    if minutes >= 30:
        return minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Database Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            if k in ("punch_in", "punch_out"):
                changes[k] = {
                    "from": to_epoch_ms(v.get("from")) if isinstance(v, dict) else None,
                    "to": to_epoch_ms(v.get("to")) if isinstance(v, dict) else None,
                }
            else:
                changes[k] = {
                    "from": v.get("from") if isinstance(v, dict) else None,
                    "to": v.get("to") if isinstance(v, dict) else None,
                }
        history.append({
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    late_val = doc.get("late_minutes")
    late_minutes = 0 if late_val is None else int(late_val)

    ot_val = doc.get("overtime_minutes")
    overtime_minutes = 0 if ot_val is None else int(ot_val)

    hd_val = doc.get("half_day")
    half_day = False if hd_val is None else bool(hd_val)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models (openapi.yaml schemas)
# --------------------------------------------------------------------------- #
EpochMillisField = Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def check_shifts_and_dates(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisField] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisField] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillisField] = None
    punch_out: Optional[EpochMillisField] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Startup & Index Management
# --------------------------------------------------------------------------- #
def ensure_indexes(db_instance):
    """
    Idempotently creates required indexes for the collections.
    Checks existing data for uniqueness conflicts before creating unique indexes.
    """
    try:
        # Check conflicts for employees.emp_code
        emp_dups = list(
            db_instance.employees.aggregate(
                [
                    {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
                    {"$match": {"count": {"$gt": 1}}},
                ]
            )
        )
        if emp_dups:
            logger.error("Data conflict: duplicate emp_code in employees: %s", emp_dups)
        else:
            db_instance.employees.create_index(
                [("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique"
            )

        db_instance.employees.create_index(
            [("department", 1), ("emp_code", 1)], name="idx_employees_dept_emp_code"
        )
        db_instance.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")

        # Check conflicts for attendance_logs(emp_code, date)
        att_dups = list(
            db_instance.attendance_logs.aggregate(
                [
                    {
                        "$group": {
                            "_id": {"emp_code": "$emp_code", "date": "$date"},
                            "count": {"$sum": 1},
                        }
                    },
                    {"$match": {"count": {"$gt": 1}}},
                ]
            )
        )
        if att_dups:
            logger.error("Data conflict: duplicate (emp_code, date) in attendance_logs: %s", att_dups)
        else:
            db_instance.attendance_logs.create_index(
                [("emp_code", 1), ("date", 1)],
                unique=True,
                name="idx_attendance_emp_date_unique",
            )

        db_instance.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="idx_attendance_date_desc_emp"
        )
        db_instance.attendance_logs.create_index(
            [("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_punch_in"
        )
        db_instance.attendance_logs.create_index(
            [("date", 1), ("status", 1)], name="idx_attendance_date_status"
        )
        db_instance.attendance_logs.create_index(
            [("date", 1), ("late_minutes", 1)], name="idx_attendance_date_late_minutes"
        )
        logger.info("MongoDB indexes verified and ensured.")
    except Exception as e:
        logger.warning("MongoDB index creation skipped or failed (database offline): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes(db)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        secs = body.punched_at // 1000
        punch_in_utc = datetime.fromtimestamp(secs, tz=timezone.utc)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = get_attendance_date(punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_minutes = compute_late_minutes(punch_in_utc, emp.get("shift_start", "09:30"), att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        secs = body.punched_at // 1000
        punch_out_utc = datetime.fromtimestamp(secs, tz=timezone.utc)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not record:
        # Check if any punch-in exists (e.g. after punched_at)
        any_punch = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", 1)],
        )
        if any_punch:
            raise HTTPException(status_code=422, detail="punched_at cannot be at or before punch_in")
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_utc = punch_in_utc.replace(microsecond=0)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punched_at cannot be at or before punch_in")

    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch-out cannot be more than 24 hours after punch-in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    overtime_minutes = compute_overtime(punch_out_utc, shift_start, shift_end, record["date"])
    half_day = compute_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated_doc is None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return serialize_attendance(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from")
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_in is not None:
        old_punch_in = old_punch_in.replace(microsecond=0)
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    if old_punch_out is not None:
        old_punch_out = old_punch_out.replace(microsecond=0)

    old_work_hours = record.get("work_hours")
    old_late_minutes = 0 if record.get("late_minutes") is None else int(record["late_minutes"])
    old_overtime_minutes = 0 if record.get("overtime_minutes") is None else int(record["overtime_minutes"])
    old_half_day = False if record.get("half_day") is None else bool(record["half_day"])

    has_status = "status" in body.model_fields_set
    has_punch_in = "punch_in" in body.model_fields_set
    has_punch_out = "punch_out" in body.model_fields_set

    new_status = body.status if has_status else old_status

    if has_punch_in:
        secs_in = body.punch_in // 1000
        new_punch_in = datetime.fromtimestamp(secs_in, tz=timezone.utc)
    else:
        new_punch_in = old_punch_in

    if has_punch_out:
        secs_out = body.punch_out // 1000
        new_punch_out = datetime.fromtimestamp(secs_out, tz=timezone.utc)
    else:
        new_punch_out = old_punch_out

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times with those statuses is 422
    if new_status in ("ABSENT", "LEAVE"):
        if has_punch_in or has_punch_out:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None

    # Rule: presence status requires a punch_in (supplied or already stored)
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # Rule: punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        calc_date = get_attendance_date(new_punch_in, shift_start, shift_end)
        if calc_date != date:
            raise HTTPException(
                status_code=422, detail="punch_in must stay on the record's attendance date"
            )

    # Rule: punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=422, detail="punch_out cannot be more than 24 hours after punch_in"
            )

    # Recompute derived fields (R2-R5)
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, shift_start, date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, shift_start, shift_end, date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Collect fields that actually changed
    changes = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}

    if to_epoch_ms(old_punch_in) != to_epoch_ms(new_punch_in):
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    if to_epoch_ms(old_punch_out) != to_epoch_ms(new_punch_out):
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in request")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic compare-and-set using current history state
    filter_query = {"_id": record["_id"]}
    if "history" in record:
        filter_query["history"] = record["history"]
    else:
        filter_query["history"] = {"$exists": False}

    update_ops = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        filter_query, update_ops, return_document=ReturnDocument.AFTER
    )
    if updated_doc is None:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return serialize_attendance(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    # Calculate weekday dates in the month (Mon-Fri)
    weekday_dates = []
    for day in range(1, num_days + 1):
        d_obj = datetime(year, month_num, day).date()
        if d_obj.weekday() < 5:
            weekday_dates.append(f"{month}-{day:02d}")

    # R7: Working days Mon-Fri, counted from joined_on for mid-month joiners
    emp_joined = emp.get("joined_on", first_day_str)
    start_date_str = max(first_day_str, emp_joined)

    working_days = 0
    if start_date_str <= last_day_str:
        start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_d = datetime.strptime(last_day_str, "%Y-%m-%d").date()
        cur = start_d
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    # MongoDB aggregation pipeline over attendance_logs
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first_day_str,
                    "$lte": last_day_str,
                },
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
                                    {"$in": ["$date", weekday_dates]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
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

    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        res = agg_results[0]
        present_days = round_half_up(res.get("present_days", 0.0), 2)
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = round_half_up(raw_pct, 2)

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
    department: Optional[str] = Query(None),
):
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    # Weekday dates for present_days rule (Mon-Fri)
    weekday_dates = []
    for day in range(1, num_days + 1):
        d_obj = datetime(year, month_num, day).date()
        if d_obj.weekday() < 5:
            weekday_dates.append(f"{month}-{day:02d}")

    # R9: Headcount includes employees whose joined_on is on or before the last day of the month
    match_emp = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$log.date", first_day_str]},
                                {"$lte": ["$$log.date", last_day_str]},
                            ]
                        },
                    }
                },
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.date", weekday_dates]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_cnt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    agg_results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in agg_results:
        dept_name = doc["_id"]
        headcount = len(doc.get("employees") or [])
        if headcount == 0:
            continue

        p_days = round_half_up(doc.get("present_days", 0.0), 2)
        wh_sum = doc.get("work_hours_sum", 0.0)
        wh_cnt = doc.get("work_hours_cnt", 0)

        if wh_cnt > 0:
            raw_avg = Decimal(str(wh_sum)) / Decimal(wh_cnt)
            avg_work_hours = round_half_up(raw_avg, 2)
        else:
            avg_work_hours = None

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": p_days,
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
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
            "$match": {
                "total_late_minutes": {"$gt": 0},
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

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    try:
        results = list(db.attendance_logs.aggregate(pipeline))
    except NotImplementedError:
        # Fallback for mock drivers (such as mongomock in unit tests) that lack $setWindowFields
        pre_pipeline = [
            stage
            for stage in pipeline
            if "$setWindowFields" not in stage
            and ("$match" not in stage or "rank" not in stage.get("$match", {}))
            and "$sort" not in stage
        ]
        raw_results = list(db.attendance_logs.aggregate(pre_pipeline))
        raw_results.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
        results = []
        for i, item in enumerate(raw_results):
            if i > 0 and item["total_late_minutes"] == raw_results[i - 1]["total_late_minutes"]:
                item["rank"] = results[i - 1]["rank"]
            else:
                item["rank"] = i + 1
            if item["rank"] <= limit:
                results.append(item)

    items = []
    for doc in results:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": str(doc["emp_code"]),
            "name": str(doc["name"]),
            "department": str(doc["department"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    num_days = (to_d - from_d).days + 1
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "all_logs",
            }
        },
        {
            "$project": {
                "employees": 1,
                "all_logs": 1,
                "dates": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": from_dt,
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$project": {
                "date": "$dates",
                "date_obj": {"$dateFromString": {"dateString": "$dates"}},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "emp",
                            "cond": {"$lte": ["$$emp.joined_on", "$dates"]},
                        }
                    }
                },
                "day_logs": {
                    "$filter": {
                        "input": "$all_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.date", "$dates"]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]
                },
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": ["$$log.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def fallback_department_trend(department: str, from_date: str, to_date: str):
    emps = list(db.employees.find({"department": department}))
    emp_codes = [e["emp_code"] for e in emps]
    logs = list(
        db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        )
    )
    logs_by_date = {}
    for l in logs:
        d = l.get("date")
        if d:
            logs_by_date.setdefault(d, []).append(l)

    from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    num_days = (to_d - from_d).days + 1

    rows = []
    for offset in range(num_days):
        cur_d = from_d + timedelta(days=offset)
        cur_d_str = cur_d.strftime("%Y-%m-%d")
        is_working_day = cur_d.weekday() < 5
        hc = len([e for e in emps if e.get("joined_on", "") <= cur_d_str])

        day_logs = logs_by_date.get(cur_d_str, [])
        present_count = 0.0
        late_count = 0
        for l in day_logs:
            st = l.get("status")
            if st in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if l.get("half_day") else 1.0
            if (l.get("late_minutes") or 0) > 0:
                late_count += 1

        if is_working_day and hc > 0:
            rate = present_count / hc
        else:
            rate = None

        rows.append({
            "date": cur_d_str,
            "is_working_day": is_working_day,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
        })

    for i, r in enumerate(rows):
        start_idx = max(0, i - 6)
        rates_in_window = [
            rows[j]["attendance_rate"]
            for j in range(start_idx, i + 1)
            if rows[j]["attendance_rate"] is not None
        ]
        if rates_in_window:
            r["moving_avg_7d"] = sum(rates_in_window) / len(rates_in_window)
        else:
            r["moving_avg_7d"] = None

    return rows


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    try:
        from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")

    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days limit")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    try:
        results = list(db.employees.aggregate(pipeline))
    except NotImplementedError:
        results = fallback_department_trend(department, from_date, to_date)

    items = []
    for doc in results:
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": round_half_up(doc.get("present_count", 0.0), 2),
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": round_half_up(doc.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(doc.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


