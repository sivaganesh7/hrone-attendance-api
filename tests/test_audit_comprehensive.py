import concurrent.futures
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main_module
from app.main import (
    app,
    round_half_up,
    to_epoch_ms,
    get_attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    IST,
)


@pytest.fixture(autouse=True)
def mock_db(db_fixture):
    return db_fixture


def test_audit_duplicate_employee_creation():
    client = TestClient(app)
    emp_payload = {
        "emp_code": "EMP8001",
        "name": "Audit User",
        "email": "audit@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    # 1. First creation succeeds (201)
    res1 = client.post("/employees", json=emp_payload)
    assert res1.status_code == 201
    assert res1.json()["emp_code"] == "EMP8001"
    assert isinstance(res1.json()["created_at"], int)

    # 2. Serial duplicate creation returns 409
    res2 = client.post("/employees", json=emp_payload)
    assert res2.status_code == 409
    assert "emp_code already exists" in res2.json()["detail"]

    # 3. Simultaneous duplicate creation across 5 threads (exactly 1x 201, 4x 409)
    emp_payload_concurrent = {
        "emp_code": "EMP8002",
        "name": "Audit Concurrent",
        "email": "audit_conc@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: client.post("/employees", json=emp_payload_concurrent).status_code, range(5)))
    assert results.count(201) == 1
    assert results.count(409) == 4


def test_audit_simultaneous_punch_ins():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8010",
            "name": "Punch In User",
            "email": "punch_in@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )
    punch_ms = 1783311000000

    def attempt_punch():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP8010", "punched_at": punch_ms}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: attempt_punch(), range(5)))

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_audit_simultaneous_punch_outs():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8020",
            "name": "Punch Out User",
            "email": "punch_out@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )
    in_ms = 1783310280000
    out_ms = 1783343100000
    client.post("/attendance/punch-in", json={"emp_code": "EMP8020", "punched_at": in_ms})

    def attempt_out():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP8020", "punched_at": out_ms}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: attempt_out(), range(5)))

    assert results.count(200) == 1
    assert results.count(409) == 4


def test_audit_concurrent_regularizations(monkeypatch):
    import threading
    import pymongo.collection

    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8030",
            "name": "Regularize User",
            "email": "reg_user@example.com",
            "department": "HR",
            "joined_on": "2026-01-01",
        },
    )
    in_ms = 1783310280000
    client.post("/attendance/punch-in", json={"emp_code": "EMP8030", "punched_at": in_ms})

    orig_find_one = pymongo.collection.Collection.find_one
    barrier = threading.Barrier(2)

    def find_one_synced(self, filter_doc, *args, **kwargs):
        res = orig_find_one(self, filter_doc, *args, **kwargs)
        if isinstance(filter_doc, dict) and filter_doc.get("date") == "2026-07-06":
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return res

    monkeypatch.setattr(pymongo.collection.Collection, "find_one", find_one_synced)

    def attempt_patch(idx):
        return client.patch(
            "/attendance/EMP8030/2026-07-06",
            json={
                "status": "WFH" if idx == 0 else "ON_DUTY",
                "reason": f"concurrent test {idx}",
                "regularized_by": f"manager{idx}",
            },
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_patch, 0)
        f2 = executor.submit(attempt_patch, 1)
        results = [f1.result(), f2.result()]

    assert results.count(200) == 1
    assert results.count(409) == 1


def test_audit_invalid_epoch_timestamps():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8040",
            "name": "Timestamp User",
            "email": "ts@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    # 1. Below 100000000000 (seconds-looking)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": 1783310280}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP8040", "punched_at": 1783310280}).status_code == 422

    # 2. Above 4102444800000 (beyond 2100)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": 5000000000000}).status_code == 422

    # 3. String timestamp
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": "1783310280000"}).status_code == 422

    # 4. Float timestamp
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": 1783310280000.5}).status_code == 422

    # 5. Boolean
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": True}).status_code == 422

    # 6. Negative number
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP8040", "punched_at": -100000000000}).status_code == 422


def test_audit_overnight_shifts():
    client = TestClient(app)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP8050",
            "name": "Overnight Worker",
            "email": "overnight@example.com",
            "department": "Operations",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        },
    )
    # Shift 22:00 to 06:00
    # Punch-in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    in_ms = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP8050", "punched_at": in_ms})
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"
    assert res_in.json()["late_minutes"] == 0

    # Punch-out at 06:40 IST on 2026-07-07 next morning (01:10 UTC)
    # Overtime = 40 minutes (from 06:00 next day to 06:40)
    out_ms = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP8050", "punched_at": out_ms})
    assert res_out.status_code == 200
    record = res_out.json()
    assert record["overtime_minutes"] == 40
    # Work hours = (06:40 - 21:55) = 8h 45m = 8.75h
    assert record["work_hours"] == 8.75
    assert record["half_day"] is False


