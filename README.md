# Employee Attendance & Analytics API

A simple attendance API made with FastAPI and MongoDB. It supports employee records, punch-in/punch-out, attendance corrections, and basic analytics.

Most of the app code is in `app/main.py`.

## Tech Used
- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Pydantic

## Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

Mac/Linux:
```bash
source .venv/bin/activate
```

Install packages:

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project folder:

```env
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>/?retryWrites=true&w=majority
MONGO_DB=attendance_dev
```

You can use a local MongoDB too. Then run:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000
```

API docs are available at `http://localhost:8000/docs`.  
Use `GET /health` to check if the app can connect to MongoDB.

## Main Features

- Add employees and list them by department.
- Punch in and punch out for attendance.
- Correct attendance records manually.
- Keep a history of changes made during corrections.
- Get monthly employee attendance summaries.
- View department summaries and trends.
- See a leaderboard for late arrivals.
- Check MongoDB query plans using the admin explain endpoint.

## API Endpoints

| Method | Endpoint | What it does |
|---|---|---|
| GET | `/health` | Checks database connection |
| POST | `/employees` | Add an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Mark punch-in |
| POST | `/attendance/punch-out` | Mark punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee report |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late arrival ranking |
| GET | `/analytics/departments/{department}/trend` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | Show query execution details |

List endpoints support pagination. `page` starts from 1 and `page_size` can be between 1 and 100.

## How It Works

### Punch-in
The API checks the employee, validates the input time, and calculates which attendance day the punch belongs to. It then saves the record. An employee can have only one attendance record per day.

### Punch-out
It finds the latest open attendance record and closes it. It returns an error if no open record is found, the record is already closed, or the time is invalid.

### Attendance correction
Admins can update status or punch times. The API checks the new values, recalculates work hours, late minutes, overtime and half-day status, then adds the changes to history.

### Analytics
MongoDB aggregation pipelines are used for reports, so most of the data processing happens in the database. The API includes monthly summaries, department stats, late rankings and daily trends.

## Data and Time Rules

- Employee codes look like `EMP0001`.
- Dates use `YYYY-MM-DD`.
- Shift times use `HH:MM` and are based on IST.
- Incoming punch times must be integer epoch milliseconds.
- Time values are stored as UTC dates in MongoDB and returned as epoch milliseconds.
- Older records without some fields get default values when read.

## Database and Safety

Indexes are created automatically when the app starts. Unique indexes prevent duplicate employee codes and multiple attendance records for the same employee and day.

The app also handles simultaneous requests. For example, if two punch-ins happen at the same time, only one should succeed. Attendance corrections use a version check to avoid overwriting another update.

## Testing

Install the development requirements and run:

```bash
pip install -r requirements-dev.txt
pytest -q
```

The tests cover validation, attendance rules, overnight shifts, corrections, analytics, concurrent requests and database indexes. The project notes report 94 passing tests on MongoDB Atlas.

For tests that do not need MongoDB aggregation features, you can also run:

```bash
USE_MONGOMOCK=1 pytest -q
```

Some analytics tests are skipped in this mode.

## Project Structure

```text
.
├── app/
│   └── main.py
├── tests/
│   ├── conftest.py
│   ├── test_core.py
│   ├── test_writes.py
│   └── test_analytics.py
├── sample_data/
├── sample_seed.py
├── requirements.txt
├── requirements-dev.txt
├── REVIEW.md
├── DECISIONS.md
└── README.md
```

All main endpoints are implemented. Check the tests and `REVIEW.md` for more details about the fixes and decisions.