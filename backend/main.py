import os
import csv
import secrets
import bcrypt
import httpx
from urllib.parse import urlencode
from io import StringIO
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text, or_, func

from database import get_db, engine, Base
import models
import schemas

DEFAULT_TEMPLATE = {
    "field": "Bookkeeping",
    "name": "Standard bookkeeping tasks",
    "tasks": [
        {"name": "Pushing bills to Xero from Dext", "role": "Bookkeeper", "task_type": "Data Entry"},
        {"name": "Bank reconciliation", "role": "Bookkeeper", "task_type": "Reconciliation"},
        {"name": "Aged payables review", "role": "Senior Bookkeeper", "task_type": "Review"},
        {"name": "Aged receivables review", "role": "Senior Bookkeeper", "task_type": "Review"},
        {"name": "Queries preparation", "role": "Bookkeeper", "task_type": "Client Query"},
    ],
}

DEFAULT_ROLES = ["Bookkeeper", "Senior Bookkeeper"]
DEFAULT_TASK_TYPES = ["Data Entry", "Reconciliation", "Review", "Client Query"]
DEFAULT_TRACKED_METRICS = ["Unreconciled transactions", "Dext bills"]


def run_startup_migrations():
    # Base.metadata.create_all only creates tables that do not exist yet, it never adds a
    # new column to a table that is already there. Since this app has no separate migration
    # tool, this checks for columns the current code expects and adds any that are missing,
    # so a schema change does not need a manual database step to deploy.
    inspector = inspect(engine)
    if "members" in inspector.get_table_names():
        existing_columns = {c["name"] for c in inspector.get_columns("members")}
        with engine.begin() as conn:
            if "role" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN role VARCHAR DEFAULT 'member'"))
                conn.execute(text(
                    "UPDATE members SET role = 'admin' WHERE color_idx = (SELECT MIN(color_idx) FROM members)"
                ))
            if "email" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN email VARCHAR"))
            if "password_hash" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN password_hash VARCHAR"))
            if "google_refresh_token" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN google_refresh_token VARCHAR"))
            if "pod_id" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN pod_id VARCHAR"))
            if "slack_email" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN slack_email VARCHAR"))
            if "slack_user_id" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN slack_user_id VARCHAR"))
            if "notification_channel" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN notification_channel VARCHAR DEFAULT 'browser'"))

    if "clients" in inspector.get_table_names():
        existing_client_columns = {c["name"] for c in inspector.get_columns("clients")}
        with engine.begin() as conn:
            if "code" not in existing_client_columns:
                conn.execute(text("ALTER TABLE clients ADD COLUMN code VARCHAR"))

    if "template_tasks" in inspector.get_table_names():
        existing_tt_columns = {c["name"] for c in inspector.get_columns("template_tasks")}
        with engine.begin() as conn:
            if "requires_bank_account" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN requires_bank_account BOOLEAN DEFAULT FALSE"))
            if "tracks_number_label" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN tracks_number_label VARCHAR DEFAULT ''"))
            if "needs_pay_period" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN needs_pay_period BOOLEAN DEFAULT FALSE"))

    if "tasks" in inspector.get_table_names():
        existing_task_columns = {c["name"] for c in inspector.get_columns("tasks")}
        with engine.begin() as conn:
            if "bank_account_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN bank_account_id VARCHAR"))
            if "bank_account_name" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN bank_account_name VARCHAR DEFAULT ''"))
            if "tracks_number_label" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN tracks_number_label VARCHAR DEFAULT ''"))
            if "start_count" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN start_count INTEGER"))
            if "end_count" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN end_count INTEGER"))
            if "adjusted_seconds" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN adjusted_seconds FLOAT"))
            if "pay_period_type" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN pay_period_type VARCHAR"))
            if "pay_period_number" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN pay_period_number INTEGER"))
            if "source_calendar_event_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_calendar_event_id VARCHAR"))
            if "source_template_name" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_template_name VARCHAR"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_startup_migrations()
    db = next(get_db())
    try:
        if db.query(models.Template).count() == 0:
            tpl = models.Template(field=DEFAULT_TEMPLATE["field"], name=DEFAULT_TEMPLATE["name"])
            db.add(tpl)
            db.flush()
            for t in DEFAULT_TEMPLATE["tasks"]:
                db.add(models.TemplateTask(
                    template_id=tpl.id, name=t["name"], role=t["role"], task_type=t["task_type"]
                ))
            db.commit()
        if db.query(models.Role).count() == 0:
            for name in DEFAULT_ROLES:
                db.add(models.Role(name=name))
            db.commit()
        if db.query(models.TaskTypeOption).count() == 0:
            for name in DEFAULT_TASK_TYPES:
                db.add(models.TaskTypeOption(name=name))
            db.commit()
        if db.query(models.TrackedMetric).count() == 0:
            for name in DEFAULT_TRACKED_METRICS:
                db.add(models.TrackedMetric(name=name))
            db.commit()
    finally:
        db.close()
    yield


app = FastAPI(title="Clockbook", lifespan=lifespan)

# Lets the Vite dev server on localhost:5173 call this API during local development.
# In production the frontend is served by this same app, so this is not needed there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def get_current_member(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in")
    token = authorization[len("Bearer "):]
    session = db.get(models.Session, token)
    if not session:
        raise HTTPException(401, "Session expired, please log in again")
    member = db.get(models.Member, session.member_id)
    if not member:
        raise HTTPException(401, "Account no longer exists")
    return member


def require_admin(member):
    if member.role not in ("admin", "super_admin"):
        raise HTTPException(403, "This action requires an admin")


def is_admin_or_above(role):
    return role in ("admin", "super_admin")


def close_open_segment(segments, end_override=None):
    segments = list(segments or [])
    if not segments:
        return segments
    last = segments[-1]
    if last.get("end"):
        return segments
    end_value = end_override if end_override else datetime.utcnow().isoformat() + "Z"
    segments[-1] = {**last, "end": end_value}
    return segments


def elapsed_seconds(segments):
    total = 0.0
    now = datetime.utcnow()
    for seg in segments or []:
        start = parse_utc_naive(seg["start"])
        end = parse_utc_naive(seg["end"]) if seg.get("end") else now
        total += max(0, (end - start).total_seconds())
    return total


