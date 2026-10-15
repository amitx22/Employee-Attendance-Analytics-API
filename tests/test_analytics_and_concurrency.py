import concurrent.futures
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
import pymongo
import os

from app.main import app

@pytest.fixture(autouse=True, scope="module")
def setup_sample_seed():
    # Load sample seed data into attendance_db
    import sample_seed
    mongo_client = pymongo.MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    db = mongo_client[os.environ.get("MONGO_DB", "attendance_db")]
    db.employees.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    yield
    db.employees.delete_many({"emp_code": {"$regex": "^EMP[789]"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP[789]"}})

def test_analytics_employee_monthly():
    with TestClient(app) as tc:
        # EMP0001 in 2026-07: 23 working days, 2 present days (July 6, July 7)
        res = tc.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert res.status_code == 200
        d = res.json()
        assert d["emp_code"] == "EMP0001"
        assert d["working_days"] == 23
        assert d["present_days"] == 2.0
        assert d["leave_days"] == 0
        assert d["late_count"] == 0
        assert d["attendance_pct"] == 8.7

        # EMP0002 joined mid-month: 2026-07-13 -> 15 working days
        res2 = tc.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        assert res2.status_code == 200
        d2 = res2.json()
        assert d2["emp_code"] == "EMP0002"
        assert d2["working_days"] == 15
        assert d2["present_days"] == 1.0

        # Unknown employee -> 404
        res_404 = tc.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert res_404.status_code == 404

def test_analytics_department_summary():
    with TestClient(app) as tc:
        res = tc.get("/analytics/departments/summary?month=2026-07")
        assert res.status_code == 200
        d = res.json()
        assert d["month"] == "2026-07"
        items = {item["department"]: item for item in d["items"]}
        
        # Engineering: EMP0001 & EMP0002 (headcount = 2)
        assert "Engineering" in items
        eng = items["Engineering"]
        assert eng["headcount"] == 2
        assert eng["present_days"] == 3.0
        assert eng["avg_work_hours"] == 9.04
        assert eng["late_count"] == 0

        # Sales: EMP0003 & EMP0004 (headcount = 2)
        assert "Sales" in items
        sales = items["Sales"]
        assert sales["headcount"] == 2
        assert sales["present_days"] == 1.5
        assert sales["avg_work_hours"] == 6.29
        assert sales["late_count"] == 1
        assert sales["total_late_minutes"] == 35
        assert sales["leave_count"] == 1

        # Specific department filter
        res_sales = tc.get("/analytics/departments/summary?month=2026-07&department=Sales")
        assert res_sales.status_code == 200
        assert len(res_sales.json()["items"]) == 1
        assert res_sales.json()["items"][0]["department"] == "Sales"

def test_analytics_late_leaderboard_and_ties():
    with TestClient(app) as tc:
        res = tc.get("/analytics/leaderboard/late?month=2026-07&limit=5")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) >= 1
        top = data["items"][0]
        assert top["rank"] == 1
        assert top["emp_code"] == "EMP0003"
        assert top["total_late_minutes"] == 35

def test_analytics_department_trend():
    with TestClient(app) as tc:
        res = tc.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-07")
        assert res.status_code == 200
        data = res.json()
        assert data["department"] == "Sales"
        items = data["items"]
        assert len(items) == 7

        # 2026-07-01: Wednesday, working day, 0 present, headcount 2 -> attendance_rate = 0.0
        assert items[0]["date"] == "2026-07-01"
        assert items[0]["is_working_day"] is True
        assert items[0]["headcount"] == 2
        assert items[0]["attendance_rate"] == 0.0

        # 2026-07-04: Saturday -> is_working_day is False, attendance_rate is None
        assert items[3]["date"] == "2026-07-04"
        assert items[3]["is_working_day"] is False
        assert items[3]["attendance_rate"] is None

        # 2026-07-06: Monday -> present_count = 1.0, headcount = 2 -> attendance_rate = 0.5
        assert items[5]["date"] == "2026-07-06"
        assert items[5]["is_working_day"] is True
        assert items[5]["present_count"] == 1.0
        assert items[5]["attendance_rate"] == 0.5

        # Unknown department -> 404
        res_404 = tc.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-07")
        assert res_404.status_code == 404

def test_concurrent_punch_in():
    # Test concurrency: simultaneous punch-ins for the same emp_code and date
    mongo_client = pymongo.MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    db = mongo_client[os.environ.get("MONGO_DB", "attendance_db")]
    
    # Create employee for concurrency test
    emp_code = "EMP7001"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })

    # Fixed timestamp for 2026-07-25 09:30:00 IST
    dt = datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    with TestClient(app) as tc:
        def do_punch_in():
            return tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms})

        # Send 10 simultaneous punch-in requests in parallel threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(do_punch_in) for _ in range(10)]
            results = [f.result() for f in futures]

        statuses = [r.status_code for r in results]
        assert statuses.count(201) == 1, f"Expected exactly one 201, got: {statuses}"
        assert statuses.count(409) == 9, f"Expected nine 409s, got: {statuses}"


def test_concurrent_punch_out():
    # Test concurrency: simultaneous punch-outs for the same open record
    mongo_client = pymongo.MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    db = mongo_client[os.environ.get("MONGO_DB", "attendance_db")]
    
    emp_code = "EMP7002"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Race Tester 2",
        "email": "race2@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })

    with TestClient(app) as tc:
        # Punch in first
        dt_in = datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc)
        ms_in = int(dt_in.timestamp() * 1000)
        res_in = tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_in})
        assert res_in.status_code == 201

        # Punch out instant
        dt_out = datetime(2026, 7, 25, 13, 0, 0, tzinfo=timezone.utc)
        ms_out = int(dt_out.timestamp() * 1000)

        def do_punch_out():
            return tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_out})

        # Send 10 simultaneous punch-outs
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(do_punch_out) for _ in range(10)]
            results = [f.result() for f in futures]

        statuses = [r.status_code for r in results]
        assert statuses.count(200) == 1, f"Expected exactly one 200, got: {statuses}"
        assert statuses.count(409) == 9, f"Expected nine 409s, got: {statuses}"
