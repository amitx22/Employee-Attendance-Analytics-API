import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import pymongo

from app.main import app, IST, instant_to_epoch_ms

client = TestClient(app)

@pytest.fixture(autouse=True, scope="function")
def clean_test_data():
    mongo_client = pymongo.MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    db = mongo_client[os.environ.get("MONGO_DB", "attendance_db")]
    db.employees.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    yield
    db.employees.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP[789]"}})

def test_health():
    with TestClient(app) as tc:
        response = tc.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

def test_create_and_list_employees():
    with TestClient(app) as tc:
        # Invalid email
        res = tc.post("/employees", json={
            "emp_code": "EMP9991",
            "name": "Test User",
            "email": "invalid_email",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Shift start == Shift end -> 422
        res = tc.post("/employees", json={
            "emp_code": "EMP9991",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Valid create
        res = tc.post("/employees", json={
            "emp_code": "EMP9991",
            "name": "Test User",
            "email": "test@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 201
        data = res.json()
        assert data["emp_code"] == "EMP9991"
        assert isinstance(data["created_at"], int)
        assert "_id" not in data

        # Duplicate create -> 409
        res_dup = tc.post("/employees", json={
            "emp_code": "EMP9991",
            "name": "Duplicate User",
            "email": "dup@example.com",
            "department": "QA",
            "joined_on": "2026-01-01"
        })
        assert res_dup.status_code == 409

        # List employees
        res_list = tc.get("/employees?department=QA")
        assert res_list.status_code == 200
        l_data = res_list.json()
        assert l_data["total"] == 1
        assert len(l_data["items"]) == 1
        assert l_data["items"][0]["emp_code"] == "EMP9991"

def test_punch_in_rules():
    with TestClient(app) as tc:
        # Create an employee with 09:30 - 18:30 shift
        tc.post("/employees", json={
            "emp_code": "EMP9001",
            "name": "Punctual User",
            "email": "punctual@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })

        # Punch in at 09:40:00 IST on 2026-07-20 -> Exactly 10 minutes grace -> late_minutes = 0
        # 2026-07-20 09:40:00 IST = 2026-07-20 04:10:00 UTC
        dt_grace = datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc)
        ms_grace = int(dt_grace.timestamp() * 1000)

        res = tc.post("/attendance/punch-in", json={
            "emp_code": "EMP9001",
            "punched_at": ms_grace
        })
        assert res.status_code == 201
        d = res.json()
        assert d["date"] == "2026-07-20"
        assert d["late_minutes"] == 0
        assert d["status"] == "PRESENT"
        assert d["punch_out"] is None

        # Duplicate punch-in for same date -> 409
        res_dup = tc.post("/attendance/punch-in", json={
            "emp_code": "EMP9001",
            "punched_at": ms_grace + 10000
        })
        assert res_dup.status_code == 409

        # Employee 2: late by 1 second (09:40:01 IST) -> late_minutes = 10
        tc.post("/employees", json={
            "emp_code": "EMP9002",
            "name": "Late User",
            "email": "late@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        dt_late = datetime(2026, 7, 20, 4, 10, 1, tzinfo=timezone.utc)
        ms_late = int(dt_late.timestamp() * 1000)
        res_late = tc.post("/attendance/punch-in", json={
            "emp_code": "EMP9002",
            "punched_at": ms_late
        })
        assert res_late.status_code == 201
        assert res_late.json()["late_minutes"] == 10

def test_punch_out_and_derived_fields():
    with TestClient(app) as tc:
        # Create EMP9001
        tc.post("/employees", json={
            "emp_code": "EMP9001",
            "name": "Punctual User",
            "email": "punctual@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        # Punch in at 09:40:00 IST on 2026-07-20
        dt_in = datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc)
        ms_in = int(dt_in.timestamp() * 1000)
        tc.post("/attendance/punch-in", json={
            "emp_code": "EMP9001",
            "punched_at": ms_in
        })

        # Punch out EMP9001 on 2026-07-20 at 19:15:00 IST (13:45:00 UTC)
        # Shift end was 18:30 IST. 19:15 - 18:30 = 45 min >= 30 min -> overtime = 45
        # Punch in was 09:40:00 IST. Total = 9 hours 35 min = 9.58 hours
        dt_out = datetime(2026, 7, 20, 13, 45, 0, tzinfo=timezone.utc)
        ms_out = int(dt_out.timestamp() * 1000)

        res = tc.post("/attendance/punch-out", json={
            "emp_code": "EMP9001",
            "punched_at": ms_out
        })
        assert res.status_code == 200
        d = res.json()
        assert d["punch_out"] == ms_out
        assert d["overtime_minutes"] == 45
        assert d["work_hours"] == 9.58
        assert d["half_day"] is False

        # Punch out again -> 409
        res_again = tc.post("/attendance/punch-out", json={
            "emp_code": "EMP9001",
            "punched_at": ms_out + 1000
        })
        assert res_again.status_code == 409


def test_overnight_shift():
    with TestClient(app) as tc:
        # Create overnight employee: 22:00 to 06:00
        tc.post("/employees", json={
            "emp_code": "EMP8001",
            "name": "Night Owl",
            "email": "night@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-07-01"
        })

        # Punch in at 01:30 IST on 2026-07-22 (2026-07-21 20:00 UTC)
        # Because 01:30 < 06:00 (shift_end), it belongs to 2026-07-21!
        dt_in = datetime(2026, 7, 21, 20, 0, 0, tzinfo=timezone.utc)
        ms_in = int(dt_in.timestamp() * 1000)

        res_in = tc.post("/attendance/punch-in", json={
            "emp_code": "EMP8001",
            "punched_at": ms_in
        })
        assert res_in.status_code == 201
        assert res_in.json()["date"] == "2026-07-21"

        # Punch out at 06:40 IST on 2026-07-22 (2026-07-22 01:10 UTC)
        # Shift end was 06:00 IST on 2026-07-22.
        # Overtime = 40 minutes (06:40 - 06:00)
        dt_out = datetime(2026, 7, 22, 1, 10, 0, tzinfo=timezone.utc)
        ms_out = int(dt_out.timestamp() * 1000)

        res_out = tc.post("/attendance/punch-out", json={
            "emp_code": "EMP8001",
            "punched_at": ms_out
        })
        assert res_out.status_code == 200
        d = res_out.json()
        assert d["date"] == "2026-07-21"
        assert d["overtime_minutes"] == 40
        assert d["work_hours"] == 5.17


def test_regularize_and_history():
    with TestClient(app) as tc:
        # Create EMP9002
        tc.post("/employees", json={
            "emp_code": "EMP9002",
            "name": "Late User",
            "email": "late@example.com",
            "department": "Testing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        dt_late = datetime(2026, 7, 20, 4, 10, 1, tzinfo=timezone.utc)
        ms_late = int(dt_late.timestamp() * 1000)
        tc.post("/attendance/punch-in", json={
            "emp_code": "EMP9002",
            "punched_at": ms_late
        })

        # Regularize EMP9002 on 2026-07-20: change status to WFH
        res = tc.patch("/attendance/EMP9002/2026-07-20", json={
            "status": "WFH",
            "reason": "Authorized work from home",
            "regularized_by": "manager.bob"
        })
        assert res.status_code == 200
        d = res.json()
        assert d["status"] == "WFH"
        assert len(d["history"]) == 1
        h = d["history"][0]
        assert h["by"] == "manager.bob"
        assert h["reason"] == "Authorized work from home"
        assert h["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}

        # Patch with no changes -> 422
        res_no_change = tc.patch("/attendance/EMP9002/2026-07-20", json={
            "status": "WFH",
            "reason": "Duplicate update",
            "regularized_by": "manager.bob"
        })
        assert res_no_change.status_code == 422

def test_explain_endpoints():
    with TestClient(app) as tc:
        endpoints = [
            "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31",
            "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
            "/admin/explain/department_summary?month=2026-07",
            "/admin/explain/late_leaderboard?month=2026-07&limit=5",
            "/admin/explain/department_trend?department=Sales&from=2026-07-01&to=2026-07-10",
        ]
        for url in endpoints:
            res = tc.get(url)
            assert res.status_code == 200, f"Failed explain for {url}: {res.text}"
            data = res.json()
            assert "explain" in data
            assert "endpoint" in data
            assert "collection" in data
