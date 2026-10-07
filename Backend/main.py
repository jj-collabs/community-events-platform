"""Community Events Platform - single FastAPI service backed by SQLite."""
import contextvars
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

DB_PATH = os.getenv("EVENTS_DB", os.path.join(os.path.dirname(__file__), "events.db"))
INDEX = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
ROLES = {"visitor", "organiser", "admin"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("events")
request_id = contextvars.ContextVar("request_id", default="-")


def log(op, outcome, **kw):
    """Structured log line. Never pass personal data here."""
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    logger.info("rid=%s op=%s outcome=%s %s", request_id.get(), op, outcome, extra)


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


# ---------- persistence ----------
@contextmanager
def tx():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # manual transactions
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialises writers -> safe capacity check
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(seed=True):
    with tx() as db:
        for ddl in (
            "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT, "
            "starts_at TEXT NOT NULL, capacity INTEGER NOT NULL, status TEXT NOT NULL, organiser_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS registrations(id TEXT PRIMARY KEY, event_id TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, created_at TEXT NOT NULL)",
        ):
            db.execute(ddl)
        if seed and db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            rows = [
                ("EVT-DEMO01", "Community Garden Day", "Plant and tidy together.", 5, 2, "PUBLISHED", "organiser-1"),
                ("EVT-DEMO02", "Board Game Night", "Bring a game, make friends.", 9, 30, "PUBLISHED", "organiser-2"),
                ("EVT-DEMO03", "Neighbourhood Cleanup", "Awaiting admin review.", 14, 20, "PENDING_REVIEW", "organiser-1"),
            ]
            for i, t, d, days, cap, st, org in rows:
                db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                           (i, t, d, (now + timedelta(days=days)).isoformat(), cap, st, org))


def record_activity(db, message):
    db.execute("INSERT INTO activity(message, created_at) VALUES(?,?)",
               (message, datetime.now(timezone.utc).isoformat()))
    log("activity", "recorded", message=repr(message))


SELECT = ("SELECT e.*, (SELECT COUNT(*) FROM registrations r WHERE r.event_id=e.id) AS registered "
          "FROM events e")


def find_event(db, eid):
    row = db.execute(SELECT + " WHERE e.id=?", (eid,)).fetchone()
    if not row:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
    return row


# ---------- helpers ----------
def who(req: Request):
    role = req.headers.get("X-Role", "visitor").lower()
    if role not in ROLES:
        raise ApiError(400, "INVALID_ROLE", "Unknown role.")
    return role, req.headers.get("X-User", "")


def validate_event(body):
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_BODY", "Request body must be a JSON object.")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ApiError(400, "TITLE_REQUIRED", "Title is required.")
    raw = body.get("starts_at")
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "DATE_REQUIRED", "Date and time is required.")
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "DATE_INVALID", "Date and time is not a valid ISO 8601 value.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= datetime.now(timezone.utc):
        raise ApiError(400, "DATE_IN_PAST", "Date and time must not be in the past.")
    cap = body.get("capacity")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ApiError(400, "CAPACITY_INVALID", "Capacity must be a whole number greater than zero.")
    desc = body.get("description") or ""
    if not isinstance(desc, str):
        raise ApiError(400, "INVALID_BODY", "Description must be text.")
    return title.strip(), desc, when.isoformat(), cap


# ---------- app ----------
@asynccontextmanager
async def lifespan(_):
    init_db()
    yield


app = FastAPI(title="Community Events", lifespan=lifespan)


@app.middleware("http")
async def correlation(req: Request, call_next):
    rid = req.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request_id.set(rid)
    log("request", "received", method=req.method, path=req.url.path)  # no IP / UA logged
    resp = await call_next(req)
    resp.headers["X-Request-ID"] = rid
    return resp


@app.exception_handler(ApiError)
async def api_error(_, e: ApiError):
    log("error", "rejected", code=e.code, status=e.status)
    return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status)


