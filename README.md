# Employee Attendance & Analytics API

A high-performance, race-safe backend API built with FastAPI, PyMongo, and MongoDB aggregation pipelines. Implements comprehensive employee attendance management, automated business rule calculations (late arrival, overtime, half-day detection, overnight shifts, IST calendar alignment), concurrency controls, and real-time analytical reporting.

## Prerequisites

- **Python**: 3.10+ (Python 3.11+ recommended)
- **MongoDB**: 6.0 or newer (tested with MongoDB 9.0)

## Installation

```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

The application reads configuration from environment variables (a local `.env` file is loaded if present; system environment variables take precedence):

- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

All required database indexes are created idempotently at application startup during the FastAPI lifespan event.

## Running the Server

Start the API with Uvicorn:

```bash
uvicorn app.main:app --port 8000
```

Once running, interactive OpenAPI documentation is accessible at:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Seed Sample Data

To populate the database with the provided sample data:

```bash
python sample_seed.py
```

## Running Automated Tests

To run the complete automated test suite verifying all 17 specification areas:

```bash
python tests/test_api.py
```

To run the 100,000-document query performance and execution plan benchmark:

```bash
python tests/test_large_scale.py
```

## Known Limitations / Incomplete Functionality

- None. All 11 endpoints, business rules (R1–R10), concurrency controls, aggregation pipelines, and explain endpoints are fully implemented and verified against the contract.
