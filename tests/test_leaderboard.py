import pytest
import pymongo
from fastapi.testclient import TestClient
import mongomock

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


def test_leaderboard_month_and_limit_validation_422():
    client = TestClient(app)

    # Invalid month formats
    assert client.get("/analytics/leaderboard/late?month=2026-13").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-7").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=invalid").status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422

    # Invalid limits
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=abc").status_code == 422


def test_leaderboard_standard_competition_ranking_and_ties():
    client = TestClient(app)

    # Create 4 employees
    for emp_code, name in [
        ("EMP0001", "Alice"),
        ("EMP0002", "Bob"),
        ("EMP0003", "Charlie"),
        ("EMP0004", "David"),
    ]:
        client.post(
            "/employees",
            json={
                "emp_code": emp_code,
                "name": name,
                "email": f"{emp_code.lower()}@example.com",
                "department": "Engineering",
                "joined_on": "2026-01-01",
            },
        )

    # Attendance logs:
    # Alice: 60 mins late
    # Bob: 40 mins late
    # Charlie: 40 mins late (tied with Bob)
    # David: 20 mins late
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 60},
        {"emp_code": "EMP0002", "date": "2026-07-02", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0003", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0004", "date": "2026-07-04", "status": "PRESENT", "late_minutes": 20},
    ])

    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    assert len(items) == 4

    # Alice: rank 1
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 60

    # Bob & Charlie: tied at rank 2, sorted by emp_code ascending
    assert items[1]["emp_code"] == "EMP0002"
    assert items[1]["rank"] == 2
    assert items[1]["total_late_minutes"] == 40

    assert items[2]["emp_code"] == "EMP0003"
    assert items[2]["rank"] == 2
    assert items[2]["total_late_minutes"] == 40

    # David: standard competition ranking skips rank 3 -> rank 4
    assert items[3]["emp_code"] == "EMP0004"
    assert items[3]["rank"] == 4
    assert items[3]["total_late_minutes"] == 20


def test_leaderboard_ties_at_cutoff():
    client = TestClient(app)

    for emp_code, name in [
        ("EMP0001", "Alice"),
        ("EMP0002", "Bob"),
        ("EMP0003", "Charlie"),
        ("EMP0004", "David"),
    ]:
        client.post(
            "/employees",
            json={
                "emp_code": emp_code,
                "name": name,
                "email": f"{emp_code.lower()}@example.com",
                "department": "Engineering",
                "joined_on": "2026-01-01",
            },
        )

    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 60},
        {"emp_code": "EMP0002", "date": "2026-07-02", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0003", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0004", "date": "2026-07-04", "status": "PRESENT", "late_minutes": 20},
    ])

    # limit=2 should return all employees with rank <= 2 (Alice, Bob, Charlie) -> 3 employees
    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert [x["emp_code"] for x in items] == ["EMP0001", "EMP0002", "EMP0003"]
    assert [x["rank"] for x in items] == [1, 2, 2]


def test_leaderboard_missing_employee_references_ignored():
    client = TestClient(app)

    # Valid employee
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

    # Attendance logs: one valid employee, one orphan/ghost employee
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP9999", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 90},
    ])

    resp = client.get("/analytics/leaderboard/late?month=2026-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1


def test_leaderboard_zero_late_minutes_excluded():
    client = TestClient(app)

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
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Bob",
            "email": "bob@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )

    # Alice is late 15 mins, Bob is on time (0 mins)
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 15},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0},
    ])

    resp = client.get("/analytics/leaderboard/late?month=2026-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0001"


def test_leaderboard_department_filter_ranks_within_department():
    client = TestClient(app)

    # Engineering: Alice (20 mins), Bob (10 mins)
    # Sales: Charlie (50 mins)
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
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Bob",
            "email": "bob@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Charlie",
            "email": "charlie@example.com",
            "department": "Sales",
            "joined_on": "2026-01-01",
        },
    )

    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 10},
        {"emp_code": "EMP0003", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50},
    ])

    # Filter by Engineering: Charlie excluded, Alice is rank 1 inside Engineering
    resp = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[0]["department"] == "Engineering"

    assert items[1]["emp_code"] == "EMP0002"
    assert items[1]["rank"] == 2
    assert items[1]["department"] == "Engineering"

    # Filter by Non-existent department: returns empty items
    resp_empty = client.get("/analytics/leaderboard/late?month=2026-07&department=Marketing")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["items"] == []