def test_audit_strict_10_minute_lateness_grace_boundary():
    # R2: Grace period 10 minutes.
    # Late only if strictly more than 10:00 (600s) after shift_start.
    # Shift 09:30:
    # 09:40:00 -> 0 late
    # 09:40:01 -> 10 late
    # 10:15:59 -> 45 late
    t_094000 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    t_094001 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
    t_101559 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)

    assert compute_late_minutes(t_094000, "09:30", "2026-07-06") == 0
    assert compute_late_minutes(t_094001, "09:30", "2026-07-06") == 10
    assert compute_late_minutes(t_101559, "09:30", "2026-07-06") == 45


def test_audit_30_minute_overtime_threshold():
    # R3: Whole minutes from shift_end to punch_out, counted only if >= 30; otherwise 0.
    # Shift end 18:30:
    # 18:59:59 (29 min) -> 0
    # 19:00:00 (30 min) -> 30
    # 19:05:59 (35 min) -> 35
    t_185959 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST).astimezone(timezone.utc)
    t_190000 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    t_190559 = datetime(2026, 7, 6, 19, 5, 59, tzinfo=IST).astimezone(timezone.utc)

    assert compute_overtime(t_185959, "09:30", "18:30", "2026-07-06") == 0
    assert compute_overtime(t_190000, "09:30", "18:30", "2026-07-06") == 30
    assert compute_overtime(t_190559, "09:30", "18:30", "2026-07-06") == 35


def test_audit_half_up_rounding():
    # R4/R8: Standard half-up rounding (not banker's round-half-to-even)
    assert round_half_up(2.345, 2) == 2.35
    assert round_half_up(2.344, 2) == 2.34
    assert round_half_up(2.355, 2) == 2.36
    assert round_half_up(2.375, 2) == 2.38
    assert round_half_up(0.41666, 4) == 0.4167
    assert round_half_up(0.41664, 4) == 0.4166


