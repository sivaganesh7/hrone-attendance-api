import mongomock
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import app.main as main_module
from app.main import (
    app,
    db,
    client,
    IST,
    round_half_up,
    to_epoch_ms,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
)


# --------------------------------------------------------------------------- #
# Unit Tests: Domain Helpers & Rules
# --------------------------------------------------------------------------- #
def test_rounding_half_up():
    assert round_half_up(2.345, 2) == 2.35
    assert round_half_up(2.344, 2) == 2.34
    assert round_half_up(2.355, 2) == 2.36
    assert round_half_up(2.375, 2) == 2.38
    assert round_half_up(None) is None


def test_epoch_ms_conversions():
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ms = to_epoch_ms(dt)
    assert ms == 1783310280000
    assert to_epoch_ms(1783310280500) == 1783310280000  # Truncates ms to whole seconds
    assert to_epoch_ms(None) is None


def test_r1_attendance_date_standard_shift():
    # Regular daytime shift: 09:30 - 18:30
    p1 = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST).astimezone(timezone.utc)
    assert get_attendance_date(p1, "09:30", "18:30") == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift: 22:00 - 06:00
    # Punch-in at 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
    p1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST).astimezone(timezone.utc)
    assert get_attendance_date(p1, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 01:10 IST on 2026-07-07 -> earlier than 06:00, belongs to previous day (2026-07-06)
    p2 = datetime(2026, 7, 7, 1, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert get_attendance_date(p2, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 06:05 IST on 2026-07-07 -> not earlier than 06:00, belongs to 2026-07-07
    p3 = datetime(2026, 7, 7, 6, 5, 0, tzinfo=IST).astimezone(timezone.utc)
    assert get_attendance_date(p3, "22:00", "06:00") == "2026-07-07"


def test_r2_lateness_rule():
    # Shift 09:30 IST:
    # 09:40:00 -> 0 (grace period 10 minutes)
    t1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(t1, "09:30", "2026-07-06") == 0

    # 09:40:01 -> strictly > 10 min -> 10 minutes late
    t2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(t2, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45 minutes late
    t3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(t3, "09:30", "2026-07-06") == 45

    # Early punch-in: 09:25:00 -> 0 late
    t4 = datetime(2026, 7, 6, 9, 25, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(t4, "09:30", "2026-07-06") == 0


def test_r3_overtime_rule():
    # Shift end 18:30 IST:
    # 18:59:59 IST -> 29 minutes -> under 30 -> 0 overtime
    out1 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(out1, "09:30", "18:30", "2026-07-06") == 0

    # 19:00:00 IST -> 30 minutes -> 30 overtime
    out2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(out2, "09:30", "18:30", "2026-07-06") == 30

    # 19:10:00 IST -> 40 minutes -> 40 overtime
    out3 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(out3, "09:30", "18:30", "2026-07-06") == 40


def test_r4_and_r5_work_hours_and_half_day():
    # Sample 1: 03:58:00Z to 13:05:00Z -> 9.12 hours
    p_in = datetime.fromisoformat("2026-07-06T03:58:00Z")
    p_out = datetime.fromisoformat("2026-07-06T13:05:00Z")
    wh = compute_work_hours(p_in, p_out)
    assert wh == 9.12
    assert compute_half_day(wh) is False

    # Half day check: < 4.50 is true
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(None) is False


# --------------------------------------------------------------------------- #
# Integration Tests: Endpoints & Validation
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def mock_db(db_fixture):
    return db_fixture


def test_health_check_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_check_503(monkeypatch):
    from unittest.mock import MagicMock
    mock_bad_client = MagicMock()
    mock_bad_client.admin.command.side_effect = Exception("Mongo offline")
    monkeypatch.setattr(main_module, "client", mock_bad_client)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database unavailable"


def test_create_employee_success():
    client = TestClient(app)
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000
    assert "_id" not in data


def test_create_employee_duplicate_409():
    client = TestClient(app)
    payload = {
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13",
    }
    resp1 = client.post("/employees", json=payload)
    assert resp1.status_code == 201

    # Attempting duplicate insert should yield 409
    resp2 = client.post("/employees", json=payload)
    assert resp2.status_code == 409


def test_create_employee_validation_errors_422():
    client = TestClient(app)
    # Invalid emp_code format
    resp1 = client.post(
        "/employees",
        json={
            "emp_code": "INVALID",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert resp1.status_code == 422

    # Same shift_start and shift_end
    resp2 = client.post(
        "/employees",
        json={
            "emp_code": "EMP0099",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        },
    )
    assert resp2.status_code == 422


def test_list_employees_pagination_and_sorting():
    client = TestClient(app)
    # Seed 3 employees
    for i in [3, 1, 2]:
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP000{i}",
                "name": f"User {i}",
                "email": f"user{i}@example.com",
                "department": "Sales" if i == 1 else "Engineering",
                "joined_on": "2026-01-01",
            },
        )

    # Page 1, page_size 2
    resp = client.get("/employees?page=1&page_size=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    # Verify sorted by emp_code asc
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0002"

    # Filter by department
    resp_dept = client.get("/employees?department=Sales")
    assert resp_dept.status_code == 200
    dept_data = resp_dept.json()
    assert dept_data["total"] == 1
    assert len(dept_data["items"]) == 1
    assert dept_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_unknown_employee_404():
    client = TestClient(app)
    resp = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert resp.status_code == 404


def test_punch_in_invalid_payload_422():
    client = TestClient(app)
    # Timestamp formatted as seconds (< 100000000000)
    resp1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310280})
    assert resp1.status_code == 422

    # Invalid status
    resp2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert resp2.status_code == 422


def test_punch_in_success_and_duplicate_409():
    client = TestClient(app)
    # Create employee
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in at 09:40:01 IST (1783311001000) -> 10 minutes late
    punch_ms = 1783311001000
    resp = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_ms, "status": "PRESENT"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert data["late_minutes"] == 10
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data

    # Punch in again for same date -> 409
    resp_dup = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_ms + 1000},
    )
    assert resp_dup.status_code == 409


def test_list_attendance_filtering_and_validation():
    client = TestClient(app)
    # Invalid date range: date_from > date_to
    resp_invalid = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert resp_invalid.status_code == 422

    # Invalid calendar date: February 31st
    resp_bad_date = client.get("/attendance?date_from=2026-02-31")
    assert resp_bad_date.status_code == 422

    # Invalid status
    resp_bad_status = client.get("/attendance?status=INVALID_STATUS")
    assert resp_bad_status.status_code == 422

    # Valid listing
    resp = client.get("/attendance?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


def test_epoch_ms_strict_types_rejected():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0100",
            "name": "Validation Test",
            "email": "test@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )

    # Float rejected with 422
    r_float = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0100", "punched_at": 1783311000000.5},
    )
    assert r_float.status_code == 422

    # String rejected with 422
    r_str = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0100", "punched_at": "1783311000000"},
    )
    assert r_str.status_code == 422

    # Seconds-looking value (< 100000000000) rejected with 422
    r_sec = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0100", "punched_at": 1783311000},
    )
    assert r_sec.status_code == 422

    # Beyond maximum (> 4102444800000) rejected with 422
    r_max = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0100", "punched_at": 4102444800001},
    )
    assert r_max.status_code == 422


