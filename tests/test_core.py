import json
import pathlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest
from bson import json_util

from app.main import (
    IST,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    attendance_date,
    derived_fields,
    from_ms,
)

DAY_EMP = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_EMP = {"shift_start": "22:00", "shift_end": "06:00"}
SAMPLE = pathlib.Path(__file__).parent.parent / "sample_data"


def ist(*args):
    return datetime(*args, tzinfo=IST)


def ms(*args):
    return int(ist(*args).timestamp() * 1000)


def new_emp(api, code="EMP0001", dept="Engineering", **kw):
    body = {"emp_code": code, "name": "Test", "email": "t@example.com", "department": dept,
            "joined_on": "2026-01-01", **kw}
    r = api.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---- rules, no database ------------------------------------------------------

@pytest.mark.parametrize("hms,expected", [
    ((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0),
])
def test_late_examples_from_r2(hms, expected):
    assert compute_late_minutes(ist(2026, 7, 6, *hms), date(2026, 7, 6), DAY_EMP) == expected


def test_sub_second_is_truncated_before_grace_check():
    punch = from_ms(ms(2026, 7, 6, 9, 40, 0) + 900)  # 09:40:00.900
    assert compute_late_minutes(punch, date(2026, 7, 6), DAY_EMP) == 0


def test_overnight_attendance_date_and_late():
    early = ist(2026, 7, 7, 0, 15)  # belongs to the shift that started on the 6th
    assert attendance_date(early, NIGHT_EMP) == date(2026, 7, 6)
    assert compute_late_minutes(early, date(2026, 7, 6), NIGHT_EMP) == 135
    assert attendance_date(ist(2026, 7, 6, 21, 55), NIGHT_EMP) == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 6, 0), NIGHT_EMP) == date(2026, 7, 7)  # not earlier than 06:00


def test_day_shift_uses_ist_date_not_utc():
    assert attendance_date(ist(2026, 7, 8, 0, 30), DAY_EMP) == date(2026, 7, 8)


@pytest.mark.parametrize("out,expected", [((19, 0, 59), 30), ((18, 59, 59), 0), ((18, 35), 0)])
def test_overtime_threshold(out, expected):
    assert compute_overtime(ist(2026, 7, 6, *out), date(2026, 7, 6), DAY_EMP) == expected


def test_overnight_overtime_ends_next_day():
    assert compute_overtime(ist(2026, 7, 7, 6, 40), date(2026, 7, 6), NIGHT_EMP) == 40


def test_work_hours_half_up():
    start = ist(2026, 7, 6, 9, 0)
    assert float(compute_work_hours(start, ist(2026, 7, 6, 10, 0, 54))) == 1.02  # 1.015
    assert float(compute_work_hours(start, ist(2026, 7, 6, 17, 0, 18))) == 8.01  # 8.005


def test_half_day_uses_rounded_hours():
    start = ist(2026, 7, 6, 9, 0)
    assert derived_fields("PRESENT", start, ist(2026, 7, 6, 13, 29, 42), date(2026, 7, 6), DAY_EMP)["half_day"] is False  # 4.495 -> 4.50
    assert derived_fields("PRESENT", start, ist(2026, 7, 6, 13, 29, 41), date(2026, 7, 6), DAY_EMP)["half_day"] is True   # 4.49


def test_rules_reproduce_every_sample_record():
    opts = json_util.JSONOptions(tz_aware=True)
    emps = {e["emp_code"]: e for e in json_util.loads((SAMPLE / "employees.json").read_text(), json_options=opts)}
    for rec in json_util.loads((SAMPLE / "attendance_logs.json").read_text(), json_options=opts):
        if rec["punch_in"] is None:
            continue
        emp = emps[rec["emp_code"]]
        day = date.fromisoformat(rec["date"])
        assert attendance_date(rec["punch_in"], emp) == day
        got = derived_fields(rec["status"], rec["punch_in"], rec["punch_out"], day, emp)
        assert got["late_minutes"] == rec["late_minutes"], rec
        assert got["work_hours"] == rec["work_hours"], rec
        assert got["overtime_minutes"] == rec["overtime_minutes"], rec
        assert got["half_day"] == rec.get("half_day", False), rec