# ---------------------------------------------------------------
# Auth
# ---------------------------------------------------------------

@app.get("/api/auth/status")
def auth_status(db: Session = Depends(get_db)):
    # Once at least one account has a password, this bootstrap path closes and everyone
    # must log in normally. Before that, this tells the frontend whether to show a plain
    # sign up screen (brand new install) or a claim screen (upgrading an older workspace
    # that had passwordless accounts already in it).
    any_secured = db.query(models.Member).filter(models.Member.password_hash.isnot(None)).count()
    if any_secured > 0:
        return {"setup_needed": False, "unclaimed": []}
    unclaimed = db.query(models.Member).filter(models.Member.password_hash.is_(None)).all()
    return {"setup_needed": True, "unclaimed": [{"id": m.id, "name": m.name} for m in unclaimed]}


@app.post("/api/auth/claim", response_model=schemas.LoginResponse)
def claim_account(payload: schemas.ClaimAccountRequest, db: Session = Depends(get_db)):
    any_secured = db.query(models.Member).filter(models.Member.password_hash.isnot(None)).count()
    if any_secured > 0:
        raise HTTPException(400, "Accounts are already set up, please log in")
    email = payload.email.strip().lower()
    if db.query(models.Member).filter(models.Member.email == email).first():
        raise HTTPException(400, "That email is already registered")
    if payload.member_id:
        member = db.get(models.Member, payload.member_id)
        if not member or member.password_hash:
            raise HTTPException(400, "That account cannot be claimed")
        member.email = email
        member.password_hash = hash_password(payload.password)
        member.role = "admin"
    else:
        count = db.query(models.Member).count()
        member = models.Member(
            name=(payload.name or "Admin").strip() or "Admin",
            email=email, color_idx=count, role="admin",
            password_hash=hash_password(payload.password),
        )
        db.add(member)
    db.commit()
    db.refresh(member)
    token = secrets.token_urlsafe(32)
    db.add(models.Session(token=token, member_id=member.id))
    db.commit()
    return schemas.LoginResponse(token=token, member=member)


