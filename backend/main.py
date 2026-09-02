import os
import csv
from io import StringIO
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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
        start = datetime.fromisoformat(seg["start"])
        end = datetime.fromisoformat(seg["end"]) if seg.get("end") else now
        total += max(0, (end - start).total_seconds())
    return total


def require_admin(actor_id, db):
    # There is no real login yet, so this checks the role of whichever member id the
    # browser says is acting, rather than a verified session. It stops accidental misuse
    # through the app itself, not a determined attempt to bypass it directly through the
    # API. Real enforcement arrives with proper authentication later.
    if not actor_id:
        raise HTTPException(403, "This action requires an admin")
    actor = db.get(models.Member, actor_id)
    if not actor or actor.role != "admin":
        raise HTTPException(403, "This action requires an admin")
    return actor


# ---------------------------------------------------------------
# Members
# ---------------------------------------------------------------

@app.get("/api/members", response_model=list[schemas.MemberOut])
def list_members(db: Session = Depends(get_db)):
    return db.query(models.Member).all()


@app.post("/api/members", response_model=schemas.MemberOut, status_code=201)
def create_member(payload: schemas.MemberCreate, db: Session = Depends(get_db)):
    count = db.query(models.Member).count()
    if count == 0:
        # The first person to ever open the app becomes its first admin
        member = models.Member(name=payload.name.strip(), color_idx=0, role="admin")
    else:
        require_admin(payload.created_by, db)
        member = models.Member(name=payload.name.strip(), color_idx=count, role="member")
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/role", response_model=schemas.MemberOut)
def update_member_role(member_id: str, payload: schemas.MemberRoleUpdate, db: Session = Depends(get_db)):
    require_admin(payload.actor_id, db)
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


# ---------------------------------------------------------------
# Clients
# ---------------------------------------------------------------

@app.get("/api/clients", response_model=list[schemas.ClientOut])
def list_clients(db: Session = Depends(get_db)):
    return db.query(models.Client).all()


@app.post("/api/clients", response_model=schemas.ClientOut, status_code=201)
def create_client(payload: schemas.ClientCreate, db: Session = Depends(get_db)):
    client = models.Client(name=payload.name.strip())
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@app.delete("/api/clients/{client_id}", status_code=204)
def delete_client(client_id: str, actor_id: str = None, db: Session = Depends(get_db)):
    require_admin(actor_id, db)
    used = db.query(models.TaskInstance).filter(models.TaskInstance.client_id == client_id).count()
    if used > 0:
        raise HTTPException(400, "This client has tracked tasks and cannot be deleted")
    client = db.get(models.Client, client_id)
    if client:
        db.delete(client)
        db.commit()
    return None


# ---------------------------------------------------------------
# Templates
# ---------------------------------------------------------------

@app.get("/api/templates", response_model=list[schemas.TemplateOut])
def list_templates(db: Session = Depends(get_db)):
    return db.query(models.Template).all()


@app.post("/api/templates", response_model=schemas.TemplateOut, status_code=201)
def create_template(payload: schemas.TemplateCreate, db: Session = Depends(get_db)):
    require_admin(payload.actor_id, db)
    tpl = models.Template(field=payload.field.strip(), name=payload.name.strip())
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template(template_id: str, actor_id: str = None, db: Session = Depends(get_db)):
    require_admin(actor_id, db)
    tpl = db.get(models.Template, template_id)
    if tpl:
        db.delete(tpl)
        db.commit()
    return None