# ---- employees -----------------------------------------------------------------

def test_create_employee_shape(api):
    e = new_emp(api)
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(e["created_at"], int) and e["created_at"] > 100_000_000_000
    assert (e["shift_start"], e["shift_end"]) == ("09:30", "18:30")


@pytest.mark.parametrize("bad", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"name": ""},
    {"shift_start": "25:00"}, {"shift_end": "9:30"}, {"joined_on": "2026-02-30"},
    {"joined_on": "05-01-2026"}, {"shift_start": "18:30"},  # equal to shift_end
])
def test_create_employee_rejects(api, bad):
    body = {"emp_code": "EMP0001", "name": "T", "email": "t@example.com", "department": "Eng",
            "joined_on": "2026-01-01", **bad}
    assert api.post("/employees", json=body).status_code == 422


def test_duplicate_employee_409(api):
    new_emp(api)
    r = api.post("/employees", json={"emp_code": "EMP0001", "name": "X", "email": "x@example.com",
                                     "department": "Eng", "joined_on": "2026-01-01"})
    assert r.status_code == 409


def test_list_employees_paging_sort_total(api):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Eng"), ("EMP0002", "Eng")]:
        new_emp(api, code, dept)
    r = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 3
    r = api.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003"]
    assert api.get("/employees", params={"department": "Eng"}).json()["total"] == 2
    for p in [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}]:
        assert api.get("/employees", params=p).status_code == 422


# ---- punch-in -----------------------------------------------------------------

def test_punch_in_creates_record(api):
    new_emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5, 0) + 999})
    assert r.status_code == 201
    assert r.json() == {
        "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": ms(2026, 7, 6, 10, 5, 0), "punch_out": None, "work_hours": None,
        "late_minutes": 35, "overtime_minutes": 0, "half_day": False, "history": [],
    }


@pytest.mark.parametrize("punched_at", [1783312500, 1783312500000.0, "1783312500000", True, 5_000_000_000_000])
def test_punch_in_rejects_bad_timestamps(api, punched_at):
    new_emp(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at}).status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "BANANA"])
def test_punch_in_rejects_non_presence_status(api, status):
    new_emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status})
    assert r.status_code == 422


def test_punch_in_unknown_employee_404(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_in_overnight_date(api):
    new_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 0, 15)})
    assert (r.json()["date"], r.json()["late_minutes"]) == ("2026-07-06", 135)


def test_second_punch_in_same_day_409(api):
    new_emp(api)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    assert api.post("/attendance/punch-in", json=body).status_code == 201
    body["punched_at"] = ms(2026, 7, 6, 14, 0)
    assert api.post("/attendance/punch-in", json=body).status_code == 409


def test_concurrent_punch_in_exactly_one_wins(api):
    new_emp(api)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(8)))
    assert codes == [201] + [409] * 7


# ---- list attendance ------------------------------------------------------------

def test_list_attendance_sort_defaults_and_filters(api, db):
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0003", "date": "2026-07-10", "status": "LEAVE", "punch_in": None, "punch_out": None},
        {"emp_code": "EMP0001", "date": "2026-07-10", "status": "ABSENT", "punch_in": None, "punch_out": None},
        {"emp_code": "EMP0001", "date": "2026-07-11", "status": "ABSENT", "punch_in": None, "punch_out": None},
    ])
    r = api.get("/attendance").json()
    assert [(d["date"], d["emp_code"]) for d in r["items"]] == [
        ("2026-07-11", "EMP0001"), ("2026-07-10", "EMP0001"), ("2026-07-10", "EMP0003")]
    legacy = r["items"][0]
    assert (legacy["half_day"], legacy["history"], legacy["late_minutes"], legacy["work_hours"]) == (False, [], 0, None)
    r = api.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-10", "page_size": 1}).json()
    assert r["total"] == 2 and len(r["items"]) == 1
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"}, {"date_from": "garbage"},
    {"date_to": "2026-13-01"}, {"status": "BANANA"}, {"page": 0}, {"page_size": 500},
])
def test_list_attendance_rejects(api, params):
    assert api.get("/attendance", params=params).status_code == 422


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}
