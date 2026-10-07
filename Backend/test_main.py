from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main

ORG = {"X-Role": "organiser", "X-User": "organiser-1"}
ADMIN = {"X-Role": "admin"}
VISITOR = {"X-Role": "visitor"}


@pytest.fixture
def c(tmp_path):
    main.DB_PATH = str(tmp_path / "test.db")
    main.init_db(seed=False)
    return TestClient(main.app)


def payload(**kw):
    base = {"title": "Test", "description": "d", "capacity": 2,
            "starts_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}
    return {**base, **kw}


def new_event(c, **kw):
    r = c.post("/api/events", json=payload(**kw), headers=ORG)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def published(c, **kw):
    eid = new_event(c, **kw)
    assert c.post(f"/api/events/{eid}/publish", headers=ADMIN).status_code == 200
    return eid


@pytest.mark.parametrize("cap", [0, -1, 1.5, "3", True])
def test_invalid_capacity_rejected(c, cap):
    r = c.post("/api/events", json=payload(capacity=cap), headers=ORG)
    assert r.status_code == 400 and r.json()["code"] == "CAPACITY_INVALID"


def test_past_date_and_missing_title_rejected(c):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert c.post("/api/events", json=payload(starts_at=past), headers=ORG).json()["code"] == "DATE_IN_PAST"
    assert c.post("/api/events", json=payload(title=" "), headers=ORG).json()["code"] == "TITLE_REQUIRED"


def test_new_event_hidden_until_published(c):
    eid = new_event(c)
    assert c.get("/api/events", headers=VISITOR).json() == []
    assert c.get(f"/api/events/{eid}", headers=VISITOR).status_code == 404
    c.post(f"/api/events/{eid}/publish", headers=ADMIN)
    assert [e["id"] for e in c.get("/api/events", headers=VISITOR).json()] == [eid]


def test_registration_rejected_when_unpublished(c):
    eid = new_event(c)
    r = c.post(f"/api/events/{eid}/registrations")
    assert r.status_code == 409 and r.json()["code"] == "EVENT_NOT_PUBLISHED"


def test_registration_rejected_when_full(c):
    eid = published(c, capacity=1)
    assert c.post(f"/api/events/{eid}/registrations").status_code == 201
    r = c.post(f"/api/events/{eid}/registrations")
    assert r.status_code == 409 and r.json()["code"] == "EVENT_FULL"


def test_registration_stores_no_personal_data(c):
    eid = published(c)
    c.post(f"/api/events/{eid}/registrations", json={"email": "a@b.c"})
    with main.tx() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(registrations)")]
    assert cols == ["id", "event_id", "created_at"]


def test_only_admin_publishes_and_owner_updates(c):
    eid = new_event(c)
    assert c.post(f"/api/events/{eid}/publish", headers=ORG).status_code == 403
    other = {"X-Role": "organiser", "X-User": "organiser-2"}
    assert c.put(f"/api/events/{eid}", json=payload(), headers=other).status_code == 403


def test_activity_recorded(c):
    eid = published(c)
    c.post(f"/api/events/{eid}/registrations")
    msgs = [a["message"] for a in c.get("/api/activity").json()]
    assert any("was published" in m for m in msgs) and any("Registration" in m for m in msgs)