def test_audit_filtered_pagination_totals():
    client = TestClient(app)
    # Seed 3 Engineering, 2 Sales
    for i in range(1, 4):
        client.post("/employees", json={
            "emp_code": f"EMP710{i}",
            "name": f"Eng {i}",
            "email": f"eng{i}@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
    for i in range(1, 3):
        client.post("/employees", json={
            "emp_code": f"EMP720{i}",
            "name": f"Sales {i}",
            "email": f"sales{i}@example.com",
            "department": "Sales",
            "joined_on": "2026-01-01",
        })

    # Total unfiltered = 5
    r_all = client.get("/employees?page=1&page_size=2")
    assert r_all.status_code == 200
    assert r_all.json()["total"] == 5
    assert len(r_all.json()["items"]) == 2

    # Total filtered by department = 3 (for Engineering)
    r_eng = client.get("/employees?department=Engineering&page=1&page_size=2")
    assert r_eng.status_code == 200
    assert r_eng.json()["total"] == 3
    assert len(r_eng.json()["items"]) == 2

    # Page bounds validation
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page_size=101").status_code == 422


def test_audit_mid_month_joiners():
    client = TestClient(app)
    # Employee joins on 2026-07-15 (Wednesday). July has 31 days.
    # Working days from July 15 to July 31: 13 Mon-Fri days.
    client.post("/employees", json={
        "emp_code": "EMP7301",
        "name": "Mid Month",
        "email": "mid@example.com",
        "department": "IT",
        "joined_on": "2026-07-15",
    })

    resp = client.get("/analytics/employees/EMP7301/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 13
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_audit_weekend_records():
    client = TestClient(app)
    client.post("/employees", json={
        "emp_code": "EMP7401",
        "name": "Weekend Worker",
        "email": "wk@example.com",
        "department": "IT",
        "joined_on": "2026-07-01",
    })
    # 2026-07-04 is Saturday, 2026-07-05 is Sunday
    # R7: present_days counts only Mon-Fri records; weekend records still add to late and overtime totals.
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP7401", "date": "2026-07-04", "status": "PRESENT", "late_minutes": 20, "overtime_minutes": 45, "half_day": False},
        {"emp_code": "EMP7401", "date": "2026-07-05", "status": "PRESENT", "late_minutes": 10, "overtime_minutes": 30, "half_day": False},
        {"emp_code": "EMP7401", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "overtime_minutes": 0, "half_day": False}, # Monday
    ])

    resp = client.get("/analytics/employees/EMP7401/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # present_days = 1.0 (only Monday counts)
    assert data["present_days"] == 1.0
    # late count = 2 (Saturday + Sunday)
    assert data["late_count"] == 2
    # total late minutes = 30
    assert data["total_late_minutes"] == 30
    # total overtime minutes = 75
    assert data["total_overtime_minutes"] == 75


def test_audit_no_log_employees():
    client = TestClient(app)
    client.post("/employees", json={
        "emp_code": "EMP7501",
        "name": "Ghost Employee",
        "email": "ghost@example.com",
        "department": "Finance",
        "joined_on": "2026-01-01",
    })
    # Monthly analytics handles zero-log employee gracefully
    res_m = client.get("/analytics/employees/EMP7501/monthly?month=2026-07")
    assert res_m.status_code == 200
    assert res_m.json()["present_days"] == 0.0
    assert res_m.json()["attendance_pct"] == 0.0

    # Department summary includes employee in headcount even with zero logs
    res_d = client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert res_d.status_code == 200
    assert len(res_d.json()["items"]) == 1
    assert res_d.json()["items"][0]["headcount"] == 1
    assert res_d.json()["items"][0]["present_days"] == 0.0
    assert res_d.json()["items"][0]["avg_work_hours"] is None

    # Leaderboard excludes zero late employees
    res_l = client.get("/analytics/leaderboard/late?month=2026-07&department=Finance")
    assert res_l.status_code == 200
    assert res_l.json()["items"] == []


def test_audit_leaderboard_ties_at_cutoff():
    client = TestClient(app)
    # Alice (50), Bob (40), Charlie (40), David (20)
    for code, name, dept in [
        ("EMP7601", "Alice", "Support"),
        ("EMP7602", "Bob", "Support"),
        ("EMP7603", "Charlie", "Support"),
        ("EMP7604", "David", "Support"),
    ]:
        client.post("/employees", json={"emp_code": code, "name": name, "email": f"{code}@a.com", "department": dept, "joined_on": "2026-01-01"})

    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP7601", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP7602", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP7603", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP7604", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
    ])

    # Limit = 2. Ranks: Alice=1, Bob=2, Charlie=2, David=4.
    # Since rank <= 2, Alice, Bob, and Charlie must all be returned (length = 3)
    resp = client.get("/analytics/leaderboard/late?month=2026-07&department=Support&limit=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert [x["rank"] for x in items] == [1, 2, 2]
    assert [x["emp_code"] for x in items] == ["EMP7601", "EMP7602", "EMP7603"]


def test_audit_missing_trend_dates_gap_filling_and_moving_avg():
    client = TestClient(app)
    client.post("/employees", json={
        "emp_code": "EMP7701",
        "name": "Trend Tester",
        "email": "trend@example.com",
        "department": "Design",
        "joined_on": "2026-07-01",
    })

    # Only 1 log on Friday 2026-07-03. Range 2026-07-01 to 2026-07-05 (5 days: Wed, Thu, Fri, Sat, Sun).
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP7701",
        "date": "2026-07-03",
        "status": "PRESENT",
        "late_minutes": 0,
        "half_day": False,
    })

    resp = client.get("/analytics/departments/Design/trend?from=2026-07-01&to=2026-07-05")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # Must contain exactly 5 rows (one per calendar day)
    assert len(items) == 5

    # Day 1: 2026-07-01 (Wed), working day, 0 present -> rate 0.0, moving_avg 0.0
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["is_working_day"] is True
    assert items[0]["attendance_rate"] == 0.0
    assert items[0]["moving_avg_7d"] == 0.0

    # Day 2: 2026-07-02 (Thu), working day, 0 present -> rate 0.0, moving_avg 0.0
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["is_working_day"] is True
    assert items[1]["attendance_rate"] == 0.0
    assert items[1]["moving_avg_7d"] == 0.0

    # Day 3: 2026-07-03 (Fri), working day, 1 present -> rate 1.0
    # moving_avg over window [0.0, 0.0, 1.0] -> 1.0 / 3 = 0.3333
    assert items[2]["date"] == "2026-07-03"
    assert items[2]["is_working_day"] is True
    assert items[2]["attendance_rate"] == 1.0
    assert items[2]["moving_avg_7d"] == 0.3333

    # Day 4: 2026-07-04 (Sat), weekend -> attendance_rate is None
    # moving_avg ignores None -> remains 0.3333
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None
    assert items[3]["moving_avg_7d"] == 0.3333

    # Day 5: 2026-07-05 (Sun), weekend -> attendance_rate is None
    # moving_avg ignores None -> remains 0.3333
    assert items[4]["date"] == "2026-07-05"
    assert items[4]["is_working_day"] is False
    assert items[4]["attendance_rate"] is None
    assert items[4]["moving_avg_7d"] == 0.3333


def test_audit_automatic_startup_indexes():
    # Verify index keys and unique flags in MongoDB
    indexes_emp = main_module.db.employees.index_information()
    assert "idx_employees_emp_code_unique" in indexes_emp
    assert indexes_emp["idx_employees_emp_code_unique"]["unique"] is True

    indexes_att = main_module.db.attendance_logs.index_information()
    assert "idx_attendance_emp_date_unique" in indexes_att
    assert indexes_att["idx_attendance_emp_date_unique"]["unique"] is True
    assert "idx_attendance_date_desc_emp" in indexes_att
    assert "idx_attendance_emp_punch_in" in indexes_att
