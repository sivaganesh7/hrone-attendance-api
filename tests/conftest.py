import os
import pytest
from pymongo import MongoClient
import mongomock
import app.main as main_module


@pytest.fixture(autouse=True)
def db_fixture():
    """
    Connects to real MongoDB if available; cleans up before and after each test.
    Ensures all indexes are verified and created.
    Falls back to mongomock if MongoDB is offline.
    """
    orig_client = main_module.client
    orig_db = main_module.db

    try:
        uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        db = client["test_hrone_attendance"]
        is_real = True
    except Exception:
        client = mongomock.MongoClient(tz_aware=True)
        db = client["attendance_db"]
        is_real = False

    db.employees.drop()
    db.attendance_logs.drop()
    main_module.client = client
    main_module.db = db
    main_module.ensure_indexes(db)

    yield db

    if is_real:
        db.employees.drop()
        db.attendance_logs.drop()
    main_module.client = orig_client
    main_module.db = orig_db
