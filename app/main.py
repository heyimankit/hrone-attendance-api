"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env file
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes initialization
# --------------------------------------------------------------------------- #
def init_db():
    # employees collection indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_att_emp_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_att_date_late")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
    except Exception:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Business-Rule Helpers (R1 - R10)
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    # Truncate to whole seconds per R1
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def round_half_up_2(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_in_ist.strftime("%H:%M") < shift_end:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace period is 10 minutes.
    A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_d = datetime.fromisoformat(att_date).date()
    shift_start_ist = datetime(att_d.year, att_d.month, att_d.day, sh_h, sh_m, 0, tzinfo=IST)
    elapsed_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    elapsed_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = elapsed_sec / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_d = datetime.fromisoformat(att_date).date()
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_date = att_d + timedelta(days=1)
    else:
        shift_end_date = att_d
    shift_end_ist = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST)
    elapsed_seconds = int((punch_out_ist - shift_end_ist).total_seconds())
    if elapsed_seconds < 0:
        return 0
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def format_history_entry(entry: dict) -> dict:
    changes_formatted = {}
    for k, v in entry.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            changes_formatted[k] = {
                "from": to_epoch_ms(v.get("from")),
                "to": to_epoch_ms(v.get("to"))
            }
        else:
            changes_formatted[k] = {
                "from": v.get("from"),
                "to": v.get("to")
            }
    return {
        "at": to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_formatted
    }


def format_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])]
    }


def parse_and_validate_month(month: str) -> tuple[str, str]:
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")
    year_num, month_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year_num, month_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"
    return start_date, end_date


