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


def test_patch_unknown_employee_404():
    client = TestClient(app)
    resp = client.patch(
        "/attendance/EMP9999/2026-07-07",
        json={"status": "PRESENT", "reason": "fixing glitch", "regularized_by": "admin"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Employee not found"


def test_patch_missing_record_404():
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
    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={"status": "PRESENT", "reason": "fixing glitch", "regularized_by": "admin"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Attendance record not found"


def test_patch_invalid_date_path_422():
    client = TestClient(app)
    resp = client.patch(
        "/attendance/EMP0001/2026-02-31",
        json={"status": "PRESENT", "reason": "fixing glitch", "regularized_by": "admin"},
    )
    assert resp.status_code == 422


def test_patch_request_body_validation_422():
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
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": 1783310280000},
    )

    # reason < 5 chars
    r1 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "bad", "regularized_by": "admin"},
    )
    assert r1.status_code == 422

    # regularized_by empty
    r2 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "valid reason", "regularized_by": ""},
    )
    assert r2.status_code == 422

    # punch_in as float
    r3 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_in": 1783310280000.5, "reason": "valid reason", "regularized_by": "admin"},
    )
    assert r3.status_code == 422

    # status invalid
    r4 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "INVALID", "reason": "valid reason", "regularized_by": "admin"},
    )
    assert r4.status_code == 422


def test_patch_absent_or_leave_with_punch_times_422():
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
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": 1783310280000},
    )

    # Supplying punch_in when setting status to ABSENT -> 422
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": 1783310280000,
            "reason": "marked absent",
            "regularized_by": "admin",
        },
    )
    assert resp.status_code == 422

    # Supplying punch_out when setting status to LEAVE -> 422
    resp2 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "LEAVE",
            "punch_out": 1783343100000,
            "reason": "on leave today",
            "regularized_by": "admin",
        },
    )
    assert resp2.status_code == 422


def test_patch_setting_absent_clears_punches_and_recomputes():
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
    # Punch in and out
    in_ms = 1783310280000
    out_ms = 1783343100000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})

    # Regularize to ABSENT
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "reason": "employee was absent, badge borrowed",
            "regularized_by": "hr.lead",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Check history entry
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.lead"
    assert h["reason"] == "employee was absent, badge borrowed"
    assert isinstance(h["at"], int)
    changes = h["changes"]
    assert changes["status"] == {"from": "PRESENT", "to": "ABSENT"}
    assert changes["punch_in"] == {"from": in_ms, "to": None}
    assert changes["punch_out"] == {"from": out_ms, "to": None}
    assert changes["work_hours"] == {"from": 9.12, "to": None}


def test_patch_presence_requires_punch_in_422():
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
    # Seed an ABSENT record (no punch_in)
    main_module.db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Setting to PRESENT without supplying punch_in -> 422
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "PRESENT",
            "reason": "was actually present",
            "regularized_by": "hr.lead",
        },
    )
    assert resp.status_code == 422


def test_patch_punch_in_date_mismatch_422():
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
    in_ms = 1783310280000  # 2026-07-06
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Supply punch_in for next day: +86400s
    in_next_day = in_ms + 86400 * 1000
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": in_next_day,
            "reason": "wrong day punch in",
            "regularized_by": "admin",
        },
    )
    assert resp.status_code == 422


def test_patch_punch_out_timing_rules_422():
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
    in_ms = 1783310280000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # punch_out earlier than punch_in
    r1 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_out": in_ms - 1000, "reason": "correction", "regularized_by": "admin"},
    )
    assert r1.status_code == 422

    # punch_out more than 24 hours after punch_in
    r2 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_out": in_ms + 25 * 3600 * 1000, "reason": "correction", "regularized_by": "admin"},
    )
    assert r2.status_code == 422


def test_patch_changes_nothing_422():
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
    in_ms = 1783310280000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Same status and punch_in -> no change
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "PRESENT",
            "punch_in": in_ms,
            "reason": "no actual changes",
            "regularized_by": "admin",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "No changes detected in request"


def test_patch_sample_8_regularization():
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
    # Punched in 10:05 IST (04:35 UTC = 1783398900000), out 18:30 IST (13:00 UTC = 1783429200000)
    in_ms = 1783398900000
    out_ms = 1783429200000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})

    # Correct punch_in to 09:28 IST (03:58 UTC = 1783396680000)
    corrected_in_ms = 1783396680000
    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": corrected_in_ms,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["work_hours"] == 9.03
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"] == {"from": in_ms, "to": corrected_in_ms}
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "work_hours" in h["changes"]
    assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    # Fields that didn't change should NOT be in changes
    assert "status" not in h["changes"]
    assert "punch_out" not in h["changes"]
    assert "overtime_minutes" not in h["changes"]
    assert "half_day" not in h["changes"]


def test_patch_multiple_regularizations_preserves_history():
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
    in_ms = 1783310280000
    out_ms = 1783343100000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})

    # Edit 1: change status to WFH
    resp1 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "WFH", "reason": "corrected to WFH", "regularized_by": "hr1"},
    )
    assert resp1.status_code == 200
    assert len(resp1.json()["history"]) == 1

    # Edit 2: change status to ON_DUTY
    resp2 = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ON_DUTY", "reason": "corrected to ON_DUTY", "regularized_by": "hr2"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["history"]) == 2
    assert data2["history"][0]["by"] == "hr1"
    assert data2["history"][1]["by"] == "hr2"
    assert data2["history"][1]["changes"]["status"] == {"from": "WFH", "to": "ON_DUTY"}


def test_patch_concurrency_conflict_409(monkeypatch):
    import threading

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
    in_ms = 1783310280000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    import pymongo.collection
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
            "/attendance/EMP0001/2026-07-06",
            json={
                "status": "WFH" if idx == 0 else "ON_DUTY",
                "reason": f"concurrent edit {idx}",
                "regularized_by": f"admin{idx}",
            },
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_patch, 0)
        f2 = executor.submit(attempt_patch, 1)
        results = [f1.result(), f2.result()]

    # Exactly one request succeeds with 200, the conflicting request gets 409
    assert results.count(200) == 1
    assert results.count(409) == 1