def test_leaderboard_sums_multiple_days_and_counts():
    client = TestClient(app)

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

    # Alice late on 3 separate days in July 2026: 15, 25, 10
    # Also 1 day in June 2026: 30 (should be excluded)
    main_module.db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 15},
        {"emp_code": "EMP0001", "date": "2026-07-10", "status": "PRESENT", "late_minutes": 25},
        {"emp_code": "EMP0001", "date": "2026-07-20", "status": "PRESENT", "late_minutes": 10},
        {"emp_code": "EMP0001", "date": "2026-06-30", "status": "PRESENT", "late_minutes": 30},
    ])

    resp = client.get("/analytics/leaderboard/late?month=2026-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["total_late_minutes"] == 50
    assert items[0]["late_count"] == 3


def test_leaderboard_native_mongodb_pipeline():
    """
    Directly tests the aggregation pipeline against real MongoDB
    to verify native MongoDB $setWindowFields and $rank window functions.
    """
    try:
        real_client = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=1000)
        real_client.server_info()
    except Exception:
        pytest.skip("Local MongoDB server not reachable for native window testing")

    test_db = real_client["test_leaderboard_native"]
    orig_client = main_module.client
    orig_db = main_module.db
    main_module.client = real_client
    main_module.db = test_db

    try:
        test_db.employees.drop()
        test_db.attendance_logs.drop()
        main_module.ensure_indexes(test_db)

        client = TestClient(app)

        # Alice: 60 mins late
        # Bob: 40 mins late
        # Charlie: 40 mins late
        # David: 20 mins late
        for emp_code, name, dept in [
            ("EMP0001", "Alice", "Engineering"),
            ("EMP0002", "Bob", "Engineering"),
            ("EMP0003", "Charlie", "Engineering"),
            ("EMP0004", "David", "Engineering"),
            ("EMP0005", "Emma", "HR"),
        ]:
            client.post(
                "/employees",
                json={
                    "emp_code": emp_code,
                    "name": name,
                    "email": f"{emp_code.lower()}@example.com",
                    "department": dept,
                    "joined_on": "2026-01-01",
                },
            )

        test_db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 60},
            {"emp_code": "EMP0002", "date": "2026-07-02", "status": "PRESENT", "late_minutes": 40},
            {"emp_code": "EMP0003", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 40},
            {"emp_code": "EMP0004", "date": "2026-07-04", "status": "PRESENT", "late_minutes": 20},
            {"emp_code": "EMP0005", "date": "2026-07-05", "status": "PRESENT", "late_minutes": 80},
            {"emp_code": "EMP9999", "date": "2026-07-05", "status": "PRESENT", "late_minutes": 100}, # unknown emp
        ])

        # Test limit cutoff with ties on real MongoDB (Eng limit=2 -> ranks 1, 2, 2 returned)
        resp = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=2")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        assert [x["emp_code"] for x in items] == ["EMP0001", "EMP0002", "EMP0003"]
        assert [x["rank"] for x in items] == [1, 2, 2]

        # Test full Eng list shows rank skipping (1, 2, 2, 4)
        resp_full = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=10")
        assert resp_full.status_code == 200
        items_full = resp_full.json()["items"]
        assert len(items_full) == 4
        assert [x["rank"] for x in items_full] == [1, 2, 2, 4]
        assert [x["emp_code"] for x in items_full] == ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]

        # Test ghost employee EMP9999 is omitted
        resp_all = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        assert resp_all.status_code == 200
        all_codes = [x["emp_code"] for x in resp_all.json()["items"]]
        assert "EMP9999" not in all_codes
        assert all_codes[0] == "EMP0005"  # Emma (80 mins) is rank 1 overall
        assert resp_all.json()["items"][0]["rank"] == 1
    finally:
        real_client.drop_database("test_leaderboard_native")
        main_module.client = orig_client
        main_module.db = orig_db