def compute_working_days(month: str, joined_on: str) -> int:
    start_date, end_date = parse_and_validate_month(month)
    if joined_on > end_date:
        return 0
    cur_d = max(datetime.fromisoformat(start_date).date(), datetime.fromisoformat(joined_on).date())
    end_d = datetime.fromisoformat(end_date).date()
    count = 0
    while cur_d <= end_d:
        if cur_d.weekday() < 5:  # Monday=0 ... Friday=4
            count += 1
        cur_d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pipelines Builders
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
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
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
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
                }
            }
        }
    ]


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str] = None) -> list:
    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
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
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "wh_records": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "all_wh": {"$push": "$wh_records.work_hours"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "flat_wh": {
                    "$reduce": {
                        "input": "$all_wh",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": [{"$size": "$flat_wh"}, 0]},
                        None,
                        {"$avg": "$flat_wh"}
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]


def build_late_leaderboard_pipeline(start_date: str, end_date: str, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date_str: str, to_date_str: str) -> list:
    from_dt = datetime.fromisoformat(from_date_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date_str).replace(tzinfo=timezone.utc)
    to_dt_bound = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date_obj": {"$dateFromString": {"dateString": from_date_str}}}},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$date_obj"},
                        5
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$cur_date"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "logs_summary"
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]
                },
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$logs_summary.present_count", 0]}, 0.0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$logs_summary.late_count", 0]}, 0]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
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
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Any]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")

    try:
        datetime.strptime(body.joined_on, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid joined_on date")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for d in cursor:
        items.append({
            "emp_code": d["emp_code"],
            "name": d["name"],
            "email": d["email"],
            "department": d["department"],
            "shift_start": d["shift_start"],
            "shift_end": d["shift_end"],
            "joined_on": d["joined_on"],
            "created_at": to_epoch_ms(d["created_at"]),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_ms(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
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
        raise HTTPException(status_code=409, detail="Attendance record already exists for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_ms(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch-in is at or before the punch-out instant.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No eligible punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="Punch-out must be strictly after punch-in and within 24 hours")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    updated_record = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated_record is None:
        raise HTTPException(status_code=409, detail="Record was already punched out concurrently")

    return format_attendance_doc(updated_record)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
            raise HTTPException(status_code=422, detail="Invalid date_from format")
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from date")
    if date_to:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
            raise HTTPException(status_code=422, detail="Invalid date_to format")
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to date")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [format_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date value")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status.value if body.status is not None else record["status"]
    has_req_in = body.punch_in is not None
    has_req_out = body.punch_out is not None

    if target_status in ("ABSENT", "LEAVE"):
        if has_req_in or has_req_out:
            raise HTTPException(status_code=422, detail="Explicit punch times cannot be supplied for ABSENT or LEAVE status")
        final_punch_in = None
        final_punch_out = None
    else:
        if has_req_in:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")
            if final_punch_in is not None and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch-in")

        if has_req_out:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")
            if final_punch_out is not None and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

    # Validate punch_in belongs to attendance date
    if final_punch_in is not None:
        expected_att_date = compute_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if expected_att_date != date:
            raise HTTPException(status_code=422, detail="Corrected punch_in does not belong to this attendance date")

    # Validate punch_out
    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Punch-out requires punch-in")
        diff_sec = (final_punch_out - final_punch_in).total_seconds()
        if diff_sec <= 0 or diff_sec > 86400:
            raise HTTPException(status_code=422, detail="Punch-out must be strictly after punch-in and within 24 hours")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = compute_half_day(final_work_hours)
            final_overtime_minutes = compute_overtime(final_punch_out, emp["shift_start"], emp["shift_end"], date)
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

    # Compare against existing values to find actual changes
    orig_status = record["status"]
    orig_punch_in = record.get("punch_in")
    if orig_punch_in is not None and orig_punch_in.tzinfo is None:
        orig_punch_in = orig_punch_in.replace(tzinfo=timezone.utc)
    orig_punch_out = record.get("punch_out")
    if orig_punch_out is not None and orig_punch_out.tzinfo is None:
        orig_punch_out = orig_punch_out.replace(tzinfo=timezone.utc)
    orig_work_hours = record.get("work_hours")
    orig_late_minutes = record.get("late_minutes", 0)
    orig_overtime_minutes = record.get("overtime_minutes", 0)
    orig_half_day = record.get("half_day", False)

    changes = {}
    if target_status != orig_status:
        changes["status"] = {"from": orig_status, "to": target_status}
    if final_punch_in != orig_punch_in:
        changes["punch_in"] = {"from": orig_punch_in, "to": final_punch_in}
    if final_punch_out != orig_punch_out:
        changes["punch_out"] = {"from": orig_punch_out, "to": final_punch_out}
    if final_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": final_work_hours}
    if final_late_minutes != orig_late_minutes:
        changes["late_minutes"] = {"from": orig_late_minutes, "to": final_late_minutes}
    if final_overtime_minutes != orig_overtime_minutes:
        changes["overtime_minutes"] = {"from": orig_overtime_minutes, "to": final_overtime_minutes}
    if final_half_day != orig_half_day:
        changes["half_day"] = {"from": orig_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry_mongo = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    current_history = record.get("history", [])

    # Atomic conditional update using optimistic concurrency on history size
    updated_record = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(current_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry_mongo
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated_record is None:
        raise HTTPException(status_code=409, detail="Record was modified concurrently by another request")

    return format_attendance_doc(updated_record)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date, end_date = parse_and_validate_month(month)
    working_days = compute_working_days(month, emp["joined_on"])

    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
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
        attendance_pct = round_half_up_2((present_days / working_days) * 100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    start_date, end_date = parse_and_validate_month(month)
    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up_2(r["avg_work_hours"]),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"])
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    start_date, end_date = parse_and_validate_month(month)
    pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"])
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", from_date):
        raise HTTPException(status_code=422, detail="Invalid 'from' date format")
    try:
        f_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'from' date value")

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", to_date):
        raise HTTPException(status_code=422, detail="Invalid 'to' date format")
    try:
        t_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'to' date value")

    if t_dt < f_dt:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    day_span = (t_dt - f_dt).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 calendar days inclusive")

    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up_4(r.get("attendance_rate")),
            "moving_avg_7d": round_half_up_4(r.get("moving_avg_7d"))
        })

    return {
        "department": department,
        "items": items
    }


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1),
    page_size: Optional[int] = Query(20),
):
    if endpoint == "attendance_list":
        if page is not None and page < 1:
            raise HTTPException(status_code=422, detail="page must be >= 1")
        if page_size is not None and (page_size < 1 or page_size > 100):
            raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")
        if date_from:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
                raise HTTPException(status_code=422, detail="Invalid date_from format")
            try:
                datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from")
        if date_to:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
                raise HTTPException(status_code=422, detail="Invalid date_to format")
            try:
                datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status.value

        p = page or 1
        ps = page_size or 20
        skip = (p - 1) * ps

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": ps
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly explain")
        start_date, end_date = parse_and_validate_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")
        start_date, end_date = parse_and_validate_month(month)
        pipeline = build_department_summary_pipeline(start_date, end_date, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")
        lim = limit or 10
        if lim < 1 or lim > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
        start_date, end_date = parse_and_validate_month(month)
        pipeline = build_late_leaderboard_pipeline(start_date, end_date, lim, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend explain")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' dates are required for department_trend explain")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", from_date):
            raise HTTPException(status_code=422, detail="Invalid 'from' date format")
        try:
            f_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'from' date value")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", to_date):
            raise HTTPException(status_code=422, detail="Invalid 'to' date format")
        try:
            t_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'to' date value")
        if t_dt < f_dt:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (t_dt - f_dt).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 calendar days inclusive")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(res))
        }

    else:
        raise HTTPException(status_code=422, detail=f"Unsupported endpoint: {endpoint}")
