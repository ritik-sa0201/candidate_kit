from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_core import ms, new_emp


def punch_in(api, code="EMP0001", at=None, **kw):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": at or ms(2026, 7, 6, 9, 28), **kw})
    assert r.status_code == 201, r.text
    return r.json()


def punch_out(api, code="EMP0001", at=None):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": at})


# ---- punch-out -------------------------------------------------------------------

def test_punch_out_normal_day(api):
    new_emp(api)
    punch_in(api)
    r = punch_out(api, at=ms(2026, 7, 6, 18, 35) + 750)
    assert r.status_code == 200
    rec = r.json()
    assert rec["punch_out"] == ms(2026, 7, 6, 18, 35)
    assert (rec["work_hours"], rec["overtime_minutes"], rec["half_day"], rec["history"]) == (9.12, 0, False, [])


def test_punch_out_overnight_closes_previous_day(api):
    new_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in(api, "EMP0005", ms(2026, 7, 6, 21, 55))
    rec = punch_out(api, "EMP0005", ms(2026, 7, 7, 6, 40)).json()
    assert (rec["date"], rec["work_hours"], rec["overtime_minutes"], rec["late_minutes"]) == ("2026-07-06", 8.75, 40, 0)


def test_punch_out_half_day(api):
    new_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 9, 30))
    rec = punch_out(api, at=ms(2026, 7, 7, 13, 0)).json()
    assert (rec["work_hours"], rec["half_day"]) == (3.5, True)


def test_punch_out_errors(api):
    new_emp(api)
    assert punch_out(api, "EMP9999", ms(2026, 7, 6, 18, 0)).status_code == 404
    assert punch_out(api, at=ms(2026, 7, 6, 18, 0)).status_code == 404  # never punched in
    punch_in(api, at=ms(2026, 7, 6, 10, 0))
    assert punch_out(api, at=ms(2026, 7, 6, 9, 0)).status_code == 422   # before punch-in
    assert punch_out(api, at=ms(2026, 7, 6, 10, 0)).status_code == 422  # equal to punch-in
    assert punch_out(api, at=ms(2026, 7, 7, 10, 0, 1)).status_code == 422  # > 24 h
    assert punch_out(api, at=1783312500).status_code == 422  # seconds
    assert punch_out(api, at=ms(2026, 7, 6, 18, 0)).status_code == 200
    assert punch_out(api, at=ms(2026, 7, 6, 19, 0)).status_code == 409


def test_punch_out_exactly_24h_is_allowed(api):
    new_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 10, 0))
    assert punch_out(api, at=ms(2026, 7, 7, 10, 0)).status_code == 200


def test_concurrent_punch_out_exactly_one_wins(api):
    new_emp(api)
    punch_in(api)
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: punch_out(api, at=ms(2026, 7, 6, 18, 35)).status_code, range(8)))
    assert codes == [200] + [409] * 7


# ---- regularize ------------------------------------------------------------------

def patch(api, code="EMP0001", day="2026-07-07", **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return api.patch(f"/attendance/{code}/{day}", json=body)


@pytest.fixture
def closed_day(api):
    """Sample record 8 before the correction: in 10:05, out 18:30."""
    new_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 10, 5))
    assert punch_out(api, at=ms(2026, 7, 7, 18, 30)).status_code == 200
    return api


def test_regularize_punch_in_matches_sample_history(closed_day):
    r = patch(closed_day, punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["late_minutes"], rec["work_hours"]) == (0, 9.03)
    [entry] = rec["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch"
    assert isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_history_appends_and_derived_fields_in_body_are_ignored(closed_day):
    patch(closed_day, punch_in=ms(2026, 7, 7, 9, 28))
    rec = patch(closed_day, punch_out=ms(2026, 7, 7, 19, 30), late_minutes=999, work_hours=1).json()
    assert len(rec["history"]) == 2
    assert rec["late_minutes"] == 0 and rec["overtime_minutes"] == 60
    assert set(rec["history"][1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}


def test_regularize_to_leave_clears_everything(closed_day):
    rec = patch(closed_day, status="LEAVE").json()
    assert (rec["punch_in"], rec["punch_out"], rec["work_hours"], rec["late_minutes"]) == (None, None, None, 0)
    assert rec["history"][0]["changes"]["punch_out"] == {"from": ms(2026, 7, 7, 18, 30), "to": None}
    # back to present needs a punch_in
    assert patch(closed_day, status="PRESENT").status_code == 422
    rec = patch(closed_day, status="WFH", punch_in=ms(2026, 7, 7, 9, 31)).json()
    assert (rec["status"], rec["late_minutes"], rec["punch_out"], len(rec["history"])) == ("WFH", 0, None, 2)


@pytest.mark.parametrize("body", [
    {"status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0)},
    {"punch_in": ms(2026, 7, 8, 9, 0)},             # another attendance date
    {"punch_out": ms(2026, 7, 7, 9, 0)},            # before punch_in
    {"punch_out": ms(2026, 7, 8, 10, 6)},           # > 24 h
    {"punch_in": ms(2026, 7, 7, 10, 5)},            # same as stored: no change
    {"status": "PRESENT"},                          # same as stored
    {},                                             # nothing to change
    {"punch_in": None},
    {"punch_in": 1783312500},
    {"status": "BANANA"},
    {"punch_in": ms(2026, 7, 7, 9, 0), "reason": "x"},
    {"punch_in": ms(2026, 7, 7, 9, 0), "regularized_by": ""},
])
def test_regularize_rejects(closed_day, body):
    assert patch(closed_day, **body).status_code == 422


def test_regularize_404s_and_bad_date(closed_day):
    assert patch(closed_day, "EMP9999", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 404
    assert patch(closed_day, day="2026-07-09", punch_in=ms(2026, 7, 9, 9, 0)).status_code == 404
    assert patch(closed_day, day="2026-7-7", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422


def test_regularize_legacy_record_without_history(api, db):
    new_emp(api)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "ABSENT",
                                   "punch_in": None, "punch_out": None})
    rec = patch(api, day="2026-07-14", status="LEAVE").json()
    assert rec["history"][0]["changes"] == {"status": {"from": "ABSENT", "to": "LEAVE"}}


def test_concurrent_regularize_loses_no_history(closed_day):
    outs = [ms(2026, 7, 7, 18, 30 + i) for i in range(1, 9)]
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda t: patch(closed_day, punch_out=t).status_code, outs))
    assert set(codes) <= {200, 409}
    rec = closed_day.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(rec["history"]) == codes.count(200) >= 1
