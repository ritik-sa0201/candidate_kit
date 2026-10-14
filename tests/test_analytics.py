"""Analytics on a small hand-built month (July 2026) with the awkward cases.

July 2026 starts on a Wednesday: 23 weekdays; weekends are 4-5, 11-12, 18-19, 25-26.
Every expected number below was worked out by hand. Needs a real MongoDB
($densify / $setWindowFields), so it is skipped under mongomock.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("USE_MONGOMOCK") == "1", reason="needs a real MongoDB")

EMPLOYEES = [
    ("EMP0001", "Engineering", "2026-01-05", "09:30", "18:30"),
    ("EMP0002", "Engineering", "2026-07-13", "09:30", "18:30"),  
    ("EMP0003", "Sales", "2026-02-01", "09:30", "18:30"),
    ("EMP0004", "Sales", "2026-02-01", "09:30", "18:30"),
    ("EMP0005", "Support", "2026-03-10", "22:00", "06:00"),       # night shift
    ("EMP0006", "Support", "2026-03-10", "09:30", "18:30"),       # no logs at all
    ("EMP0007", "Engineering", "2026-08-01", "09:30", "18:30"),   # joins after July
    ("EMP0008", "Ops", "2026-08-03", "09:30", "18:30"),           # whole dept not joined yet
    ("EMP0009", "Engineering", "2026-07-10", "09:30", "18:30"),   # joins Fri 10th: 16 weekdays
]


def log(emp, day, status, wh=None, late=0, ot=0, half=False, legacy=False):
    doc = {"emp_code": emp, "date": day, "status": status, "punch_in": None, "punch_out": None,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
    if not legacy:
        doc.update(half_day=half, history=[])
    return doc


LOGS = [
    log("EMP0001", "2026-07-06", "PRESENT", 9.12),
    log("EMP0001", "2026-07-07", "PRESENT", 4.25, late=15, half=True),
    log("EMP0001", "2026-07-08", "WFH", 8.0),
    log("EMP0001", "2026-07-11", "PRESENT", 5.0, late=20, ot=40),  # Saturday
    log("EMP0001", "2026-07-13", "LEAVE"),
    log("EMP0001", "2026-07-14", "PRESENT", None, late=40),         # still open
    log("EMP0001", "2026-06-30", "PRESENT", 9.0, late=100),         # other month
    log("EMP0001", "2026-08-01", "PRESENT", 9.0, late=100),         # other month
    log("EMP0002", "2026-07-13", "PRESENT", 8.5, legacy=True),      # no half_day / history
    log("EMP0002", "2026-07-15", "ON_DUTY", 9.0, late=60),
    log("EMP0003", "2026-07-06", "PRESENT", 9.08, late=35, ot=40),
    log("EMP0003", "2026-07-08", "LEAVE"),
    log("EMP0003", "2026-07-09", "ABSENT"),
    log("EMP0004", "2026-07-07", "WFH", 3.5, half=True),
    log("EMP0004", "2026-07-08", "ABSENT"),
    log("EMP0004", "2026-07-10", "PRESENT", 9.0, late=75),          # ties EMP0001 on 75
    log("EMP0005", "2026-07-06", "PRESENT", 8.75, ot=40),
    log("EMP0005", "2026-07-07", "PRESENT", 7.98, late=15),         # avg 8.365 -> 8.37 half-up
    log("EMP0009", "2026-07-10", "PRESENT", 4.0, half=True),
    log("EMP9999", "2026-07-06", "PRESENT", 9.0, late=500),         # employee doesn't exist
]


@pytest.fixture
def month(api, db):
    db.employees.insert_many([
        {"emp_code": c, "name": f"Name {c}", "email": f"{c.lower()}@example.com", "department": d,
         "shift_start": s, "shift_end": e, "joined_on": j}
        for c, d, j, s, e in EMPLOYEES
    ])
    db.attendance_logs.insert_many([dict(d) for d in LOGS])
    return api


def monthly(api, code, month="2026-07"):
    return api.get(f"/analytics/employees/{code}/monthly", params={"month": month})


def test_monthly_regular_employee(month):
    assert monthly(month, "EMP0001").json() == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 3.5,
        "leave_days": 1, "late_count": 3, "total_late_minutes": 75, "total_overtime_minutes": 40,
        "attendance_pct": 15.22,
    }


def test_monthly_joiners_and_no_logs(month):
    r = monthly(month, "EMP0002").json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (15, 2.0, 13.33)
    r = monthly(month, "EMP0009").json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (16, 0.5, 3.13)  # 3.125 half-up
    r = monthly(month, "EMP0006").json()
    assert (r["working_days"], r["present_days"], r["late_count"], r["attendance_pct"]) == (23, 0.0, 0, 0.0)
    r = monthly(month, "EMP0007").json()
    assert (r["working_days"], r["attendance_pct"]) == (0, None)


def test_monthly_errors(month):
    assert monthly(month, "EMP9999").status_code == 404
    for bad in ["2026-13", "2026-7", "July", ""]:
        assert monthly(month, "EMP0001", bad).status_code == 422


def test_department_summary(month):
    r = month.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert r == {"month": "2026-07", "items": [
        {"department": "Engineering", "headcount": 3, "present_days": 6.0, "avg_work_hours": 6.84,
         "late_count": 4, "total_late_minutes": 135, "leave_count": 1, "on_duty_count": 1},
        {"department": "Sales", "headcount": 2, "present_days": 2.5, "avg_work_hours": 7.19,
         "late_count": 2, "total_late_minutes": 110, "leave_count": 1, "on_duty_count": 0},
        {"department": "Support", "headcount": 2, "present_days": 2.0, "avg_work_hours": 8.37,
         "late_count": 1, "total_late_minutes": 15, "leave_count": 0, "on_duty_count": 0},
    ]}


def test_department_summary_filters(month):
    get = lambda **p: month.get("/analytics/departments/summary", params={"month": "2026-07", **p}).json()["items"]
    assert [i["department"] for i in get(department="Sales")] == ["Sales"]
    assert get(department="Ops") == []      # headcount 0 -> omitted
    assert get(department="sales") == []    # case-sensitive
    aug = month.get("/analytics/departments/summary", params={"month": "2026-08"}).json()["items"]
    assert {i["department"]: i["headcount"] for i in aug} == {"Engineering": 4, "Ops": 1, "Sales": 2, "Support": 2}


def board(api, **params):
    r = api.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})
    assert r.status_code == 200, r.text
    return [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in r.json()["items"]]


def test_leaderboard_ties_and_limit(month):
    assert board(month) == [(1, "EMP0001", 75, 3), (1, "EMP0004", 75, 1), (3, "EMP0002", 60, 1),
                            (4, "EMP0003", 35, 1), (5, "EMP0005", 15, 1)]
    assert board(month, limit=1) == [(1, "EMP0001", 75, 3), (1, "EMP0004", 75, 1)]
    assert board(month, limit=2) == board(month, limit=1)
    assert [r[1] for r in board(month, limit=3)] == ["EMP0001", "EMP0004", "EMP0002"]


def test_leaderboard_department_and_fields(month):
    assert board(month, department="Sales") == [(1, "EMP0004", 75, 1), (2, "EMP0003", 35, 1)]
    item = month.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"][0]
    assert (item["name"], item["department"]) == ("Name EMP0001", "Engineering")
    for bad in [{"limit": 0}, {"limit": 51}]:
        assert month.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422


def trend(api, dept, start, end):
    return api.get(f"/analytics/departments/{dept}/trend", params={"from": start, "to": end})


def test_trend_engineering(month):
    r = trend(month, "Engineering", "2026-07-09", "2026-07-16").json()
    rows = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
             i["attendance_rate"], i["moving_avg_7d"]) for i in r["items"]]
    assert r["department"] == "Engineering"
    assert rows == [
        ("2026-07-09", True, 1, 0, 0, 0.0, 0.0),
        ("2026-07-10", True, 2, 0.5, 0, 0.25, 0.125),
        ("2026-07-11", False, 2, 1, 1, None, 0.125),
        ("2026-07-12", False, 2, 0, 0, None, 0.125),
        ("2026-07-13", True, 3, 1, 0, 0.3333, 0.1944),
        ("2026-07-14", True, 3, 1, 1, 0.3333, 0.2292),
        ("2026-07-15", True, 3, 1, 1, 0.3333, 0.25),
        ("2026-07-16", True, 3, 0, 0, 0.0, 0.25),
    ]


def test_trend_nobody_joined_yet(month):
    items = trend(month, "Ops", "2026-07-01", "2026-07-03").json()["items"]
    assert [(i["date"], i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [
        ("2026-07-01", 0, None, None), ("2026-07-02", 0, None, None), ("2026-07-03", 0, None, None)]


def test_trend_single_day_and_errors(month):
    assert len(trend(month, "Sales", "2026-07-04", "2026-07-04").json()["items"]) == 1
    assert trend(month, "Nope", "2026-07-01", "2026-07-03").status_code == 404
    assert trend(month, "Sales", "2026-07-10", "2026-07-01").status_code == 422
    assert trend(month, "Sales", "2026-07-01", "2026-10-01").status_code == 422   # 93 days
    assert trend(month, "Sales", "2026-07-01", "2026-09-30").status_code == 200   # 92 days
    assert month.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(month, endpoint, params):
    r = month.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    text = r.text
    assert "IXSCAN" in text and "COLLSCAN" not in text, endpoint


@pytest.mark.parametrize("endpoint,params", [
    ("employee_monthly", {"month": "2026-07"}),
    ("department_summary", {}),
    ("late_leaderboard", {}),
    ("department_trend", {"department": "Sales"}),
    ("nope", {}),
])
def test_explain_missing_params(month, endpoint, params):
    assert month.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