@app.exception_handler(Exception)
async def unexpected(_, e: Exception):
    logger.exception("rid=%s unexpected error", request_id.get())  # details stay server-side
    return JSONResponse({"code": "INTERNAL_ERROR", "message": "Something went wrong."}, status_code=500)


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/events")
def list_events(req: Request):
    role, user = who(req)
    with tx() as db:
        if role == "admin":
            rows = db.execute(SELECT + " ORDER BY e.starts_at").fetchall()
        elif role == "organiser":
            rows = db.execute(SELECT + " WHERE e.organiser_id=? ORDER BY e.starts_at", (user,)).fetchall()
        else:
            rows = db.execute(SELECT + " WHERE e.status='PUBLISHED' ORDER BY e.starts_at").fetchall()
    log("list_events", "ok", role=role, count=len(rows))
    return [dict(r) for r in rows]


@app.get("/api/events/{eid}")
def get_event(eid: str, req: Request):
    role, user = who(req)
    with tx() as db:
        ev = dict(find_event(db, eid))
    visible = role == "admin" or ev["status"] == "PUBLISHED" or (role == "organiser" and ev["organiser_id"] == user)
    if not visible:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")  # don't reveal unpublished events
    return ev


@app.post("/api/events", status_code=201)
async def create_event(req: Request):
    role, user = who(req)
    if role != "organiser" or not user:
        raise ApiError(403, "FORBIDDEN", "Only organisers can create events.")
    title, desc, when, cap = validate_event(await req.json())
    eid = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    with tx() as db:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                   (eid, title, desc, when, cap, "PENDING_REVIEW", user))
        ev = dict(find_event(db, eid))
    log("create_event", "created", event_id=eid, organiser=user)
    return ev


@app.put("/api/events/{eid}")
async def update_event(eid: str, req: Request):
    role, user = who(req)
    if role != "organiser":
        raise ApiError(403, "FORBIDDEN", "Only organisers can update events.")
    title, desc, when, cap = validate_event(await req.json())
    with tx() as db:
        ev = find_event(db, eid)
        if ev["organiser_id"] != user:
            raise ApiError(403, "FORBIDDEN", "You can only update events you manage.")
        if cap < ev["registered"]:
            raise ApiError(400, "CAPACITY_INVALID", "Capacity cannot be below existing registrations.")
        db.execute("UPDATE events SET title=?, description=?, starts_at=?, capacity=? WHERE id=?",
                   (title, desc, when, cap, eid))
        out = dict(find_event(db, eid))
    log("update_event", "updated", event_id=eid)
    return out


@app.post("/api/events/{eid}/publish")
def publish(eid: str, req: Request):
    role, _ = who(req)
    if role != "admin":
        raise ApiError(403, "FORBIDDEN", "Only administrators can publish events.")
    with tx() as db:
        ev = find_event(db, eid)
        if ev["status"] != "PENDING_REVIEW":
            raise ApiError(409, "INVALID_STATE", "Only events pending review can be published.")
        db.execute("UPDATE events SET status='PUBLISHED' WHERE id=?", (eid,))
        record_activity(db, f"Event {eid} was published.")
        out = dict(find_event(db, eid))
    log("publish_event", "published", event_id=eid)
    return out


@app.post("/api/events/{eid}/registrations", status_code=201)
def register(eid: str):
    # Body, headers, IP and user agent are deliberately ignored: anonymous interest only.
    with tx() as db:
        ev = find_event(db, eid)
        if ev["status"] != "PUBLISHED":
            raise ApiError(409, "EVENT_NOT_PUBLISHED", "Registration is not available until the event is published.")
        if ev["registered"] >= ev["capacity"]:
            raise ApiError(409, "EVENT_FULL", "Registration is no longer available because this event is full.")
        rid = f"REG-{uuid.uuid4().hex[:8].upper()}"
        db.execute("INSERT INTO registrations VALUES(?,?,?)",
                   (rid, eid, datetime.now(timezone.utc).isoformat()))
        record_activity(db, f"Registration {rid} created for event {eid}.")
    log("register", "stored", event_id=eid, registration_id=rid)
    return {"registration_id": rid, "event_id": eid}


@app.get("/api/activity")
def activity():
    with tx() as db:
        rows = db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