def test_timestamp_truncation_to_whole_seconds():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0200",
            "name": "Truncation Test",
            "email": "trunc@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # 09:40:01.850 IST -> 1783311001850 ms
    # Must truncate to 09:40:01.000 IST -> 1783311001000 ms
    resp = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0200", "punched_at": 1783311001850},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["punch_in"] == 1783311001000
    assert data["late_minutes"] == 10


def test_invalid_calendar_dates_422():
    client = TestClient(app)
    # Invalid joined_on date (Feb 31)
    resp = client.post(
        "/employees",
        json={
            "emp_code": "EMP0300",
            "name": "Bad Date",
            "email": "baddate@example.com",
            "department": "QA",
            "joined_on": "2026-02-31",
        },
    )
    assert resp.status_code == 422


def test_pagination_bounds_validation_422():
    client = TestClient(app)
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page_size=0").status_code == 422
    assert client.get("/employees?page_size=101").status_code == 422

    assert client.get("/attendance?page=0").status_code == 422
    assert client.get("/attendance?page_size=0").status_code == 422
    assert client.get("/attendance?page_size=101").status_code == 422


def test_concurrency_duplicate_employee_creation():
    import concurrent.futures

    client = TestClient(app)
    payload = {
        "emp_code": "EMP9001",
        "name": "Concurrent User",
        "email": "concurrent@example.com",
        "department": "IT",
        "joined_on": "2026-01-01",
    }

    def attempt_create():
        return client.post("/employees", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: attempt_create(), range(5)))

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrency_duplicate_punch_in():
    import concurrent.futures

    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP9002",
            "name": "Punch Concurrency",
            "email": "punch_conc@example.com",
            "department": "IT",
            "joined_on": "2026-01-01",
        },
    )

    punch_ms = 1783311000000

    def attempt_punch():
        return client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9002", "punched_at": punch_ms},
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: attempt_punch(), range(5)))

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_naive_datetime_handling_in_helpers():
    # Naive datetimes passed directly should be assumed UTC and not crash
    naive_in = datetime(2026, 7, 6, 3, 58, 0)
    naive_out = datetime(2026, 7, 6, 13, 5, 0)

    # get_attendance_date
    att_date = get_attendance_date(naive_in, "09:30", "18:30")
    assert att_date == "2026-07-06"

    # compute_late_minutes
    late = compute_late_minutes(naive_in, "09:30", "2026-07-06")
    assert late == 0

    # compute_work_hours
    wh = compute_work_hours(naive_in, naive_out)
    assert wh == 9.12

    # compute_overtime with 5 min past shift (18:35 IST) -> under 30 min -> 0
    ot_under = compute_overtime(naive_out, "09:30", "18:30", "2026-07-06")
    assert ot_under == 0

    # compute_overtime with 40 min past shift (19:10 IST = 13:40 UTC) -> 40
    ot_40 = compute_overtime(datetime(2026, 7, 6, 13, 40, 0), "09:30", "18:30", "2026-07-06")
    assert ot_40 == 40

    # to_epoch_ms
    ms = to_epoch_ms(naive_in)
    assert ms == 1783310280000


