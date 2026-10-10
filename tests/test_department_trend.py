from datetime import datetime, date, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
import mongomock
from pymongo import MongoClient

import app.main as main_module
from app.main import app, build_department_trend_pipeline, round_half_up


@pytest.fixture(autouse=True)
def mock_db(db_fixture):
    return db_fixture


def test_trend_validation_errors():
    client = TestClient(app)

    # Missing parameters
    assert client.get("/analytics/departments/Engineering/trend").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-01").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?to=2026-07-05").status_code == 422

    # Invalid date formats
    assert client.get("/analytics/departments/Engineering/trend?from=invalid&to=2026-07-05").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-7-1&to=2026-07-05").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-13-01&to=2026-07-05").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-02-30&to=2026-03-05").status_code == 422

    # to < from
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-01").status_code == 422

    # Range exceeds 92 days: 2026-01-01 to 2026-04-03 is 93 days
    assert client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03").status_code == 422


def test_trend_unknown_department_404():
    client = TestClient(app)
    # No employees exist in department "UnknownDept"
    resp = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Department not found"


def test_trend_range_boundary_exactly_92_days():
    client = TestClient(app)
    # Create employee so department exists
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # 2026-01-01 to 2026-04-02 is exactly 92 days (Jan 31 + Feb 28 + Mar 31 + Apr 2 = 92)
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-02")
    assert resp.status_code == 200
    data = resp.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 92
    assert data["items"][0]["date"] == "2026-01-01"
    assert data["items"][-1]["date"] == "2026-04-02"


def test_trend_gaps_weekends_and_zero_records():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Alice",
            "email": "alice@example.com",
            "department": "Sales",
            "joined_on": "2026-07-01",
        },
    )

    # 2026-07-03 is Friday, 07-04 is Saturday, 07-05 is Sunday, 07-06 is Monday, 07-07 is Tuesday
    # Only punch-in on Friday (2026-07-03) and Saturday (2026-07-04)
    # Friday 09:30 IST: 2026-07-03T04:00:00Z -> 1783051200000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783051200000})

    # Saturday 09:30 IST: 2026-07-04T04:00:00Z -> 1783137600000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783137600000})

    resp = client.get("/analytics/departments/Sales/trend?from=2026-07-03&to=2026-07-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["department"] == "Sales"
    items = data["items"]
    assert len(items) == 5

    # Friday 2026-07-03: working day, present_count=1.0, late_count=0, rate=1.0, moving_avg=1.0
    r0 = items[0]
    assert r0["date"] == "2026-07-03"
    assert r0["is_working_day"] is True
    assert r0["headcount"] == 1
    assert r0["present_count"] == 1.0
    assert r0["late_count"] == 0
    assert r0["attendance_rate"] == 1.0
    assert r0["moving_avg_7d"] == 1.0

    # Saturday 2026-07-04: weekend, worked on site (present_count=1.0), rate is null
    r1 = items[1]
    assert r1["date"] == "2026-07-04"
    assert r1["is_working_day"] is False
    assert r1["headcount"] == 1
    assert r1["present_count"] == 1.0
    assert r1["late_count"] == 0
    assert r1["attendance_rate"] is None
    # Moving avg considers window [r0(1.0), r1(null)] -> mean is 1.0
    assert r1["moving_avg_7d"] == 1.0

    # Sunday 2026-07-05: weekend, no records, present_count=0.0, rate is null
    r2 = items[2]
    assert r2["date"] == "2026-07-05"
    assert r2["is_working_day"] is False
    assert r2["headcount"] == 1
    assert r2["present_count"] == 0.0
    assert r2["late_count"] == 0
    assert r2["attendance_rate"] is None
    assert r2["moving_avg_7d"] == 1.0

    # Monday 2026-07-06: working day with NO records at all -> rate 0.0
    r3 = items[3]
    assert r3["date"] == "2026-07-06"
    assert r3["is_working_day"] is True
    assert r3["headcount"] == 1
    assert r3["present_count"] == 0.0
    assert r3["late_count"] == 0
    assert r3["attendance_rate"] == 0.0
    # Window [1.0, None, None, 0.0] -> non-null: 1.0, 0.0 -> mean: 0.5
    assert r3["moving_avg_7d"] == 0.5

    # Tuesday 2026-07-07: working day with NO records at all -> rate 0.0
    r4 = items[4]
    assert r4["date"] == "2026-07-07"
    assert r4["is_working_day"] is True
    assert r4["headcount"] == 1
    assert r4["present_count"] == 0.0
    assert r4["late_count"] == 0
    assert r4["attendance_rate"] == 0.0
    # Window [1.0, None, None, 0.0, 0.0] -> non-null: 1.0, 0.0, 0.0 -> mean: 1.0/3 = 0.3333
    assert r4["moving_avg_7d"] == 0.3333


def test_trend_joiners_and_headcount_increase():
    client = TestClient(app)
    # EMP0001 joined 2026-07-01
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "User 1",
            "email": "u1@example.com",
            "department": "Ops",
            "joined_on": "2026-07-01",
        },
    )
    # EMP0002 joined 2026-07-03
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "User 2",
            "email": "u2@example.com",
            "department": "Ops",
            "joined_on": "2026-07-03",
        },
    )
    # EMP0003 joined future 2026-08-01 (should NOT be counted in July)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "User 3",
            "email": "u3@example.com",
            "department": "Ops",
            "joined_on": "2026-08-01",
        },
    )

    resp = client.get("/analytics/departments/Ops/trend?from=2026-07-01&to=2026-07-04")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 4

    assert items[0]["date"] == "2026-07-01"
    assert items[0]["headcount"] == 1  # Only EMP0001

    assert items[1]["date"] == "2026-07-02"
    assert items[1]["headcount"] == 1  # Only EMP0001

    assert items[2]["date"] == "2026-07-03"
    assert items[2]["headcount"] == 2  # EMP0001 + EMP0002

    assert items[3]["date"] == "2026-07-04"
    assert items[3]["headcount"] == 2  # EMP0001 + EMP0002


