import pytest
from fastapi.testclient import TestClient
import mongomock
from pymongo import MongoClient

import app.main as main_module
from app.main import app


@pytest.fixture(autouse=True)
def mock_db(db_fixture):
    return db_fixture


def test_explain_unsupported_endpoint_422():
    client = TestClient(app)
    assert client.get("/admin/explain/unknown_endpoint").status_code == 422
    assert client.get("/admin/explain/punch_in").status_code == 422
    assert client.get("/admin/explain/employees").status_code == 422


def test_explain_parameter_validation_422():
    client = TestClient(app)

    # 1. attendance_list invalid parameters
    assert client.get("/admin/explain/attendance_list?date_from=invalid").status_code == 422
    assert client.get("/admin/explain/attendance_list?date_to=invalid").status_code == 422
    assert client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-05").status_code == 422
    assert client.get("/admin/explain/attendance_list?page=0").status_code == 422
    assert client.get("/admin/explain/attendance_list?page_size=101").status_code == 422
    assert client.get("/admin/explain/attendance_list?status=UNKNOWN").status_code == 422

    # 2. employee_monthly missing/invalid parameters
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
    assert client.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=invalid").status_code == 422
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13").status_code == 422

    # 3. department_summary missing/invalid parameters
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/department_summary?month=invalid").status_code == 422
    assert client.get("/admin/explain/department_summary?month=2026-13").status_code == 422

    # 4. late_leaderboard missing/invalid parameters
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    assert client.get("/admin/explain/late_leaderboard?month=invalid").status_code == 422
    assert client.get("/admin/explain/late_leaderboard?month=2026-07&limit=0").status_code == 422
    assert client.get("/admin/explain/late_leaderboard?month=2026-07&limit=51").status_code == 422

    # 5. department_trend missing/invalid parameters
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering").status_code == 422
    assert client.get("/admin/explain/department_trend?from=2026-07-01&to=2026-07-05").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-05&to=2026-07-01").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-03").status_code == 422


def test_explain_endpoints_response_structure():
    client = TestClient(app)

    # 1. attendance_list
    r1 = client.get("/admin/explain/attendance_list?page=1&page_size=20")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # 2. employee_monthly
    r2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"
    assert "explain" in d2

    # 3. department_summary
    r3 = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"
    assert "explain" in d3

    # 4. late_leaderboard
    r4 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"
    assert "explain" in d4

    # 5. department_trend
    r5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "employees"
    assert "explain" in d5


def test_explain_real_mongodb_execution_plans_and_indexes():
    """
    Connects to real MongoDB instance, seeds data with indexes, calls all 5 explain endpoints,
    and inspects winning plans for presence of IXSCAN and absence of COLLSCAN.
    """
    try:
        real_client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
        real_client.admin.command("ping")
    except Exception:
        pytest.skip("Local MongoDB server not available for real execution plan verification")

    orig_client = main_module.client
    orig_db = main_module.db
    real_db = real_client["test_admin_explain_real_db"]
    real_db.employees.drop()
    real_db.attendance_logs.drop()

    main_module.client = real_client
    main_module.db = real_db
    main_module.ensure_indexes(real_db)

    # Seed 50 employees and 1000 logs
    for i in range(1, 51):
        real_db.employees.insert_one({
            "emp_code": f"EMP{i:04d}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering" if i <= 25 else "Sales",
            "joined_on": "2026-01-01" if i % 2 == 0 else "2026-07-01",
            "shift_start": "09:30",
            "shift_end": "18:30",
        })

    for d_idx in range(1, 15):
        d_str = f"2026-07-{d_idx:02d}"
        for i in range(1, 51):
            real_db.attendance_logs.insert_one({
                "emp_code": f"EMP{i:04d}",
                "date": d_str,
                "status": "PRESENT" if i % 3 != 0 else "ABSENT",
                "late_minutes": 25 if i % 5 == 0 else 0,
                "overtime_minutes": 40 if i % 7 == 0 else 0,
                "work_hours": 8.5,
                "half_day": False,
            })

    try:
        client = TestClient(app)

        # 1. attendance_list
        resp = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        exp_str = str(data["explain"])
        assert ("IXSCAN" in exp_str or "ixseek" in exp_str)
        assert ("COLLSCAN" not in exp_str and "cscan" not in exp_str)

        # 2. employee_monthly
        resp = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "employee_monthly"
        assert data["collection"] == "attendance_logs"
        exp_str = str(data["explain"])
        assert ("IXSCAN" in exp_str or "ixseek" in exp_str)
        assert ("COLLSCAN" not in exp_str and "cscan" not in exp_str)

        # 3. department_summary
        resp = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "department_summary"
        assert data["collection"] == "employees"
        exp_str = str(data["explain"])
        assert ("IXSCAN" in exp_str or "ixseek" in exp_str)
        assert ("COLLSCAN" not in exp_str and "cscan" not in exp_str)

        # 4. late_leaderboard
        resp = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "late_leaderboard"
        assert data["collection"] == "attendance_logs"
        exp_str = str(data["explain"])
        assert ("IXSCAN" in exp_str or "ixseek" in exp_str)
        assert ("COLLSCAN" not in exp_str and "cscan" not in exp_str)

        # 5. department_trend
        resp = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "department_trend"
        assert data["collection"] == "employees"
        exp_str = str(data["explain"])
        assert ("IXSCAN" in exp_str or "ixseek" in exp_str)
        assert ("COLLSCAN" not in exp_str and "cscan" not in exp_str)

    finally:
        real_db.employees.drop()
        real_db.attendance_logs.drop()
        main_module.client = orig_client
        main_module.db = orig_db
