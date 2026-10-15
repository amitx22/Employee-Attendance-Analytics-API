# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HR Attendance & Analytics API adhering to OpenAPI contract v2.0 and business rules R1 - R10.

## How to Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run MongoDB:**
   Ensure MongoDB 6.0+ is running on `mongodb://localhost:27017` (or set `MONGO_URI` and `MONGO_DB` environment variables).

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The service initializes all indexes at startup and is ready when `GET /health` returns `200`.

5. **Run test suite:**
   ```bash
   python -m pytest -v tests/
   ```

## Architecture & Implementation Notes

- **Single file application:** All application routes, business rules, aggregation pipelines, and models reside in `app/main.py`.
- **Concurrency & Races:** Handled via MongoDB unique compound indexes (`emp_code, date`) and atomic `find_one_and_update` with optimistic history revision locking.
- **Aggregation Pipelines:** All analytics endpoints (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) run as native MongoDB aggregation pipelines using `$densify` and `$setWindowFields` without in-memory Python looping.
- **Indexes & Explain Plans:** All main queries and aggregation stages are backed by targeted compound indexes and achieve `IXSCAN` with zero `COLLSCAN`.
- Review findings and architectural decisions are documented in `REVIEW.md` and `DECISIONS.md`.
