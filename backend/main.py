import os
import csv
import secrets
import bcrypt
from io import StringIO
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text, or_

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

    if "template_tasks" in inspector.get_table_names():
        existing_tt_columns = {c["name"] for c in inspector.get_columns("template_tasks")}
        with engine.begin() as conn:
            if "requires_bank_account" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN requires_bank_account BOOLEAN DEFAULT FALSE"))
            if "tracks_number_label" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN tracks_number_label VARCHAR DEFAULT ''"))

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
    if member.role != "admin":
        raise HTTPException(403, "This action requires an admin")


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
    if payload.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be admin or member")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    if member.role == "admin" and payload.role == "member":
        admin_count = db.query(models.Member).filter(models.Member.role == "admin").count()
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
    if member.role == "admin":
        admin_count = db.query(models.Member).filter(models.Member.role == "admin").count()
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
# Clients
# ---------------------------------------------------------------

@app.get("/api/clients", response_model=list[schemas.ClientOut])
def list_clients(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Client).all()


@app.post("/api/clients", response_model=schemas.ClientOut, status_code=201)
def create_client(payload: schemas.ClientCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    client = models.Client(name=payload.name.strip())
    db.add(client)
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
    if current_member.role != "admin":
        query = query.filter(models.TaskInstance.owner_id == current_member.id)
    return query.order_by(models.TaskInstance.created_at.desc()).all()


@app.post("/api/tasks", response_model=schemas.TaskOut, status_code=201)
def create_task(payload: schemas.TaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    owner_id = payload.owner_id or current_member.id
    if owner_id != current_member.id and current_member.role != "admin":
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
    task.segments = [*(task.segments or []), {"start": datetime.utcnow().isoformat() + "Z", "end": None}]
    task.status = "running"
    db.commit()
    db.refresh(task)
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
    return task


@app.post("/api/tasks/{task_id}/reset", response_model=schemas.TaskOut)
def reset_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # For a mistaken click, wipes all tracked time back to zero and returns the task to
    # To do, rather than deleting the task itself. The owner can fix their own mistake, and
    # an admin can step in too if someone needs help undoing it.
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id and current_member.role != "admin":
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
    require_admin(current_member)
    task = db.get(models.TaskInstance, task_id)
    if task:
        db.delete(task)
        db.commit()
    return None


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


def build_export_rows(db, client_id, pushed, date_from=None, date_to=None, submitted_by=None):
    query = db.query(models.TaskInstance).filter(models.TaskInstance.status == "submitted")
    if client_id and client_id != "all":
        query = query.filter(models.TaskInstance.client_id == client_id)
    if submitted_by and submitted_by != "all":
        query = query.filter(models.TaskInstance.submitted_by_id == submitted_by)
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
        })
    return rows


@app.get("/api/export")
def get_export(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "admin":
        submitted_by = current_member.id
    return build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by)


@app.get("/api/export.csv")
def get_export_csv(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "admin":
        submitted_by = current_member.id
    rows = build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by)
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
        return FileResponse("dist/index.html")
