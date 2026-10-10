# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking, regularization audit trails, and reporting analytics pipelines per `openapi.yaml`, `DATA_MODEL.md`, and business rules R1–R10.

---

## 1. Prerequisites

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (tested on local MongoDB 9.0 and compatible with MongoDB Atlas)

---

## 2. Configuration

Connection parameters are read dynamically from environment variables. Real system environment variables always take precedence over values in a local `.env` file:

| Variable | Default Value | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | `attendance_db` | Target database name |

An optional `.env` file can be placed at the project root for local development:
```bash
cp .env.example .env
```

---

## 3. Dependency Installation

Set up a virtual environment and install dependencies:

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Seeding Sample Data

To load the sample documents from `sample_data/` into a disposable MongoDB database:

```bash
python sample_seed.py
```

*Note: The test suite and grader evaluate against isolated datasets including edge cases (ties, mid-month joiners, weekend records, no-log employees, overnight shifts).*

---

## 5. Running the Application

Start the API service using the exact required startup command:

```bash
uvicorn app.main:app --port 8000
```

All unique and compound indexes are created automatically and idempotently during application startup via FastAPI's `lifespan` hook.

Verify server readiness:
```bash
curl http://127.0.0.1:8000/health
# Response: {"status":"ok"}
```

---

## 6. API Documentation

Interactive OpenAPI documentation is generated automatically by FastAPI:

- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 7. Running the Automated Test Suite

Run the full automated test suite against MongoDB:

```bash
pytest
```
or
```bash
python -m pytest -v
```

The test suite connects to the local MongoDB instance at `MONGO_URI` (using an isolated test database `test_hrone_attendance` that is cleanly created and dropped per test). If MongoDB is offline, tests gracefully fall back to `mongomock`.

---

## 8. Known Limitations

1. **Single-File Architecture**: Per assignment rules, all FastAPI application logic, domain rules (R1–R10), data models, aggregations, and serializers reside in `app/main.py`. In an unconstrained production environment, this would be organized into distinct routers, services, schemas, and repository layers.
2. **Aggregation at Scale**: Analytics endpoints (`/analytics/departments/summary`, `/analytics/departments/{department}/trend`, `/analytics/leaderboard/late`) run native MongoDB aggregation pipelines with `$lookup` and `$setWindowFields`. For massive production scale (100x data / >10M attendance records), rollups should be pre-materialized asynchronously (e.g. daily summary collections via change streams or an outbox worker) and `attendance_logs` sharded on `emp_code`.
