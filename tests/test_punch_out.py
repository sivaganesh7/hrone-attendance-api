import concurrent.futures
from datetime import datetime, timezone, timedelta
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, IST


@pytest.fixture(autouse=True)
def mock_db(db_fixture):
    return db_fixture


def test_punch_out_unknown_employee_404():
    client = TestClient(app)
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Employee not found"


def test_punch_out_no_punch_in_found_404():
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
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No punch-in found"


def test_punch_out_invalid_timestamp_types_422():
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

    # Float rejected
    r_float = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": 1783343100000.5},
    )
    assert r_float.status_code == 422

    # String rejected
    r_str = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": "1783343100000"},
    )
    assert r_str.status_code == 422

    # Seconds-looking rejected
    r_sec = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": 1783343100},
    )
    assert r_sec.status_code == 422

    # Beyond maximum rejected
    r_max = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": 4102444800001},
    )
    assert r_max.status_code == 422


def test_punch_out_at_or_before_punch_in_422():
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
    punch_in_ms = 1783310280000  # 03:58:00 UTC
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms},
    )

    # Punch out at exact same second
    r_same = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms},
    )
    assert r_same.status_code == 422

    # Punch out earlier than punch in
    r_earlier = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms - 1000},
    )
    assert r_earlier.status_code == 422


def test_punch_out_more_than_24_hours_after_punch_in_422():
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
    punch_in_ms = 1783310280000
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms},
    )

    # 24 hours + 1 second later -> 422
    punch_out_late = punch_in_ms + (24 * 3600 + 1) * 1000
    r_late = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_late},
    )
    assert r_late.status_code == 422


def test_punch_out_standard_shift_and_half_up_rounding():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # Sample 1: punched in 09:28 IST (03:58:00Z), out 18:35 IST (13:05:00Z)
    punch_in_ms = 1783310280000
    punch_out_ms = 1783343100000

    resp_in = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms},
    )
    assert resp_in.status_code == 201

    resp_out = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ms},
    )
    assert resp_out.status_code == 200
    data = resp_out.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["status"] == "PRESENT"
    assert data["punch_in"] == punch_in_ms
    assert data["punch_out"] == punch_out_ms
    assert data["work_hours"] == 9.12
    assert data["late_minutes"] == 0
    # Overtime 5 min < 30 min minimum -> 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []
    assert "_id" not in data


def test_punch_out_half_day_rule():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0004",
            "name": "Kavita Nair",
            "email": "kavita@example.com",
            "department": "Marketing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
        },
    )

    # Sample 4: 09:30 IST (04:00 UTC) to 13:00 IST (07:30 UTC) -> 3.5 hours
    # 2026-07-07: 04:00:00 UTC -> 1783396800000
    # 07:30:00 UTC -> 1783409400000
    in_ms = 1783396800000
    out_ms = 1783409400000

    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0004", "punched_at": in_ms, "status": "WFH"},
    )
    resp = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0004", "punched_at": out_ms},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["work_hours"] == 3.5
    assert data["half_day"] is True
    assert data["status"] == "WFH"


def test_punch_out_late_and_overtime():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Deepak Verma",
            "email": "deepak@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-03-01",
        },
    )

    # Sample 2: Punched in 10:05 IST (04:35 UTC), out 19:10 IST (13:40 UTC)
    in_ms = 1783312500000
    out_ms = 1783345200000

    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0003", "punched_at": in_ms},
    )
    resp = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0003", "punched_at": out_ms},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 9.08
    assert data["half_day"] is False


def test_punch_out_overnight_shift():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Karthik Rajan",
            "email": "karthik@example.com",
            "department": "Operations",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-02-15",
        },
    )

    # Sample 7: Overnight shift 22:00 - 06:00.
    # Punch-in: 2026-07-06 21:55 IST (16:25 UTC) -> 1783355100000
    # Punch-out: 2026-07-07 06:40 IST (01:10 UTC next day) -> 1783386600000
    in_ms = 1783355100000
    out_ms = 1783386600000

    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": in_ms},
    )
    resp = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": out_ms},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-07-06"
    assert data["work_hours"] == 8.75
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 40
    assert data["half_day"] is False


def test_punch_out_timestamp_truncation():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    in_ms = 1783310280000
    # Provide ms fraction: 1783343100999
    out_ms = 1783343100999

    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})
    assert resp.status_code == 200
    data = resp.json()
    # Stored and returned truncated to whole seconds
    assert data["punch_out"] == 1783343100000


def test_punch_out_already_punched_out_409():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    in_ms = 1783310280000
    out_ms = 1783343100000

    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    resp1 = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})
    assert resp1.status_code == 200

    # Second punch out attempt on the same closed record -> 409
    resp2 = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms + 1000})
    assert resp2.status_code == 409
    assert resp2.json()["detail"] == "record is already punched out"


def test_punch_out_concurrency_race():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0099",
            "name": "Concurrency User",
            "email": "conc@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    in_ms = 1783310280000
    out_ms = 1783343100000

    client.post("/attendance/punch-in", json={"emp_code": "EMP0099", "punched_at": in_ms})

    def attempt_punch_out():
        return client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0099", "punched_at": out_ms},
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: attempt_punch_out(), range(5)))

    assert results.count(200) == 1
    assert results.count(409) == 4


def test_punch_out_default_now():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0050",
            "name": "Now User",
            "email": "now@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    # Punch in 1 hour ago
    one_hour_ago = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0050", "punched_at": one_hour_ago})

    # Punch out with omitted punched_at
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0050"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["punch_out"] is not None
    assert data["punch_out"] > one_hour_ago
    assert data["work_hours"] is not None


def test_punch_out_exactly_24_hours():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0060",
            "name": "Exact 24h",
            "email": "exact24@example.com",
            "department": "Support",
            "joined_on": "2026-01-05",
        },
    )
    in_ms = 1783310280000
    exact_24h_ms = in_ms + 24 * 3600 * 1000

    client.post("/attendance/punch-in", json={"emp_code": "EMP0060", "punched_at": in_ms})
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0060", "punched_at": exact_24h_ms})
    assert resp.status_code == 200
    assert resp.json()["work_hours"] == 24.0


def test_punch_out_closes_most_recent_eligible_record():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0070",
            "name": "Multi Day User",
            "email": "multiday@example.com",
            "department": "Support",
            "joined_on": "2026-01-05",
        },
    )
    # Day 1: 2026-07-06 (in and out)
    in_day1 = 1783310280000
    out_day1 = 1783343100000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0070", "punched_at": in_day1})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0070", "punched_at": out_day1})

    # Day 2: 2026-07-07 (in, open)
    in_day2 = in_day1 + 86400 * 1000
    out_day2 = out_day1 + 86400 * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0070", "punched_at": in_day2})

    # Punch out should close Day 2
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0070", "punched_at": out_day2})
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-07-07"
    assert resp.json()["punch_in"] == in_day2
    assert resp.json()["punch_out"] == out_day2

