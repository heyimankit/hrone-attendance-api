"""
Comprehensive Automated Test Suite for HROne Assignment.
Exercises all 17 required areas against a clean test database.
"""
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.getcwd())

from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Point test suite to a dedicated clean test database
os.environ["MONGO_DB"] = "hrone_test_db"

import app.main as main_module
from app.main import app, db, client, IST, init_db

test_client = TestClient(app)

class TestHROneAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Clean test database
        db.employees.drop()
        db.attendance_logs.drop()
        init_db()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        init_db()

    # 1. Health check
    def test_01_health_check(self):
        res = test_client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

        # Test failure when MongoDB ping fails
        with patch.object(main_module, "client") as mock_client:
            mock_client.admin.command.side_effect = Exception("Connection lost")
            res_fail = test_client.get("/health")
            self.assertEqual(res_fail.status_code, 503)

    # 2. Employee creation and validations
    def test_02_employee_creation(self):
        emp_payload = {
            "emp_code": "EMP0101",
            "name": "Arjun Sharma",
            "email": "arjun@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-15"
        }
        res = test_client.post("/employees", json=emp_payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP0101")
        self.assertNotIn("_id", data)
        self.assertIsInstance(data["created_at"], int)

        # Duplicate emp_code
        res_dup = test_client.post("/employees", json=emp_payload)
        self.assertEqual(res_dup.status_code, 409)

        # Invalid emp_code
        bad_emp = dict(emp_payload, emp_code="INVALID")
        self.assertEqual(test_client.post("/employees", json=bad_emp).status_code, 422)

        # Invalid email
        bad_email = dict(emp_payload, emp_code="EMP0102", email="notanemail")
        self.assertEqual(test_client.post("/employees", json=bad_email).status_code, 422)

        # Equal shift_start and shift_end
        equal_shifts = dict(emp_payload, emp_code="EMP0103", shift_start="10:00", shift_end="10:00")
        self.assertEqual(test_client.post("/employees", json=equal_shifts).status_code, 422)

        # Invalid joined_on date
        bad_date = dict(emp_payload, emp_code="EMP0104", joined_on="2026-02-30")
        self.assertEqual(test_client.post("/employees", json=bad_date).status_code, 422)

    # 3. Employee listing
    def test_03_employee_listing(self):
        # Create 3 employees
        test_client.post("/employees", json={
            "emp_code": "EMP0002", "name": "B", "email": "b@ex.com",
            "department": "Sales", "joined_on": "2026-01-01"
        })
        test_client.post("/employees", json={
            "emp_code": "EMP0001", "name": "A", "email": "a@ex.com",
            "department": "Engineering", "joined_on": "2026-01-01"
        })
        test_client.post("/employees", json={
            "emp_code": "EMP0003", "name": "C", "email": "c@ex.com",
            "department": "Engineering", "joined_on": "2026-01-01"
        })

        # List all sorted
        res = test_client.get("/employees")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([x["emp_code"] for x in body["items"]], ["EMP0001", "EMP0002", "EMP0003"])

        # Filter department
        res_dept = test_client.get("/employees?department=Engineering")
        self.assertEqual(res_dept.json()["total"], 2)
        self.assertEqual([x["emp_code"] for x in res_dept.json()["items"]], ["EMP0001", "EMP0003"])

        # Pagination bounds
        self.assertEqual(test_client.get("/employees?page=0").status_code, 422)
        self.assertEqual(test_client.get("/employees?page_size=101").status_code, 422)

    # 4. Punch-in
    def test_04_punch_in(self):
        # Normal shift emp
        test_client.post("/employees", json={
            "emp_code": "EMP0010", "name": "Day Shift", "email": "day@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        # Overnight shift emp (22:00 to 06:00)
        test_client.post("/employees", json={
            "emp_code": "EMP0020", "name": "Night Shift", "email": "night@ex.com",
            "department": "Support", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"
        })

        # 4a. Normal punch in at 2026-07-06 09:28:00 IST (03:58:00 UTC)
        # 2026-07-06 03:58:00 UTC in ms = 1783310280000
        res = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP0010",
            "punched_at": 1783310280000,
            "status": "PRESENT"
        })
        self.assertEqual(res.status_code, 201)
        doc = res.json()
        self.assertEqual(doc["date"], "2026-07-06")
        self.assertEqual(doc["late_minutes"], 0)
        self.assertIsNone(doc["work_hours"])
        self.assertFalse(doc["half_day"])
        self.assertNotIn("id", doc)
        self.assertNotIn("_id", doc)

        # 4b. Overnight punch-in at 2026-07-07 00:30:00 IST (earlier than 06:00 shift_end)
        # 2026-07-06 19:00:00 UTC in ms = 1783364400000
        res_night = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP0020",
            "punched_at": 1783364400000,
            "status": "PRESENT"
        })
        self.assertEqual(res_night.status_code, 201)
        # Belongs to previous day (2026-07-06) per R1!
        self.assertEqual(res_night.json()["date"], "2026-07-06")

        # 4c. Unknown employee -> 404
        self.assertEqual(test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code, 404)

        # 4d. Invalid status -> 422
        self.assertEqual(test_client.post("/attendance/punch-in", json={"emp_code": "EMP0010", "status": "ABSENT"}).status_code, 422)

        # 4e. Invalid timestamp (too small, looks like seconds) -> 422
        self.assertEqual(test_client.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": 1783310280}).status_code, 422)

    # 5. Punch-in concurrency
    def test_05_punch_in_concurrency(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0050", "name": "Race Emp", "email": "race@ex.com",
            "department": "Eng", "joined_on": "2026-01-01"
        })
        payload = {"emp_code": "EMP0050", "punched_at": 1783310280000}

        def do_punch():
            return test_client.post("/attendance/punch-in", json=payload).status_code

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(lambda _: do_punch(), range(5)))

        self.assertEqual(results.count(201), 1)
        self.assertEqual(results.count(409), 4)

    # 6. Late minutes boundaries (R2)
    def test_06_late_minutes_boundaries(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0060", "name": "Late Tester", "email": "late@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        # Date 2026-07-06. Shift start: 09:30 IST = 04:00 UTC = 1783310400000 ms
        base_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)

        # 09:40:00 IST (+600 sec) -> not strictly more than 10 min -> late = 0
        t1 = int((base_utc + timedelta(seconds=600)).timestamp() * 1000)
        r1 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0060", "punched_at": t1}).json()
        self.assertEqual(r1["late_minutes"], 0)

        # Next day: 2026-07-07. 09:40:01 IST (+601 sec) -> late = 10
        base_utc_2 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
        t2 = int((base_utc_2 + timedelta(seconds=601)).timestamp() * 1000)
        r2 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0060", "punched_at": t2}).json()
        self.assertEqual(r2["late_minutes"], 10)

        # Next day: 2026-07-08. 10:15:59 IST (+45m 59s = +2759 sec) -> late = 45
        base_utc_3 = datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc)
        t3 = int((base_utc_3 + timedelta(seconds=2759)).timestamp() * 1000)
        r3 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0060", "punched_at": t3}).json()
        self.assertEqual(r3["late_minutes"], 45)

    # 6b. Attendance listing validations
    def test_06b_attendance_listing_validations(self):
        # date_from > date_to must return HTTP 422
        res_bad_range = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
        self.assertEqual(res_bad_range.status_code, 422)

        # Invalid date format must return HTTP 422
        res_bad_format = test_client.get("/attendance?date_from=not-a-date")
        self.assertEqual(res_bad_format.status_code, 422)

    # 7. Overtime boundaries (R3) & 8. Work hours and half-day (R4, R5)
    def test_07_and_08_punch_out_overtime_and_work_hours(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0070", "name": "OT Tester", "email": "ot@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        # Punch in at 09:30 IST on 2026-07-06 (04:00 UTC)
        pin_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0070", "punched_at": int(pin_dt.timestamp() * 1000)})

        # Shift end is 18:30 IST (13:00 UTC)
        # 7a. Punch out at 18:59:59 IST (29m 59s overtime < 30) -> overtime = 0
        pout_dt_under = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
        # Note: punch-out closes the record. Let's test with a clone employee for exact 30 min
        res_out = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0070", "punched_at": int(pout_dt_under.timestamp() * 1000)})
        self.assertEqual(res_out.status_code, 200)
        data = res_out.json()
        self.assertEqual(data["overtime_minutes"], 0)
        self.assertEqual(data["half_day"], False)
        # Work hours: 4:00 to 13:29:59 = 34199 seconds / 3600 = 9.4997... -> 9.50
        self.assertEqual(data["work_hours"], 9.50)

        # 7b. Test half-day boundary (< 4.50 is half day, >= 4.50 is full day)
        test_client.post("/employees", json={
            "emp_code": "EMP0071", "name": "Half Day", "email": "hd@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        # In at 04:00 UTC
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0071", "punched_at": int(pin_dt.timestamp() * 1000)})
        # Out at 08:29:59 UTC (4h 29m 59s = 4.50 hours? 16199s / 3600 = 4.4997 -> 4.50)
        # Exactly 4.49 hours = 16164 seconds -> out at 04:00 + 16164s
        pout_449 = pin_dt + timedelta(seconds=16164)
        r_hd = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0071", "punched_at": int(pout_449.timestamp() * 1000)}).json()
        self.assertEqual(r_hd["work_hours"], 4.49)
        self.assertTrue(r_hd["half_day"])

        # 7c. Exactly 30 minutes overtime -> overtime = 30
        test_client.post("/employees", json={
            "emp_code": "EMP0072", "name": "OT 30", "email": "ot30@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0072", "punched_at": int(pin_dt.timestamp() * 1000)})
        pout_ot30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
        r_ot30 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0072", "punched_at": int(pout_ot30.timestamp() * 1000)}).json()
        self.assertEqual(r_ot30["overtime_minutes"], 30)

    # 9. Punch-out across midnight, double punch-out, and > 24 hours
    def test_09_punch_out_validation_and_concurrency(self):
        # Overnight shift (22:00 to 06:00)
        test_client.post("/employees", json={
            "emp_code": "EMP0090", "name": "Night Out", "email": "nightout@ex.com",
            "department": "Support", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"
        })
        # In at 2026-07-06 21:55 IST = 16:25 UTC
        pin = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0090", "punched_at": int(pin.timestamp() * 1000)})

        # Out at 2026-07-07 06:40 IST = 01:10 UTC (next day)
        pout = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
        res = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0090", "punched_at": int(pout.timestamp() * 1000)})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["work_hours"], 8.75)
        self.assertEqual(data["overtime_minutes"], 40)

        # Attempt second punch-out -> 409
        self.assertEqual(test_client.post("/attendance/punch-out", json={"emp_code": "EMP0090", "punched_at": int(pout.timestamp() * 1000)}).status_code, 409)

        # Punch out > 24 hours after punch-in -> 422
        test_client.post("/employees", json={
            "emp_code": "EMP0091", "name": "Late Out", "email": "lateout@ex.com",
            "department": "Support", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        pin2 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0091", "punched_at": int(pin2.timestamp() * 1000)})
        pout_too_late = pin2 + timedelta(hours=24, seconds=1)
        self.assertEqual(test_client.post("/attendance/punch-out", json={"emp_code": "EMP0091", "punched_at": int(pout_too_late.timestamp() * 1000)}).status_code, 422)

    # 10. Manual Attendance Correction (PATCH)
    def test_10_regularize_attendance(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0100", "name": "Regularize Tester", "email": "reg@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        # Punch in late: 10:05 IST (04:35 UTC), out 18:30 IST (13:00 UTC)
        pin = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
        pout = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0100", "punched_at": int(pin.timestamp() * 1000)})
        test_client.post("/attendance/punch-out", json={"emp_code": "EMP0100", "punched_at": int(pout.timestamp() * 1000)})

        # Correct punch-in to 09:28 IST (03:58 UTC)
        new_pin = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
        res = test_client.patch("/attendance/EMP0100/2026-07-07", json={
            "reason": "Biometric machine was offline",
            "regularized_by": "HR Manager",
            "punch_in": int(new_pin.timestamp() * 1000)
        })
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["work_hours"], 9.03)
        self.assertEqual(len(body["history"]), 1)
        hist = body["history"][0]
        self.assertEqual(hist["by"], "HR Manager")
        self.assertEqual(hist["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertIn("punch_in", hist["changes"])

        # Request changing nothing -> 422
        res_nothing = test_client.patch("/attendance/EMP0100/2026-07-07", json={
            "reason": "Trying same thing again",
            "regularized_by": "HR Manager",
            "punch_in": int(new_pin.timestamp() * 1000)
        })
        self.assertEqual(res_nothing.status_code, 422)

        # Setting status ABSENT clears punch times and derived fields
        res_abs = test_client.patch("/attendance/EMP0100/2026-07-07", json={
            "reason": "Actually on unauthorized leave",
            "regularized_by": "HR Admin",
            "status": "ABSENT"
        })
        self.assertEqual(res_abs.status_code, 200)
        body_abs = res_abs.json()
        self.assertEqual(body_abs["status"], "ABSENT")
        self.assertIsNone(body_abs["punch_in"])
        self.assertIsNone(body_abs["punch_out"])
        self.assertIsNone(body_abs["work_hours"])
        self.assertEqual(len(body_abs["history"]), 2)

        # Setting ABSENT while supplying punch times -> 422
        res_bad_abs = test_client.patch("/attendance/EMP0100/2026-07-07", json={
            "reason": "Invalid combination test",
            "regularized_by": "HR Admin",
            "status": "ABSENT",
            "punch_in": int(new_pin.timestamp() * 1000)
        })
        self.assertEqual(res_bad_abs.status_code, 422)

        # Corrected punch-in belonging to a different attendance date -> 422
        # Record is for 2026-07-07; punch-in for 2026-07-08 (04:00 UTC = 09:30 IST July 8th) must fail with 422
        wrong_date_pin = datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc)
        res_wrong_date = test_client.patch("/attendance/EMP0100/2026-07-07", json={
            "reason": "Accidentally sent next day punch",
            "regularized_by": "HR Admin",
            "punch_in": int(wrong_date_pin.timestamp() * 1000)
        })
        self.assertEqual(res_wrong_date.status_code, 422)

    # 11. Concurrent regularizations OCC test
    def test_11_concurrent_regularizations(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0110", "name": "OCC Emp", "email": "occ@ex.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })
        pin = datetime(2026, 7, 7, 4, 30, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP0110", "punched_at": int(pin.timestamp() * 1000)})

        p1 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
        p2 = datetime(2026, 7, 7, 4, 5, 0, tzinfo=timezone.utc)

        def reg(p_val, by_name):
            return test_client.patch("/attendance/EMP0110/2026-07-07", json={
                "reason": "Concurrent update test",
                "regularized_by": by_name,
                "punch_in": int(p_val.timestamp() * 1000)
            }).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(reg, p1, "HR1")
            fut2 = executor.submit(reg, p2, "HR2")
            results = [fut1.result(), fut2.result()]

        # One must succeed (200), loser receives 409
        self.assertIn(200, results)

    # 12. Employee Monthly Analytics
    def test_12_employee_monthly_analytics(self):
        # Joined mid-month: 2026-07-13 (Monday)
        test_client.post("/employees", json={
            "emp_code": "EMP0120", "name": "Mid Month", "email": "mid@ex.com",
            "department": "Eng", "joined_on": "2026-07-13"
        })
        # Mon-Fri from 2026-07-13 to 2026-07-31 is 15 weekdays!
        # Add 1 full day (PRESENT), 1 half day (WFH), 1 LEAVE, 1 weekend record (Saturday)
        db.attendance_logs.insert_many([
            # Mon 2026-07-13: Full day present
            {"emp_code": "EMP0120", "date": "2026-07-13", "status": "PRESENT", "late_minutes": 10, "overtime_minutes": 0, "half_day": False},
            # Tue 2026-07-14: Half day WFH
            {"emp_code": "EMP0120", "date": "2026-07-14", "status": "WFH", "late_minutes": 0, "overtime_minutes": 30, "half_day": True},
            # Wed 2026-07-15: LEAVE
            {"emp_code": "EMP0120", "date": "2026-07-15", "status": "LEAVE", "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
            # Sat 2026-07-18: Weekend work! (Doesn't add to present_days, but adds to overtime)
            {"emp_code": "EMP0120", "date": "2026-07-18", "status": "PRESENT", "late_minutes": 0, "overtime_minutes": 60, "half_day": False},
        ])

        res = test_client.get("/analytics/employees/EMP0120/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["working_days"], 15)
        # present_days: 1.0 (full) + 0.5 (half) = 1.5. Weekend not counted.
        self.assertEqual(body["present_days"], 1.5)
        self.assertEqual(body["leave_days"], 1)
        self.assertEqual(body["late_count"], 1)
        self.assertEqual(body["total_late_minutes"], 10)
        # overtime: 30 + 60 = 90
        self.assertEqual(body["total_overtime_minutes"], 90)
        # attendance_pct: 1.5 / 15 * 100 = 10.0%
        self.assertEqual(body["attendance_pct"], 10.0)

    # 13. Department Monthly Summary
    def test_13_department_summary(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0131", "name": "E1", "email": "e1@ex.com",
            "department": "DeptA", "joined_on": "2026-01-01"
        })
        # Employee with zero logs!
        test_client.post("/employees", json={
            "emp_code": "EMP0132", "name": "E2", "email": "e2@ex.com",
            "department": "DeptA", "joined_on": "2026-01-01"
        })
        # Employee who joined next month -> should be excluded!
        test_client.post("/employees", json={
            "emp_code": "EMP0133", "name": "E3", "email": "e3@ex.com",
            "department": "DeptA", "joined_on": "2026-08-01"
        })
        # Department with 0 eligible headcount -> omitted!
        test_client.post("/employees", json={
            "emp_code": "EMP0134", "name": "E4", "email": "e4@ex.com",
            "department": "DeptB", "joined_on": "2026-08-01"
        })

        db.attendance_logs.insert_one({
            "emp_code": "EMP0131", "date": "2026-07-06", "status": "PRESENT",
            "work_hours": 8.0, "late_minutes": 15, "overtime_minutes": 0, "half_day": False
        })

        res = test_client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 1)
        dept_a = items[0]
        self.assertEqual(dept_a["department"], "DeptA")
        self.assertEqual(dept_a["headcount"], 2)
        self.assertEqual(dept_a["present_days"], 1.0)
        self.assertEqual(dept_a["avg_work_hours"], 8.0)
        self.assertEqual(dept_a["late_count"], 1)

        # Department filter check
        res_filtered = test_client.get("/analytics/departments/summary?month=2026-07&department=DeptA")
        self.assertEqual(res_filtered.status_code, 200)
        self.assertEqual(len(res_filtered.json()["items"]), 1)
        self.assertEqual(res_filtered.json()["items"][0]["department"], "DeptA")

        res_none = test_client.get("/analytics/departments/summary?month=2026-07&department=NonExistent")
        self.assertEqual(res_none.status_code, 200)
        self.assertEqual(len(res_none.json()["items"]), 0)

    # 14. Late Leaderboard
    def test_14_late_leaderboard(self):
        test_client.post("/employees", json={"emp_code": "EMP0141", "name": "Alice", "email": "a1@ex.com", "department": "Eng", "joined_on": "2026-01-01"})
        test_client.post("/employees", json={"emp_code": "EMP0142", "name": "Bob", "email": "b1@ex.com", "department": "Eng", "joined_on": "2026-01-01"})
        test_client.post("/employees", json={"emp_code": "EMP0143", "name": "Charlie", "email": "c1@ex.com", "department": "Eng", "joined_on": "2026-01-01"})
        test_client.post("/employees", json={"emp_code": "EMP0144", "name": "David", "email": "d1@ex.com", "department": "Eng", "joined_on": "2026-01-01"})
        test_client.post("/employees", json={"emp_code": "EMP0145", "name": "Eve", "email": "e1@ex.com", "department": "Sales", "joined_on": "2026-01-01"})

        db.attendance_logs.insert_many([
            {"emp_code": "EMP0141", "date": "2026-07-01", "late_minutes": 45},
            {"emp_code": "EMP0142", "date": "2026-07-01", "late_minutes": 30},
            {"emp_code": "EMP0143", "date": "2026-07-01", "late_minutes": 30},
            {"emp_code": "EMP0144", "date": "2026-07-01", "late_minutes": 15},
            {"emp_code": "EMP0145", "date": "2026-07-01", "late_minutes": 60},
        ])

        # Limit = 2 without department filter. Eve is 1 (60), Alice is 2 (45).
        res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[0]["emp_code"], "EMP0145")
        self.assertEqual(items[1]["rank"], 2)
        self.assertEqual(items[1]["emp_code"], "EMP0141")

        # Department filter = Eng. Alice is 1, Bob and Charlie tie at 2. Since rank <= limit, all 3 are returned!
        res_eng = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2&department=Eng")
        self.assertEqual(res_eng.status_code, 200)
        items_eng = res_eng.json()["items"]
        self.assertEqual(len(items_eng), 3)
        self.assertEqual(items_eng[0]["rank"], 1)
        self.assertEqual(items_eng[0]["emp_code"], "EMP0141")
        self.assertEqual(items_eng[1]["rank"], 2)
        self.assertEqual(items_eng[2]["rank"], 2)

        # Department filter = Sales. Eve is 1.
        res_sales = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2&department=Sales")
        self.assertEqual(res_sales.status_code, 200)
        items_sales = res_sales.json()["items"]
        self.assertEqual(len(items_sales), 1)
        self.assertEqual(items_sales[0]["rank"], 1)
        self.assertEqual(items_sales[0]["emp_code"], "EMP0145")

    # 15. Department Trend
    def test_15_department_trend(self):
        test_client.post("/employees", json={
            "emp_code": "EMP0151", "name": "Trend1", "email": "t1@ex.com",
            "department": "Product", "joined_on": "2026-07-01"
        })
        # Wed 2026-07-01: 1 present
        db.attendance_logs.insert_one({
            "emp_code": "EMP0151", "date": "2026-07-01", "status": "PRESENT",
            "late_minutes": 0, "half_day": False
        })

        # Range 2026-07-01 to 2026-07-05 (5 days)
        res = test_client.get("/analytics/departments/Product/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["date"], "2026-07-01")
        self.assertTrue(items[0]["is_working_day"])
        self.assertEqual(items[0]["attendance_rate"], 1.0)
        # 2026-07-04 is Saturday
        self.assertFalse(items[3]["is_working_day"])
        self.assertIsNone(items[3]["attendance_rate"])

        # > 92 days -> 422
        res_long = test_client.get("/analytics/departments/Product/trend?from=2026-01-01&to=2026-04-10")
        self.assertEqual(res_long.status_code, 422)

        # to < from -> 422
        res_rev = test_client.get("/analytics/departments/Product/trend?from=2026-07-05&to=2026-07-01")
        self.assertEqual(res_rev.status_code, 422)

        # Unknown department -> 404
        res_404 = test_client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(res_404.status_code, 404)

    # 16. Explain endpoint
    def test_16_explain_endpoint(self):
        # attendance_list
        r1 = test_client.get("/admin/explain/attendance_list?emp_code=EMP0001")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("executionStats", r1.json()["explain"])

        # employee_monthly
        r2 = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("executionStats", r2.json()["explain"])

        # department_summary
        r3 = test_client.get("/admin/explain/department_summary?month=2026-07")
        self.assertEqual(r3.status_code, 200)

        # late_leaderboard
        r4 = test_client.get("/admin/explain/late_leaderboard?month=2026-07")
        self.assertEqual(r4.status_code, 200)

        # department_trend
        r5 = test_client.get("/admin/explain/department_trend?department=Product&from=2026-07-01&to=2026-07-05")
        self.assertEqual(r5.status_code, 200)

        # Missing required parameter -> 422
        r_fail = test_client.get("/admin/explain/employee_monthly")
        self.assertEqual(r_fail.status_code, 422)

if __name__ == "__main__":
    unittest.main()
