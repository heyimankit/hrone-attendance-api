"""
Scale and Performance Test with ~100,000 attendance records.
Verifies that queries use IXSCAN and avoid COLLSCAN, and that pagination and aggregations stay fast.
"""
from datetime import datetime, timezone, timedelta, time as dt_time
import random
import time
from pymongo import MongoClient

db = MongoClient("mongodb://localhost:27017")["hrone_perf_test"]
db.employees.drop()
db.attendance_logs.drop()

import os
import sys
sys.path.insert(0, os.getcwd())
import app.main as main_module
# Create indexes
db.employees.create_index([("emp_code", 1)], unique=True)
db.employees.create_index([("department", 1), ("emp_code", 1)])
db.employees.create_index([("department", 1), ("joined_on", 1)])

db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])

print("Creating 1,000 employees...")
departments = ["Engineering", "Sales", "Support", "HR", "Marketing"]
employees = []
for i in range(1, 1001):
    emp_code = f"EMP{i:04d}"
    dept = departments[i % len(departments)]
    employees.append({
        "emp_code": emp_code,
        "name": f"Employee {i}",
        "email": f"emp{i}@example.com",
        "department": dept,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)
    })
db.employees.insert_many(employees)
print(f"Created {len(employees)} employees.")

print("Generating 100,000 attendance records...")
# 100 days across 1,000 employees = 100,000 records
base_date = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
dates = [(base_date + timedelta(days=d)).isoformat() for d in range(100)]

batch = []
total_records = 0
start_gen = time.time()

for emp in employees:
    emp_c = emp["emp_code"]
    for d_str in dates:
        batch.append({
            "emp_code": emp_c,
            "date": d_str,
            "status": "PRESENT" if random.random() > 0.1 else "LEAVE",
            "punch_in": datetime.combine(datetime.fromisoformat(d_str).date(), dt_time(4, 0, 0), tzinfo=timezone.utc),
            "punch_out": datetime.combine(datetime.fromisoformat(d_str).date(), dt_time(13, 0, 0), tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 15 if random.random() > 0.8 else 0,
            "overtime_minutes": 30 if random.random() > 0.8 else 0,
            "half_day": False,
            "history": []
        })
        if len(batch) >= 10000:
            db.attendance_logs.insert_many(batch)
            total_records += len(batch)
            batch = []

if batch:
    db.attendance_logs.insert_many(batch)
    total_records += len(batch)

print(f"Inserted {total_records} attendance records in {time.time() - start_gen:.2f}s.")

# Test 1: Attendance listing query execution plan
print("\n--- Testing Attendance List Plan ---")
cmd_list = {
    "find": "attendance_logs",
    "filter": {"emp_code": "EMP0500"},
    "sort": {"date": -1, "emp_code": 1},
    "skip": 0,
    "limit": 20
}
explain_list = db.command("explain", cmd_list, verbosity="executionStats")
stats = explain_list.get("executionStats", {})
print(f"List Query executionTimeMillis: {stats.get('executionTimeMillis')} ms")
print(f"totalDocsExamined: {stats.get('totalDocsExamined')}, nReturned: {stats.get('nReturned')}")

# Check for IXSCAN
planner = explain_list.get("queryPlanner", {})
winning_plan_str = str(planner.get("winningPlan", {}))
print(f"Uses IXSCAN: {'IXSCAN' in winning_plan_str}")
print(f"Avoids COLLSCAN: {'COLLSCAN' not in winning_plan_str}")

# Test 2: Monthly aggregation pipeline
print("\n--- Testing Employee Monthly Pipeline ---")
t0 = time.time()
pipeline_monthly = [
    {"$match": {"emp_code": "EMP0500", "date": {"$gte": "2026-02-01", "$lte": "2026-02-28"}}},
    {"$group": {"_id": None, "count": {"$sum": 1}, "late": {"$sum": "$late_minutes"}}}
]
res_monthly = list(db.attendance_logs.aggregate(pipeline_monthly))
print(f"Monthly agg took: {(time.time() - t0)*1000:.2f} ms. Result: {res_monthly}")

explain_monthly = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline_monthly, "cursor": {}}, verbosity="executionStats")
plan_str = str(explain_monthly)
print(f"Monthly pipeline uses IXSCAN: {'IXSCAN' in plan_str}")

print("\n--- Scale test completed successfully! ---")
