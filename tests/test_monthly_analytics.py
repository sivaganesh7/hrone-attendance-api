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


def test_monthly_unknown_employee_404():
    client = TestClient(app)
    resp = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Employee not found"


def test_monthly_invalid_month_422():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        },
    )
    # Month 13 is invalid
    resp1 = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
    assert resp1.status_code == 422

    # Malformed string
    resp2 = client.get("/analytics/employees/EMP0001/monthly?month=July2026")
    assert resp2.status_code == 422


def test_monthly_no_log_employee():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        },
    )

    # July 2026 has 23 weekdays (July 1st is Wednesday, July 31st is Friday)
    resp = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 0.0


def test_monthly_mid_month_joiner():
    client = TestClient(app)
    # Joined Monday, July 20, 2026 -> 10 working days remaining in July (July 20-24, July 27-31)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "New Joiner",
            "email": "joiner@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-20",
        },
    )

    # Seed 8 full days and 1 half day (WFH) -> 8.5 present days
    weekdays = [
        "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
        "2026-07-27", "2026-07-28", "2026-07-29"
    ]
    for d in weekdays:
        main_module.db.attendance_logs.insert_one({
            "emp_code": "EMP0002",
            "date": d,
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, int(d.split("-")[2]), 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, int(d.split("-")[2]), 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

    # Half day on 2026-07-30
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-30",
        "status": "WFH",
        "punch_in": datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 30, 7, 30, tzinfo=timezone.utc),
        "work_hours": 3.5,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": True,
    })

    resp = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 10
    assert data["present_days"] == 8.5
    # (8.5 / 10) * 100 = 85.0
    assert data["attendance_pct"] == 85.0


def test_monthly_zero_working_days():
    client = TestClient(app)
    # Joined next month: August 1, 2026
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Future Joiner",
            "email": "future@example.com",
            "department": "Engineering",
            "joined_on": "2026-08-01",
        },
    )

    resp = client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] is None


def test_monthly_weekend_records_and_overtime():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0004",
            "name": "Weekend Worker",
            "email": "weekend@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # Saturday record (2026-07-11): PRESENT, 15 min late, 40 min overtime
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0004",
        "date": "2026-07-11",
        "status": "PRESENT",
        "late_minutes": 15,
        "overtime_minutes": 40,
        "work_hours": 8.0,
        "half_day": False,
    })

    # Monday record (2026-07-13): PRESENT, 0 late, 30 overtime
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0004",
        "date": "2026-07-13",
        "status": "PRESENT",
        "late_minutes": 0,
        "overtime_minutes": 30,
        "work_hours": 8.5,
        "half_day": False,
    })

    resp = client.get("/analytics/employees/EMP0004/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # Weekend record does NOT count towards present_days
    assert data["present_days"] == 1.0
    # Weekend record DOES contribute to late and overtime
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 15
    assert data["total_overtime_minutes"] == 70  # 40 + 30


def test_monthly_leave_and_half_day_and_absent():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Leave Test",
            "email": "leave@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # 2 LEAVE days
    for d in ["2026-07-06", "2026-07-07"]:
        main_module.db.attendance_logs.insert_one({
            "emp_code": "EMP0005",
            "date": d,
            "status": "LEAVE",
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

    # 1 ABSENT day
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0005",
        "date": "2026-07-08",
        "status": "ABSENT",
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    # 1 Half day WFH
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0005",
        "date": "2026-07-09",
        "status": "WFH",
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": True,
    })

    resp = client.get("/analytics/employees/EMP0005/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["leave_days"] == 2
    assert data["present_days"] == 0.5


def test_monthly_r8_rounding():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0006",
            "name": "Rounding Test",
            "email": "round@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # 7 present days in July (23 working days) -> 7 / 23 * 100 = 30.43478... -> 30.43
    for day in range(6, 13):  # 7 days
        d_str = f"2026-07-{day:02d}"
        d_obj = datetime(2026, 7, day).date()
        if d_obj.weekday() < 5:
            main_module.db.attendance_logs.insert_one({
                "emp_code": "EMP0006",
                "date": d_str,
                "status": "PRESENT",
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            })

    # July 6 to 10 are 5 weekdays. Let's add 2 more weekdays on July 13, 14
    for day in [13, 14]:
        d_str = f"2026-07-{day:02d}"
        main_module.db.attendance_logs.insert_one({
            "emp_code": "EMP0006",
            "date": d_str,
            "status": "PRESENT",
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

    resp = client.get("/analytics/employees/EMP0006/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 7.0
    # 7 / 23 * 100 = 30.434782... rounded half-up is 30.43
    assert data["attendance_pct"] == 30.43