@app.post("/api/auth/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    member = db.query(models.Member).filter(models.Member.email == email).first()
    if not member or not member.password_hash or not verify_password(payload.password, member.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    token = secrets.token_urlsafe(32)
    db.add(models.Session(token=token, member_id=member.id))
    db.commit()
    return schemas.LoginResponse(token=token, member=member)


@app.post("/api/auth/logout", status_code=204)
def logout(authorization: str = Header(None), db: Session = Depends(get_db)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        session = db.get(models.Session, token)
        if session:
            db.delete(session)
            db.commit()
    return None


@app.get("/api/auth/me", response_model=schemas.MemberOut)
def get_me(current_member: models.Member = Depends(get_current_member)):
    return current_member


@app.get("/api/time")
def get_server_time():
    # Lets the browser measure any gap between its own clock and the server's, so a live
    # running timer can correct for it instead of drifting the moment a computer's clock
    # disagrees with the server, this has no effect on anything actually saved, every
    # stored timestamp already comes from the server regardless.
    return {"now": datetime.utcnow().isoformat() + "Z"}


# ---------------------------------------------------------------
# Google Calendar integration (optional, per person)
#
# Each person connects their own calendar if they want to, nobody is required to. Only a
# read-only scope is ever requested, nothing here can create, change, or delete anything in
# anyone's calendar. The refresh token this produces is the one long-lived secret involved,
# and it is never returned by any API response, MemberOut only ever exposes a computed
# connected boolean. A short-lived access token is fetched fresh from that refresh token each
# time a check actually happens, rather than cached, keeping the logic simple and avoiding any
# separate expiry bookkeeping.
# ---------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://clockbook.up.railway.app/api/auth/google/callback")
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


@app.get("/api/auth/google/connect-url")
def get_google_connect_url(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google Calendar integration has not been configured on this server yet")
    # Clear out any previous, unused attempt for this person before issuing a fresh one
    db.query(models.GoogleOAuthState).filter(models.GoogleOAuthState.member_id == current_member.id).delete()
    state_row = models.GoogleOAuthState(member_id=current_member.id)
    db.add(state_row)
    db.commit()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        # Forces Google to hand back a refresh token every time, not just on the very first
        # ever consent, so reconnecting after a disconnect still works correctly
        "prompt": "consent",
        "state": state_row.state,
    }
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}


@app.get("/api/auth/google/callback")
def google_oauth_callback(code: str = None, state: str = None, error: str = None, db: Session = Depends(get_db)):
    # This lands here via a plain browser redirect from Google, not an authenticated API
    # call, so the state value, checked against what was stored when the person clicked
    # Connect, is what safely identifies which member this belongs to.
    if error or not code or not state:
        return RedirectResponse(url="/?calendar=error")
    state_row = db.get(models.GoogleOAuthState, state)
    if not state_row:
        return RedirectResponse(url="/?calendar=error")
    member_id = state_row.member_id
    db.delete(state_row)
    db.commit()

    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=10)
        resp.raise_for_status()
        refresh_token = resp.json().get("refresh_token")
    except Exception:
        return RedirectResponse(url="/?calendar=error")

    if not refresh_token:
        return RedirectResponse(url="/?calendar=error")

    member = db.get(models.Member, member_id)
    if member:
        member.google_refresh_token = refresh_token
        db.commit()

    return RedirectResponse(url="/?calendar=connected")


@app.post("/api/auth/google/disconnect", response_model=schemas.MemberOut)
def disconnect_google_calendar(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    current_member.google_refresh_token = None
    db.commit()
    db.refresh(current_member)
    return current_member


def get_google_access_token(member):
    if not member.google_refresh_token:
        return None
    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "refresh_token": member.google_refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception:
        return None


@app.get("/api/calendar/meeting-now")
def get_meeting_now(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Only ever checks the calendar of whoever is asking, using only their own stored token,
    # there is no way for this to see or reveal another person's calendar or meetings.
    if not current_member.google_refresh_token:
        return {"connected": False, "meeting": None}
    access_token = get_google_access_token(current_member)
    if not access_token:
        return {"connected": True, "meeting": None}

    now = datetime.utcnow()
    # A generous look-back window so an already-in-progress meeting is still found, the
    # precise "is this actually happening right now" check happens below regardless
    time_min = (now - timedelta(hours=6)).isoformat() + "Z"
    time_max = (now + timedelta(minutes=1)).isoformat() + "Z"
    try:
        resp = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime"},
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("items", [])
    except Exception:
        return {"connected": True, "meeting": None}

    for event in events:
        has_meet_link = bool(event.get("hangoutLink")) or any(
            ep.get("entryPointType") == "video"
            for ep in (event.get("conferenceData") or {}).get("entryPoints", [])
        )
        if not has_meet_link:
            continue
        start = event.get("start", {}).get("dateTime")
        end = event.get("end", {}).get("dateTime")
        if not start or not end:
            continue  # an all-day event, not a timed meeting
        start_dt = parse_utc_naive(start)
        end_dt = parse_utc_naive(end)
        if start_dt and end_dt and start_dt <= now <= end_dt:
            return {"connected": True, "meeting": {"id": event.get("id"), "summary": event.get("summary") or "Meeting"}}

    return {"connected": True, "meeting": None}


@app.get("/api/calendar/events")
def get_calendar_events(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # A plain, real view of what is actually on the connected calendar, useful both as a
    # genuinely handy view and as the clearest possible proof the connection is working,
    # since seeing real events is far easier to verify than waiting for the exact right
    # moment for the background meeting-now check to fire.
    if not current_member.google_refresh_token:
        return {"connected": False, "events": []}
    access_token = get_google_access_token(current_member)
    if not access_token:
        return {"connected": True, "events": [], "error": "Could not refresh access, try reconnecting"}

    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=7)).isoformat() + "Z"
    try:
        resp = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime", "maxResults": 20},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception:
        return {"connected": True, "events": [], "error": "Could not reach Google Calendar"}

    events = []
    for event in items:
        start = event.get("start", {})
        end = event.get("end", {})
        has_meet_link = bool(event.get("hangoutLink")) or any(
            ep.get("entryPointType") == "video"
            for ep in (event.get("conferenceData") or {}).get("entryPoints", [])
        )
        events.append({
            "id": event.get("id"),
            "summary": event.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date"),
            "all_day": "dateTime" not in start,
            "has_meet_link": has_meet_link,
        })
    return {"connected": True, "events": events}


@app.get("/api/calendar/suggested-tasks")
def get_suggested_tasks(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Calendar events without a meeting link, these are never meetings to prompt a pause
    # for, they're plain work items that might be worth turning into a To Do task. Checks
    # against every task this person has ever created, not just the recent window the
    # dashboard itself is optimized for, so an event does not get suggested again just
    # because the task it already produced was submitted a while ago.
    if not current_member.google_refresh_token:
        return {"connected": False, "suggestions": []}
    access_token = get_google_access_token(current_member)
    if not access_token:
        return {"connected": True, "suggestions": [], "error": "Could not refresh access, try reconnecting"}

    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=7)).isoformat() + "Z"
    try:
        resp = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime", "maxResults": 20},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception:
        return {"connected": True, "suggestions": [], "error": "Could not reach Google Calendar"}

    already_used_ids = {
        row[0] for row in db.query(models.TaskInstance.source_calendar_event_id)
        .filter(
            models.TaskInstance.owner_id == current_member.id,
            models.TaskInstance.source_calendar_event_id.isnot(None),
        ).all()
    }
    dismissed_ids = {
        row[0] for row in db.query(models.DismissedSuggestion.calendar_event_id)
        .filter(models.DismissedSuggestion.member_id == current_member.id).all()
    }
    already_used_ids |= dismissed_ids

    suggestions = []
    for event in items:
        event_id = event.get("id")
        if not event_id or event_id in already_used_ids:
            continue
        # A recurring event gives every single occurrence its own unique id, the shared
        # recurringEventId is what actually identifies "this same repeating thing", a
        # one-off event has no recurringEventId at all, so it falls back to its own id
        series_id = event.get("recurringEventId") or event_id
        if series_id in dismissed_ids:
            continue
        has_meet_link = bool(event.get("hangoutLink")) or any(
            ep.get("entryPointType") == "video"
            for ep in (event.get("conferenceData") or {}).get("entryPoints", [])
        )
        if has_meet_link:
            continue
        start = event.get("start", {})
        suggestions.append({
            "id": event_id,
            "series_id": series_id,
            "summary": event.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date"),
            "all_day": "dateTime" not in start,
        })
    return {"connected": True, "suggestions": suggestions}


@app.post("/api/calendar/suggested-tasks/{event_id}/dismiss", status_code=204)
def dismiss_suggested_task(event_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Marking a suggestion as not needed is just a personal reminder being cleared, it never
    # touches or creates a task, so it does not need any of the task-creation permissions.
    existing = db.query(models.DismissedSuggestion).filter(
        models.DismissedSuggestion.member_id == current_member.id,
        models.DismissedSuggestion.calendar_event_id == event_id,
    ).first()
    if not existing:
        db.add(models.DismissedSuggestion(member_id=current_member.id, calendar_event_id=event_id))
        db.commit()
    return None


# ---------------------------------------------------------------
# Members
# ---------------------------------------------------------------

@app.get("/api/members", response_model=list[schemas.MemberOut])
def list_members(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Member).all()


@app.post("/api/members", response_model=schemas.MemberOut, status_code=201)
def create_member(payload: schemas.MemberCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    email = payload.email.strip().lower()
    if db.query(models.Member).filter(models.Member.email == email).first():
        raise HTTPException(400, "That email is already registered")
    count = db.query(models.Member).count()
    member = models.Member(
        name=payload.name.strip(), email=email, color_idx=count, role="member",
        password_hash=hash_password(payload.password),
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/role", response_model=schemas.MemberOut)
def update_member_role(member_id: str, payload: schemas.MemberRoleUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    if payload.role not in ("admin", "member", "super_admin"):
        raise HTTPException(400, "Role must be admin, member, or super_admin")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    # An admin can always promote themselves to super admin, since otherwise nobody could
    # ever become the first one. Touching anyone else's super admin status, granting it or
    # taking it away, is reserved for an existing super admin.
    acting_on_self = member_id == current_member.id
    if not acting_on_self and (payload.role == "super_admin" or member.role == "super_admin") and current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can manage another person's super admin access")
    if is_admin_or_above(member.role) and not is_admin_or_above(payload.role):
        admin_count = db.query(models.Member).filter(models.Member.role.in_(["admin", "super_admin"])).count()
        if admin_count <= 1:
            raise HTTPException(400, "At least one admin is required")
    member.role = payload.role
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/credentials", response_model=schemas.MemberOut)
def set_member_credentials(member_id: str, payload: schemas.LoginRequest, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Lets an admin give login access to a legacy passwordless account, or reset someone's
    # password if they are locked out. There is no email delivery in this app, so whatever
    # password is set here needs to be shared with that person directly.
    require_admin(current_member)
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    email = payload.email.strip().lower()
    existing = db.query(models.Member).filter(models.Member.email == email, models.Member.id != member_id).first()
    if existing:
        raise HTTPException(400, "That email is already registered")
    member.email = email
    member.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(member)
    return member


@app.delete("/api/members/{member_id}", status_code=204)
def delete_member(member_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    if member_id == current_member.id:
        raise HTTPException(400, "You cannot delete your own account")
    member = db.get(models.Member, member_id)
    if not member:
        return None
    if member.role == "super_admin" and current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can delete a super admin")
    if is_admin_or_above(member.role):
        admin_count = db.query(models.Member).filter(models.Member.role.in_(["admin", "super_admin"])).count()
        if admin_count <= 1:
            raise HTTPException(400, "At least one admin is required")
    has_tasks = db.query(models.TaskInstance).filter(
        or_(models.TaskInstance.owner_id == member_id, models.TaskInstance.submitted_by_id == member_id)
    ).count()
    if has_tasks > 0:
        raise HTTPException(400, "This person has tracked tasks and cannot be deleted")
    db.query(models.Session).filter(models.Session.member_id == member_id).delete()
    db.delete(member)
    db.commit()
    return None


# ---------------------------------------------------------------
# Slack notifications, optional per person. Deliberately uses users.list rather than
# users.lookupByEmail: Slack's own docs contradict themselves on whether a modern bot
# token can use that method, and other developers have hit it silently failing in
# practice, so this fetches the member list once and matches by email locally instead,
# a path that is unambiguously documented to work with a bot token.
# ---------------------------------------------------------------

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


def find_slack_user_id_by_email(email: str):
    if not SLACK_BOT_TOKEN:
        return None, "Slack is not set up for this workspace yet"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    cursor = None
    try:
        for _ in range(20):  # a firm-sized workspace should resolve well within this many pages
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = httpx.get("https://slack.com/api/users.list", headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return None, f"Slack error: {data.get('error', 'unknown')}"
            for member in data.get("members", []):
                if (member.get("profile", {}).get("email") or "").lower() == email.lower():
                    return member.get("id"), None
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        return None, "Could not reach Slack"
    return None, "No Slack user found with that email in this workspace"


def send_slack_message(slack_user_id: str, text: str):
    if not SLACK_BOT_TOKEN or not slack_user_id:
        return False
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": slack_user_id, "text": text},
            timeout=10,
        )
        return resp.json().get("ok", False)
    except Exception:
        return False


@app.patch("/api/members/{member_id}/slack", response_model=schemas.MemberOut)
def connect_slack(member_id: str, payload: schemas.SlackConnect, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Self-service only, matching how connecting Google Calendar already works, nobody
    # connects this on someone else's behalf
    if member_id != current_member.id:
        raise HTTPException(403, "You can only connect your own Slack account")
    email = payload.slack_email.strip()
    if not email:
        raise HTTPException(400, "Enter the email your Slack account uses")
    slack_user_id, error = find_slack_user_id_by_email(email)
    if not slack_user_id:
        raise HTTPException(400, error or "Could not find that Slack user")
    current_member.slack_email = email
    current_member.slack_user_id = slack_user_id
    db.commit()
    db.refresh(current_member)
    return current_member


@app.post("/api/members/{member_id}/slack/disconnect", response_model=schemas.MemberOut)
def disconnect_slack(member_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if member_id != current_member.id:
        raise HTTPException(403, "You can only disconnect your own Slack account")
    current_member.slack_email = None
    current_member.slack_user_id = None
    if current_member.notification_channel == "slack":
        current_member.notification_channel = "browser"
    db.commit()
    db.refresh(current_member)
    return current_member


@app.post("/api/members/{member_id}/slack/test")
def test_slack(member_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if member_id != current_member.id:
        raise HTTPException(403, "You can only test your own Slack connection")
    if not current_member.slack_user_id:
        raise HTTPException(400, "Connect Slack first")
    ok = send_slack_message(current_member.slack_user_id, "This is a test notification from Clockbook. If you can see this, it's working.")
    if not ok:
        raise HTTPException(400, "Could not send a test message, check the Slack setup")
    return {"sent": True}


@app.patch("/api/members/{member_id}/notification-channel", response_model=schemas.MemberOut)
def update_notification_channel(member_id: str, payload: schemas.NotificationChannelUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if member_id != current_member.id:
        raise HTTPException(403, "You can only change your own notification preference")
    if payload.channel not in ("browser", "slack"):
        raise HTTPException(400, "Channel must be browser or slack")
    if payload.channel == "slack" and not current_member.slack_user_id:
        raise HTTPException(400, "Connect Slack before switching to it")
    current_member.notification_channel = payload.channel
    db.commit()
    db.refresh(current_member)
    return current_member


@app.post("/api/notifications/relay")
def relay_notification(payload: dict, current_member: models.Member = Depends(get_current_member)):
    # The one place a notification actually gets sent to Slack. Only ever sends to the
    # calling person's own resolved Slack id, and only if they have chosen Slack as their
    # channel, so this can never be used to message anyone else
    if current_member.notification_channel != "slack" or not current_member.slack_user_id:
        return {"sent": False, "reason": "not using Slack"}
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "Missing text")
    ok = send_slack_message(current_member.slack_user_id, text)
    return {"sent": ok}


# ---------------------------------------------------------------
# Clients
# ---------------------------------------------------------------

@app.get("/api/clients", response_model=list[schemas.ClientOut])
def list_clients(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Client).all()


def check_client_code_available(db, code, exclude_client_id=None):
    if not code:
        return
    query = db.query(models.Client).filter(func.lower(models.Client.code) == code.lower())
    if exclude_client_id:
        query = query.filter(models.Client.id != exclude_client_id)
    existing = query.first()
    if existing:
        raise HTTPException(400, f'The code "{code}" is already used by {existing.name}')


@app.post("/api/clients", response_model=schemas.ClientOut, status_code=201)
def create_client(payload: schemas.ClientCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    code = payload.code.strip() if payload.code else None
    check_client_code_available(db, code)
    client = models.Client(name=payload.name.strip(), code=code)
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@app.patch("/api/clients/{client_id}", response_model=schemas.ClientOut)
def update_client(client_id: str, payload: schemas.ClientCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    client = db.get(models.Client, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    new_name = payload.name.strip()
    if not new_name:
        raise HTTPException(400, "Enter a client name")
    code = payload.code.strip() if payload.code else None
    check_client_code_available(db, code, exclude_client_id=client_id)
    client.name = new_name
    client.code = code
    # Tasks store their own copy of the client name for historical display, keep every
    # existing task in sync too, so old and new entries never show two different names for
    # what is now the same client.
    db.query(models.TaskInstance).filter(models.TaskInstance.client_id == client_id).update({"client_name": new_name})
    db.commit()
    db.refresh(client)
    return client


@app.delete("/api/clients/{client_id}", status_code=204)
def delete_client(client_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    used = db.query(models.TaskInstance).filter(models.TaskInstance.client_id == client_id).count()
    if used > 0:
        raise HTTPException(400, "This client has tracked tasks and cannot be deleted")
    client = db.get(models.Client, client_id)
    if client:
        db.delete(client)
        db.commit()
    return None


@app.post("/api/clients/{keep_id}/merge/{duplicate_id}", response_model=schemas.ClientOut)
def merge_clients(keep_id: str, duplicate_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Reassigns every task and bank account that belonged to the duplicate, across every
    # user in the firm, not just the person doing the merge, so nobody's tracked time gets
    # orphaned or silently lost. The duplicate client is then removed entirely.
    require_admin(current_member)
    if keep_id == duplicate_id:
        raise HTTPException(400, "Cannot merge a client into itself")
    keep_client = db.get(models.Client, keep_id)
    duplicate_client = db.get(models.Client, duplicate_id)
    if not keep_client or not duplicate_client:
        raise HTTPException(404, "Client not found")

    db.query(models.TaskInstance).filter(models.TaskInstance.client_id == duplicate_id).update(
        {"client_id": keep_id, "client_name": keep_client.name}, synchronize_session=False
    )
    db.query(models.BankAccount).filter(models.BankAccount.client_id == duplicate_id).update(
        {"client_id": keep_id}, synchronize_session=False
    )
    db.delete(duplicate_client)
    db.commit()
    db.refresh(keep_client)
    return keep_client


# ---------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------

@app.get("/api/bank-accounts", response_model=list[schemas.BankAccountOut])
def list_bank_accounts(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.BankAccount).all()


@app.post("/api/bank-accounts", response_model=schemas.BankAccountOut, status_code=201)
def create_bank_account(payload: schemas.BankAccountCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    account = models.BankAccount(client_id=payload.client_id, name=payload.name.strip())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.delete("/api/bank-accounts/{account_id}", status_code=204)
def delete_bank_account(account_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    used = db.query(models.TaskInstance).filter(models.TaskInstance.bank_account_id == account_id).count()
    if used > 0:
        raise HTTPException(400, "This bank account has tracked tasks and cannot be deleted")
    account = db.get(models.BankAccount, account_id)
    if account:
        db.delete(account)
        db.commit()
    return None


# ---------------------------------------------------------------
# Roles and task types
# ---------------------------------------------------------------

@app.get("/api/roles", response_model=list[schemas.RoleOut])
def list_roles(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Role).order_by(models.Role.name).all()


@app.post("/api/roles", response_model=schemas.RoleOut, status_code=201)
def create_role(payload: schemas.RoleCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    name = payload.name.strip()
    if db.query(models.Role).filter(models.Role.name == name).first():
        raise HTTPException(400, "That role already exists")
    role = models.Role(name=name)
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@app.delete("/api/roles/{role_id}", status_code=204)
def delete_role(role_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    role = db.get(models.Role, role_id)
    if role:
        db.delete(role)
        db.commit()
    return None


# ---------------------------------------------------------------
# Pods (teams). Any admin can see the list, so they know what pod they and others are in,
# but only a super admin can create, delete, or reassign one, since an admin who could
# change their own pod assignment could simply unassign themselves to see everyone again,
# which would make the restriction meaningless.
# ---------------------------------------------------------------

@app.get("/api/pods", response_model=list[schemas.PodOut])
def list_pods(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    return db.query(models.Pod).order_by(models.Pod.name).all()


@app.post("/api/pods", response_model=schemas.PodOut, status_code=201)
def create_pod(payload: schemas.PodCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can create pods")
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Enter a pod name")
    if db.query(models.Pod).filter(models.Pod.name == name).first():
        raise HTTPException(400, "A pod with this name already exists")
    pod = models.Pod(name=name)
    db.add(pod)
    db.commit()
    db.refresh(pod)
    return pod


@app.delete("/api/pods/{pod_id}", status_code=204)
def delete_pod(pod_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can delete pods")
    pod = db.get(models.Pod, pod_id)
    if pod:
        db.query(models.Member).filter(models.Member.pod_id == pod_id).update({"pod_id": None})
        db.delete(pod)
        db.commit()
    return None


@app.patch("/api/members/{member_id}/pod", response_model=schemas.MemberOut)
def update_member_pod(member_id: str, payload: schemas.MemberPodUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can change pod assignments")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    if payload.pod_id:
        if not db.get(models.Pod, payload.pod_id):
            raise HTTPException(404, "Pod not found")
    member.pod_id = payload.pod_id
    db.commit()
    db.refresh(member)
    return member


@app.get("/api/task-types", response_model=list[schemas.TaskTypeOut])
def list_task_types(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.TaskTypeOption).order_by(models.TaskTypeOption.name).all()


@app.post("/api/task-types", response_model=schemas.TaskTypeOut, status_code=201)
def create_task_type(payload: schemas.TaskTypeCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    name = payload.name.strip()
    if db.query(models.TaskTypeOption).filter(models.TaskTypeOption.name == name).first():
        raise HTTPException(400, "That task type already exists")
    task_type = models.TaskTypeOption(name=name)
    db.add(task_type)
    db.commit()
    db.refresh(task_type)
    return task_type


@app.delete("/api/task-types/{task_type_id}", status_code=204)
def delete_task_type(task_type_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task_type = db.get(models.TaskTypeOption, task_type_id)
    if task_type:
        db.delete(task_type)
        db.commit()
    return None


@app.get("/api/tracked-metrics", response_model=list[schemas.TrackedMetricOut])
def list_tracked_metrics(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.TrackedMetric).order_by(models.TrackedMetric.name).all()


@app.post("/api/tracked-metrics", response_model=schemas.TrackedMetricOut, status_code=201)
def create_tracked_metric(payload: schemas.TrackedMetricCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    name = payload.name.strip()
    if db.query(models.TrackedMetric).filter(models.TrackedMetric.name == name).first():
        raise HTTPException(400, "That metric already exists")
    metric = models.TrackedMetric(name=name)
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


@app.delete("/api/tracked-metrics/{metric_id}", status_code=204)
def delete_tracked_metric(metric_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    metric = db.get(models.TrackedMetric, metric_id)
    if metric:
        db.delete(metric)
        db.commit()
    return None


# ---------------------------------------------------------------
# Templates
# ---------------------------------------------------------------

@app.get("/api/templates", response_model=list[schemas.TemplateOut])
def list_templates(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Template).all()


@app.post("/api/templates", response_model=schemas.TemplateOut, status_code=201)
def create_template(payload: schemas.TemplateCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = models.Template(field=payload.field.strip(), name=payload.name.strip())
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@app.patch("/api/templates/{template_id}", response_model=schemas.TemplateOut)
def update_template(template_id: str, payload: schemas.TemplateCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = db.get(models.Template, template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    field = payload.field.strip()
    name = payload.name.strip()
    if not field or not name:
        raise HTTPException(400, "Enter both a field and a template name")
    old_name = tpl.name
    tpl.field = field
    tpl.name = name
    if old_name != name:
        db.query(models.TaskInstance).filter(models.TaskInstance.source_template_name == old_name).update(
            {"source_template_name": name}, synchronize_session=False
        )
    db.commit()
    db.refresh(tpl)
    return tpl


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template(template_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = db.get(models.Template, template_id)
    if tpl:
        db.delete(tpl)
        db.commit()
    return None


@app.post("/api/templates/{template_id}/tasks", response_model=schemas.TemplateTaskOut, status_code=201)
def add_template_task(template_id: str, payload: schemas.TemplateTaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = db.get(models.Template, template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    task = models.TemplateTask(
        template_id=template_id,
        name=payload.name.strip(),
        role=payload.role.strip(),
        task_type=payload.task_type.strip(),
        requires_bank_account=payload.requires_bank_account,
        tracks_number_label=payload.tracks_number_label.strip(),
        needs_pay_period=payload.needs_pay_period,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@app.put("/api/templates/{template_id}/tasks/{task_id}", response_model=schemas.TemplateTaskOut)
def update_template_task(template_id: str, task_id: str, payload: schemas.TemplateTaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task = db.get(models.TemplateTask, task_id)
    if not task or task.template_id != template_id:
        raise HTTPException(404, "Task not found")
    task.name = payload.name.strip()
    task.role = payload.role.strip()
    task.task_type = payload.task_type.strip()
    task.requires_bank_account = payload.requires_bank_account
    task.tracks_number_label = payload.tracks_number_label.strip()
    task.needs_pay_period = payload.needs_pay_period
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/templates/{template_id}/tasks/{task_id}", status_code=204)
def delete_template_task(template_id: str, task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task = db.get(models.TemplateTask, task_id)
    if task and task.template_id == template_id:
        db.delete(task)
        db.commit()
    return None


# ---------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------

@app.get("/api/tasks", response_model=list[schemas.TaskOut])
def list_tasks(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # The dashboard only ever needs work that is still active, plus whatever was submitted
    # recently. Without this, every task ever submitted stays in this response forever, and
    # this endpoint is polled every few seconds, so that only ever grows. The 3 day window is
    # deliberately generous, far wider than any single timezone offset could require, so the
    # frontend's own "is this actually today" check still decides exactly what counts,
    # unchanged, this just avoids sending months of already-finished history along for no
    # reason. Historical data is untouched, and Export always reaches it through /api/export.
    recent_cutoff = datetime.utcnow() - timedelta(days=3)
    query = db.query(models.TaskInstance).filter(
        or_(models.TaskInstance.status != "submitted", models.TaskInstance.submitted_at >= recent_cutoff)
    )
    if current_member.role == "super_admin":
        pass  # sees everything, including other super admins, regardless of any pod
    elif current_member.role == "admin":
        super_admin_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if super_admin_ids:
            query = query.filter(~models.TaskInstance.owner_id.in_(super_admin_ids))
        if current_member.pod_id:
            pod_member_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
            query = query.filter(models.TaskInstance.owner_id.in_(pod_member_ids))
    else:
        query = query.filter(models.TaskInstance.owner_id == current_member.id)
    return query.order_by(models.TaskInstance.created_at.desc()).all()


@app.post("/api/tasks", response_model=schemas.TaskOut, status_code=201)
def create_task(payload: schemas.TaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    owner_id = payload.owner_id or current_member.id
    if owner_id != current_member.id and not is_admin_or_above(current_member.role):
        raise HTTPException(403, "Only an admin can assign a task to someone else")
    task = models.TaskInstance(
        client_id=payload.client_id,
        client_name=payload.client_name,
        name=payload.name.strip(),
        role=payload.role.strip(),
        task_type=payload.task_type.strip(),
        owner_id=owner_id,
        status="todo",
        segments=[],
        bank_account_id=payload.bank_account_id,
        bank_account_name=payload.bank_account_name.strip(),
        tracks_number_label=payload.tracks_number_label.strip(),
        pay_period_type=payload.pay_period_type,
        pay_period_number=payload.pay_period_number,
        source_calendar_event_id=payload.source_calendar_event_id,
        source_template_name=payload.source_template_name,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/start", response_model=schemas.TaskOut)
def start_task(task_id: str, payload: schemas.TaskStart = schemas.TaskStart(), current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.status == "submitted":
        raise HTTPException(400, "This task has already been submitted and cannot be started again")

    if task.tracks_number_label and task.start_count is None:
        if payload.start_count is None:
            raise HTTPException(400, f"Enter the starting {task.tracks_number_label.lower()} before starting the timer")
        task.start_count = payload.start_count

    # Enforce the one running timer per person rule on the server, not just in the browser
    others = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id == current_member.id,
        models.TaskInstance.status == "running",
        models.TaskInstance.id != task_id,
    ).all()
    for other in others:
        other.segments = close_open_segment(other.segments)
        other.status = "paused"

    if not task.owner_id:
        task.owner_id = current_member.id
    already_running_with_open_segment = (
        task.status == "running" and bool(task.segments) and not task.segments[-1].get("end")
    )
    if not already_running_with_open_segment:
        task.segments = [*(task.segments or []), {"start": datetime.utcnow().isoformat() + "Z", "end": None}]
    task.status = "running"
    db.commit()
    db.refresh(task)
    print(f"[timer-diagnostic] start task={task.id} status={task.status} "
          f"last_segment_start={task.segments[-1]['start'] if task.segments else None} "
          f"last_segment_end={task.segments[-1].get('end') if task.segments else None}")
    return task


@app.post("/api/tasks/{task_id}/pause", response_model=schemas.TaskOut)
def pause_task(task_id: str, payload: schemas.TaskPause = schemas.TaskPause(), current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    task.segments = close_open_segment(task.segments, payload.end_at)
    task.status = "paused"
    db.commit()
    db.refresh(task)
    open_count = sum(1 for s in (task.segments or []) if not s.get("end"))
    print(f"[timer-diagnostic] pause task={task.id} status={task.status} "
          f"last_segment_start={task.segments[-1]['start'] if task.segments else None} "
          f"last_segment_end={task.segments[-1].get('end') if task.segments else None} "
          f"open_segments_remaining={open_count}")
    return task


@app.post("/api/tasks/{task_id}/reset", response_model=schemas.TaskOut)
def reset_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # For a mistaken click, wipes all tracked time back to zero and returns the task to
    # To do, rather than deleting the task itself. The owner can fix their own mistake, and
    # an admin can step in too if someone needs help undoing it.
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id and not is_admin_or_above(current_member.role):
        raise HTTPException(403, "This task belongs to someone else")
    if task.status not in ("running", "paused"):
        raise HTTPException(400, "Only a running or paused task can be reset")
    task.segments = []
    task.status = "todo"
    task.start_count = None
    task.end_count = None
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/submit", response_model=schemas.TaskOut)
def submit_task(task_id: str, payload: schemas.TaskSubmit, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.tracks_number_label and payload.end_count is None:
        raise HTTPException(400, f"Enter the ending {task.tracks_number_label.lower()} before submitting")
    if payload.adjusted_seconds is not None and payload.adjusted_seconds < 0:
        raise HTTPException(400, "Adjusted time cannot be negative")
    task.segments = close_open_segment(task.segments)
    tracked_seconds = elapsed_seconds(task.segments)
    # Only actually record an adjustment if it genuinely differs from what was tracked,
    # a coincidental match should not get flagged as an edit
    if payload.adjusted_seconds is not None and abs(payload.adjusted_seconds - tracked_seconds) >= 1:
        task.adjusted_seconds = payload.adjusted_seconds
    else:
        task.adjusted_seconds = None
    task.status = "submitted"
    task.note = payload.note
    task.end_count = payload.end_count
    if payload.role is not None:
        task.role = payload.role
    if payload.task_type is not None:
        task.task_type = payload.task_type
    task.submitted_at = datetime.utcnow()
    task.submitted_by_id = current_member.id
    task.pushed_to_karbon = False
    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/reassign", response_model=schemas.TaskOut)
def reassign_task(task_id: str, payload: schemas.TaskReassign, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status == "running":
        raise HTTPException(400, "Pause the timer before reassigning this task")
    task.owner_id = payload.owner_id
    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/toggle-pushed", response_model=schemas.TaskOut)
def toggle_pushed(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.pushed_to_karbon = not task.pushed_to_karbon
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        return None
    if not is_admin_or_above(current_member.role):
        if task.owner_id != current_member.id:
            raise HTTPException(403, "This task belongs to someone else")
        if task.status == "submitted":
            raise HTTPException(403, "Only an admin can delete a task that has already been submitted")
    db.delete(task)
    db.commit()
    return None


def find_orphaned_open_segments(segments):
    # Any segment other than the very last one that has no end is orphaned, close_open_segment
    # never looks at these, only ever the last one, so nothing in the app was ever going to
    # notice or fix them on its own
    segments = segments or []
    return [i for i, s in enumerate(segments[:-1]) if not s.get("end")]


@app.get("/api/admin/scan-corrupted-tasks")
def scan_corrupted_tasks(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    results = []
    for task in db.query(models.TaskInstance).all():
        orphaned = find_orphaned_open_segments(task.segments)
        last_segment_open = bool(task.segments) and not task.segments[-1].get("end")
        stuck_last_segment = last_segment_open and task.status != "running"
        if orphaned or stuck_last_segment:
            results.append({
                "id": task.id, "name": task.name, "client_name": task.client_name, "status": task.status,
                "current_elapsed_hours": round(elapsed_seconds(task.segments) / 3600, 2),
                "orphaned_segment_count": len(orphaned),
                "last_segment_stuck_open": stuck_last_segment,
            })
    return {"affected_tasks": results}


@app.post("/api/tasks/{task_id}/repair-segments")
def repair_task_segments(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Closes every orphaned segment, using the next segment's own start time, since that is
    # the moment the gap this segment represents actually ended. If the very last segment is
    # also stuck open on a task that isn't running, that mirrors the exact bug already fixed
    # for new tasks, closed here using submitted_at for a submitted task, or right now for a
    # paused one, since there is no way to recover the true original moment.
    require_admin(current_member)
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    segments = list(task.segments or [])
    before_hours = round(elapsed_seconds(segments) / 3600, 2)
    for i in find_orphaned_open_segments(segments):
        segments[i] = {**segments[i], "end": segments[i + 1]["start"]}
    if segments and not segments[-1].get("end") and task.status != "running":
        fallback_end = task.submitted_at.isoformat() + "Z" if task.status == "submitted" and task.submitted_at else datetime.utcnow().isoformat() + "Z"
        segments[-1] = {**segments[-1], "end": fallback_end}
    task.segments = segments
    db.commit()
    db.refresh(task)
    after_hours = round(elapsed_seconds(task.segments) / 3600, 2)
    return {"task": schemas.TaskOut.model_validate(task), "before_hours": before_hours, "after_hours": after_hours}


# ---------------------------------------------------------------
# Export
# ---------------------------------------------------------------

def parse_utc_naive(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def build_export_rows(db, client_id, pushed, date_from=None, date_to=None, submitted_by=None, exclude_owner_ids=None, include_owner_ids=None):
    query = db.query(models.TaskInstance).filter(models.TaskInstance.status == "submitted")
    if client_id and client_id != "all":
        query = query.filter(models.TaskInstance.client_id == client_id)
    if submitted_by and submitted_by != "all":
        query = query.filter(models.TaskInstance.submitted_by_id == submitted_by)
    if exclude_owner_ids:
        query = query.filter(~models.TaskInstance.submitted_by_id.in_(exclude_owner_ids))
    if include_owner_ids is not None:
        query = query.filter(models.TaskInstance.submitted_by_id.in_(include_owner_ids))
    if pushed == "pending":
        query = query.filter(models.TaskInstance.pushed_to_karbon.is_(False))
    elif pushed == "pushed":
        query = query.filter(models.TaskInstance.pushed_to_karbon.is_(True))
    from_dt = parse_utc_naive(date_from)
    to_dt = parse_utc_naive(date_to)
    if from_dt:
        query = query.filter(models.TaskInstance.submitted_at >= from_dt)
    if to_dt:
        query = query.filter(models.TaskInstance.submitted_at <= to_dt)
    tasks = query.order_by(models.TaskInstance.submitted_at.desc()).all()

    members = {m.id: m.name for m in db.query(models.Member).all()}
    rows = []
    for t in tasks:
        tracked_seconds = elapsed_seconds(t.segments)
        is_adjusted = t.adjusted_seconds is not None
        final_seconds = t.adjusted_seconds if is_adjusted else tracked_seconds
        change = None
        if t.start_count is not None and t.end_count is not None:
            change = t.end_count - t.start_count
        rows.append({
            "id": t.id,
            "date": t.submitted_at.strftime("%Y-%m-%d") if t.submitted_at else "",
            "submitted_at": (t.submitted_at.isoformat() + "Z") if t.submitted_at else None,
            "client": t.client_name,
            "task": t.name,
            "role": t.role,
            "task_type": t.task_type,
            "seconds": round(final_seconds, 1),
            "tracked_seconds": round(tracked_seconds, 1) if is_adjusted else None,
            "hours": round(final_seconds / 3600, 2),
            "tracked_hours": round(tracked_seconds / 3600, 2) if is_adjusted else None,
            "adjusted": is_adjusted,
            "note": t.note,
            "tracked_by": members.get(t.submitted_by_id, ""),
            "pushed": t.pushed_to_karbon,
            "bank_account": t.bank_account_name,
            "metric": t.tracks_number_label,
            "start_count": t.start_count,
            "end_count": t.end_count,
            "change": change,
            "pay_period_type": t.pay_period_type,
            "pay_period_number": t.pay_period_number,
        })
    return rows


@app.get("/api/export")
def get_export(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    exclude_owner_ids = None
    include_owner_ids = None
    if current_member.role == "member":
        submitted_by = current_member.id
    elif current_member.role == "admin":
        exclude_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if current_member.pod_id:
            include_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
    return build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by, exclude_owner_ids, include_owner_ids)


@app.get("/api/export.csv")
def get_export_csv(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    exclude_owner_ids = None
    include_owner_ids = None
    if current_member.role == "member":
        submitted_by = current_member.id
    elif current_member.role == "admin":
        exclude_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if current_member.pod_id:
            include_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
    rows = build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by, exclude_owner_ids, include_owner_ids)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Date", "Client", "Task", "Role", "Task Type", "Hours", "Tracked Hours", "Notes", "Tracked by", "Pushed to Karbon",
        "Bank Account", "Metric", "Start Count", "End Count", "Change",
    ])
    for r in rows:
        writer.writerow([
            r["date"], r["client"], r["task"], r["role"], r["task_type"],
            r["hours"], r["tracked_hours"] if r["tracked_hours"] is not None else "",
            r["note"], r["tracked_by"], "Yes" if r["pushed"] else "No",
            r["bank_account"], r["metric"],
            r["start_count"] if r["start_count"] is not None else "",
            r["end_count"] if r["end_count"] is not None else "",
            r["change"] if r["change"] is not None else "",
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=karbon-time-export.csv"},
    )


# ---------------------------------------------------------------
# Serve the built frontend (production only, see frontend/vite.config.js)
# ---------------------------------------------------------------

if os.path.isdir("dist"):
    app.mount("/assets", StaticFiles(directory="dist/assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        candidate = os.path.join("dist", full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse("dist/index.html")
