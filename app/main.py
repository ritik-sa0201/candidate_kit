"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)

Instants are stored as BSON dates (UTC) and exposed as epoch milliseconds.
Calendar dates and shift times are plain strings, interpreted in IST.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # never overrides variables that are already set

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # read datetimes back as aware UTC, not naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # shouldn't happen with tz_aware=True, but be safe
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    # R1: truncate to whole seconds before anything else
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST date of the punch, except early-morning punches of an overnight shift."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Business rules R2 - R5
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    start, _ = shift_window(day, emp)
    seconds = int((punch_in - start).total_seconds())
    # late only if strictly more than 10:00 after start; then count from start
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    _, end = shift_window(day, emp)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derived_fields(status: str, punch_in, punch_out, day: date, emp: dict) -> dict:
    """The four computed fields for a record in the given state (see DATA_MODEL.md)."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    fields = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields["work_hours"] = float(hours)
        fields["overtime_minutes"] = compute_overtime(punch_out, day, emp)
        fields["half_day"] = hours < HALF_DAY_BELOW  # R5 uses the rounded value
    return fields


# --------------------------------------------------------------------------- #
# Validation types and models
# --------------------------------------------------------------------------- #
def _check_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    date.fromisoformat(value)  # rejects 2026-02-30
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=50)]
MAX_PUNCH_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92


def validation_error(loc: tuple, msg: str) -> RequestValidationError:
    """422 with FastAPI's usual body, for checks that need more than one field."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # defaults are None but an explicit null is still rejected (the types are not Optional).
    # Unknown fields such as late_minutes are ignored, as the contract asks.
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _plain(value):
    return to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {
        field: {"from": _plain(change.get("from")), "to": _plain(change.get("to"))}
        for field, change in (entry.get("changes") or {}).items()
    }
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    # older records may lack half_day / history / the minute counters
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
indexes_ready = False