def test_trend_zero_headcount_days():
    client = TestClient(app)
    # Employee joined 2026-07-10
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Future Joiner",
            "email": "future@example.com",
            "department": "Finance",
            "joined_on": "2026-07-10",
        },
    )

    # Query range 2026-07-01 to 2026-07-05: department exists, but headcount is 0
    resp = client.get("/analytics/departments/Finance/trend?from=2026-07-01&to=2026-07-05")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 5
    for item in items:
        assert item["headcount"] == 0
        assert item["attendance_rate"] is None
        assert item["moving_avg_7d"] is None


def test_trend_presence_statuses_half_day_and_late():
    client = TestClient(app)
    # Two employees in Support
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Emp 1",
            "email": "e1@example.com",
            "department": "Support",
            "joined_on": "2026-07-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Emp 2",
            "email": "e2@example.com",
            "department": "Support",
            "joined_on": "2026-07-01",
        },
    )

    # Seed directly into attendance_logs:
    # 2026-07-06 (Monday):
    # EMP0001: WFH, half_day=True (0.5), late_minutes=15
    # EMP0002: ON_DUTY, half_day=False (1.0), late_minutes=0
    main_module.db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "WFH",
            "punch_in": datetime(2026, 7, 6, 4, 15, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 7, 30, tzinfo=timezone.utc),
            "work_hours": 3.25,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        },
        {
            "emp_code": "EMP0002",
            "date": "2026-07-06",
            "status": "ON_DUTY",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # 2026-07-07 (Tuesday):
        # EMP0001: LEAVE (0.0)
        # EMP0002: ABSENT (0.0)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP0002",
            "date": "2026-07-07",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
    ])

    resp = client.get("/analytics/departments/Support/trend?from=2026-07-06&to=2026-07-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2

    # Monday 07-06: present_count = 0.5 + 1.0 = 1.5, late_count = 1
    # attendance_rate = 1.5 / 2 = 0.75
    assert items[0]["present_count"] == 1.5
    assert items[0]["late_count"] == 1
    assert items[0]["attendance_rate"] == 0.75
    assert items[0]["moving_avg_7d"] == 0.75

    # Tuesday 07-07: present_count = 0.0, late_count = 0
    # attendance_rate = 0.0 / 2 = 0.0
    # moving_avg_7d = (0.75 + 0.0) / 2 = 0.375
    assert items[1]["present_count"] == 0.0
    assert items[1]["late_count"] == 0
    assert items[1]["attendance_rate"] == 0.0
    assert items[1]["moving_avg_7d"] == 0.375


def test_trend_moving_average_7d_window_sliding():
    client = TestClient(app)
    # Single employee in Tech
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Dev",
            "email": "dev@example.com",
            "department": "Tech",
            "joined_on": "2026-07-01",
        },
    )

    # 10 consecutive weekdays from 2026-07-06 to 2026-07-17 (excluding weekends)
    # Rates on weekdays:
    # 07-06 (Mon): 1.0
    # 07-07 (Tue): 1.0
    # 07-08 (Wed): 0.0
    # 07-09 (Thu): 1.0
    # 07-10 (Fri): 0.5
    # 07-11 (Sat): weekend -> null
    # 07-12 (Sun): weekend -> null
    # 07-13 (Mon): 1.0
    # 07-14 (Tue): 0.5
    # 07-15 (Wed): 0.0
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-09", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-10", "status": "PRESENT", "half_day": True, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-13", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT", "half_day": True, "late_minutes": 0},
    ])

    resp = client.get("/analytics/departments/Tech/trend?from=2026-07-06&to=2026-07-15")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 10

    # Rates:
    # idx 0 (07-06): 1.0 -> avg = 1.0
    assert items[0]["attendance_rate"] == 1.0
    assert items[0]["moving_avg_7d"] == 1.0

    # idx 1 (07-07): 1.0 -> window [1.0, 1.0] -> avg = 1.0
    assert items[1]["attendance_rate"] == 1.0
    assert items[1]["moving_avg_7d"] == 1.0

    # idx 2 (07-08): 0.0 -> window [1.0, 1.0, 0.0] -> avg = 2/3 = 0.6667
    assert items[2]["attendance_rate"] == 0.0
    assert items[2]["moving_avg_7d"] == 0.6667

    # idx 3 (07-09): 1.0 -> window [1.0, 1.0, 0.0, 1.0] -> avg = 3/4 = 0.75
    assert items[3]["attendance_rate"] == 1.0
    assert items[3]["moving_avg_7d"] == 0.75

    # idx 4 (07-10): 0.5 -> window [1.0, 1.0, 0.0, 1.0, 0.5] -> avg = 3.5/5 = 0.7
    assert items[4]["attendance_rate"] == 0.5
    assert items[4]["moving_avg_7d"] == 0.7

    # idx 5 (07-11 Sat): null -> window [1.0, 1.0, 0.0, 1.0, 0.5, null] -> avg = 3.5/5 = 0.7
    assert items[5]["attendance_rate"] is None
    assert items[5]["moving_avg_7d"] == 0.7

    # idx 6 (07-12 Sun): null -> window [1.0, 1.0, 0.0, 1.0, 0.5, null, null] -> avg = 3.5/5 = 0.7
    assert items[6]["attendance_rate"] is None
    assert items[6]["moving_avg_7d"] == 0.7

    # idx 7 (07-13 Mon): 1.0 -> window is last 7 rows (idx 1..7): [1.0, 0.0, 1.0, 0.5, null, null, 1.0]
    # Non-null values: 1.0, 0.0, 1.0, 0.5, 1.0 -> sum = 3.5 / 5 = 0.7
    assert items[7]["attendance_rate"] == 1.0
    assert items[7]["moving_avg_7d"] == 0.7

    # idx 8 (07-14 Tue): 0.5 -> window (idx 2..8): [0.0, 1.0, 0.5, null, null, 1.0, 0.5]
    # Non-null values: 0.0, 1.0, 0.5, 1.0, 0.5 -> sum = 3.0 / 5 = 0.6
    assert items[8]["attendance_rate"] == 0.5
    assert items[8]["moving_avg_7d"] == 0.6

    # idx 9 (07-15 Wed): 0.0 -> window (idx 3..9): [1.0, 0.5, null, null, 1.0, 0.5, 0.0]
    # Non-null values: 1.0, 0.5, 1.0, 0.5, 0.0 -> sum = 3.0 / 5 = 0.6
    assert items[9]["attendance_rate"] == 0.0
    assert items[9]["moving_avg_7d"] == 0.6