@app.post("/api/templates/{template_id}/tasks", response_model=schemas.TemplateTaskOut, status_code=201)
def add_template_task(template_id: str, payload: schemas.TemplateTaskCreate, db: Session = Depends(get_db)):
    require_admin(payload.actor_id, db)
    tpl = db.get(models.Template, template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    task = models.TemplateTask(
        template_id=template_id,
        name=payload.name.strip(),
        role=payload.role.strip(),
        task_type=payload.task_type.strip(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@app.put("/api/templates/{template_id}/tasks/{task_id}", response_model=schemas.TemplateTaskOut)
def update_template_task(template_id: str, task_id: str, payload: schemas.TemplateTaskCreate, db: Session = Depends(get_db)):
    require_admin(payload.actor_id, db)
    task = db.get(models.TemplateTask, task_id)
    if not task or task.template_id != template_id:
        raise HTTPException(404, "Task not found")
    task.name = payload.name.strip()
    task.role = payload.role.strip()
    task.task_type = payload.task_type.strip()
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/templates/{template_id}/tasks/{task_id}", status_code=204)
def delete_template_task(template_id: str, task_id: str, actor_id: str = None, db: Session = Depends(get_db)):
    require_admin(actor_id, db)
    task = db.get(models.TemplateTask, task_id)
    if task and task.template_id == template_id:
        db.delete(task)
        db.commit()
    return None


# ---------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------

@app.get("/api/tasks", response_model=list[schemas.TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(models.TaskInstance).order_by(models.TaskInstance.created_at.desc()).all()


@app.post("/api/tasks", response_model=schemas.TaskOut, status_code=201)
def create_task(payload: schemas.TaskCreate, db: Session = Depends(get_db)):
    task = models.TaskInstance(
        client_id=payload.client_id,
        client_name=payload.client_name,
        name=payload.name.strip(),
        role=payload.role.strip(),
        task_type=payload.task_type.strip(),
        owner_id=payload.owner_id,
        status="todo",
        segments=[],
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/start", response_model=schemas.TaskOut)
def start_task(task_id: str, payload: schemas.TaskStart, db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    # Enforce the one running timer per person rule on the server, not just in the browser
    others = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id == payload.owner_id,
        models.TaskInstance.status == "running",
        models.TaskInstance.id != task_id,
    ).all()
    for other in others:
        other.segments = close_open_segment(other.segments)
        other.status = "paused"

    if not task.owner_id:
        task.owner_id = payload.owner_id
    task.segments = [*(task.segments or []), {"start": datetime.utcnow().isoformat() + "Z", "end": None}]
    task.status = "running"
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/pause", response_model=schemas.TaskOut)
def pause_task(task_id: str, payload: schemas.TaskPause = schemas.TaskPause(), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.segments = close_open_segment(task.segments, payload.end_at)
    task.status = "paused"
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/submit", response_model=schemas.TaskOut)
def submit_task(task_id: str, payload: schemas.TaskSubmit, db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status == "running":
        task.segments = close_open_segment(task.segments)
    task.status = "submitted"
    task.note = payload.note
    task.submitted_at = datetime.utcnow()
    task.submitted_by_id = task.owner_id
    task.pushed_to_karbon = False
    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/reassign", response_model=schemas.TaskOut)
def reassign_task(task_id: str, payload: schemas.TaskReassign, db: Session = Depends(get_db)):
    require_admin(payload.actor_id, db)
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
def toggle_pushed(task_id: str, db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.pushed_to_karbon = not task.pushed_to_karbon
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str, actor_id: str = None, db: Session = Depends(get_db)):
    require_admin(actor_id, db)
    task = db.get(models.TaskInstance, task_id)
    if task:
        db.delete(task)
        db.commit()
    return None


# ---------------------------------------------------------------
# Export
# ---------------------------------------------------------------

def parse_utc_naive(value):
    # The browser sends full timestamps ending in Z (UTC). submitted_at is stored as naive
    # UTC, so this converts to the same naive form for a correct comparison, the same fix
    # applied earlier to the timer display.
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def build_export_rows(db, client_id, pushed, date_from=None, date_to=None):
    query = db.query(models.TaskInstance).filter(models.TaskInstance.status == "submitted")
    if client_id and client_id != "all":
        query = query.filter(models.TaskInstance.client_id == client_id)
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
        hours = elapsed_seconds(t.segments) / 3600
        rows.append({
            "id": t.id,
            "date": t.submitted_at.strftime("%Y-%m-%d") if t.submitted_at else "",
            "client": t.client_name,
            "task": t.name,
            "role": t.role,
            "task_type": t.task_type,
            "hours": round(hours, 2),
            "note": t.note,
            "tracked_by": members.get(t.submitted_by_id, ""),
            "pushed": t.pushed_to_karbon,
        })
    return rows


@app.get("/api/export")
def get_export(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    return build_export_rows(db, client_id, pushed, date_from, date_to)


@app.get("/api/export.csv")
def get_export_csv(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    rows = build_export_rows(db, client_id, pushed, date_from, date_to)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Client", "Task", "Role", "Task Type", "Hours", "Notes", "Tracked by", "Pushed to Karbon"])
    for r in rows:
        writer.writerow([
            r["date"], r["client"], r["task"], r["role"], r["task_type"],
            r["hours"], r["note"], r["tracked_by"], "Yes" if r["pushed"] else "No",
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