def ensure_indexes():
    global indexes_ready
    employees.create_index("emp_code", unique=True)
    employees.create_index([("department", 1), ("joined_on", 1)])
    # one record per employee per day; also the race guard for punch-in
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # GET /attendance sort, and date-range scans for the analytics
    logs.create_index([("date", -1), ("emp_code", 1)])
    # punch-out: latest punch_in at or before a given instant
    logs.create_index([("emp_code", 1), ("punch_in", -1)])
    # department summary without a department filter starts from joined_on
    employees.create_index("joined_on")
    indexes_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        # don't block startup on a slow DB; /health retries before reporting ready
        print(f"index creation deferred: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # unique index decides duplicates, no pre-check
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # (emp_code, date) is unique: concurrent duplicates -> exactly one wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(("query", "date_to"), "date_to is before date_from")
    q = {}
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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    rec = logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        # an open record that starts after ts means the punch-out is before the punch-in
        if logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$gt": ts}, "punch_out": None}):
            raise validation_error(("body", "punched_at"), "punch-out is before punch-in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise validation_error(("body", "punched_at"), "punch-out must be after punch-in")
    if ts - rec["punch_in"] > MAX_PUNCH_SPAN:
        raise validation_error(("body", "punched_at"), "punch-out is more than 24 hours after punch-in")

    day = date.fromisoformat(rec["date"])
    update = {"punch_out": ts, **derived_fields(rec["status"], rec["punch_in"], ts, day, emp)}
    # the filter only matches while the record is still open, so of two
    # simultaneous punch-outs exactly one matches
    result = logs.update_one({"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return record_out(logs.find_one({"_id": rec["_id"]}))


def _stored_state(rec: dict) -> dict:
    return {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date_: Annotated[DateStr, Path(alias="date")], body: RegularizeIn):
    sent = body.model_fields_set & {"status", "punch_in", "punch_out"}
    if not sent:
        raise validation_error(("body",), "nothing to change")

    emp = get_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date_})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = _stored_state(rec)
    status = body.status or old["status"]

    if status in PRESENCE:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise validation_error(("body", "punch_in"), f"status {status} needs a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp).isoformat() != date_:
            raise validation_error(("body", "punch_in"), "punch_in is not on this attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + MAX_PUNCH_SPAN):
            raise validation_error(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(("body", "status"), f"status {status} cannot have punch times")
        new_in = new_out = None

    new = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived_fields(status, new_in, new_out, date.fromisoformat(date_), emp),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise validation_error(("body",), "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # optimistic lock: only write if nobody changed the record since we read it.
    # history length is part of the check, so two corrections can't both append
    # on top of the same version and one of them lose its context.
    n = len(rec.get("history") or [])
    guard = {"_id": rec["_id"], "status": old["status"], "punch_in": old["punch_in"],
             "punch_out": old["punch_out"], f"history.{n}": {"$exists": False}}
    if n:
        guard[f"history.{n - 1}"] = {"$exists": True}

    result = logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
    if result.matched_count == 0:
        raise HTTPException(409, "record was changed by another request, retry")
    return record_out(logs.find_one({"_id": rec["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics
#
# Each pipeline is built by a function so that the endpoint and /admin/explain
# run exactly the same thing. Dates are "YYYY-MM-DD" strings, so string range
# matches work and use the indexes. Final half-up rounding is done with Decimal
# on the few aggregated numbers ($round in MongoDB rounds half to even).
# --------------------------------------------------------------------------- #
DAY_OF_WEEK = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
IS_WEEKDAY = {"$lte": [DAY_OF_WEEK, 5]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
PRESENT_WEIGHT = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_half_up(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_weekdays(first: date, last: date) -> int:
    days = (last - first).days + 1
    return sum(1 for i in range(max(days, 0)) if (first + timedelta(days=i)).weekday() < 5)


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_WEIGHT, 0]}},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    match = {"joined_on": {"$lte": last}}  # R9, so people with no logs still count
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # per-employee totals for the month; the join uses the (emp_code, date) index
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_WEIGHT, 0]}},
                    # hours kept in hundredths so the sum is exact
                    "hours_x100": {"$sum": {"$cond": [HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "hours_count": count_if(HAS_HOURS),
                    "late_count": count_if(IS_LATE),
                    "total_late_minutes": {"$sum": LATE},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "month",
        }},
        {"$unwind": {"path": "$month", "preserveNullAndEmptyArrays": True}},
        # $sum skips missing values, so employees without logs add 0
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$month.present_days"},
            "hours_x100": {"$sum": "$month.hours_x100"},
            "hours_count": {"$sum": "$month.hours_count"},
            "late_count": {"$sum": "$month.late_count"},
            "total_late_minutes": {"$sum": "$month.total_late_minutes"},
            "leave_count": {"$sum": "$month.leave_count"},
            "on_duty_count": {"$sum": "$month.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee doesn't exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, not the row count
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, first: date, last: date) -> list:
    lo, hi = first.isoformat(), last.isoformat()
    zero = {"$literal": 0}
    window_7 = {"documents": [-6, "current"]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                {"$project": {"_id": 0, "date": 1, "joined": zero, "present": PRESENT_WEIGHT,
                              "late": {"$cond": [IS_LATE, 1, 0]}}},
            ],
            "as": "events",
        }},
        # Turn every employee into events: their logs, a +1 headcount on the day they
        # joined (or on `from` if earlier), and an empty event on `from` so the range
        # is never empty for $densify.
        {"$project": {"events": {"$concatArrays": [
            "$events",
            [{"date": lo, "joined": zero, "present": zero, "late": zero}],
            {"$cond": [
                {"$lte": ["$joined_on", hi]},
                [{"date": {"$max": ["$joined_on", lo]}, "joined": {"$literal": 1}, "present": zero, "late": zero}],
                [],
            ]},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {
            "_id": "$events.date",
            "joined": {"$sum": "$events.joined"},
            "present_count": {"$sum": "$events.present"},
            "late_count": {"$sum": "$events.late"},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # one row per calendar day; upper bound is exclusive
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day",
            "bounds": [datetime.combine(first, time(), UTC), datetime.combine(last + timedelta(days=1), time(), UTC)],
        }}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # rate in 1/10000ths, rounded half-up with integer maths:
        # floor(present / headcount * 10000 + 0.5) = floor((present*20000 + hc) / (2*hc))
        {"$addFields": {"rate_x10000": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                {"$multiply": ["$headcount", 2]},
            ]}},
            None,
        ]}}},
        # only rows inside the range exist, so early rows get a shorter window
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "rate_sum": {"$sum": "$rate_x10000", "window": window_7},
            "rate_n": {"$sum": {"$cond": [{"$eq": ["$rate_x10000", None]}, 0, 1]}, "window": window_7},
        }}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$eq": ["$rate_x10000", None]}, None, {"$divide": ["$rate_x10000", 10000]},
            ]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$rate_n", 0]},
                {"$divide": [
                    {"$floor": {"$divide": [{"$add": [{"$multiply": ["$rate_sum", 2]}, "$rate_n"]},
                                            {"$multiply": ["$rate_n", 2]}]}},
                    10000,
                ]},
                None,
            ]},
        }},
    ]


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise validation_error(("query", "to"), "to is before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise validation_error(("query", "to"), f"range is longer than {MAX_TREND_DAYS} days")
    return first, last


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)

    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    totals = rows[0] if rows else {}
    present = Decimal(str(totals.get("present_days", 0)))
    pct = None
    if working_days:
        pct = round_half_up(present / working_days * 100, "0.01")
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, "0.01"),
        "leave_days": int(totals.get("leave_days", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in employees.aggregate(department_summary_pipeline(first.isoformat(), last.isoformat(), department)):
        avg = None
        if row["hours_count"]:
            avg = round_half_up(Decimal(int(row["hours_x100"])) / row["hours_count"] / 100, "0.01")
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(Decimal(str(row["present_days"])), "0.01"),
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = logs.aggregate(late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    first, last = trend_range(start, end)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(department_trend_pipeline(department, first, last)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def _required(name: str, value):
    if value is None:
        raise validation_error(("query", name), "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            first, last = month_bounds(_required("month", month))
            collection = "attendance_logs"
            pipeline = employee_monthly_pipeline(_required("emp_code", emp_code), first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            first, last = month_bounds(_required("month", month))
            collection = "employees"
            pipeline = department_summary_pipeline(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(_required("month", month))
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        else:
            first, last = trend_range(_required("from", start), _required("to", end))
            collection = "employees"
            pipeline = department_trend_pipeline(_required("department", department), first, last)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command("explain", command, verbosity="executionStats")
    # explain output holds BSON types (Timestamp, ObjectId...) that plain JSON can't encode
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