def test_serialize_attendance_null_and_missing_fallbacks():
    from app.main import serialize_attendance

    doc = {
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": None,
        "overtime_minutes": None,
        "half_day": None,
        "history": None,
    }
    serialized = serialize_attendance(doc)
    assert serialized["late_minutes"] == 0
    assert serialized["overtime_minutes"] == 0
    assert serialized["half_day"] is False
    assert serialized["history"] == []


def test_list_attendance_sorting_order():
    client = TestClient(app)
    # Create 2 employees
    for emp in ["EMP0010", "EMP0020"]:
        client.post(
            "/employees",
            json={
                "emp_code": emp,
                "name": f"User {emp}",
                "email": f"{emp}@example.com",
                "department": "Engineering",
                "joined_on": "2026-01-01",
            },
        )

    # Create logs:
    # EMP0020 on 2026-07-06
    # EMP0010 on 2026-07-06
    # EMP0010 on 2026-07-07
    client.post("/attendance/punch-in", json={"emp_code": "EMP0020", "punched_at": 1783311000000})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": 1783311000000})
    # Next day: +86400s
    client.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": 1783311000000 + 86400000})

    resp = client.get("/attendance?page=1&page_size=10")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # Should be sorted date DESC, emp_code ASC
    # Item 0: 2026-07-07, EMP0010
    # Item 1: 2026-07-06, EMP0010
    # Item 2: 2026-07-06, EMP0020
    assert items[0]["date"] == "2026-07-07" and items[0]["emp_code"] == "EMP0010"
    assert items[1]["date"] == "2026-07-06" and items[1]["emp_code"] == "EMP0010"
    assert items[2]["date"] == "2026-07-06" and items[2]["emp_code"] == "EMP0020"

