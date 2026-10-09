from datetime import datetime, timezone
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture(autouse=True)
def mock_db():
    mock_client = mongomock.MongoClient(tz_aware=True)
    mock_database = mock_client["attendance_db"]
    orig_client = main_module.client
    orig_db = main_module.db
    main_module.client = mock_client
    main_module.db = mock_database

    main_module.ensure_indexes(mock_database)

    yield mock_database

    main_module.client = orig_client
    main_module.db = orig_db


def test_dept_summary_invalid_month_422():
    client = TestClient(app)
    resp = client.get("/analytics/departments/summary?month=2026-13")
    assert resp.status_code == 422


def test_dept_summary_headcount_includes_zero_logs_and_excludes_future():
    client = TestClient(app)
    # EMP0001: joined Jan 2026, has logs
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "User 1",
            "email": "user1@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    # EMP0002: joined July 15, 2026, zero logs
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "User 2",
            "email": "user2@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-15",
        },
    )
    # EMP0003: future joiner August 2026
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "User 3",
            "email": "user3@example.com",
            "department": "Engineering",
            "joined_on": "2026-08-01",
        },
    )

    # 1 log for EMP0001 in July
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Engineering"
    # EMP0001 + EMP0002 = 2; EMP0003 is excluded
    assert item["headcount"] == 2
    assert item["present_days"] == 1.0


def test_dept_summary_omits_zero_headcount_departments():
    client = TestClient(app)
    # Marketing employee joins next month: August 2026
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0004",
            "name": "Future Marketing",
            "email": "mkt@example.com",
            "department": "Marketing",
            "joined_on": "2026-08-15",
        },
    )

    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # Zero headcount departments are omitted
    assert data["items"] == []


def test_dept_summary_sorting_ascending():
    client = TestClient(app)
    depts = ["Sales", "Engineering", "Customer Support"]
    for i, d in enumerate(depts):
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP001{i}",
                "name": f"User {d}",
                "email": f"user{i}@example.com",
                "department": d,
                "joined_on": "2026-01-01",
            },
        )

    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    dept_names = [it["department"] for it in items]
    assert dept_names == ["Customer Support", "Engineering", "Sales"]


def test_dept_summary_filter_by_department():
    client = TestClient(app)
    depts = ["Sales", "Engineering"]
    for i, d in enumerate(depts):
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP002{i}",
                "name": f"User {d}",
                "email": f"user2{i}@example.com",
                "department": d,
                "joined_on": "2026-01-01",
            },
        )

    resp = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Engineering"

    # Non-existent department filter
    resp_empty = client.get("/analytics/departments/summary?month=2026-07&department=NonExistent")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["items"] == []


def test_dept_summary_metrics_calculations():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0030",
            "name": "Dev 1",
            "email": "dev1@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0031",
            "name": "Dev 2",
            "email": "dev2@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # Dev 1 logs:
    # 2026-07-06 (Mon): PRESENT, 9.0h, late 10 min
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0030",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 10,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-07 (Tue): ON_DUTY, 8.0h, late 0 min
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0030",
        "date": "2026-07-07",
        "status": "ON_DUTY",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-11 (Sat, weekend): PRESENT, 7.0h, late 15 min -> weekend does not count present_days, but late adds up
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0030",
        "date": "2026-07-11",
        "status": "PRESENT",
        "work_hours": 7.0,
        "late_minutes": 15,
        "overtime_minutes": 0,
        "half_day": False,
    })

    # Dev 2 logs:
    # 2026-07-08 (Wed): LEAVE -> counts to leave_count
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0031",
        "date": "2026-07-08",
        "status": "LEAVE",
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-09 (Thu): WFH, half_day: True, 3.5h
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0031",
        "date": "2026-07-09",
        "status": "WFH",
        "work_hours": 3.5,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": True,
    })

    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["headcount"] == 2
    # present_days: Dev 1 Mon (1.0) + Dev 1 Tue (1.0) + Dev 2 Thu half day (0.5) = 2.5
    assert item["present_days"] == 2.5
    # on_duty_count: 1 (Dev 1 Tue)
    assert item["on_duty_count"] == 1
    # leave_count: 1 (Dev 2 Wed)
    assert item["leave_count"] == 1
    # late_count: 2 (Dev 1 Mon 10 min + Dev 1 Sat 15 min)
    assert item["late_count"] == 2
    assert item["total_late_minutes"] == 25
    # avg_work_hours: mean of qualifying present-status records with non-null work_hours
    # Records: 9.0, 8.0, 7.0, 3.5 -> sum = 27.5, count = 4 -> mean = 6.875 -> half-up 6.88
    assert item["avg_work_hours"] == 6.88


def test_dept_summary_avg_work_hours_null():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0040",
            "name": "Only Leave",
            "email": "leaveonly@example.com",
            "department": "HR",
            "joined_on": "2026-01-01",
        },
    )
    # Only a LEAVE record (work_hours is null)
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0040",
        "date": "2026-07-06",
        "status": "LEAVE",
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["department"] == "HR"
    assert item["headcount"] == 1
    assert item["present_days"] == 0.0
    assert item["avg_work_hours"] is None