def test_trend_mongodb_native_aggregation_pipeline():
    """
    Directly tests the MongoDB aggregation pipeline against a real MongoDB instance if available.
    """
    try:
        real_client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
        real_client.admin.command("ping")
    except Exception:
        pytest.skip("Local MongoDB server not available for native pipeline test")

    test_db = real_client["test_trend_real_mongo"]
    test_db.employees.drop()
    test_db.attendance_logs.drop()

    test_db.employees.insert_many([
        {"emp_code": "EMP01", "name": "A", "department": "QA", "joined_on": "2026-07-01"},
        {"emp_code": "EMP02", "name": "B", "department": "QA", "joined_on": "2026-07-03"},
    ])

    test_db.attendance_logs.insert_many([
        {"emp_code": "EMP01", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 15, "half_day": False},
        {"emp_code": "EMP01", "date": "2026-07-04", "status": "WFH", "late_minutes": 0, "half_day": True},
        {"emp_code": "EMP01", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": True},
        {"emp_code": "EMP02", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
    ])

    pipe = build_department_trend_pipeline("QA", "2026-07-03", "2026-07-07")
    results = list(test_db.employees.aggregate(pipe))

    assert len(results) == 5
    # Friday 07-03
    assert results[0]["date"] == "2026-07-03"
    assert results[0]["is_working_day"] is True
    assert results[0]["headcount"] == 2
    assert results[0]["present_count"] == 1.0
    assert results[0]["late_count"] == 1
    assert round_half_up(results[0]["attendance_rate"], 4) == 0.5
    assert round_half_up(results[0]["moving_avg_7d"], 4) == 0.5

    # Saturday 07-04
    assert results[1]["date"] == "2026-07-04"
    assert results[1]["is_working_day"] is False
    assert results[1]["present_count"] == 0.5
    assert results[1]["attendance_rate"] is None
    assert round_half_up(results[1]["moving_avg_7d"], 4) == 0.5

    # Sunday 07-05
    assert results[2]["date"] == "2026-07-05"
    assert results[2]["is_working_day"] is False
    assert results[2]["attendance_rate"] is None
    assert round_half_up(results[2]["moving_avg_7d"], 4) == 0.5

    # Monday 07-06: EMP01 (0.5) + EMP02 (1.0) = 1.5 present / 2 HC = 0.75
    assert results[3]["date"] == "2026-07-06"
    assert results[3]["is_working_day"] is True
    assert results[3]["headcount"] == 2
    assert results[3]["present_count"] == 1.5
    assert round_half_up(results[3]["attendance_rate"], 4) == 0.75
    # Window non-null: [0.5, 0.75] -> mean: 1.25 / 2 = 0.625
    assert round_half_up(results[3]["moving_avg_7d"], 4) == 0.625

    # Tuesday 07-07: gap day, 0 present / 2 HC = 0.0
    assert results[4]["date"] == "2026-07-07"
    assert results[4]["is_working_day"] is True
    assert results[4]["headcount"] == 2
    assert results[4]["present_count"] == 0.0
    assert round_half_up(results[4]["attendance_rate"], 4) == 0.0
    # Window non-null: [0.5, 0.75, 0.0] -> mean: 1.25 / 3 = 0.4167
    assert round_half_up(results[4]["moving_avg_7d"], 4) == 0.4167

    test_db.employees.drop()
    test_db.attendance_logs.drop()
