import os
import csv
import secrets
import base64
import hashlib
import hmac
import html
import bcrypt
import httpx
import time
import logging
import json
from urllib.parse import urlencode
from io import StringIO
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text, or_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from database import get_db, engine, Base, SessionLocal
import models
import schemas

CLOCKBOOK_VERSION = (os.environ.get("CLOCKBOOK_VERSION") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown").strip()
STARTUP_MIGRATION_LOCK_KEY = 424242017

# Transactional invitation email. These are deployment settings, not tenant-owned data.
# RESEND_FROM_EMAIL should use a verified Resend domain in production, for example:
# ClockBook <invites@clockbook.example>
RESEND_API_KEY = (os.environ.get("RESEND_API_KEY") or "").strip()
RESEND_FROM_EMAIL = (os.environ.get("RESEND_FROM_EMAIL") or "").strip()
RESEND_REPLY_TO = (os.environ.get("RESEND_REPLY_TO") or "").strip()
CLOCKBOOK_PUBLIC_URL = (os.environ.get("CLOCKBOOK_PUBLIC_URL") or "").strip().rstrip("/")
RESEND_EMAIL_ENDPOINT = "https://api.resend.com/emails"
MAX_REQUEST_BYTES = int(os.environ.get("CLOCKBOOK_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

logger = logging.getLogger("clockbook")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(getattr(logging, (os.environ.get("CLOCKBOOK_LOG_LEVEL") or "INFO").upper(), logging.INFO))
logger.propagate = False


def _log_event(event: str, **fields):
    payload = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **fields}
    logger.info(json.dumps(payload, default=str, separators=(",", ":")))


def _rate_limit_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _enforce_rate_limit(db: Session, identity: str, limit: int, window_seconds: int):
    """Database-backed fixed-window limiter, safe across multiple Railway app instances."""
    now = datetime.utcnow()
    epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    window_epoch = epoch - (epoch % window_seconds)
    window_start = datetime.utcfromtimestamp(window_epoch)
    key = _rate_limit_identity(identity)

    # Lock an existing bucket on PostgreSQL. SQLite serializes writes itself. A first-use
    # insert can race, so retry once after an IntegrityError.
    for attempt in range(2):
        try:
            query = db.query(models.RateLimitBucket).filter(models.RateLimitBucket.key == key)
            if engine.dialect.name == "postgresql":
                query = query.with_for_update()
            bucket = query.first()
            if bucket is None:
                db.add(models.RateLimitBucket(key=key, window_start=window_start, count=1, updated_at=now))
                db.commit()
                return
            if bucket.window_start != window_start:
                bucket.window_start = window_start
                bucket.count = 1
            elif bucket.count >= limit:
                db.rollback()
                retry_after = max(1, window_seconds - (epoch - window_epoch))
                raise HTTPException(429, "Too many attempts. Please try again shortly.", headers={"Retry-After": str(retry_after)})
            else:
                bucket.count += 1
            bucket.updated_at = now
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise


def _client_ip(request: Request) -> str:
    # On Railway/proxies, X-Forwarded-For is the useful client address. Only the first hop
    # is used and it is never persisted raw; it is immediately hashed into a rate-limit key.
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _ensure_operational_indexes():
    """Add safe, non-semantic indexes for common production query paths."""
    if engine.dialect.name not in ("postgresql", "sqlite"):
        return
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_owner_status ON tasks (tenant_id, owner_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_submitted_at ON tasks (tenant_id, submitted_at)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_submitted_by_date ON tasks (tenant_id, submitted_by_id, submitted_at)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_client_status ON tasks (tenant_id, client_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_submitted_pod_date ON tasks (tenant_id, submitted_pod_id, submitted_at)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_calendar_event ON tasks (tenant_id, source_calendar_event_id)",
        "CREATE INDEX IF NOT EXISTS ix_members_tenant_pod ON members (tenant_id, pod_id)",
        "CREATE INDEX IF NOT EXISTS ix_sessions_tenant_member ON sessions (tenant_id, member_id)",
        "CREATE INDEX IF NOT EXISTS ix_help_events_tenant_member_created ON help_events (tenant_id, member_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_inactivity_tenant_member_started ON inactivity_events (tenant_id, member_id, started_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_invitations_pending_email ON tenant_invitations (tenant_id, LOWER(email)) WHERE accepted_at IS NULL AND revoked_at IS NULL",
    ]
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for stmt in statements:
            table = stmt.split(" ON ", 1)[1].split(" ", 1)[0]
            if table in tables:
                conn.execute(text(stmt))

    # New client codes are mandatory and case-insensitively unique. Legacy installations
    # can already contain duplicate/non-coded clients, so only add the database invariant
    # once the existing non-empty codes are clean. Until then, the API-level advisory lock
    # and availability check still prevent any new duplicate code from being created.
    if "clients" in tables:
        with engine.begin() as conn:
            duplicate_code = conn.execute(text(
                "SELECT LOWER(TRIM(code)) AS normalized_code "
                "FROM clients WHERE code IS NOT NULL AND TRIM(code) <> '' "
                "GROUP BY tenant_id, LOWER(TRIM(code)) HAVING COUNT(*) > 1 LIMIT 1"
            )).first()
            if duplicate_code is None:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_tenant_code_ci "
                    "ON clients (tenant_id, LOWER(code)) WHERE code IS NOT NULL AND TRIM(code) <> ''"
                ))
            else:
                _log_event("client_code_unique_index_pending", duplicate_code=duplicate_code[0])


def _cleanup_operational_state(db: Session):
    cutoff = datetime.utcnow() - timedelta(days=2)
    db.query(models.RateLimitBucket).filter(models.RateLimitBucket.updated_at < cutoff).delete(synchronize_session=False)
    db.query(models.GoogleOAuthState).filter(models.GoogleOAuthState.created_at < datetime.utcnow() - timedelta(hours=1)).delete(synchronize_session=False)
    db.commit()


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
BUILTIN_HELPING_TASK_TYPE = "Helping/Training"
LEGACY_BUILTIN_HELPING_TASK_TYPE = "Helping"
BUILTIN_LEARNING_TASK_TYPE = "Learning & Development"
INTERNAL_MEETING_TASK_TYPE = "Non-billable: Colleague Meeting"
MIN_LEARNING_NOTE_WORDS = 5
MAX_LEARNING_NOTE_WORDS = 40
DEFAULT_TASK_TYPES = ["Data Entry", "Reconciliation", "Review", "Client Query", BUILTIN_HELPING_TASK_TYPE, BUILTIN_LEARNING_TASK_TYPE]
DEFAULT_LEARNING_CATEGORIES = [
    "Tax", "VAT", "Payroll", "Bookkeeping", "Year-End Accounts", "Accounts Production",
    "Company Secretarial / CRO", "Systems / Software", "Internal Processes", "Other",
]
DEFAULT_TRACKED_METRICS = ["Unreconciled transactions", "Dext bills"]
UNASSIGNED_CLIENT_ID = "__clockbook_unassigned__"
UNASSIGNED_CLIENT_NAME = "No client assigned"
AROUND_TENANT_ID = "tenant_around_finance"
AROUND_TENANT_NAME = "Around Finance"
AROUND_TENANT_SLUG = "around-finance"


def _is_builtin_task_type_name(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {BUILTIN_HELPING_TASK_TYPE.lower(), BUILTIN_LEARNING_TASK_TYPE.lower()}


def _is_learning_task_type(value: str) -> bool:
    return (value or "").strip().lower() == BUILTIN_LEARNING_TASK_TYPE.lower()


def _ensure_builtin_helping_task_type_for_current_tenant(db: Session):
    existing = db.query(models.TaskTypeOption).filter(
        func.lower(models.TaskTypeOption.name) == BUILTIN_HELPING_TASK_TYPE.lower()
    ).first()
    legacy = db.query(models.TaskTypeOption).filter(
        func.lower(models.TaskTypeOption.name) == LEGACY_BUILTIN_HELPING_TASK_TYPE.lower()
    ).first()

    # Rename the short-lived original built-in label in place so existing tenant data,
    # templates and historical task reporting continue under one category.
    if existing is None and legacy is not None:
        legacy.name = BUILTIN_HELPING_TASK_TYPE
        legacy.is_billable = False
        existing = legacy
        legacy = None
    elif existing is None:
        existing = models.TaskTypeOption(name=BUILTIN_HELPING_TASK_TYPE, is_billable=False)
        db.add(existing)
    else:
        existing.is_billable = False

    db.query(models.TemplateTask).filter(
        func.lower(models.TemplateTask.task_type) == LEGACY_BUILTIN_HELPING_TASK_TYPE.lower()
    ).update({models.TemplateTask.task_type: BUILTIN_HELPING_TASK_TYPE}, synchronize_session=False)
    db.query(models.TaskInstance).filter(
        func.lower(models.TaskInstance.task_type) == LEGACY_BUILTIN_HELPING_TASK_TYPE.lower()
    ).update({models.TaskInstance.task_type: BUILTIN_HELPING_TASK_TYPE}, synchronize_session=False)

    # If both labels ever existed briefly, collapse the legacy option after references move.
    if legacy is not None and legacy.id != existing.id:
        db.delete(legacy)

    learning = db.query(models.TaskTypeOption).filter(
        func.lower(models.TaskTypeOption.name) == BUILTIN_LEARNING_TASK_TYPE.lower()
    ).first()
    if learning is None:
        learning = models.TaskTypeOption(name=BUILTIN_LEARNING_TASK_TYPE, is_billable=False)
        db.add(learning)
    else:
        learning.is_billable = False
    db.flush()


def _ensure_builtin_task_types_for_all_tenants(db: Session):
    previous = db.info.get("tenant_id")
    tenant_ids = [row[0] for row in db.query(models.Tenant.id).all()]
    try:
        for tenant_id in tenant_ids:
            db.info["tenant_id"] = tenant_id
            _ensure_builtin_helping_task_type_for_current_tenant(db)
    finally:
        if previous is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous


def _ensure_learning_categories_for_all_tenants(db: Session):
    previous = db.info.get("tenant_id")
    tenant_ids = [row[0] for row in db.query(models.Tenant.id).all()]
    try:
        for tenant_id in tenant_ids:
            db.info["tenant_id"] = tenant_id
            existing = {row[0].lower() for row in db.query(models.LearningCategory.name).all()}
            for name in DEFAULT_LEARNING_CATEGORIES:
                if name.lower() not in existing:
                    db.add(models.LearningCategory(name=name))
            db.flush()
    finally:
        if previous is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous


def _unassigned_client_id(tenant_id: str) -> str:
    # Preserve the legacy primary key for Around Finance so existing tasks/FKs need no rewrite.
    # Future tenants receive their own reserved client row because client IDs are globally unique.
    return UNASSIGNED_CLIENT_ID if tenant_id == AROUND_TENANT_ID else f"{UNASSIGNED_CLIENT_ID}:{tenant_id}"



def _current_tenant_id(db: Session) -> str:
    tenant_id = db.info.get("tenant_id")
    if not tenant_id:
        raise HTTPException(401, "No active workspace")
    return tenant_id


def inactivity_audit_enabled(db: Session) -> bool:
    setting = db.query(models.TenantSetting).filter(models.TenantSetting.key == "inactivity_audit_enabled").first()
    return bool(setting and setting.value.strip().lower() in ("1", "true", "yes", "on"))


def set_inactivity_audit_enabled(db: Session, enabled: bool) -> bool:
    setting = db.query(models.TenantSetting).filter(models.TenantSetting.key == "inactivity_audit_enabled").first()
    if setting is None:
        setting = models.TenantSetting(key="inactivity_audit_enabled", value="true" if enabled else "false")
        db.add(setting)
    else:
        setting.value = "true" if enabled else "false"
    db.commit()
    return enabled



def run_multitenant_migration():
    """One-time, backward-safe migration of the existing workspace into Around Finance.

    Existing primary keys and business records are preserved. Tenant columns are introduced
    nullable, backfilled, and only then made NOT NULL on PostgreSQL. User identity is split
    from tenant membership without requiring existing staff to re-register.
    """
    tenant_tables = [
        "pods", "members", "sessions", "login_events", "clock_start_events",
        "karbon_reconciliation_notes", "google_oauth_states", "dismissed_suggestions",
        "clients", "bank_accounts", "roles", "task_type_options", "tracked_metrics",
        "templates", "template_tasks", "tasks", "help_events", "inactivity_events",
    ]
    with engine.begin() as conn:
        # IMPORTANT: inspect schema changes through the SAME connection that performs
        # the ALTER TABLE statements. PostgreSQL holds an ACCESS EXCLUSIVE lock for
        # ALTER TABLE until this transaction commits. Inspecting the same table through
        # a second pooled connection here can block against our own uncommitted DDL and
        # leave application startup stuck at "Waiting for application startup".
        migration_inspector = inspect(conn)
        tables = set(migration_inspector.get_table_names())
        # tenants/users/tenant_settings are created by metadata before this runs.
        existing_tenant = conn.execute(text("SELECT id FROM tenants WHERE id = :id"), {"id": AROUND_TENANT_ID}).first()
        if not existing_tenant:
            tenant_columns = {c["name"] for c in inspect(conn).get_columns("tenants")}
            if "version" in tenant_columns:
                conn.execute(text(
                    "INSERT INTO tenants (id, name, slug, status, created_at, version) "
                    "VALUES (:id, :name, :slug, 'active', :created_at, 1)"
                ), {"id": AROUND_TENANT_ID, "name": AROUND_TENANT_NAME, "slug": AROUND_TENANT_SLUG, "created_at": datetime.utcnow()})
            else:
                conn.execute(text(
                    "INSERT INTO tenants (id, name, slug, status, created_at) "
                    "VALUES (:id, :name, :slug, 'active', :created_at)"
                ), {"id": AROUND_TENANT_ID, "name": AROUND_TENANT_NAME, "slug": AROUND_TENANT_SLUG, "created_at": datetime.utcnow()})

        for table in tenant_tables:
            if table not in tables:
                continue
            columns = {c["name"] for c in inspect(conn).get_columns(table)}
            if "tenant_id" not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN tenant_id VARCHAR"))
            conn.execute(text(f"UPDATE {table} SET tenant_id = :tenant_id WHERE tenant_id IS NULL OR tenant_id = ''"), {"tenant_id": AROUND_TENANT_ID})

        if "members" in tables:
            member_columns = {c["name"] for c in inspect(conn).get_columns("members")}
            if "user_id" not in member_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN user_id VARCHAR"))
        if "sessions" in tables:
            session_columns = {c["name"] for c in inspect(conn).get_columns("sessions")}
            if "user_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN user_id VARCHAR"))
            if "instance_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN instance_id VARCHAR"))
            if "last_seen_at" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN last_seen_at TIMESTAMP"))
                conn.execute(text("UPDATE sessions SET last_seen_at = created_at WHERE last_seen_at IS NULL"))

        # Promote each legacy login identity to a global User. The Member row remains the
        # tenant-specific profile/role/capacity record, so all existing IDs and UI references stay valid.
        if "members" in tables:
            legacy_members = conn.execute(text(
                "SELECT id, email, password_hash FROM members WHERE email IS NOT NULL AND TRIM(email) <> ''"
            )).fetchall()
            for member_id, email, password_hash in legacy_members:
                normalized = (email or "").strip().lower()
                if not normalized:
                    continue
                user = conn.execute(text("SELECT id, password_hash FROM users WHERE LOWER(email) = :email"), {"email": normalized}).first()
                if user:
                    user_id = user[0]
                    if not user[1] and password_hash:
                        conn.execute(text("UPDATE users SET password_hash = :password_hash WHERE id = :id"), {"password_hash": password_hash, "id": user_id})
                else:
                    user_id = "usr_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
                    user_columns = {c["name"] for c in inspect(conn).get_columns("users")}
                    if "version" in user_columns:
                        conn.execute(text(
                            "INSERT INTO users (id, email, password_hash, default_tenant_id, status, created_at, version) "
                            "VALUES (:id, :email, :password_hash, :tenant_id, 'active', :created_at, 1)"
                        ), {"id": user_id, "email": normalized, "password_hash": password_hash, "tenant_id": AROUND_TENANT_ID, "created_at": datetime.utcnow()})
                    else:
                        conn.execute(text(
                            "INSERT INTO users (id, email, password_hash, default_tenant_id, status, created_at) "
                            "VALUES (:id, :email, :password_hash, :tenant_id, 'active', :created_at)"
                        ), {"id": user_id, "email": normalized, "password_hash": password_hash, "tenant_id": AROUND_TENANT_ID, "created_at": datetime.utcnow()})
                conn.execute(text("UPDATE members SET user_id = :user_id, email = :email WHERE id = :member_id"), {"user_id": user_id, "email": normalized, "member_id": member_id})

            conn.execute(text(
                "UPDATE sessions SET user_id = (SELECT members.user_id FROM members WHERE members.id = sessions.member_id) "
                "WHERE user_id IS NULL"
            ))

        # Existing system settings were workspace-owned before multi-tenancy. Copy them to
        # Around Finance once; keep the old table only for deployment-global backwards compatibility.
        if "system_settings" in tables:
            rows = conn.execute(text("SELECT key, value FROM system_settings")).fetchall()
            for key, value in rows:
                exists = conn.execute(text(
                    "SELECT id FROM tenant_settings WHERE tenant_id = :tenant_id AND key = :key"
                ), {"tenant_id": AROUND_TENANT_ID, "key": key}).first()
                if not exists:
                    tenant_setting_columns = {c["name"] for c in inspect(conn).get_columns("tenant_settings")}
                    if "version" in tenant_setting_columns:
                        conn.execute(text(
                            "INSERT INTO tenant_settings (id, tenant_id, key, value, version) VALUES (:id, :tenant_id, :key, :value, 1)"
                        ), {"id": models.gen_id("tset"), "tenant_id": AROUND_TENANT_ID, "key": key, "value": value or ""})
                    else:
                        conn.execute(text(
                            "INSERT INTO tenant_settings (id, tenant_id, key, value) VALUES (:id, :tenant_id, :key, :value)"
                        ), {"id": models.gen_id("tset"), "tenant_id": AROUND_TENANT_ID, "key": key, "value": value or ""})

        if engine.dialect.name == "postgresql":
            # Remove legacy global uniqueness so two tenants can use the same business names/codes.
            for table, constraint in [
                ("members", "members_email_key"), ("clients", "clients_code_key"),
                ("pods", "pods_name_key"), ("roles", "roles_name_key"),
                ("task_type_options", "task_type_options_name_key"),
                ("tracked_metrics", "tracked_metrics_name_key"),
            ]:
                if table in tables:
                    conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"))
            for table in tenant_tables:
                if table in tables:
                    conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"))

        # Tenant-local uniqueness and lookup indexes. LOWER(code) preserves the current
        # case-insensitive client-code rule while allowing the same code in another tenant.
        if "clients" in tables:
            conn.execute(text("DROP INDEX IF EXISTS uq_clients_code_ci"))
            duplicate = conn.execute(text(
                "SELECT tenant_id, LOWER(TRIM(code)) AS normalized_code FROM clients "
                "WHERE code IS NOT NULL AND TRIM(code) <> '' "
                "GROUP BY tenant_id, LOWER(TRIM(code)) HAVING COUNT(*) > 1 LIMIT 1"
            )).first()
            if duplicate is None:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_tenant_code_ci "
                    "ON clients (tenant_id, LOWER(code)) WHERE code IS NOT NULL AND TRIM(code) <> ''"
                ))
            else:
                _log_event("client_code_unique_index_pending", tenant_id=duplicate[0], duplicate_code=duplicate[1])
        for name, table, column in [
            ("uq_pods_tenant_name_idx", "pods", "name"),
            ("uq_roles_tenant_name_idx", "roles", "name"),
            ("uq_task_types_tenant_name_idx", "task_type_options", "name"),
            ("uq_metrics_tenant_name_idx", "tracked_metrics", "name"),
        ]:
            if table in tables:
                conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} (tenant_id, {column})"))
        if "members" in tables:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_members_tenant_user_idx ON members (tenant_id, user_id) WHERE user_id IS NOT NULL"))
        if "sessions" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_tenant_member ON sessions (tenant_id, member_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_instance_id ON sessions (instance_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_last_seen_at ON sessions (last_seen_at)"))

    _log_event("multitenant_migration_ready", default_tenant=AROUND_TENANT_ID)


def run_hardening_migrations():
    """Backward-safe columns/indexes for audit logging and optimistic concurrency."""
    versioned_tables = [
        "tenants", "users", "tenant_settings", "pods", "members", "tenant_invitations",
        "karbon_reconciliation_notes", "clients", "bank_accounts", "roles",
        "task_type_options", "tracked_metrics", "templates", "template_tasks",
    ]
    with engine.begin() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        for table in versioned_tables:
            if table not in tables:
                continue
            cols = {c["name"] for c in inspect(conn).get_columns(table)}
            if "version" not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN version INTEGER NOT NULL DEFAULT 1"))
            conn.execute(text(f"UPDATE {table} SET version = 1 WHERE version IS NULL"))
        if "audit_events" in tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_events_tenant_created ON audit_events (tenant_id, created_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_events_tenant_entity ON audit_events (tenant_id, entity_type, entity_id)"))


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
            if "weekly_capacity_hours" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN weekly_capacity_hours FLOAT DEFAULT 40.0"))
                conn.execute(text("UPDATE members SET weekly_capacity_hours = 40.0 WHERE weekly_capacity_hours IS NULL"))
            if "capacity_effective_from" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN capacity_effective_from DATE"))
                # Existing installations should not manufacture historical unused capacity.
                # Start capacity from the date this migration is first deployed; admins can
                # move the date earlier later if historical capacity is genuinely required.
                conn.execute(text("UPDATE members SET capacity_effective_from = CURRENT_DATE WHERE capacity_effective_from IS NULL"))
            if "timezone_name" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN timezone_name VARCHAR"))
            elif engine.dialect.name == "postgresql":
                # Older deployments created this column with Asia/Colombo as a database
                # default. Stop silently assigning a location to new members while
                # preserving every existing member's stored value.
                conn.execute(text("ALTER TABLE members ALTER COLUMN timezone_name DROP DEFAULT"))
            if "work_arrangement" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN work_arrangement VARCHAR DEFAULT 'office'"))
                conn.execute(text("UPDATE members SET work_arrangement = 'office' WHERE work_arrangement IS NULL OR work_arrangement = ''"))
            if "can_view_leave_capacity_insights" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN can_view_leave_capacity_insights BOOLEAN DEFAULT FALSE"))
                conn.execute(text("UPDATE members SET can_view_leave_capacity_insights = FALSE WHERE can_view_leave_capacity_insights IS NULL"))
            if "additional_permissions" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN additional_permissions JSON"))
                conn.execute(text("UPDATE members SET additional_permissions = '[]' WHERE additional_permissions IS NULL"))
            if "staff_tour_completed" not in existing_columns:
                conn.execute(text("ALTER TABLE members ADD COLUMN staff_tour_completed BOOLEAN DEFAULT FALSE"))
                conn.execute(text("UPDATE members SET staff_tour_completed = FALSE WHERE staff_tour_completed IS NULL"))

    if "google_oauth_states" in inspector.get_table_names():
        existing_google_state_columns = {c["name"] for c in inspector.get_columns("google_oauth_states")}
        with engine.begin() as conn:
            if "code_verifier" not in existing_google_state_columns:
                conn.execute(text("ALTER TABLE google_oauth_states ADD COLUMN code_verifier VARCHAR"))

    if "clients" in inspector.get_table_names():
        existing_client_columns = {c["name"] for c in inspector.get_columns("clients")}
        with engine.begin() as conn:
            if "code" not in existing_client_columns:
                conn.execute(text("ALTER TABLE clients ADD COLUMN code VARCHAR"))

    if "task_type_options" in inspector.get_table_names():
        existing_task_type_columns = {c["name"] for c in inspector.get_columns("task_type_options")}
        with engine.begin() as conn:
            if "is_billable" not in existing_task_type_columns:
                conn.execute(text("ALTER TABLE task_type_options ADD COLUMN is_billable BOOLEAN DEFAULT FALSE"))
                # Preserve the app's previous convention on upgrade, then let admins manage
                # billing explicitly from Settings going forward.
                conn.execute(text("UPDATE task_type_options SET is_billable = TRUE WHERE LOWER(name) LIKE 'billable:%'"))
                conn.execute(text("UPDATE task_type_options SET is_billable = FALSE WHERE is_billable IS NULL"))

    if "templates" in inspector.get_table_names():
        existing_template_columns = {c["name"] for c in inspector.get_columns("templates")}
        with engine.begin() as conn:
            if "category" not in existing_template_columns:
                conn.execute(text("ALTER TABLE templates ADD COLUMN category VARCHAR"))

    if "template_tasks" in inspector.get_table_names():
        existing_tt_columns = {c["name"] for c in inspector.get_columns("template_tasks")}
        with engine.begin() as conn:
            if "requires_bank_account" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN requires_bank_account BOOLEAN DEFAULT FALSE"))
            if "tracks_number_label" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN tracks_number_label VARCHAR DEFAULT ''"))
            if "needs_pay_period" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN needs_pay_period BOOLEAN DEFAULT FALSE"))
            if "period_types" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN period_types JSON"))
                conn.execute(text("UPDATE template_tasks SET period_types = '[]' WHERE period_types IS NULL"))
            if "period_required" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN period_required BOOLEAN DEFAULT FALSE"))
            if "position" not in existing_tt_columns:
                conn.execute(text("ALTER TABLE template_tasks ADD COLUMN position INTEGER DEFAULT 0"))
                # Preserve the existing created order when introducing explicit positions.
                rows = conn.execute(text("SELECT id, template_id FROM template_tasks ORDER BY template_id, created_at, id")).fetchall()
                next_pos = {}
                for row in rows:
                    template_id = row[1]
                    pos = next_pos.get(template_id, 0)
                    conn.execute(text("UPDATE template_tasks SET position = :position WHERE id = :id"), {"position": pos, "id": row[0]})
                    next_pos[template_id] = pos + 1

    if "tasks" in inspector.get_table_names():
        existing_task_columns = {c["name"] for c in inspector.get_columns("tasks")}
        with engine.begin() as conn:
            if "helped_member_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN helped_member_id VARCHAR"))
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
            if "needs_pay_period" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN needs_pay_period BOOLEAN DEFAULT FALSE"))
            if "period_types" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_types JSON"))
                conn.execute(text("UPDATE tasks SET period_types = '[]' WHERE period_types IS NULL"))
            if "period_required" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_required BOOLEAN DEFAULT FALSE"))
            if "period_type" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_type VARCHAR"))
            if "period_year" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_year INTEGER"))
            if "period_number" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_number INTEGER"))
            if "period_start" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_start VARCHAR"))
            if "period_end" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN period_end VARCHAR"))
            if "source_calendar_event_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_calendar_event_id VARCHAR"))
            if "quick_meeting_request_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN quick_meeting_request_id VARCHAR"))
            if "calendar_event_deleted_at" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN calendar_event_deleted_at TIMESTAMP"))
            if "source_template_task_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_template_task_id VARCHAR"))
            if "source_template_name" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_template_name VARCHAR"))
            if "source_template_field" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_template_field VARCHAR"))
            if "source_template_category" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN source_template_category VARCHAR"))
            if "submitted_pod_id" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN submitted_pod_id VARCHAR"))
            if "last_heartbeat_at" not in existing_task_columns:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN last_heartbeat_at TIMESTAMP"))

    if "time_integrity_audit_entries" in inspector.get_table_names():
        existing_integrity_columns = {c["name"] for c in inspect(engine).get_columns("time_integrity_audit_entries")}
        with engine.begin() as conn:
            if "recorded_timezone_name" not in existing_integrity_columns:
                conn.execute(text("ALTER TABLE time_integrity_audit_entries ADD COLUMN recorded_timezone_name VARCHAR"))
                # Existing snapshots pre-date immutable timezone capture. Seed them from the
                # member profile available at deployment; all future rows capture it at RecordedAt.
                conn.execute(text(
                    "UPDATE time_integrity_audit_entries SET recorded_timezone_name = COALESCE("
                    "(SELECT NULLIF(m.timezone_name, '') FROM members m WHERE m.id = time_integrity_audit_entries.member_id), 'UTC') "
                    "WHERE recorded_timezone_name IS NULL OR recorded_timezone_name = ''"
                ))
            if "task_started_at" not in existing_integrity_columns:
                conn.execute(text("ALTER TABLE time_integrity_audit_entries ADD COLUMN task_started_at TIMESTAMP"))
            if "task_ended_at" not in existing_integrity_columns:
                conn.execute(text("ALTER TABLE time_integrity_audit_entries ADD COLUMN task_ended_at TIMESTAMP"))

    # Normalize the old payroll-only requirement into the generic Work period model.
    # Existing generic period configurations were already mandatory at completion, so preserve
    # that behavior. The legacy columns remain in place so historical rows/exports still work.
    with Session(engine) as migration_db:
        changed = False
        for template_task in migration_db.query(models.TemplateTask).all():
            configured = list(template_task.period_types or [])
            if template_task.needs_pay_period and not configured:
                template_task.period_types = ["weekly", "fortnightly", "monthly"]
                configured = list(template_task.period_types)
                changed = True
            if configured and not template_task.period_required:
                template_task.period_required = True
                changed = True
            if template_task.needs_pay_period:
                template_task.needs_pay_period = False
                changed = True
        for task_instance in migration_db.query(models.TaskInstance).all():
            configured = list(task_instance.period_types or [])
            if task_instance.needs_pay_period and not configured:
                task_instance.period_types = ["weekly", "fortnightly", "monthly"]
                configured = list(task_instance.period_types)
                changed = True
            if configured and not task_instance.period_required:
                task_instance.period_required = True
                changed = True

        # Backfill the broad template field for historical tasks where the template name
        # still uniquely identifies a current template. Future tasks copy this at creation.
        template_name_rows = {}
        for tpl in migration_db.query(models.Template).all():
            template_name_rows.setdefault(tpl.name, []).append(tpl.field)
        unique_template_fields = {name: fields[0] for name, fields in template_name_rows.items() if len(set(fields)) == 1}
        for task_instance in migration_db.query(models.TaskInstance).all():
            if not getattr(task_instance, "source_template_field", None) and task_instance.source_template_name in unique_template_fields:
                task_instance.source_template_field = unique_template_fields[task_instance.source_template_name]
                changed = True
            # Historical pod membership was not previously stored. For legacy submitted rows
            # we can only seed the snapshot from the person's current pod. From Phase 4 onward
            # every submission records the exact pod at that moment, so later pod moves cannot
            # grant a new admin access to earlier work.
            if task_instance.status == "submitted" and not getattr(task_instance, "submitted_pod_id", None) and task_instance.submitted_by_id:
                submitted_member = migration_db.get(models.Member, task_instance.submitted_by_id)
                if submitted_member:
                    task_instance.submitted_pod_id = submitted_member.pod_id
                    changed = True
        if changed:
            migration_db.commit()

    if "help_events" in inspector.get_table_names():
        existing_help_event_columns = {c["name"] for c in inspector.get_columns("help_events")}
        with engine.begin() as conn:
            if "task_id" not in existing_help_event_columns:
                conn.execute(text("ALTER TABLE help_events ADD COLUMN task_id VARCHAR"))
            if "adjusted" not in existing_help_event_columns:
                conn.execute(text("ALTER TABLE help_events ADD COLUMN adjusted BOOLEAN DEFAULT FALSE"))
            if "context" not in existing_help_event_columns:
                conn.execute(text("ALTER TABLE help_events ADD COLUMN context TEXT DEFAULT ''"))
            if "inactivity_event_id" not in existing_help_event_columns:
                conn.execute(text("ALTER TABLE help_events ADD COLUMN inactivity_event_id VARCHAR"))



def _ensure_one_running_timer_invariant(db: Session):
    """Repair any legacy duplicate running timers, then add the database invariant.

    PostgreSQL is the production target and SQLite is useful for local/test installs; both
    support the partial unique index below. The repair only runs for an already-impossible
    state left by older code: keep the most recently active timer running and pause the rest
    at server time so deployment does not fail while adding the constraint.
    """
    running = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id.isnot(None),
        models.TaskInstance.status == "running",
    ).all()
    by_owner = {}
    for task in running:
        by_owner.setdefault(task.owner_id, []).append(task)
    repaired = 0
    for owner_tasks in by_owner.values():
        if len(owner_tasks) <= 1:
            continue
        keeper = max(
            owner_tasks,
            key=lambda t: (t.last_heartbeat_at or t.created_at or datetime.min, t.created_at or datetime.min),
        )
        for task in owner_tasks:
            if task.id == keeper.id:
                continue
            task.segments = close_open_segment(task.segments)
            task.status = "paused"
            repaired += 1
    if repaired:
        db.commit()
        print(f"[timer-integrity] repaired {repaired} duplicate running timer(s) before enabling invariant")

    dialect = engine.dialect.name
    if dialect in ("postgresql", "sqlite"):
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_one_running_per_owner "
                "ON tasks (owner_id) WHERE status = 'running' AND owner_id IS NOT NULL"
            ))
    else:
        print(f"[timer-integrity] database-level one-running-timer index not installed for dialect={dialect}")


def _ensure_quick_meeting_idempotency_invariant():
    """Ensure one local Quick Meeting result per user/request key.

    The Google event itself also receives a deterministic event id derived from the same
    request key, so this DB rule is the local half of end-to-end idempotency.
    """
    if engine.dialect.name in ("postgresql", "sqlite"):
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_quick_meeting_request "
                "ON tasks (owner_id, quick_meeting_request_id) "
                "WHERE quick_meeting_request_id IS NOT NULL AND owner_id IS NOT NULL"
            ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)

    # Railway can briefly run old/new instances together during deploys. Serialize the
    # existing idempotent startup migrations on PostgreSQL so two instances never ALTER
    # the same schema concurrently. SQLite local/test installs do not need this lock.
    migration_lock_conn = None
    try:
        if engine.dialect.name == "postgresql":
            migration_lock_conn = engine.connect()
            migration_lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": STARTUP_MIGRATION_LOCK_KEY})
        run_multitenant_migration()
        run_hardening_migrations()
        run_startup_migrations()
        _ensure_operational_indexes()
    finally:
        if migration_lock_conn is not None:
            try:
                migration_lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": STARTUP_MIGRATION_LOCK_KEY})
            finally:
                migration_lock_conn.close()

    db = next(get_db())
    try:
        db.info["tenant_id"] = AROUND_TENANT_ID
        around_unassigned_id = _unassigned_client_id(AROUND_TENANT_ID)
        if db.get(models.Client, around_unassigned_id) is None:
            db.add(models.Client(id=around_unassigned_id, name=UNASSIGNED_CLIENT_NAME, code=None))
            db.commit()
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

        # Built-in task types are platform defaults, so also backfill them into every
        # existing tenant instead of only seeding newly-created workspaces.
        _ensure_builtin_task_types_for_all_tenants(db)
        _ensure_learning_categories_for_all_tenants(db)
        db.commit()

        # Upgrade legacy Google refresh tokens to the same encrypted-at-rest storage already
        # used by the other integrations. If the deployment key is not configured yet, keep
        # the old token usable and retry on a later startup/request rather than taking
        # ClockBook offline. New Google connections never write plaintext tokens.
        google_tokens_changed = False
        for member in db.query(models.Member).filter(models.Member.google_refresh_token.isnot(None)).all():
            stored = member.google_refresh_token or ""
            if stored and not stored.startswith("enc:v1:"):
                try:
                    member.google_refresh_token = "enc:v1:" + _encrypt_secret(stored)
                    google_tokens_changed = True
                except HTTPException:
                    break
        if google_tokens_changed:
            db.commit()

        # Phase 2 timer integrity: make the existing one-running-timer business rule a real
        # database invariant as well as application logic. This does not change normal timer
        # behaviour; it closes the millisecond race where two tabs could both start at once.
        _ensure_one_running_timer_invariant(db)
        # Phase 3 integration integrity: retries/double actions for Quick Meeting must
        # resolve to the same local task rather than creating duplicates.
        _ensure_quick_meeting_idempotency_invariant()
        _cleanup_operational_state(db)
        _log_event("startup_ready", version=CLOCKBOOK_VERSION, database=engine.dialect.name)
    finally:
        db.close()
    yield


app = FastAPI(title="Clockbook", lifespan=lifespan)


@app.exception_handler(StaleDataError)
async def stale_write_handler(request: Request, exc: StaleDataError):
    return JSONResponse(
        status_code=409,
        content={"detail": "This record was changed by someone else. Refresh and try again."},
    )

# Lets the Vite dev server on localhost:5173 call this API during local development.
# In production the frontend is served by this same app, so this is not needed there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def operational_request_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or secrets.token_hex(16)
    started = time.perf_counter()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body is too large"}, headers={"X-Request-ID": request_id})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"}, headers={"X-Request-ID": request_id})
    # Also measure the actual body so chunked/missing Content-Length requests cannot bypass
    # the limit. Starlette caches request.body() for the downstream FastAPI parser.
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Request body is too large"}, headers={"X-Request-ID": request_id})
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        _log_event("request_error", request_id=request_id, method=request.method, path=request.url.path, duration_ms=duration_ms, error=type(exc).__name__)
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-ClockBook-Version"] = CLOCKBOOK_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    _log_event("request", request_id=request_id, method=request.method, path=request.url.path, status=response.status_code, duration_ms=duration_ms)
    return response


@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok", "version": CLOCKBOOK_VERSION}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable", "version": CLOCKBOOK_VERSION})


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def get_current_member(
    authorization: str = Header(None),
    x_clockbook_instance: str = Header(None, alias="X-ClockBook-Instance"),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in")
    token = authorization[len("Bearer "):]
    # Session tokens are globally unique and intentionally resolved before tenant scoping.
    session = db.get(models.Session, token)
    if not session or not session.tenant_id:
        raise HTTPException(401, "Session no longer valid, please log in again")
    # New browser sessions are bound to the instance that created them. Older sessions created
    # before this feature remain backwards-compatible until their first instance heartbeat.
    if session.instance_id and x_clockbook_instance and session.instance_id != x_clockbook_instance:
        raise HTTPException(401, "This ClockBook session is active in another browser instance")
    db.info["tenant_id"] = session.tenant_id
    db.info["session_token"] = token
    db.info["clockbook_instance_id"] = x_clockbook_instance
    member = db.query(models.Member).filter(
        models.Member.id == session.member_id,
        models.Member.tenant_id == session.tenant_id,
    ).first()
    if not member:
        db.info.pop("tenant_id", None)
        db.delete(session)
        db.commit()
        raise HTTPException(401, "Account no longer exists in this workspace")
    if session.user_id and member.user_id and session.user_id != member.user_id:
        raise HTTPException(401, "Session membership is no longer valid")
    db.info["actor_member_id"] = member.id
    return member


def require_admin(member):
    if member.role not in ("admin", "super_admin"):
        raise HTTPException(403, "This action requires an admin")


def _require_expected_version(record, expected_version):
    if expected_version is None:
        raise HTTPException(409, "This screen is out of date. Refresh and try again.")
    current = int(getattr(record, "version", 1) or 1)
    if int(expected_version) != current:
        raise HTTPException(409, "This record was changed by someone else. Refresh and try again.")


def is_admin_or_above(role):
    return role in ("admin", "super_admin")


# Fine-grained permissions that a Super Admin may delegate without creating another role.
# Security/workspace ownership controls deliberately stay Super-Admin-only.
PERMISSION_INSIGHTS_LEAVE_CAPACITY = "insights_leave_capacity"
PERMISSION_REPORT_HELP = "report_help"
PERMISSION_REPORT_OVERRIDES = "report_manual_overrides"
PERMISSION_REPORT_AUDIT = "report_audit"
PERMISSION_MANAGE_PODS = "manage_pods"
PERMISSION_ADD_STAFF_MANUALLY = "add_staff_manually"
PERMISSION_MANAGE_LEARNING_CATEGORIES = "manage_learning_categories"
PERMISSION_MANAGE_DELEGATION_EXCLUSIONS = "manage_delegation_exclusions"
PERMISSION_MANAGE_WORKSPACE_BRANDING = "manage_workspace_branding"
PERMISSION_MANAGE_INTEGRATIONS = "manage_integrations"
PERMISSION_MANAGE_AUDIT_RECORDING = "manage_audit_recording"
PERMISSION_MANAGE_SUPER_ADMINS = "manage_super_admins"
PERMISSION_VIEW_TRACKED_TIME = "view_tracked_time"
PERMISSION_TIME_INTEGRITY_AUDIT = "view_time_integrity_audit"
DELEGATABLE_ADMIN_PERMISSIONS = {
    PERMISSION_REPORT_HELP,
    PERMISSION_REPORT_OVERRIDES,
    PERMISSION_REPORT_AUDIT,
    PERMISSION_MANAGE_PODS,
    PERMISSION_ADD_STAFF_MANUALLY,
    PERMISSION_MANAGE_LEARNING_CATEGORIES,
    PERMISSION_MANAGE_DELEGATION_EXCLUSIONS,
    PERMISSION_MANAGE_WORKSPACE_BRANDING,
    PERMISSION_MANAGE_INTEGRATIONS,
    PERMISSION_MANAGE_AUDIT_RECORDING,
    PERMISSION_MANAGE_SUPER_ADMINS,
    PERMISSION_VIEW_TRACKED_TIME,
    PERMISSION_TIME_INTEGRITY_AUDIT,
}
ALL_DELEGATABLE_PERMISSIONS = DELEGATABLE_ADMIN_PERMISSIONS | {PERMISSION_INSIGHTS_LEAVE_CAPACITY}


def _additional_permissions(member):
    raw = getattr(member, "additional_permissions", None) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return {str(item) for item in raw if str(item) in ALL_DELEGATABLE_PERMISSIONS}


def _has_permission(member, permission):
    if member.role == "super_admin":
        return True
    if permission == PERMISSION_INSIGHTS_LEAVE_CAPACITY and bool(getattr(member, "can_view_leave_capacity_insights", False)):
        return True
    return permission in _additional_permissions(member)


def _require_delegated_admin_permission(member, permission, message="This action requires additional admin permission"):
    if member.role == "super_admin":
        return
    if member.role != "admin" or not _has_permission(member, permission):
        raise HTTPException(403, message)


def _member_in_admin_scope(current_member, target_member):
    """Server-side scope used for privileged mutations.

    Super admins can manage everyone. Regular admins can never manage a super admin and,
    when assigned to a pod, can only manage people in that same pod. Staff can only ever
    match themselves. This mirrors the existing read-side Dashboard/Export visibility rules
    instead of trusting whichever member/task ID the browser sends.
    """
    if not target_member:
        return False
    if getattr(target_member, "tenant_id", None) != getattr(current_member, "tenant_id", None):
        return False
    if current_member.role == "super_admin":
        return True
    if current_member.role == "admin":
        if target_member.role == "super_admin":
            return False
        if current_member.pod_id and target_member.pod_id != current_member.pod_id:
            return False
        return True
    return target_member.id == current_member.id


def _require_member_in_scope(current_member, member_id, db: Session):
    target = db.get(models.Member, member_id)
    if not target:
        raise HTTPException(404, "Member not found")
    if not _member_in_admin_scope(current_member, target):
        raise HTTPException(403, "You cannot manage that person")
    return target


def _require_task_in_scope(current_member, task, db: Session, owner_can_access=True):
    if not task:
        raise HTTPException(404, "Task not found")
    if getattr(task, "tenant_id", None) != getattr(current_member, "tenant_id", None):
        raise HTTPException(404, "Task not found")
    if owner_can_access and task.owner_id == current_member.id:
        return task
    if current_member.role not in ("admin", "super_admin"):
        raise HTTPException(403, "This task belongs to someone else")
    if not task.owner_id:
        return task
    owner = db.get(models.Member, task.owner_id)
    if not owner or not _member_in_admin_scope(current_member, owner):
        raise HTTPException(403, "You cannot manage that task")
    if current_member.role == "admin" and current_member.pod_id and task.status == "submitted":
        if getattr(task, "submitted_pod_id", None) != current_member.pod_id:
            raise HTTPException(403, "You cannot manage historical work from another pod")
    return task


def _revoke_member_sessions(db: Session, member_id: str):
    # ClockBook deliberately has no inactivity/session timeout. Tokens live for the browser
    # session, but security-sensitive account changes must be able to invalidate them now.
    # Bulk DELETE does not reliably inherit the ORM loader criteria used for tenant scoping,
    # so include the active tenant explicitly rather than depending on implicit filtering.
    query = db.query(models.Session).filter(models.Session.member_id == member_id)
    tenant_id = db.info.get("tenant_id")
    if tenant_id:
        query = query.filter(models.Session.tenant_id == tenant_id)
    query.delete(synchronize_session=False)


def _lock_timer_owner(db: Session, member_id: str):
    # Serialize Start operations for the same person across tabs, workers and app instances.
    # The partial unique index remains the final database backstop. SQLite serializes writes
    # itself, so the explicit advisory lock is only needed on PostgreSQL.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:member_id))"), {"member_id": member_id})


def _get_task_for_update(db: Session, task_id: str):
    # Row-lock state transitions so Pause/Submit/Reset/Start requests arriving from two tabs
    # cannot overwrite each other after reading the same stale task state.
    return db.query(models.TaskInstance).filter(
        models.TaskInstance.id == task_id
    ).with_for_update().one_or_none()


def _validated_pause_end_at(task, requested_end_at=None):
    # Ordinary pauses are always stamped by the server. A supplied timestamp is reserved for
    # ClockBook's existing sleep/lock/stale-heartbeat recovery flows, which intentionally
    # backdate to when the machine actually went away. Bound that recovery timestamp using
    # server-known state so a manipulated API request cannot arbitrarily rewrite tracked time.
    if not requested_end_at:
        return None
    try:
        requested = parse_utc_naive(requested_end_at)
    except Exception:
        raise HTTPException(400, "Invalid pause timestamp")

    now = datetime.utcnow()
    if requested > now + timedelta(seconds=5):
        raise HTTPException(400, "Pause timestamp cannot be in the future")

    segments = list(task.segments or [])
    if not segments or segments[-1].get("end"):
        return None
    try:
        segment_start = parse_utc_naive(segments[-1]["start"])
    except Exception:
        raise HTTPException(409, "The running timer has an invalid start timestamp")
    if requested < segment_start:
        raise HTTPException(400, "Pause timestamp cannot be before the timer started")

    # Heartbeats are server timestamps. Allow one heartbeat interval plus tolerance for an
    # in-flight request crossing the exact sleep/lock boundary, but reject older arbitrary
    # backdating. The current client sends heartbeats every 45 seconds.
    if task.last_heartbeat_at and requested < task.last_heartbeat_at - timedelta(seconds=90):
        raise HTTPException(400, "Pause timestamp is older than the server's last active signal")
    return requested.isoformat() + "Z"


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

ACTIVE_INSTANCE_WINDOW_SECONDS = 180

def _sessions_for_user(db: Session, user_id: str):
    return db.query(models.Session).filter(
        models.Session.user_id == user_id
    ).execution_options(skip_tenant_scope=True)

def _active_sessions_for_user(db: Session, user_id: str):
    cutoff = datetime.utcnow() - timedelta(seconds=ACTIVE_INSTANCE_WINDOW_SECONDS)
    return _sessions_for_user(db, user_id).filter(
        func.coalesce(models.Session.last_seen_at, models.Session.created_at) >= cutoff
    ).all()

def _create_single_user_session(db: Session, user_id: str, member_id: str, tenant_id: str, instance_id: str | None, takeover: bool = False):
    active = _active_sessions_for_user(db, user_id)
    normalized_instance_id = (instance_id or None)

    # A refresh/redeploy can leave the browser at the login screen while the server still has
    # that exact browser instance's live session. Re-authenticating from the same instance is
    # a reconnect, not a competing ClockBook. Reuse the existing token instead of forcing a
    # takeover or weakening the active-instance timeout. A genuinely different instance still
    # receives the normal conflict below.
    if active and not takeover and normalized_instance_id:
        same_instance = [s for s in active if s.instance_id == normalized_instance_id]
        other_instances = [s for s in active if s.instance_id != normalized_instance_id]
        if same_instance and not other_instances:
            existing = max(same_instance, key=lambda s: (s.last_seen_at or s.created_at or datetime.min))
            existing.member_id = member_id
            existing.tenant_id = tenant_id
            existing.last_seen_at = datetime.utcnow()
            for session in same_instance:
                if session.token != existing.token:
                    db.delete(session)
            return existing.token

    if active and not takeover:
        raise HTTPException(409, "ClockBook is already active in another browser or device. Use the existing ClockBook, or choose 'Use ClockBook here instead'.")
    # One user identity gets one live ClockBook session across every workspace. Stale sessions
    # are removed automatically; an explicit takeover also revokes a still-active session.
    _sessions_for_user(db, user_id).delete(synchronize_session=False)
    token = secrets.token_urlsafe(32)
    db.add(models.Session(
        token=token, user_id=user_id, member_id=member_id, tenant_id=tenant_id, instance_id=normalized_instance_id,
        last_seen_at=datetime.utcnow(),
    ))
    return token

@app.get("/api/auth/status")
def auth_status(db: Session = Depends(get_db)):
    any_secured = db.query(models.User).filter(models.User.password_hash.isnot(None), models.User.status == "active").count()
    if any_secured > 0:
        return {"setup_needed": False, "unclaimed": []}
    unclaimed = db.query(models.Member).filter(
        models.Member.tenant_id == AROUND_TENANT_ID, models.Member.user_id.is_(None)
    ).all()
    return {"setup_needed": True, "unclaimed": [{"id": m.id, "name": m.name} for m in unclaimed]}


@app.post("/api/auth/claim", response_model=schemas.LoginResponse)
def claim_account(payload: schemas.ClaimAccountRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(db, f"claim:{_client_ip(request)}", limit=10, window_seconds=900)
    if db.query(models.User).filter(models.User.password_hash.isnot(None), models.User.status == "active").count() > 0:
        raise HTTPException(400, "Accounts are already set up, please log in")
    email = payload.email.strip().lower()
    if db.query(models.User).filter(func.lower(models.User.email) == email).first():
        raise HTTPException(400, "That email is already registered")
    user = models.User(email=email, password_hash=hash_password(payload.password), default_tenant_id=AROUND_TENANT_ID)
    db.add(user)
    db.flush()
    db.info["tenant_id"] = AROUND_TENANT_ID
    if payload.member_id:
        member = db.query(models.Member).filter(models.Member.id == payload.member_id).first()
        if not member or member.user_id:
            raise HTTPException(400, "That account cannot be claimed")
        member.user_id = user.id
        member.email = email
        member.password_hash = user.password_hash
        member.role = "admin"
    else:
        count = db.query(models.Member).count()
        member = models.Member(
            name=(payload.name or "Admin").strip() or "Admin", user_id=user.id,
            email=email, color_idx=count, role="admin", password_hash=user.password_hash,
        )
        db.add(member)
    db.commit()
    db.refresh(member)
    token = _create_single_user_session(db, user.id, member.id, member.tenant_id, getattr(payload, "instance_id", None), takeover=True)
    db.add(models.LoginEvent(member_id=member.id))
    db.commit()
    return schemas.LoginResponse(token=token, member=member)


@app.post("/api/auth/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    ip = _client_ip(request)
    _enforce_rate_limit(db, f"login-ip:{ip}", limit=50, window_seconds=900)
    _enforce_rate_limit(db, f"login-account:{ip}:{email}", limit=10, window_seconds=900)
    user = db.query(models.User).filter(func.lower(models.User.email) == email, models.User.status == "active").first()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")

    memberships = db.query(models.Member).filter(models.Member.user_id == user.id).order_by(models.Member.id).all()
    if not memberships:
        raise HTTPException(403, "This account is not assigned to a ClockBook workspace")
    requested_tenant = getattr(payload, "tenant_id", None)
    tenant_id = requested_tenant or user.default_tenant_id
    member = next((m for m in memberships if m.tenant_id == tenant_id), None) if tenant_id else None
    if member is None and len(memberships) == 1:
        member = memberships[0]
        tenant_id = member.tenant_id
    if member is None:
        raise HTTPException(409, "Choose a workspace before signing in")
    db.info["tenant_id"] = tenant_id
    token = _create_single_user_session(
        db, user.id, member.id, member.tenant_id, getattr(payload, "instance_id", None), takeover=bool(getattr(payload, "takeover", False))
    )
    db.add(models.LoginEvent(member_id=member.id))
    db.commit()
    return schemas.LoginResponse(token=token, member=member)


@app.post("/api/auth/logout", status_code=204)
def logout(authorization: str = Header(None), db: Session = Depends(get_db)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        session = db.get(models.Session, token)
        if session:
            # Logout is an intentional end to the user's active ClockBook session. Pause any
            # running timer at one server-authoritative timestamp before revoking the login
            # session, so tracked time cannot silently continue after the user has logged out.
            db.info["tenant_id"] = session.tenant_id
            _lock_timer_owner(db, session.member_id)
            logout_at = datetime.utcnow().isoformat() + "Z"
            running_tasks = db.query(models.TaskInstance).filter(
                models.TaskInstance.owner_id == session.member_id,
                models.TaskInstance.status == "running",
            ).with_for_update().all()
            for task in running_tasks:
                task.segments = close_open_segment(task.segments, logout_at)
                task.status = "paused"
            db.delete(session)
            db.commit()
    return None


@app.get("/api/auth/me", response_model=schemas.MemberOut)
def get_me(current_member: models.Member = Depends(get_current_member)):
    return current_member


@app.post("/api/auth/instance-heartbeat")
def instance_heartbeat(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    token = db.info.get("session_token")
    session = db.get(models.Session, token) if token else None
    if not session:
        raise HTTPException(401, "Session no longer valid, please log in again")
    instance_id = db.info.get("clockbook_instance_id")
    if session.instance_id and instance_id and session.instance_id != instance_id:
        raise HTTPException(401, "This ClockBook session is active in another browser instance")
    if not session.instance_id and instance_id:
        session.instance_id = instance_id
    session.last_seen_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.get("/api/auth/workspaces")
def list_my_workspaces(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.user_id:
        return {"active_tenant_id": current_member.tenant_id, "workspaces": []}
    memberships = db.query(models.Member).filter(
        models.Member.user_id == current_member.user_id
    ).execution_options(skip_tenant_scope=True).all()
    tenant_ids = [m.tenant_id for m in memberships]
    tenants = db.query(models.Tenant).filter(models.Tenant.id.in_(tenant_ids)).all() if tenant_ids else []
    by_id = {t.id: t for t in tenants}
    logo_rows = db.query(models.TenantSetting).filter(
        models.TenantSetting.tenant_id.in_(tenant_ids),
        models.TenantSetting.key == "workspace_logo_data_url",
    ).execution_options(skip_tenant_scope=True).all() if tenant_ids else []
    logos_by_tenant = {row.tenant_id: row.value for row in logo_rows if row.value}
    return {
        "active_tenant_id": current_member.tenant_id,
        "workspaces": [
            {
                "id": m.tenant_id,
                "name": by_id[m.tenant_id].name if m.tenant_id in by_id else m.tenant_id,
                "role": m.role,
                "logo_data_url": logos_by_tenant.get(m.tenant_id, ""),
            }
            for m in memberships
        ],
    }


def _validate_workspace_logo_data_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    allowed_prefixes = ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")
    prefix = next((p for p in allowed_prefixes if value.startswith(p)), None)
    if not prefix:
        raise HTTPException(400, "Logo must be a PNG, JPEG or WebP image")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except Exception:
        raise HTTPException(400, "Logo image data is invalid")
    if len(raw) > 350_000:
        raise HTTPException(400, "Logo is too large. Use an image under 350 KB after resizing")
    return value


@app.get("/api/workspace/branding")
def get_workspace_branding(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    tenant = db.get(models.Tenant, current_member.tenant_id)
    return {
        "workspace_id": current_member.tenant_id,
        "name": tenant.name if tenant else current_member.tenant_id,
        "logo_data_url": _setting_value(db, "workspace_logo_data_url"),
    }


@app.put("/api/workspace/branding")
def update_workspace_branding(payload: schemas.WorkspaceBrandingUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_WORKSPACE_BRANDING, "You do not have permission to manage workspace branding")
    logo = _validate_workspace_logo_data_url(payload.logo_data_url or "")
    _set_setting_value(db, "workspace_logo_data_url", logo)
    db.commit()
    return {
        "workspace_id": current_member.tenant_id,
        "logo_data_url": logo,
    }


@app.post("/api/auth/switch-workspace/{tenant_id}", response_model=schemas.LoginResponse)
def switch_workspace(tenant_id: str, authorization: str = Header(None), current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.user_id:
        raise HTTPException(400, "This account is not linked to a multi-workspace identity")
    target = db.query(models.Member).filter(
        models.Member.user_id == current_member.user_id, models.Member.tenant_id == tenant_id
    ).execution_options(skip_tenant_scope=True).first()
    if not target:
        raise HTTPException(403, "You are not a member of that workspace")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in")
    old_token = authorization[len("Bearer "):]
    old_session = db.get(models.Session, old_token)
    instance_id = old_session.instance_id if old_session else db.info.get("clockbook_instance_id")
    _sessions_for_user(db, current_member.user_id).delete(synchronize_session=False)
    db.flush()
    db.info["tenant_id"] = tenant_id
    new_token = secrets.token_urlsafe(32)
    db.add(models.Session(
        token=new_token, user_id=current_member.user_id, member_id=target.id,
        instance_id=instance_id, last_seen_at=datetime.utcnow(),
    ))
    db.add(models.LoginEvent(member_id=target.id))
    db.commit()
    return schemas.LoginResponse(token=new_token, member=target)


def _platform_admin_emails():
    return {e.strip().lower() for e in (os.environ.get("CLOCKBOOK_PLATFORM_ADMIN_EMAILS") or "").split(",") if e.strip()}


def _require_platform_admin(current_member: models.Member, db: Session):
    user = db.get(models.User, current_member.user_id) if current_member.user_id else None
    if not user or user.email.lower() not in _platform_admin_emails():
        raise HTTPException(403, "Platform administrator access is required")
    return user


def _slugify_tenant(value: str) -> str:
    value = (value or "").strip().lower()
    out = []
    dash = False
    for ch in value:
        if ch.isalnum():
            out.append(ch)
            dash = False
        elif not dash and out:
            out.append("-")
            dash = True
    return "".join(out).strip("-")


def _seed_new_tenant(db: Session, tenant_id: str):
    previous = db.info.get("tenant_id")
    db.info["tenant_id"] = tenant_id
    try:
        unassigned_id = _unassigned_client_id(tenant_id)
        if db.get(models.Client, unassigned_id) is None:
            db.add(models.Client(id=unassigned_id, name=UNASSIGNED_CLIENT_NAME, code=None))
        if db.query(models.Template).count() == 0:
            tpl = models.Template(field=DEFAULT_TEMPLATE["field"], name=DEFAULT_TEMPLATE["name"])
            db.add(tpl); db.flush()
            for pos, t in enumerate(DEFAULT_TEMPLATE["tasks"]):
                db.add(models.TemplateTask(template_id=tpl.id, name=t["name"], role=t["role"], task_type=t["task_type"], position=pos))
        if db.query(models.Role).count() == 0:
            for name in DEFAULT_ROLES:
                db.add(models.Role(name=name))
        if db.query(models.TaskTypeOption).count() == 0:
            for name in DEFAULT_TASK_TYPES:
                db.add(models.TaskTypeOption(name=name))
        if db.query(models.TrackedMetric).count() == 0:
            for name in DEFAULT_TRACKED_METRICS:
                db.add(models.TrackedMetric(name=name))
        if db.query(models.LearningCategory).count() == 0:
            for name in DEFAULT_LEARNING_CATEGORIES:
                db.add(models.LearningCategory(name=name))
        db.flush()
    finally:
        if previous is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous


@app.get("/api/platform/tenants", response_model=list[schemas.TenantOut])
def platform_list_tenants(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_platform_admin(current_member, db)
    return db.query(models.Tenant).order_by(models.Tenant.name).all()


@app.post("/api/platform/tenants", response_model=schemas.TenantOut, status_code=201)
def platform_create_tenant(payload: schemas.TenantCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    user = _require_platform_admin(current_member, db)
    name = payload.name.strip()
    requested_slug = _slugify_tenant(payload.slug or "")
    base_slug = requested_slug or _slugify_tenant(name)
    if not name or not base_slug:
        raise HTTPException(400, "Workspace name is required")
    if requested_slug:
        if db.query(models.Tenant).filter(models.Tenant.slug == requested_slug).first():
            raise HTTPException(400, "That workspace slug is already in use")
        slug = requested_slug
    else:
        # Display names are not tenant identities and may legitimately repeat. When the
        # generated readable slug collides, add a suffix while the real tenant ID remains
        # globally unique and is what data ownership is scoped against.
        slug = base_slug
        suffix = 2
        while db.query(models.Tenant).filter(models.Tenant.slug == slug).first():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
    tenant = models.Tenant(name=name, slug=slug, status="active")
    db.add(tenant); db.flush()
    previous = db.info.get("tenant_id")
    db.info["tenant_id"] = tenant.id
    try:
        membership = models.Member(
            user_id=user.id, name=current_member.name, email=user.email, password_hash=user.password_hash,
            color_idx=0, role="super_admin", timezone_name=current_member.timezone_name,
        )
        db.add(membership); db.flush()
        _seed_new_tenant(db, tenant.id)
    finally:
        if previous is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous
    db.commit()
    db.refresh(tenant)
    return tenant


@app.post("/api/admin/sessions/revoke-workspace")
def revoke_workspace_sessions(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    """Emergency tenant-scoped session revocation.

    This deliberately revokes the caller too. The response may arrive before the browser
    notices, and the next authenticated request must require a fresh login.
    """
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a Super Admin can revoke all workspace sessions")
    count = db.query(models.Session).delete(synchronize_session=False)
    db.commit()
    _log_event("workspace_sessions_revoked", tenant_id=current_member.tenant_id, count=count)
    return {"revoked_sessions": int(count or 0), "scope": "workspace"}


@app.post("/api/platform/sessions/revoke-all")
def revoke_all_platform_sessions(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    """Break-glass global revocation for a verified platform administrator."""
    _require_platform_admin(current_member, db)
    previous_skip = db.info.get("skip_tenant_scope")
    db.info["skip_tenant_scope"] = True
    try:
        count = db.query(models.Session).delete(synchronize_session=False)
        db.commit()
    finally:
        if previous_skip is None:
            db.info.pop("skip_tenant_scope", None)
        else:
            db.info["skip_tenant_scope"] = previous_skip
    _log_event("platform_sessions_revoked", count=count)
    return {"revoked_sessions": int(count or 0), "scope": "platform"}


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
# Each person connects their own calendar if they want to, nobody is required to. The
# calendar-events scope lets Clockbook read that person's events and, when they explicitly
# choose Quick Meeting, create a real event in their own primary calendar. The refresh token
# this produces is the one long-lived secret involved,
# and it is never returned by any API response, MemberOut only ever exposes a computed
# connected boolean. A short-lived access token is fetched fresh from that refresh token each
# time a check actually happens, rather than cached, keeping the logic simple and avoiding any
# separate expiry bookkeeping.
# ---------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "https://clockbook.up.railway.app/api/auth/google/callback")
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@app.get("/api/auth/google/connect-url")
def get_google_connect_url(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google Calendar integration has not been configured on this server yet")
    # OAuth attempts are single-use and short-lived. Remove this member's previous attempt
    # plus any globally stale rows, then bind this attempt to a PKCE verifier as well as state.
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    db.query(models.GoogleOAuthState).filter(
        or_(models.GoogleOAuthState.member_id == current_member.id, models.GoogleOAuthState.created_at < cutoff)
    ).delete(synchronize_session=False)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state_row = models.GoogleOAuthState(member_id=current_member.id, code_verifier=code_verifier)
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
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
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
    # State is deliberately short-lived. PKCE prevents an intercepted authorization code
    # from being exchanged without the verifier generated by ClockBook for this attempt.
    if not state_row.created_at or state_row.created_at < datetime.utcnow() - timedelta(minutes=10) or not state_row.code_verifier:
        db.delete(state_row)
        db.commit()
        return RedirectResponse(url="/?calendar=error")
    member_id = state_row.member_id
    tenant_id = state_row.tenant_id
    code_verifier = state_row.code_verifier
    db.delete(state_row)
    db.commit()

    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }, timeout=10)
        resp.raise_for_status()
        refresh_token = resp.json().get("refresh_token")
    except Exception:
        return RedirectResponse(url="/?calendar=error")

    if not refresh_token:
        return RedirectResponse(url="/?calendar=error")

    db.info["tenant_id"] = tenant_id
    member = db.query(models.Member).filter(models.Member.id == member_id).first()
    if member:
        try:
            member.google_refresh_token = "enc:v1:" + _encrypt_secret(refresh_token)
            db.commit()
        except HTTPException:
            db.rollback()
            return RedirectResponse(url="/?calendar=error")

    return RedirectResponse(url="/?calendar=connected")


@app.post("/api/auth/google/disconnect", response_model=schemas.MemberOut)
def disconnect_google_calendar(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Best-effort revoke at Google as well as forgetting it locally. A Google outage must not
    # prevent the user from disconnecting ClockBook, so local removal always wins.
    try:
        refresh_token = _google_refresh_token_value(current_member, db)
        if refresh_token:
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": refresh_token}, timeout=10)
    except Exception:
        pass
    current_member.google_refresh_token = None
    db.commit()
    db.refresh(current_member)
    return current_member


def _google_refresh_token_value(member, db: Session = None):
    stored = member.google_refresh_token
    if not stored:
        return None
    if stored.startswith("enc:v1:"):
        return _decrypt_secret(stored[len("enc:v1:"):])

    # Backwards compatibility for tokens stored before encryption was introduced. Use the
    # existing token for this request, then migrate it in place when secure integration
    # storage is configured. No new Google token is ever written in plaintext.
    if db is not None:
        try:
            member.google_refresh_token = "enc:v1:" + _encrypt_secret(stored)
            db.commit()
        except HTTPException:
            db.rollback()
    return stored


def get_google_access_token(member, db: Session = None):
    if not member.google_refresh_token:
        return None
    try:
        refresh_token = _google_refresh_token_value(member, db)
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }, timeout=10)
        if resp.status_code == 400:
            try:
                token_error = resp.json().get("error")
            except Exception:
                token_error = None
            if token_error == "invalid_grant":
                # Consent was revoked or the refresh token otherwise became invalid. Mark the
                # integration disconnected instead of pretending it is still connected forever.
                member.google_refresh_token = None
                if db is not None:
                    db.commit()
                return None
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
    access_token = get_google_access_token(current_member, db)
    if not access_token:
        return {"connected": bool(current_member.google_refresh_token), "meeting": None}

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
            already_tracked = db.query(models.TaskInstance.id).filter(
                models.TaskInstance.owner_id == current_member.id,
                models.TaskInstance.source_calendar_event_id == event.get("id"),
            ).first()
            if already_tracked:
                continue
            return {"connected": True, "meeting": {"id": event.get("id"), "summary": event.get("summary") or "Meeting"}}

    return {"connected": True, "meeting": None}


@app.get("/api/calendar/events")
def get_calendar_events(start: str = None, end: str = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # A plain, real view of what is actually on the connected calendar, useful both as a
    # genuinely handy view and as the clearest possible proof the connection is working,
    # since seeing real events is far easier to verify than waiting for the exact right
    # moment for the background meeting-now check to fire.
    if not current_member.google_refresh_token:
        return {"connected": False, "events": []}
    access_token = get_google_access_token(current_member, db)
    if not access_token:
        return {"connected": bool(current_member.google_refresh_token), "events": [], "error": "Could not refresh access, try reconnecting"}

    now = datetime.utcnow()
    def parse_bound(value, fallback):
        if not value:
            return fallback
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            raise HTTPException(400, "Invalid calendar date range")

    range_start = parse_bound(start, now)
    range_end = parse_bound(end, range_start + timedelta(days=7))
    if range_end <= range_start or range_end - range_start > timedelta(days=62):
        raise HTTPException(400, "Calendar date range must be positive and no longer than 62 days")
    time_min = range_start.isoformat() + "Z"
    time_max = range_end.isoformat() + "Z"
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
        meet_url = event.get("hangoutLink")
        if not meet_url:
            for ep in (event.get("conferenceData") or {}).get("entryPoints", []):
                if ep.get("entryPointType") == "video" and ep.get("uri"):
                    meet_url = ep.get("uri")
                    break
        events.append({
            "id": event.get("id"),
            "summary": event.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "all_day": "dateTime" not in start,
            "has_meet_link": has_meet_link,
            "meet_url": meet_url,
            "html_link": event.get("htmlLink"),
            "attendees": [a.get("email") for a in event.get("attendees", []) if a.get("email")],
        })
    return {"connected": True, "events": events}


def _calendar_attendee_emails(payload, current_member, db):
    attendee_emails = []
    seen_emails = set()
    for member_id in getattr(payload, "attendee_member_ids", []) or []:
        member = db.get(models.Member, member_id)
        if not member:
            raise HTTPException(404, "One of the selected Clockbook users was not found")
        if member.id == current_member.id:
            continue
        email = (member.email or "").strip().lower()
        if not email:
            raise HTTPException(400, f"{member.name} does not have an email address in Clockbook")
        if email not in seen_emails:
            seen_emails.add(email)
            attendee_emails.append(email)
    for raw_email in getattr(payload, "external_emails", []) or []:
        email = raw_email.strip().lower()
        if not email:
            continue
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise HTTPException(400, f"Invalid guest email: {raw_email}")
        if email not in seen_emails and email != (current_member.email or "").strip().lower():
            seen_emails.add(email)
            attendee_emails.append(email)
    return attendee_emails


def _google_event_time(value, all_day):
    if all_day:
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return {"date": d.date().isoformat()}
        except Exception:
            return {"date": value[:10]}
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "Invalid event date/time")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return {"dateTime": d.isoformat()}


@app.post("/api/calendar/events", status_code=201)
def create_calendar_event(payload: schemas.CalendarEventCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.google_refresh_token:
        raise HTTPException(400, "Connect Google Calendar before creating an event")
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(400, "Event name is required")
    access_token = get_google_access_token(current_member, db)
    if not access_token:
        raise HTTPException(400, "Could not refresh Google Calendar access. Reconnect your calendar and try again")
    attendees = _calendar_attendee_emails(payload, current_member, db)
    body = {
        "summary": summary,
        "start": _google_event_time(payload.start, payload.all_day),
        "end": _google_event_time(payload.end, payload.all_day),
        "attendees": [{"email": e} for e in attendees],
    }
    params = {"sendUpdates": "all"}
    if payload.create_meet:
        body["conferenceData"] = {"createRequest": {"requestId": secrets.token_urlsafe(18), "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        params["conferenceDataVersion"] = 1
    try:
        resp = httpx.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            params=params, json=body, timeout=15,
        )
        if resp.status_code in (401, 403):
            raise HTTPException(403, "Google Calendar needs event-edit permission. Reconnect Google Calendar once, then try again")
        resp.raise_for_status()
        event = resp.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not create the Google Calendar event")
    return {"id": event.get("id"), "summary": event.get("summary") or summary}


@app.patch("/api/calendar/events/{event_id}")
def update_calendar_event(event_id: str, payload: schemas.CalendarEventUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.google_refresh_token:
        raise HTTPException(400, "Connect Google Calendar before editing an event")
    access_token = get_google_access_token(current_member, db)
    if not access_token:
        raise HTTPException(400, "Could not refresh Google Calendar access. Reconnect your calendar and try again")
    body = {}
    if payload.summary is not None:
        summary = payload.summary.strip()
        if not summary:
            raise HTTPException(400, "Event name is required")
        body["summary"] = summary
    all_day = bool(payload.all_day) if payload.all_day is not None else False
    if payload.start is not None:
        body["start"] = _google_event_time(payload.start, all_day)
    if payload.end is not None:
        body["end"] = _google_event_time(payload.end, all_day)
    try:
        resp = httpx.patch(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            params={"sendUpdates": "all"}, json=body, timeout=15,
        )
        if resp.status_code == 404:
            raise HTTPException(404, "Calendar event no longer exists")
        if resp.status_code in (401, 403):
            raise HTTPException(403, "Google Calendar needs event-edit permission. Reconnect Google Calendar once, then try again")
        resp.raise_for_status()
        # Keep an unsubmitted ClockBook task linked to this event in sync with a renamed
        # Calendar event. Submitted history is intentionally immutable. Rescheduling itself
        # does not alter tracked timer segments.
        linked = db.query(models.TaskInstance).filter(
            models.TaskInstance.owner_id == current_member.id,
            models.TaskInstance.source_calendar_event_id == event_id,
        ).all()
        for task in linked:
            if payload.summary is not None and task.status != "submitted":
                task.name = summary
            task.calendar_event_deleted_at = None
        if linked:
            db.commit()
        return {"ok": True, "reconciled_tasks": len(linked)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not update the Google Calendar event")


def _mark_calendar_event_deleted(db: Session, member_id: str, event_id: str) -> int:
    linked = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id == member_id,
        models.TaskInstance.source_calendar_event_id == event_id,
    ).all()
    if linked:
        deleted_at = datetime.utcnow()
        for task in linked:
            task.calendar_event_deleted_at = deleted_at
        db.commit()
    return len(linked)


@app.delete("/api/calendar/events/{event_id}")
def delete_calendar_event(event_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.google_refresh_token:
        raise HTTPException(400, "Connect Google Calendar before deleting an event")
    access_token = get_google_access_token(current_member, db)
    if not access_token:
        raise HTTPException(400, "Could not refresh Google Calendar access. Reconnect your calendar and try again")
    try:
        resp = httpx.delete(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"sendUpdates": "all"}, timeout=15,
        )
        if resp.status_code == 404:
            reconciled = _mark_calendar_event_deleted(db, current_member.id, event_id)
            return {"ok": True, "reconciled_tasks": reconciled}
        if resp.status_code in (401, 403):
            raise HTTPException(403, "Google Calendar needs event-edit permission. Reconnect Google Calendar once, then try again")
        resp.raise_for_status()
        reconciled = _mark_calendar_event_deleted(db, current_member.id, event_id)
        return {"ok": True, "reconciled_tasks": reconciled}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not delete the Google Calendar event")


@app.post("/api/calendar/quick-meeting", status_code=201)
def create_quick_meeting(payload: schemas.QuickMeetingCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if not current_member.google_refresh_token:
        raise HTTPException(400, "Connect Google Calendar before creating a meeting")

    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(400, "Meeting name is required")
    if payload.duration_minutes < 5 or payload.duration_minutes > 240:
        raise HTTPException(400, "Meeting duration must be between 5 and 240 minutes")

    request_id = (payload.request_id or "").strip()
    if not request_id:
        # Backwards compatibility for an older frontend during a rolling deployment. The
        # current frontend always supplies a stable key for the lifetime of the modal.
        request_id = secrets.token_urlsafe(24)
    if len(request_id) > 200:
        raise HTTPException(400, "Invalid meeting request id")

    # A replay/retry of an already-completed request returns the existing task instead of
    # creating another event. Fetching the Google event is best-effort and only enriches links.
    existing = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id == current_member.id,
        models.TaskInstance.quick_meeting_request_id == request_id,
    ).first()
    if existing:
        event = {}
        access_token = get_google_access_token(current_member, db)
        if access_token and existing.source_calendar_event_id:
            try:
                ev_resp = httpx.get(
                    f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{existing.source_calendar_event_id}",
                    headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
                )
                if ev_resp.is_success:
                    event = ev_resp.json()
            except Exception:
                pass
        meet_url = event.get("hangoutLink")
        if not meet_url:
            for entry in (event.get("conferenceData") or {}).get("entryPoints", []):
                if entry.get("entryPointType") == "video" and entry.get("uri"):
                    meet_url = entry.get("uri")
                    break
        return {
            "event_id": existing.source_calendar_event_id,
            "summary": existing.name,
            "meet_url": meet_url,
            "calendar_url": event.get("htmlLink"),
            "attendees": [],
            "task": schemas.TaskOut.model_validate(existing).model_dump(mode="json"),
            "reused": True,
        }

    access_token = get_google_access_token(current_member, db)
    if not access_token:
        raise HTTPException(400, "Could not refresh Google Calendar access. Reconnect your calendar and try again")

    attendee_emails = []
    seen_emails = set()
    for member_id in payload.attendee_member_ids:
        member = db.get(models.Member, member_id)
        if not member:
            raise HTTPException(404, "One of the selected Clockbook users was not found")
        if member.id == current_member.id:
            continue
        email = (member.email or "").strip().lower()
        if not email:
            raise HTTPException(400, f"{member.name} does not have an email address in Clockbook")
        if email not in seen_emails:
            seen_emails.add(email)
            attendee_emails.append(email)

    for raw_email in payload.external_emails:
        email = raw_email.strip().lower()
        if not email:
            continue
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise HTTPException(400, f"Invalid guest email: {raw_email}")
        if email not in seen_emails and email != (current_member.email or "").strip().lower():
            seen_emails.add(email)
            attendee_emails.append(email)

    now = datetime.utcnow().replace(microsecond=0)
    end = now + timedelta(minutes=payload.duration_minutes)
    # Google permits caller-supplied event ids. Deriving one from our idempotency key makes
    # duplicate/retried inserts converge on the same external event too, not just the same DB row.
    event_id = "cb" + hashlib.sha256(f"{current_member.id}:{request_id}".encode("utf-8")).hexdigest()[:40]
    event_body = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": now.isoformat() + "Z"},
        "end": {"dateTime": end.isoformat() + "Z"},
        "attendees": [{"email": email} for email in attendee_emails],
        "conferenceData": {
            "createRequest": {
                "requestId": "cb-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32],
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    try:
        resp = httpx.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            params={"conferenceDataVersion": 1, "sendUpdates": "all"},
            json=event_body,
            timeout=15,
        )
        if resp.status_code in (401, 403):
            raise HTTPException(403, "Google Calendar needs meeting-creation permission. Reconnect Google Calendar once, then try again")
        if resp.status_code == 429:
            raise HTTPException(503, "Google Calendar is temporarily rate-limiting requests. Please try again shortly")
        if resp.status_code == 409:
            # The same idempotent request already created the Google event (for example the
            # first response was lost). Re-read that exact event and continue local recovery.
            existing_resp = httpx.get(
                f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
            )
            existing_resp.raise_for_status()
            event = existing_resp.json()
        else:
            resp.raise_for_status()
            event = resp.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not create the Google Calendar meeting")

    returned_event_id = event.get("id") or event_id

    client = None
    if payload.client_id:
        client = db.get(models.Client, payload.client_id)
        if not client:
            raise HTTPException(404, "Client not found")
    if not client:
        client = get_or_create_internal_support_client(db)

    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=summary,
        task_type="Non-billable: Colleague Meeting",
        owner_id=current_member.id,
        status="todo",
        segments=[],
        note="",
        source_calendar_event_id=returned_event_id,
        quick_meeting_request_id=request_id,
        calendar_event_deleted_at=None,
    )
    db.add(task)
    try:
        db.commit()
        db.refresh(task)
    except IntegrityError:
        # Concurrent retry won the local insert race. Reuse its task instead of duplicating.
        db.rollback()
        task = db.query(models.TaskInstance).filter(
            models.TaskInstance.owner_id == current_member.id,
            models.TaskInstance.quick_meeting_request_id == request_id,
        ).first()
        if not task:
            raise
        reused = True
    else:
        reused = False
        task = start_task(task.id, schemas.TaskStart(), current_member, db)

    meet_url = event.get("hangoutLink")
    if not meet_url:
        for entry in (event.get("conferenceData") or {}).get("entryPoints", []):
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                meet_url = entry.get("uri")
                break

    return {
        "event_id": returned_event_id,
        "summary": task.name,
        "meet_url": meet_url,
        "calendar_url": event.get("htmlLink"),
        "attendees": attendee_emails,
        "task": schemas.TaskOut.model_validate(task).model_dump(mode="json"),
        "reused": reused,
    }


@app.get("/api/calendar/suggested-tasks")
def get_suggested_tasks(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Calendar events without a meeting link, these are never meetings to prompt a pause
    # for, they're plain work items that might be worth turning into a To Do task. Checks
    # against every task this person has ever created, not just the recent window the
    # dashboard itself is optimized for, so an event does not get suggested again just
    # because the task it already produced was submitted a while ago.
    if not current_member.google_refresh_token:
        return {"connected": False, "suggestions": []}
    access_token = get_google_access_token(current_member, db)
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
# Tenant invitations / memberships
# ---------------------------------------------------------------

INVITATION_TTL_DAYS = 7


def _invitation_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invitation_url(request: Request, token: str) -> str:
    # Production serves the frontend from this FastAPI app. CLOCKBOOK_PUBLIC_URL is
    # preferred so invitation links remain correct behind proxies/custom domains.
    base = CLOCKBOOK_PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/?invite={token}"


def _send_invitation_email(*, request: Request, token: str, invitation: models.TenantInvitation, tenant: models.Tenant, inviter: models.Member):
    """Send one tenant invitation through Resend.

    Invitation creation never depends on email availability. If Resend is unavailable or
    misconfigured, the secure invitation remains valid and the UI can still copy its link.
    Raw invitation tokens are never written to logs.
    """
    if not RESEND_API_KEY or not RESEND_FROM_EMAIL:
        return {"status": "not_configured", "error": "Invitation email is not configured."}

    invite_url = _invitation_url(request, token)
    workspace_name = tenant.name if tenant else "your ClockBook workspace"
    recipient_name = invitation.name or invitation.email
    role_label = {"member": "Staff", "admin": "Admin", "super_admin": "Super Admin"}.get(invitation.role, invitation.role)
    safe_name = html.escape(recipient_name)
    safe_workspace = html.escape(workspace_name)
    safe_inviter = html.escape(inviter.name or "A ClockBook administrator")
    safe_role = html.escape(role_label)
    safe_url = html.escape(invite_url, quote=True)

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [invitation.email],
        "subject": f"You're invited to {workspace_name} on ClockBook",
        "html": (
            '<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#171717">'
            "<h2 style='margin-bottom:8px'>You're invited to ClockBook</h2>"
            f'<p>Hi {safe_name},</p>'
            f'<p>{safe_inviter} invited you to join <strong>{safe_workspace}</strong> as <strong>{safe_role}</strong>.</p>'
            f'<p style="margin:28px 0"><a href="{safe_url}" style="background:#171717;color:#fff;text-decoration:none;padding:11px 18px;border-radius:7px;display:inline-block">Accept invitation</a></p>'
            '<p>This invitation expires in 7 days. If you already use ClockBook, you can join with your existing login.</p>'
            f'<p style="font-size:12px;color:#666;word-break:break-all">If the button does not work, open: {safe_url}</p>'
            '<p style="font-size:12px;color:#888">If you were not expecting this invitation, you can ignore this email.</p>'
            '</div>'
        ),
    }
    if RESEND_REPLY_TO:
        payload["reply_to"] = RESEND_REPLY_TO

    try:
        response = httpx.post(
            RESEND_EMAIL_ENDPOINT,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json", "Idempotency-Key": f"clockbook-invite/{invitation.id}/{_invitation_token_hash(token)[:20]}"},
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json() if response.content else {}
        _log_event(
            "tenant_invitation_email_sent",
            tenant_id=invitation.tenant_id,
            invitation_id=invitation.id,
            recipient_hash=_rate_limit_identity(invitation.email.lower())[:16],
            provider="resend",
        )
        return {"status": "sent", "provider_message_id": data.get("id")}
    except Exception as exc:
        _log_event(
            "tenant_invitation_email_failed",
            tenant_id=invitation.tenant_id,
            invitation_id=invitation.id,
            recipient_hash=_rate_limit_identity(invitation.email.lower())[:16],
            provider="resend",
            error_type=type(exc).__name__,
        )
        return {"status": "failed", "error": "Resend could not deliver the invitation email. You can still copy the invitation link."}


def _invite_role_allowed(current_member: models.Member, role: str) -> bool:
    if role not in ("member", "admin", "super_admin"):
        return False
    if role == "super_admin" and current_member.role != "super_admin":
        return False
    return current_member.role in ("admin", "super_admin")


def _find_invitation_by_token(db: Session, token: str, lock: bool = False):
    token = (token or "").strip()
    if not token:
        return None
    q = db.query(models.TenantInvitation).filter(
        models.TenantInvitation.token_hash == _invitation_token_hash(token)
    ).execution_options(skip_tenant_scope=True)
    if lock and engine.dialect.name == "postgresql":
        q = q.with_for_update()
    return q.first()


def _validate_live_invitation(invitation: models.TenantInvitation):
    if not invitation:
        raise HTTPException(404, "Invitation not found")
    if invitation.accepted_at:
        raise HTTPException(410, "This invitation has already been used")
    if invitation.revoked_at:
        raise HTTPException(410, "This invitation has been revoked")
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(410, "This invitation has expired")


@app.get("/api/invitations/{token}", response_model=schemas.TenantInvitationPublic)
def get_invitation(token: str, db: Session = Depends(get_db)):
    invitation = _find_invitation_by_token(db, token)
    _validate_live_invitation(invitation)
    tenant = db.get(models.Tenant, invitation.tenant_id)
    if not tenant or tenant.status != "active":
        raise HTTPException(410, "This workspace is no longer available")
    user = db.query(models.User).filter(func.lower(models.User.email) == invitation.email.lower()).first()
    return schemas.TenantInvitationPublic(
        workspace_name=tenant.name,
        email=invitation.email,
        name=invitation.name,
        role=invitation.role,
        existing_user=bool(user),
        expires_at=invitation.expires_at,
    )


@app.post("/api/invitations/{token}/accept", response_model=schemas.LoginResponse)
def accept_invitation(token: str, payload: schemas.TenantInvitationAccept, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(db, f"invite-accept:{_client_ip(request)}", limit=20, window_seconds=900)
    invitation = _find_invitation_by_token(db, token, lock=True)
    _validate_live_invitation(invitation)
    tenant = db.get(models.Tenant, invitation.tenant_id)
    if not tenant or tenant.status != "active":
        raise HTTPException(410, "This workspace is no longer available")

    email = invitation.email.strip().lower()
    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if user:
        if user.status != "active":
            raise HTTPException(400, "This login is not active")
        if not user.password_hash or not verify_password(payload.password, user.password_hash):
            # This is intentionally a 400 rather than a 401. A failed invitation password
            # check must not make the browser treat an unrelated current session as revoked.
            raise HTTPException(400, "That password does not match your existing ClockBook login")
    else:
        if len(payload.password or "") < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        user = models.User(
            email=email,
            password_hash=hash_password(payload.password),
            default_tenant_id=invitation.tenant_id,
        )
        db.add(user)
        db.flush()

    # From this point on, every tenant-owned read/write is automatically scoped to the
    # invited workspace, including the membership and new session created below.
    db.info["tenant_id"] = invitation.tenant_id
    existing_membership = db.query(models.Member).filter(models.Member.user_id == user.id).first()
    if existing_membership:
        invitation.accepted_at = datetime.utcnow()
        db.commit()
        raise HTTPException(400, "You already belong to this workspace")

    display_name = (payload.name or invitation.name or "").strip()
    if not display_name:
        raise HTTPException(400, "Name is required")
    color_idx = db.query(models.Member).count()
    member = models.Member(
        user_id=user.id,
        name=display_name,
        email=user.email,
        password_hash=user.password_hash,
        color_idx=color_idx,
        role=invitation.role,
    )
    db.add(member)
    db.flush()
    invitation.accepted_at = datetime.utcnow()
    session_token = secrets.token_urlsafe(32)
    db.add(models.Session(token=session_token, user_id=user.id, member_id=member.id))
    db.add(models.LoginEvent(member_id=member.id))
    db.commit()
    db.refresh(member)
    return schemas.LoginResponse(token=session_token, member=member)


@app.get("/api/tenant-invitations", response_model=list[schemas.TenantInvitationOut])
def list_tenant_invitations(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    # Be explicit here even though the ORM tenant guard also applies. Invitations are an
    # access-control surface and must never leak into another workspace's Staff page.
    return (
        db.query(models.TenantInvitation)
        .filter(models.TenantInvitation.tenant_id == current_member.tenant_id)
        .order_by(models.TenantInvitation.created_at.desc())
        .limit(100)
        .all()
    )


@app.post("/api/tenant-invitations", response_model=schemas.TenantInvitationCreated, status_code=201)
def create_tenant_invitation(payload: schemas.TenantInvitationCreate, request: Request, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    email = payload.email.strip().lower()
    name = payload.name.strip()
    role = (payload.role or "member").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    if not email or "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    if not _invite_role_allowed(current_member, role):
        raise HTTPException(403, "You cannot invite someone with that access level")

    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if user and db.query(models.Member).filter(
        models.Member.user_id == user.id,
        models.Member.tenant_id == current_member.tenant_id,
    ).first():
        raise HTTPException(400, "That person is already a member of this workspace")

    now = datetime.utcnow()
    pending = db.query(models.TenantInvitation).filter(
        models.TenantInvitation.tenant_id == current_member.tenant_id,
        func.lower(models.TenantInvitation.email) == email,
        models.TenantInvitation.accepted_at.is_(None),
        models.TenantInvitation.revoked_at.is_(None),
    ).order_by(models.TenantInvitation.created_at.desc()).first()
    if pending and pending.expires_at >= now:
        raise HTTPException(400, "There is already a pending invitation for that email")
    if pending and pending.expires_at < now:
        pending.revoked_at = now

    raw_token = secrets.token_urlsafe(32)
    invitation = models.TenantInvitation(
        tenant_id=current_member.tenant_id,
        email=email,
        name=name,
        role=role,
        token_hash=_invitation_token_hash(raw_token),
        invited_by_id=current_member.id,
        expires_at=now + timedelta(days=INVITATION_TTL_DAYS),
    )
    db.add(invitation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "There is already a pending invitation for that email")
    db.refresh(invitation)
    tenant = db.get(models.Tenant, current_member.tenant_id)
    delivery = _send_invitation_email(request=request, token=raw_token, invitation=invitation, tenant=tenant, inviter=current_member)
    return schemas.TenantInvitationCreated(
        id=invitation.id,
        email=invitation.email,
        name=invitation.name,
        role=invitation.role,
        status=invitation.status,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        token=raw_token,
        email_status=delivery.get("status", "failed"),
        email_error=delivery.get("error"),
        provider_message_id=delivery.get("provider_message_id"),
    )


@app.post("/api/tenant-invitations/{invitation_id}/regenerate", response_model=schemas.TenantInvitationCreated)
def regenerate_tenant_invitation(invitation_id: str, request: Request, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    invitation = db.query(models.TenantInvitation).filter(
        models.TenantInvitation.id == invitation_id,
        models.TenantInvitation.tenant_id == current_member.tenant_id,
    ).first()
    if not invitation:
        raise HTTPException(404, "Invitation not found")
    if invitation.accepted_at:
        raise HTTPException(400, "An accepted invitation cannot be regenerated")
    if invitation.revoked_at:
        raise HTTPException(400, "A revoked invitation cannot be regenerated")
    if not _invite_role_allowed(current_member, invitation.role):
        raise HTTPException(403, "You cannot manage that invitation")
    raw_token = secrets.token_urlsafe(32)
    invitation.token_hash = _invitation_token_hash(raw_token)
    invitation.expires_at = datetime.utcnow() + timedelta(days=INVITATION_TTL_DAYS)
    db.commit()
    db.refresh(invitation)
    tenant = db.get(models.Tenant, current_member.tenant_id)
    delivery = _send_invitation_email(request=request, token=raw_token, invitation=invitation, tenant=tenant, inviter=current_member)
    return schemas.TenantInvitationCreated(
        id=invitation.id,
        email=invitation.email,
        name=invitation.name,
        role=invitation.role,
        status=invitation.status,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        token=raw_token,
        email_status=delivery.get("status", "failed"),
        email_error=delivery.get("error"),
        provider_message_id=delivery.get("provider_message_id"),
    )


@app.delete("/api/tenant-invitations/{invitation_id}", status_code=204)
def revoke_tenant_invitation(invitation_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    invitation = db.query(models.TenantInvitation).filter(
        models.TenantInvitation.id == invitation_id,
        models.TenantInvitation.tenant_id == current_member.tenant_id,
    ).first()
    if not invitation:
        return None
    if invitation.accepted_at:
        raise HTTPException(400, "An accepted invitation cannot be revoked")
    if not _invite_role_allowed(current_member, invitation.role):
        raise HTTPException(403, "You cannot manage that invitation")
    invitation.revoked_at = datetime.utcnow()
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
    _require_delegated_admin_permission(current_member, PERMISSION_ADD_STAFF_MANUALLY, "You do not have permission to add staff manually")
    name = payload.name.strip()
    email = payload.email.strip().lower()
    if not name:
        raise HTTPException(400, "Name is required")
    if not email or "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    if len(payload.password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if not email:
        raise HTTPException(400, "Email is required")
    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if user and db.query(models.Member).filter(
        models.Member.user_id == user.id,
        models.Member.tenant_id == current_member.tenant_id,
    ).first():
        raise HTTPException(400, "That person is already a member of this workspace")
    if user:
        raise HTTPException(400, "That email already has a ClockBook login. Invite them to this workspace instead")
    if not user:
        user = models.User(
            email=email, password_hash=hash_password(payload.password),
            default_tenant_id=current_member.tenant_id,
        )
        db.add(user)
        db.flush()
    count = db.query(models.Member).count()
    member = models.Member(
        tenant_id=current_member.tenant_id,
        user_id=user.id, name=name, email=email, color_idx=count, role="member",
        password_hash=user.password_hash,
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
    _require_expected_version(member, payload.expected_version)
    if current_member.role != "super_admin" and not _member_in_admin_scope(current_member, member):
        raise HTTPException(403, "You cannot manage that person's role")
    # Super-admin authority is never self-grantable. A regular admin may continue managing
    # ordinary Admin/Staff roles, but only an existing super admin can grant, remove, or
    # otherwise touch super-admin status.
    if current_member.role != "super_admin" and (payload.role == "super_admin" or member.role == "super_admin"):
        raise HTTPException(403, "Only a super admin can manage super admin access")
    if is_admin_or_above(member.role) and not is_admin_or_above(payload.role):
        admin_count = db.query(models.Member).filter(models.Member.role.in_(["admin", "super_admin"])).count()
        if admin_count <= 1:
            raise HTTPException(400, "At least one admin is required")
    role_changed = member.role != payload.role
    member.role = payload.role
    if role_changed and payload.role != "admin":
        # Staff may retain personal leave/capacity Insights access, but report permissions
        # are meaningful only for Admins and must not survive a downgrade.
        member.additional_permissions = sorted(_additional_permissions(member) - DELEGATABLE_ADMIN_PERMISSIONS)
    if role_changed:
        _revoke_member_sessions(db, member.id)
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/capacity", response_model=schemas.MemberOut)
def update_member_capacity(member_id: str, payload: schemas.MemberCapacityUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    _require_expected_version(member, payload.expected_version)
    if not _member_in_admin_scope(current_member, member):
        raise HTTPException(403, "You cannot change capacity for that person")
    value = float(payload.weekly_capacity_hours)
    if value < 0 or value > 168:
        raise HTTPException(400, "Weekly capacity must be between 0 and 168 hours")
    member.weekly_capacity_hours = round(value, 2)
    # Treat an explicitly supplied null as a real update so admins can clear
    # the effective date. A cleared date means capacity applies across the
    # selected reporting period instead of being limited by a start date.
    if "capacity_effective_from" in payload.model_fields_set:
        member.capacity_effective_from = payload.capacity_effective_from
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/insights-permission", response_model=schemas.MemberOut)
def update_member_insights_permission(member_id: str, payload: schemas.MemberInsightsPermissionUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can manage leave and capacity insights access")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    _require_expected_version(member, payload.expected_version)
    member.can_view_leave_capacity_insights = bool(payload.enabled)
    permissions = _additional_permissions(member)
    if payload.enabled:
        permissions.add(PERMISSION_INSIGHTS_LEAVE_CAPACITY)
    else:
        permissions.discard(PERMISSION_INSIGHTS_LEAVE_CAPACITY)
    member.additional_permissions = sorted(permissions)
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/additional-permissions", response_model=schemas.MemberOut)
def update_member_additional_permissions(member_id: str, payload: schemas.MemberAdditionalPermissionsUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can manage additional permissions")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    if member.role == "super_admin":
        raise HTTPException(400, "Super Admins already have full access")
    _require_expected_version(member, payload.expected_version)
    requested = {str(item) for item in payload.permissions}
    unknown = requested - ALL_DELEGATABLE_PERMISSIONS
    if unknown:
        raise HTTPException(400, "Unknown permission: " + ", ".join(sorted(unknown)))
    if member.role != "admin":
        invalid = requested & DELEGATABLE_ADMIN_PERMISSIONS
        if invalid:
            raise HTTPException(400, "Elevated permissions can only be granted to Admins")
    member.additional_permissions = sorted(requested)
    # Keep the established field synchronized so existing Insights logic and older clients
    # continue to behave exactly as before while the permission appears in one unified UI.
    member.can_view_leave_capacity_insights = PERMISSION_INSIGHTS_LEAVE_CAPACITY in requested
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/auth/tour", response_model=schemas.MemberOut)
def update_staff_tour(payload: schemas.StaffTourUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # This is deliberately self-service: completing or skipping the onboarding tour only
    # changes the current user's own preference and grants no additional access. Replaying
    # the tour from the UI does not reset this flag, so it will not auto-open again later.
    current_member.staff_tour_completed = bool(payload.completed)
    db.commit()
    db.refresh(current_member)
    return current_member


@app.patch("/api/auth/timezone", response_model=schemas.MemberOut)
def set_initial_timezone(payload: schemas.InitialTimezoneSet, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # One-time self-service setup only. Once a member has a timezone, the existing
    # admin/super-admin endpoint remains the only way to change it.
    if (current_member.timezone_name or "").strip():
        raise HTTPException(403, "Your time zone is managed by an administrator")
    value = (payload.timezone_name or "").strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, "Choose a valid IANA time zone")
    current_member.timezone_name = value
    db.commit()
    db.refresh(current_member)
    return current_member


@app.patch("/api/members/{member_id}/timezone", response_model=schemas.MemberOut)
def update_member_timezone(member_id: str, payload: schemas.MemberTimezoneUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    _require_expected_version(member, payload.expected_version)
    if not _member_in_admin_scope(current_member, member):
        raise HTTPException(403, "You cannot change the time zone for that person")
    value = (payload.timezone_name or "").strip()
    if value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(400, "Choose a valid IANA time zone")
        member.timezone_name = value
    else:
        member.timezone_name = None
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/work-arrangement", response_model=schemas.MemberOut)
def update_member_work_arrangement(member_id: str, payload: schemas.MemberWorkArrangementUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    _require_expected_version(member, payload.expected_version)
    if not _member_in_admin_scope(current_member, member):
        raise HTTPException(403, "You cannot change the work arrangement for that person")
    member.work_arrangement = payload.work_arrangement
    db.commit()
    db.refresh(member)
    return member


@app.patch("/api/members/{member_id}/credentials", response_model=schemas.MemberOut)
def set_member_credentials(member_id: str, payload: schemas.LoginRequest, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Authentication belongs to the global User identity. A tenant admin may reset a user
    # who belongs only to this tenant; changing credentials for a shared multi-tenant identity
    # is intentionally blocked because it would affect that person's access elsewhere.
    require_admin(current_member)
    member = _require_member_in_scope(current_member, member_id, db)
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")
    user = db.get(models.User, member.user_id) if member.user_id else None
    if user:
        membership_count = db.query(models.Member).filter(models.Member.user_id == user.id).execution_options(skip_tenant_scope=True).count()
        if membership_count > 1:
            raise HTTPException(400, "This login belongs to more than one workspace. Global credentials cannot be reset from a workspace admin screen")
        other_user = db.query(models.User).filter(func.lower(models.User.email) == email, models.User.id != user.id).first()
        if other_user:
            raise HTTPException(400, "That email is already registered")
        user.email = email
        user.password_hash = hash_password(payload.password)
        member.email = email
        member.password_hash = user.password_hash
    else:
        if db.query(models.User).filter(func.lower(models.User.email) == email).first():
            raise HTTPException(400, "That email is already registered")
        user = models.User(email=email, password_hash=hash_password(payload.password), default_tenant_id=current_member.tenant_id)
        db.add(user)
        db.flush()
        member.user_id = user.id
        member.email = email
        member.password_hash = user.password_hash
    _revoke_member_sessions(db, member.id)
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
    if not _member_in_admin_scope(current_member, member):
        raise HTTPException(403, "You cannot delete that person")
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
    user_id = member.user_id
    db.query(models.Session).filter(models.Session.member_id == member_id).delete()
    db.delete(member)
    db.flush()
    if user_id:
        # Temporarily bypass the active tenant scope only to determine whether the global
        # identity still has another tenant membership.
        previous_skip = db.info.get("skip_tenant_scope")
        db.info["skip_tenant_scope"] = True
        try:
            remaining = db.query(models.Member).filter(models.Member.user_id == user_id).count()
            if remaining == 0:
                user = db.get(models.User, user_id)
                if user:
                    db.delete(user)
        finally:
            if previous_skip is None:
                db.info.pop("skip_tenant_scope", None)
            else:
                db.info["skip_tenant_scope"] = previous_skip
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


def _slack_token_for_tenant(db: Session):
    token_enc = _setting_value(db, "slack_bot_token_encrypted") if db is not None else ""
    if token_enc:
        return _decrypt_secret(token_enc)
    if db is not None and _current_tenant_id(db) != AROUND_TENANT_ID:
        return ""
    return SLACK_BOT_TOKEN


def find_slack_user_id_by_email(email: str, db: Session = None):
    slack_token = _slack_token_for_tenant(db) if db is not None else SLACK_BOT_TOKEN
    if not slack_token:
        return None, "Slack is not set up for this workspace yet"
    headers = {"Authorization": f"Bearer {slack_token}"}
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


def send_slack_message(slack_user_id: str, text: str, db: Session = None):
    slack_token = _slack_token_for_tenant(db) if db is not None else SLACK_BOT_TOKEN
    if not slack_token or not slack_user_id:
        return False
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {slack_token}"},
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
    slack_user_id, error = find_slack_user_id_by_email(email, db)
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
    ok = send_slack_message(current_member.slack_user_id, "This is a test notification from Clockbook. If you can see this, it's working.", db)
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
def relay_notification(payload: dict, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # The one place a notification actually gets sent to Slack. Only ever sends to the
    # calling person's own resolved Slack id, and only if they have chosen Slack as their
    # channel, so this can never be used to message anyone else
    if current_member.notification_channel != "slack" or not current_member.slack_user_id:
        return {"sent": False, "reason": "not using Slack"}
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "Missing text")
    ok = send_slack_message(current_member.slack_user_id, text, db)
    return {"sent": ok}


def get_or_create_internal_support_client(db: Session):
    # A single, dedicated client standing in for firm-internal time that isn't tied to any
    # real client, reused every time rather than creating a new one per event
    client = db.query(models.Client).filter(models.Client.name == "Internal Support").first()
    if not client:
        client = models.Client(name="Internal Support")
        db.add(client)
        db.flush()
    return client


@app.post("/api/ad-hoc-meetings/start", response_model=schemas.TaskOut, status_code=201)
def start_ad_hoc_meeting(payload: schemas.AdHocMeetingCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # colleague_id is optional so the Ctrl+M shortcut can start immediately. The dashboard
    # button can still choose a colleague before starting, and both paths use the same timer.
    colleague = None
    if payload.colleague_id:
        colleague = db.get(models.Member, payload.colleague_id)
        if not colleague:
            raise HTTPException(404, "Colleague not found")
        if colleague.id == current_member.id:
            raise HTTPException(400, "Select another colleague")

    client = get_or_create_internal_support_client(db)
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=f"Ad hoc meeting with {colleague.name}" if colleague else "Ad hoc meeting",
        task_type="Non-billable: Colleague Meeting",
        owner_id=current_member.id,
        status="todo",
        segments=[],
        note="",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Reuse the app's existing start-task path so the normal one-running-timer rule stays
    # exactly the same: any current timer is paused and this meeting becomes the active timer.
    return start_task(task.id, schemas.TaskStart(), current_member, db)


@app.post("/api/ad-hoc-meetings/{task_id}/finish", response_model=schemas.TaskOut)
def finish_ad_hoc_meeting(task_id: str, payload: schemas.AdHocMeetingFinish, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.task_type != "Non-billable: Colleague Meeting":
        raise HTTPException(400, "This is not an ad hoc meeting")
    if task.status not in ("running", "paused"):
        raise HTTPException(400, "This meeting is already completed")

    colleague = None
    if payload.colleague_id:
        colleague = db.get(models.Member, payload.colleague_id)
        if not colleague:
            raise HTTPException(404, "Colleague not found")
        if colleague.id == current_member.id:
            raise HTTPException(400, "Select another colleague")
    if payload.interaction not in ("general", "helped", "received"):
        raise HTTPException(400, "interaction must be general, helped, or received")
    if payload.interaction in ("helped", "received") and not colleague:
        raise HTTPException(400, "Select a colleague for help interactions")
    if not colleague and not task.source_calendar_event_id:
        raise HTTPException(400, "Select a colleague")
    context = payload.context.strip()
    if not context:
        raise HTTPException(400, "Meeting context is required")

    task.segments = close_open_segment(task.segments)
    seconds = elapsed_seconds(task.segments)
    task.status = "submitted"
    task.submitted_at = datetime.utcnow()
    task.submitted_by_id = current_member.id
    task.submitted_pod_id = current_member.pod_id
    task.pushed_to_karbon = False
    task.note = context

    if payload.interaction == "helped":
        task.name = f"Helped {colleague.name}"
        task.task_type = "Non-billable: Colleague Support"
    elif payload.interaction == "received":
        task.name = f"Received help from {colleague.name}"
        task.task_type = "Non-billable: Colleague Support"
    elif colleague and not task.source_calendar_event_id:
        task.name = f"Ad hoc meeting with {colleague.name}"
    # For a Quick Meeting keep the real calendar meeting title as the task name.

    db.flush()

    # Help classifications feed the existing help report without creating a second task or
    # duplicating the tracked time. General collaboration remains a normal meeting only.
    if payload.interaction in ("helped", "received"):
        event = models.HelpEvent(
            member_id=current_member.id,
            colleague_id=colleague.id,
            direction=payload.interaction,
            seconds=seconds,
            source="ad_hoc_meeting",
            task_id=task.id,
            adjusted=False,
            context=context,
        )
        db.add(event)

    db.commit()
    db.refresh(task)
    return task


@app.post("/api/help-events", response_model=schemas.HelpEventOut, status_code=201)
def create_help_event(payload: schemas.HelpEventCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if payload.direction not in ("helped", "received"):
        raise HTTPException(400, "direction must be 'helped' or 'received'")
    if payload.seconds < 0:
        raise HTTPException(400, "seconds cannot be negative")
    context = payload.context.strip()
    if not context:
        raise HTTPException(400, "Help context is required")
    colleague = db.get(models.Member, payload.colleague_id)
    if not colleague:
        raise HTTPException(404, "Colleague not found")

    linked_inactivity = None
    if payload.inactivity_event_id:
        linked_inactivity = db.get(models.InactivityEvent, payload.inactivity_event_id)
        if not linked_inactivity or linked_inactivity.member_id != current_member.id:
            raise HTTPException(400, "Invalid inactivity event")
        if payload.source != "sleep_alert":
            raise HTTPException(400, "Only sleep/lock help can resolve an inactivity event")

    client = get_or_create_internal_support_client(db)
    now = datetime.utcnow()
    start = now - timedelta(seconds=payload.seconds)
    task_name = f"Helped {colleague.name}" if payload.direction == "helped" else f"Received help from {colleague.name}"
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=task_name,
        task_type="Non-billable: Colleague Support",
        owner_id=current_member.id,
        status="submitted",
        segments=[{"start": start.isoformat() + "Z", "end": now.isoformat() + "Z"}],
        note=context,
        submitted_at=now,
        submitted_by_id=current_member.id,
        submitted_pod_id=current_member.pod_id,
    )
    db.add(task)
    db.flush()

    event = models.HelpEvent(
        member_id=current_member.id,
        colleague_id=payload.colleague_id,
        direction=payload.direction,
        seconds=payload.seconds,
        source=payload.source,
        task_id=task.id,
        adjusted=payload.adjusted,
        context=context,
        inactivity_event_id=linked_inactivity.id if linked_inactivity else None,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@app.post("/api/inactivity-events")
def create_inactivity_event(payload: schemas.InactivityEventCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Recording is controlled live from Clockbook Settings. When disabled, clients can
    # keep calling this endpoint without errors and nothing is persisted.
    if not inactivity_audit_enabled(db):
        return {"recorded": False}
    if payload.kind not in ("screen_locked", "sleep_gap", "stale_gap"):
        raise HTTPException(400, "Unknown inactivity event type")
    started = payload.started_at.replace(tzinfo=None) if payload.started_at.tzinfo else payload.started_at
    ended = payload.ended_at.replace(tzinfo=None) if payload.ended_at.tzinfo else payload.ended_at
    if ended <= started:
        raise HTTPException(400, "ended_at must be after started_at")
    seconds = (ended - started).total_seconds()
    if seconds < 1:
        raise HTTPException(400, "Inactivity period is too short")
    event = models.InactivityEvent(
        member_id=current_member.id,
        kind=payload.kind,
        started_at=started,
        ended_at=ended,
        seconds=seconds,
        task_id=payload.task_id,
    )
    db.add(event)
    db.commit()
    return {"recorded": True, "id": event.id}


@app.get("/api/inactivity-events/status")
def inactivity_audit_status(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    if current_member.role != "super_admin" and not (
        current_member.role == "admin" and (
            _has_permission(current_member, PERMISSION_REPORT_AUDIT)
            or _has_permission(current_member, PERMISSION_MANAGE_AUDIT_RECORDING)
        )
    ):
        raise HTTPException(403, "You do not have access to Audit status")
    return {"enabled": inactivity_audit_enabled(db)}


@app.put("/api/inactivity-events/status")
def update_inactivity_audit_status(payload: schemas.InactivityAuditSettingUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_AUDIT_RECORDING, "You do not have permission to change audit recording")
    return {"enabled": set_inactivity_audit_enabled(db, payload.enabled)}


@app.get("/api/insights/delegation-exclusions")
def get_delegation_suggestion_exclusions(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_DELEGATION_EXCLUSIONS, "You do not have permission to manage delegation suggestion exclusions")
    return {"exclusions": _delegation_exclusions(db), "defaults": list(DEFAULT_DELEGATION_EXCLUSIONS)}


@app.put("/api/insights/delegation-exclusions")
def update_delegation_suggestion_exclusions(payload: schemas.DelegationSuggestionExclusionsUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_DELEGATION_EXCLUSIONS, "You do not have permission to manage delegation suggestion exclusions")
    return {"exclusions": _set_delegation_exclusions(db, payload.exclusions), "defaults": list(DEFAULT_DELEGATION_EXCLUSIONS)}


@app.get("/api/inactivity-events", response_model=list[schemas.InactivityEventDetail])
def get_inactivity_events(date_from: str = None, date_to: str = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_REPORT_AUDIT, "You do not have access to the Audit report")
    if not inactivity_audit_enabled(db):
        return []
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    query = db.query(models.InactivityEvent).filter(models.InactivityEvent.member_id.in_(allowed_ids)).order_by(models.InactivityEvent.started_at.desc())
    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date_from must be YYYY-MM-DD")
        query = query.filter(models.InactivityEvent.started_at >= start)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(400, "date_to must be YYYY-MM-DD")
        query = query.filter(models.InactivityEvent.started_at < end)
    events = query.all()

    # Help classifications from a sleep/lock prompt carry the exact inactivity-event ID
    # that produced that prompt. Deduct only the linked help duration from that exact
    # inactivity period. If the help duration covers the whole period, the row disappears;
    # if it covers only part, the unexplained remainder stays visible.
    linked_help_seconds = {}
    for inactivity_event_id, help_seconds in (
        db.query(models.HelpEvent.inactivity_event_id, models.HelpEvent.seconds)
        .filter(
            models.HelpEvent.source == "sleep_alert",
            models.HelpEvent.inactivity_event_id.isnot(None),
        )
        .all()
    ):
        if inactivity_event_id:
            linked_help_seconds[inactivity_event_id] = linked_help_seconds.get(inactivity_event_id, 0.0) + max(float(help_seconds or 0), 0.0)

    member_names = {m.id: m.name for m in db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).all()}
    result = []
    for e in events:
        explained = min(float(e.seconds or 0), linked_help_seconds.get(e.id, 0.0))
        remaining = max(float(e.seconds or 0) - explained, 0.0)
        if remaining <= 0:
            continue
        result.append(
            schemas.InactivityEventDetail(
                id=e.id, member_id=e.member_id, member_name=member_names.get(e.member_id, "Unknown"),
                kind=e.kind, started_at=e.started_at, ended_at=e.ended_at, seconds=remaining,
                original_seconds=float(e.seconds or 0), help_seconds=explained, task_id=e.task_id
            )
        )
    return result


@app.get("/api/help-events/summary", response_model=list[schemas.HelpSummaryRow])
def help_events_summary(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_REPORT_HELP, "You do not have access to the Help activity report")
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    # Every member who has either given or received help shows up here, so this starts from
    # the member list rather than the events, or someone with only one side of the ledger
    # (e.g. only ever helped, never received) would be missing from their own row
    members = db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).all()
    events = db.query(models.HelpEvent).filter(models.HelpEvent.member_id.in_(allowed_ids)).all()
    by_member = {m.id: {"member_id": m.id, "member_name": m.name, "helped_seconds": 0.0, "received_seconds": 0.0, "helped_count": 0, "received_count": 0} for m in members}
    for e in events:
        row = by_member.get(e.member_id)
        if not row:
            continue
        if e.direction == "helped":
            row["helped_seconds"] += e.seconds
            row["helped_count"] += 1
        else:
            row["received_seconds"] += e.seconds
            row["received_count"] += 1
    return [r for r in by_member.values() if r["helped_count"] > 0 or r["received_count"] > 0]


@app.get("/api/help-events/detail", response_model=list[schemas.HelpEventDetail])
def help_events_detail(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_REPORT_HELP, "You do not have access to the Help activity report")
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    events = db.query(models.HelpEvent).filter(models.HelpEvent.member_id.in_(allowed_ids)).order_by(models.HelpEvent.created_at.desc()).all()
    names = {m.id: m.name for m in db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).all()}
    return [
        schemas.HelpEventDetail(
            id=e.id,
            member_name=names.get(e.member_id, "Unknown"),
            colleague_name=names.get(e.colleague_id, "Other team member"),
            direction=e.direction,
            seconds=e.seconds,
            created_at=e.created_at,
            task_id=e.task_id,
            adjusted=e.adjusted,
            context=e.context or "",
        )
        for e in events
    ]



# ---------------------------------------------------------------
# Insights
# ---------------------------------------------------------------

def _insights_allowed_member_ids(current_member: models.Member, db: Session):
    if current_member.role == "member":
        return {current_member.id}
    if current_member.role == "super_admin":
        return {m.id for m in db.query(models.Member.id).all()}
    # Match the existing admin visibility model used by Dashboard / Export. By default,
    # regular admins do not see Super Admin data. A Super Admin can explicitly delegate
    # Manage Super Admins, which adds Super Admin accounts to the admin's normal read scope
    # without allowing role/security changes to those accounts. Pod scope still applies to
    # ordinary members; Super Admins are included explicitly because they are commonly podless.
    query = db.query(models.Member.id).filter(models.Member.role != "super_admin")
    if current_member.pod_id:
        query = query.filter(models.Member.pod_id == current_member.pod_id)
    allowed = {m.id for m in query.all()}
    if _has_permission(current_member, PERMISSION_MANAGE_SUPER_ADMINS):
        allowed.update(m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all())
    return allowed


def _insights_task_work_date(task: models.TaskInstance):
    starts = []
    for seg in (task.segments or []):
        value = seg.get("start") if isinstance(seg, dict) else None
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            starts.append(dt)
        except Exception:
            continue
    return min(starts) if starts else task.submitted_at


def _insights_task_seconds(task: models.TaskInstance):
    """Return the final submitted duration used by every Insights metric.

    A submitted task can contain timer-tracked time, a manually entered duration, or an
    edited duration that replaces what the timer captured.  Insights must use the same
    final duration shown in Export, not raw timer segments only.
    """
    tracked = max(float(elapsed_seconds(task.segments)), 0.0)
    adjusted = getattr(task, "adjusted_seconds", None)
    if adjusted is None:
        return tracked
    try:
        return max(float(adjusted), 0.0)
    except (TypeError, ValueError):
        return tracked


def _insights_is_support(task: models.TaskInstance):
    return (task.task_type or "") == "Non-billable: Colleague Support"


def _insights_is_meeting(task: models.TaskInstance):
    task_type = (task.task_type or "").lower()
    name = (task.name or "").lower()
    # Calendar-tracked meetings can have arbitrary event titles (for example an internal
    # academy/session name), so source_calendar_event_id is the strongest signal.
    return bool(task.source_calendar_event_id) or "meeting" in task_type or "meeting" in name


def _insights_is_billable(task: models.TaskInstance, billing_by_type=None):
    task_type = (task.task_type or "").strip()
    if billing_by_type is not None and task_type in billing_by_type:
        return bool(billing_by_type[task_type])
    # Backwards-compatible fallback for historical/special task types that are not present
    # in Settings. Configured task types use the explicit Settings toggle above.
    return task_type.lower().startswith("billable:")


def _insights_client_work_type(task: models.TaskInstance):
    """Return the broad client-work bucket used by the Client Work drill-down.

    Prefer the template field because it is the most stable broad classification (for
    example Bookkeeping, Year-End Accounts, Tax, Company Secretarial / CRO). Historical
    or ad-hoc billable tasks fall back to their configured task type and finally task name.
    """
    label = (
        getattr(task, "source_template_field", None)
        or getattr(task, "source_template_category", None)
        or task.task_type
        or task.name
        or "Other client work"
    )
    label = str(label).strip() or "Other client work"
    if label.lower().startswith("billable:"):
        label = label.split(":", 1)[1].strip() or "Other client work"
    return label


def _insights_client_work_period_label(task: models.TaskInstance):
    label = period_label(task)
    return label or "Period not recorded"


def _insights_client_work_period_key(task: models.TaskInstance):
    key = period_key(task)
    return key if key.strip("|") else "unassigned"


def _insights_client_work_metric(task: models.TaskInstance):
    label = (getattr(task, "tracks_number_label", None) or "").strip()
    if not label or task.start_count is None or task.end_count is None:
        return None
    try:
        quantity = max(int(task.end_count) - int(task.start_count), 0)
    except (TypeError, ValueError):
        return None
    return label, quantity


def _insights_business_days(start_date, end_date):
    if end_date < start_date:
        return 0
    count = 0
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _insights_capacity_effective_start(member: models.Member, start_date):
    effective_from = getattr(member, "capacity_effective_from", None)
    return max(start_date, effective_from) if effective_from else start_date


def _insights_capacity_seconds(member: models.Member, start_date, end_date, unavailable_by_date=None):
    effective_start = _insights_capacity_effective_start(member, start_date)
    if effective_start > end_date:
        return 0.0
    weekly_hours = max(float(getattr(member, "weekly_capacity_hours", 40.0) or 0.0), 0.0)
    business_days = _insights_business_days(effective_start, end_date)
    base_seconds = weekly_hours * 3600.0 * (business_days / 5.0)
    if not unavailable_by_date:
        return base_seconds
    adjustment = 0.0
    cursor = effective_start
    daily_capacity = weekly_hours * 3600.0 / 5.0
    while cursor <= end_date:
        if cursor.weekday() < 5:
            adjustment += min(max(float(unavailable_by_date.get(cursor.isoformat(), 0.0) or 0.0), 0.0), daily_capacity)
        cursor += timedelta(days=1)
    return max(base_seconds - adjustment, 0.0)


def _insights_capacity_tasks(member: models.Member, tasks, start_date, end_date):
    effective_start = _insights_capacity_effective_start(member, start_date)
    if effective_start > end_date:
        return []
    rows = []
    for task in tasks:
        dt = _insights_task_work_date(task)
        if dt and effective_start <= dt.date() <= end_date:
            rows.append(task)
    return rows


def _insights_capacity_trend(member: models.Member, tasks, start_date, end_date, billing_by_type=None, unavailable_by_date=None):
    effective_start = _insights_capacity_effective_start(member, start_date)
    if effective_start > end_date:
        return []

    # A single-week report is easier to interpret day-by-day. Longer periods
    # use calendar-week buckets (Monday-Sunday), which keeps the chart readable.
    use_daily = (end_date - start_date).days <= 6
    buckets = {}
    if use_daily:
        cursor = effective_start
        while cursor <= end_date:
            buckets[cursor.isoformat()] = {
                "period_start": cursor.isoformat(),
                "capacity_seconds": round(_insights_capacity_seconds(member, cursor, cursor, unavailable_by_date), 1),
                "tracked_seconds": 0.0,
                "billable_seconds": 0.0,
            }
            cursor += timedelta(days=1)
    else:
        first_week = effective_start - timedelta(days=effective_start.weekday())
        last_week = end_date - timedelta(days=end_date.weekday())
        cursor = first_week
        while cursor <= last_week:
            week_end = cursor + timedelta(days=6)
            overlap_start = max(cursor, effective_start)
            overlap_end = min(week_end, end_date)
            buckets[cursor.isoformat()] = {
                "period_start": cursor.isoformat(),
                "capacity_seconds": round(_insights_capacity_seconds(member, overlap_start, overlap_end, unavailable_by_date), 1),
                "tracked_seconds": 0.0,
                "billable_seconds": 0.0,
            }
            cursor += timedelta(days=7)

    for task in tasks:
        dt = _insights_task_work_date(task)
        if not dt:
            continue
        day = dt.date()
        if day < effective_start or day > end_date:
            continue
        bucket_start = day if use_daily else day - timedelta(days=day.weekday())
        row = buckets.get(bucket_start.isoformat())
        if not row:
            continue
        seconds = _insights_task_seconds(task)
        row["tracked_seconds"] += seconds
        if _insights_is_billable(task, billing_by_type):
            row["billable_seconds"] += seconds
    return [
        {**row, "tracked_seconds": round(row["tracked_seconds"], 1), "billable_seconds": round(row["billable_seconds"], 1)}
        for _, row in sorted(buckets.items())
    ]


def _insights_change(current_value, previous_value):
    current_value = float(current_value or 0)
    previous_value = float(previous_value or 0)
    # A percentage increase from a zero baseline is undefined.  Return None and
    # let the UI describe this as "New" rather than a misleading 100% rise.
    if previous_value <= 0:
        return None
    return round(((current_value - previous_value) / previous_value) * 100, 1)


@app.get("/api/insights/client-work")
def get_insights_client_work(
    member_id: str = None,
    view: str = "this_month",
    date_from: str = None,
    date_to: str = None,
    current_member: models.Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    """Billable client work for one visible person, grouped by work period.

    Visibility filters choose which work periods are shown, not which task records count
    inside a shown period. Once a period qualifies (for example September bookkeeping
    that was worked on again in October), every historical submitted billable task for
    that same client + work type + recorded period contributes to its total.

    The endpoint deliberately reuses the existing Insights visibility model: staff can
    only see themselves, Admins can only see members already in their permitted/pod
    scope, and Super Admins can see everyone. Non-billable internal/admin/help/L&D work
    never enters this response.
    """
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    target_id = member_id or current_member.id
    if target_id not in allowed_ids:
        raise HTTPException(403, "You cannot view client work for that person")
    target = db.get(models.Member, target_id)
    if not target:
        raise HTTPException(404, "Member not found")

    normalized_view = (view or "this_month").strip().lower()
    # Keep the older view names as API-compatible aliases even though the current
    # UI uses week/month/custom activity windows. The activity window decides
    # which work periods appear; it never truncates the historical records that
    # contribute to a qualifying period's accumulated total.
    if normalized_view not in {
        "this_week", "last_week", "this_month", "last_month",
        "last_90_days", "custom", "all",
        "recent", "year_to_date", "last_12_months",
    }:
        raise HTTPException(400, "Invalid client work view")
    try:
        target_zone = ZoneInfo((getattr(target, "timezone_name", None) or "UTC").strip())
    except (ZoneInfoNotFoundError, ValueError):
        target_zone = timezone.utc
    today_local = datetime.now(target_zone).date()

    activity_from = None
    activity_to = None
    if normalized_view != "all":
        if normalized_view in {"recent", "last_90_days"}:
            activity_from, activity_to = today_local - timedelta(days=89), today_local
        elif normalized_view == "year_to_date":
            activity_from, activity_to = date(today_local.year, 1, 1), today_local
        elif normalized_view == "last_12_months":
            activity_from, activity_to = today_local - timedelta(days=364), today_local
        elif normalized_view == "this_week":
            activity_from = today_local - timedelta(days=today_local.weekday())
            activity_to = today_local
        elif normalized_view == "last_week":
            this_week_start = today_local - timedelta(days=today_local.weekday())
            activity_to = this_week_start - timedelta(days=1)
            activity_from = activity_to - timedelta(days=6)
        elif normalized_view == "this_month":
            activity_from = date(today_local.year, today_local.month, 1)
            activity_to = today_local
        elif normalized_view == "last_month":
            this_month_start = date(today_local.year, today_local.month, 1)
            activity_to = this_month_start - timedelta(days=1)
            activity_from = date(activity_to.year, activity_to.month, 1)
        elif normalized_view == "custom":
            if not date_from or not date_to:
                raise HTTPException(400, "Custom client work view requires date_from and date_to")
            try:
                activity_from = date.fromisoformat(date_from)
                activity_to = date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(400, "Invalid custom client work date")
            if activity_from > activity_to:
                raise HTTPException(400, "Client work date_from must be on or before date_to")

    billing_by_type = {
        row.name: bool(row.is_billable)
        for row in db.query(models.TaskTypeOption).all()
    }
    tasks = db.query(models.TaskInstance).filter(
        models.TaskInstance.status == "submitted",
        models.TaskInstance.submitted_by_id == target_id,
    ).all()
    if current_member.role == "admin" and current_member.pod_id:
        tasks = [t for t in tasks if _submitted_task_visible_to_admin_pod(t, current_member)]

    tasks = [
        t for t in tasks
        if _insights_is_billable(t, billing_by_type)
        and t.client_id
        and (t.client_name or "").strip()
        and (t.client_name or "").strip().lower() not in {"internal support", "internal admin"}
    ]

    clients = {}
    total_seconds = 0.0
    total_task_records = 0
    for task in tasks:
        seconds = _insights_task_seconds(task)
        if seconds <= 0:
            continue
        total_seconds += seconds
        total_task_records += 1
        work_dt = _insights_task_work_date(task)
        work_date = work_dt.date().isoformat() if work_dt else None
        work_type = _insights_client_work_type(task)
        pkey = _insights_client_work_period_key(task)
        plabel = _insights_client_work_period_label(task)
        client = clients.setdefault(task.client_id, {
            "client_id": task.client_id,
            "client_name": task.client_name,
            "seconds": 0.0,
            "task_records": 0,
            "engagements": {},
        })
        client["seconds"] += seconds
        client["task_records"] += 1
        engagement_key = f"{work_type}||{pkey}"
        engagement = client["engagements"].setdefault(engagement_key, {
            "key": engagement_key,
            "work_type": work_type,
            "period": plabel,
            "period_key": pkey,
            "seconds": 0.0,
            "task_records": 0,
            "first_work_date": None,
            "last_work_date": None,
            "activity_dates": set(),
            "tasks": {},
            "metrics": {},
        })
        engagement["seconds"] += seconds
        engagement["task_records"] += 1
        if work_date:
            engagement["activity_dates"].add(work_date)
            if engagement["first_work_date"] is None or work_date < engagement["first_work_date"]:
                engagement["first_work_date"] = work_date
            if engagement["last_work_date"] is None or work_date > engagement["last_work_date"]:
                engagement["last_work_date"] = work_date

        task_label = (task.name or "Task").strip() or "Task"
        task_row = engagement["tasks"].setdefault(task_label, {
            "task": task_label,
            "seconds": 0.0,
            "records": 0,
            "entries": [],
        })
        task_row["seconds"] += seconds
        task_row["records"] += 1
        task_row["entries"].append({
            "task_id": task.id,
            "work_date": work_date,
            "seconds": round(seconds, 1),
        })

        metric = _insights_client_work_metric(task)
        if metric:
            label, quantity = metric
            engagement["metrics"][label] = engagement["metrics"].get(label, 0) + quantity

    client_rows = []
    total_engagements = 0
    visible_total_seconds = 0.0
    visible_task_records = 0

    def engagement_is_visible(engagement):
        if normalized_view == "all":
            return True
        if activity_from is None or activity_to is None:
            return False
        for raw_date in engagement.get("activity_dates", set()):
            try:
                worked_on = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                continue
            if activity_from <= worked_on <= activity_to:
                return True
        return False

    for client in clients.values():
        engagements = []
        client_seconds = 0.0
        client_task_records = 0
        for engagement in client.pop("engagements").values():
            if not engagement_is_visible(engagement):
                continue
            engagement.pop("activity_dates", None)
            engagement["seconds"] = round(engagement["seconds"], 1)
            engagement["tasks"] = sorted(
                (
                    {
                        **row,
                        "seconds": round(row["seconds"], 1),
                        "entries": sorted(
                            row.get("entries", []),
                            key=lambda entry: (entry.get("work_date") or "", entry.get("task_id") or ""),
                            reverse=True,
                        ),
                    }
                    for row in engagement["tasks"].values()
                ),
                key=lambda row: (-row["seconds"], row["task"].lower()),
            )
            engagement["metrics"] = [
                {"label": label, "quantity": quantity}
                for label, quantity in sorted(engagement["metrics"].items(), key=lambda item: item[0].lower())
            ]
            engagements.append(engagement)
            client_seconds += engagement["seconds"]
            client_task_records += engagement["task_records"]
        if not engagements:
            continue
        engagements.sort(key=lambda row: (row.get("last_work_date") or "", row["work_type"].lower(), row["period"]), reverse=True)
        total_engagements += len(engagements)
        client["seconds"] = round(client_seconds, 1)
        client["task_records"] = client_task_records
        client["engagement_count"] = len(engagements)
        client["engagements"] = engagements
        client_rows.append(client)
        visible_total_seconds += client_seconds
        visible_task_records += client_task_records
    client_rows.sort(key=lambda row: (-row["seconds"], row["client_name"].lower()))


    return {
        "member_id": target.id,
        "member_name": target.name,
        "view": normalized_view,
        "view_activity_from": activity_from.isoformat() if activity_from else None,
        "view_activity_to": activity_to.isoformat() if activity_to else None,
        "billable_seconds": round(visible_total_seconds, 1),
        "client_count": len(client_rows),
        "engagement_count": total_engagements,
        "task_records": visible_task_records,
        "clients": client_rows,
    }


@app.get("/api/insights")
def get_insights(
    member_id: str = None,
    date_from: str = None,
    date_to: str = None,
    capacity_pod_id: str = None,
    current_member: models.Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    target_id = member_id or current_member.id
    if target_id not in allowed_ids:
        raise HTTPException(403, "You cannot view insights for that person")
    target = db.get(models.Member, target_id)
    if not target:
        raise HTTPException(404, "Member not found")

    today = datetime.utcnow().date()
    try:
        start_date = datetime.fromisoformat(date_from).date() if date_from else today - timedelta(days=89)
        end_date = datetime.fromisoformat(date_to).date() if date_to else today
    except ValueError:
        raise HTTPException(400, "Invalid insight date range")
    if end_date < start_date:
        raise HTTPException(400, "date_to must be on or after date_from")
    days = (end_date - start_date).days + 1

    # The selected range is the planning window. It may extend into the future
    # (for example, "This month" is the full calendar month). Actual activity
    # metrics must never manufacture future work, so cap them at today.
    actual_end_date = min(end_date, today)
    has_actual_days = actual_end_date >= start_date
    actual_days = (actual_end_date - start_date).days + 1 if has_actual_days else 0

    # Preserve the existing "previous equivalent period" comparison for actual
    # metrics by comparing the elapsed portion only, rather than a full future-
    # inclusive planning window.
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=max(actual_days, 1) - 1)

    all_tasks = db.query(models.TaskInstance).filter(
        models.TaskInstance.status == "submitted",
        models.TaskInstance.submitted_by_id == target_id,
    ).all()
    if current_member.role == "admin" and current_member.pod_id:
        # Current pod membership grants access to the person, but not retroactively to work
        # they submitted while they belonged to another pod.
        all_tasks = [t for t in all_tasks if _submitted_task_visible_to_admin_pod(t, current_member)]

    def in_range(task, start, end):
        dt = _insights_task_work_date(task)
        if not dt:
            return False
        d = dt.date()
        return start <= d <= end

    current_tasks = [t for t in all_tasks if has_actual_days and in_range(t, start_date, actual_end_date)]
    previous_tasks = [t for t in all_tasks if actual_days > 0 and in_range(t, previous_start, previous_end)]

    billing_by_type = {
        row.name: bool(row.is_billable)
        for row in db.query(models.TaskTypeOption).all()
    }

    def task_totals(tasks):
        tracked = focused = meeting = 0.0
        completed = 0
        for task in tasks:
            seconds = _insights_task_seconds(task)
            tracked += seconds
            completed += 1
            if _insights_is_support(task):
                continue
            if _insights_is_meeting(task):
                meeting += seconds
            else:
                focused += seconds
        return {"tracked": tracked, "focused": focused, "meeting": meeting, "completed": completed}

    current_totals = task_totals(current_tasks)
    previous_totals = task_totals(previous_tasks)

    # Client/billable time for the selected reporting period is independent of
    # capacity effective dates. Capacity-scoped billable time is calculated
    # separately below for utilisation.
    current_billable_total_seconds = sum(
        _insights_task_seconds(t) for t in current_tasks if _insights_is_billable(t, billing_by_type)
    )
    previous_billable_total_seconds = sum(
        _insights_task_seconds(t) for t in previous_tasks if _insights_is_billable(t, billing_by_type)
    )

    individual_calamari_maps, individual_calamari_meta = _calamari_daily_adjustments(db, [target], start_date, end_date, include_leave_breakdown=True)
    target_unavailable = individual_calamari_maps.get(target.id, {})
    previous_calamari_maps, _previous_calamari_meta = _calamari_daily_adjustments(db, [target], previous_start, previous_end)
    target_previous_unavailable = previous_calamari_maps.get(target.id, {})
    current_capacity_tasks = _insights_capacity_tasks(target, current_tasks, start_date, actual_end_date) if has_actual_days else []
    previous_capacity_tasks = _insights_capacity_tasks(target, previous_tasks, previous_start, previous_end) if actual_days > 0 else []
    current_capacity_tracked_seconds = sum(_insights_task_seconds(t) for t in current_capacity_tasks)
    previous_capacity_tracked_seconds = sum(_insights_task_seconds(t) for t in previous_capacity_tasks)
    current_billable_seconds = sum(_insights_task_seconds(t) for t in current_capacity_tasks if _insights_is_billable(t, billing_by_type))
    previous_billable_seconds = sum(_insights_task_seconds(t) for t in previous_capacity_tasks if _insights_is_billable(t, billing_by_type))

    # Planned capacity covers the full selected window, including future approved
    # leave/public holidays. Utilisation, however, uses capacity only up to today.
    capacity_seconds = _insights_capacity_seconds(target, start_date, end_date, target_unavailable)
    utilization_capacity_seconds = _insights_capacity_seconds(target, start_date, actual_end_date, target_unavailable) if has_actual_days else 0.0
    previous_capacity_seconds = _insights_capacity_seconds(target, previous_start, previous_end, target_previous_unavailable) if actual_days > 0 else 0.0
    overall_utilization = round((current_capacity_tracked_seconds / utilization_capacity_seconds) * 100, 1) if utilization_capacity_seconds > 0 else None
    client_utilization = round((current_billable_seconds / utilization_capacity_seconds) * 100, 1) if utilization_capacity_seconds > 0 else None
    capacity_trend = _insights_capacity_trend(target, current_capacity_tasks, start_date, end_date, billing_by_type, target_unavailable)
    leave_trends = _insights_leave_summary([target], individual_calamari_meta.get("_leave_by_member", {}), start_date, end_date)

    current_help = db.query(models.HelpEvent).filter(
        models.HelpEvent.member_id == target_id,
        models.HelpEvent.created_at >= datetime.combine(start_date, datetime.min.time()),
        models.HelpEvent.created_at < datetime.combine((actual_end_date if has_actual_days else start_date - timedelta(days=1)) + timedelta(days=1), datetime.min.time()),
    ).all() if has_actual_days else []
    previous_help = db.query(models.HelpEvent).filter(
        models.HelpEvent.member_id == target_id,
        models.HelpEvent.created_at >= datetime.combine(previous_start, datetime.min.time()),
        models.HelpEvent.created_at < datetime.combine(previous_end + timedelta(days=1), datetime.min.time()),
    ).all()

    def help_totals(events):
        helped = sum(float(e.seconds or 0) for e in events if e.direction == "helped")
        received = sum(float(e.seconds or 0) for e in events if e.direction == "received")
        return helped, received

    helped_seconds, received_seconds = help_totals(current_help)
    prev_helped_seconds, prev_received_seconds = help_totals(previous_help)

    # Weekly support trend, aligned to Mondays, using the exact recorded support events.
    # Empty weeks are kept as zero so reductions in support remain visible.
    week_map = {}
    first_week = start_date - timedelta(days=start_date.weekday())
    last_week = end_date - timedelta(days=end_date.weekday())
    cursor = first_week
    while cursor <= last_week:
        key = cursor.isoformat()
        week_map[key] = {"week_start": key, "helped_seconds": 0.0, "received_seconds": 0.0}
        cursor += timedelta(days=7)
    for event in current_help:
        day = event.created_at.date()
        week_start = day - timedelta(days=day.weekday())
        key = week_start.isoformat()
        row = week_map.setdefault(key, {"week_start": key, "helped_seconds": 0.0, "received_seconds": 0.0})
        if event.direction == "helped":
            row["helped_seconds"] += float(event.seconds or 0)
        else:
            row["received_seconds"] += float(event.seconds or 0)
    support_trend = [week_map[k] for k in sorted(week_map)]

    work_mix_map = {}
    task_type_mix_map = {}
    top_client_map = {}
    for task in current_tasks:
        seconds = _insights_task_seconds(task)
        if _insights_is_support(task):
            category_label = "Colleague support"
            task_type_label = "Colleague support"
        elif _insights_is_meeting(task):
            category_label = "Meetings"
            task_type_label = "Meetings"
        else:
            # Prefer the optional template-level broad category. If none was selected,
            # fall back to the task type so custom tasks and uncategorised templates
            # still land in a meaningful Insights bucket without forcing extra input.
            task_type_label = (task.task_type or task.name or "Other").strip() or "Other"
            category_label = (getattr(task, "source_template_category", None) or task_type_label).strip() or "Other"
        work_mix_map[category_label] = work_mix_map.get(category_label, 0.0) + seconds
        task_type_mix_map[task_type_label] = task_type_mix_map.get(task_type_label, 0.0) + seconds
        if task.client_name and task.client_name != "Internal Support":
            top_client_map[task.client_name] = top_client_map.get(task.client_name, 0.0) + seconds

    work_mix = [
        {"label": label, "seconds": round(seconds, 1)}
        for label, seconds in sorted(work_mix_map.items(), key=lambda item: item[1], reverse=True)[:8]
    ]
    task_type_mix = [
        {"label": label, "seconds": round(seconds, 1)}
        for label, seconds in sorted(task_type_mix_map.items(), key=lambda item: item[1], reverse=True)[:8]
    ]
    top_clients = [
        {"label": label, "seconds": round(seconds, 1)}
        for label, seconds in sorted(top_client_map.items(), key=lambda item: item[1], reverse=True)[:6]
    ]

    # Tracked-time trend. Short ranges need day-by-day detail; medium ranges use
    # calendar weeks and long ranges use months. Pre-seed every bucket so days/weeks
    # with no submitted time remain visible instead of disappearing from the trend.
    trend_days = actual_days if has_actual_days else 0
    trend_granularity = "daily" if trend_days <= 14 else ("weekly" if trend_days <= 120 else "monthly")
    trend_map = {}

    if has_actual_days:
        cursor = start_date
        while cursor <= actual_end_date:
            if trend_granularity == "daily":
                # Insights' short-range trend represents working days only. Weekend zeroes
                # create artificial dips between Friday and Monday, so do not seed them.
                if cursor.weekday() >= 5:
                    cursor += timedelta(days=1)
                    continue
                bucket = cursor
            elif trend_granularity == "weekly":
                bucket = cursor - timedelta(days=cursor.weekday())
            else:
                bucket = cursor.replace(day=1)
            key = bucket.isoformat()
            trend_map.setdefault(key, {"seconds": 0.0, "billable_seconds": 0.0})
            cursor += timedelta(days=1)

    for task in current_tasks:
        dt = _insights_task_work_date(task)
        if not dt:
            continue
        d = dt.date()
        if trend_granularity == "daily":
            # Keep weekend activity out of the daily trend as well. Summary totals and all
            # other Insights calculations remain unchanged; this only affects the chart.
            if d.weekday() >= 5:
                continue
            bucket = d
        elif trend_granularity == "weekly":
            bucket = d - timedelta(days=d.weekday())
        else:
            bucket = d.replace(day=1)
        key = bucket.isoformat()
        row = trend_map.setdefault(key, {"seconds": 0.0, "billable_seconds": 0.0})
        seconds = _insights_task_seconds(task)
        row["seconds"] += seconds
        if _insights_is_billable(task, billing_by_type):
            row["billable_seconds"] += seconds
    tracked_trend = [
        {
            "period_start": key,
            "seconds": round(trend_map[key]["seconds"], 1),
            "billable_seconds": round(trend_map[key]["billable_seconds"], 1),
        }
        for key in sorted(trend_map)
    ]

    tracked_work_dates = {
        _insights_task_work_date(task).date()
        for task in current_tasks
        if _insights_task_work_date(task) is not None and _insights_task_seconds(task) > 0
    }
    working_days = 0
    cursor_day = start_date
    while has_actual_days and cursor_day <= actual_end_date:
        if cursor_day.weekday() < 5:
            working_days += 1
        cursor_day += timedelta(days=1)
    tracked_working_days = sum(1 for d in tracked_work_dates if d.weekday() < 5)
    tracking_consistency = round((tracked_working_days / working_days) * 100, 1) if working_days else 0.0

    average_task_seconds = (
        current_totals["tracked"] / current_totals["completed"] if current_totals["completed"] else 0.0
    )

    delegation_candidates = []
    # Delegation is deliberately an admin-owned review, never a system judgement. We only
    # surface evidence when this admin is viewing their own insights and a staff member has
    # already completed the same template/task signature at least once.
    if current_member.role in ("admin", "super_admin") and target_id == current_member.id:
        staff_query = db.query(models.Member).filter(models.Member.role == "member")
        if current_member.role == "admin" and current_member.pod_id:
            staff_query = staff_query.filter(models.Member.pod_id == current_member.pod_id)
        staff_members = staff_query.all()
        staff_ids = {m.id for m in staff_members}
        staff_names = {m.id: m.name for m in staff_members}
        staff_tasks = []
        if staff_ids:
            staff_tasks = db.query(models.TaskInstance).filter(
                models.TaskInstance.status == "submitted",
                models.TaskInstance.submitted_by_id.in_(staff_ids),
            ).all()
        if current_member.role == "admin" and current_member.pod_id:
            staff_tasks = [t for t in staff_tasks if _submitted_task_visible_to_admin_pod(t, current_member)]

        def task_key(task):
            return "|".join([
                (task.source_template_name or "").strip().lower(),
                (task.name or "").strip().lower(),
                (task.task_type or "").strip().lower(),
            ])

        delegation_exclusions = {value.casefold() for value in _delegation_exclusions(db)}
        support_is_excluded = bool({"support given", "support received"} & delegation_exclusions)

        def eligible_for_delegation(task):
            # Delegation insight is intended for repeatable staff work. Meetings remain
            # inherently non-delegation suggestions; all other category/task exclusions are
            # tenant-configurable by Super Admin in Settings.
            if not bool((task.source_template_name or "").strip()) or _insights_is_meeting(task):
                return False
            if support_is_excluded and _insights_is_support(task):
                return False
            task_labels = {
                (task.task_type or "").strip().casefold(),
                (task.name or "").strip().casefold(),
                (task.source_template_name or "").strip().casefold(),
            }
            task_labels.discard("")
            return not bool(task_labels & delegation_exclusions)

        evidence = {}
        for task in staff_tasks:
            if not eligible_for_delegation(task):
                continue
            key = task_key(task)
            row = evidence.setdefault(key, set())
            if task.submitted_by_id:
                row.add(task.submitted_by_id)

        own_by_key = {}
        for task in current_tasks:
            if not eligible_for_delegation(task):
                continue
            evidence_key = task_key(task)
            if evidence_key not in evidence:
                continue
            # Keep the staff-evidence match broad across clients, but split the admin's
            # displayed opportunities by client so the insight is actionable.
            client_key = (task.client_id or "").strip().lower() or (task.client_name or "").strip().lower()
            display_key = f"{evidence_key}|client:{client_key}"
            row = own_by_key.setdefault(display_key, {
                "task_key": display_key,
                "client_name": task.client_name or "",
                "template_name": task.source_template_name or "",
                "task": task.name,
                "task_type": task.task_type or "",
                "occurrences": 0,
                "seconds": 0.0,
                "staff_ids": set(),
            })
            row["occurrences"] += 1
            row["seconds"] += _insights_task_seconds(task)
            row["staff_ids"].update(evidence[evidence_key])

        for row in own_by_key.values():
            delegation_candidates.append({
                "task_key": row["task_key"],
                "client_name": row["client_name"],
                "template_name": row["template_name"],
                "task": row["task"],
                "task_type": row["task_type"],
                "occurrences": row["occurrences"],
                "seconds": round(row["seconds"], 1),
                "staff_names": sorted(staff_names[mid] for mid in row["staff_ids"] if mid in staff_names),
            })
        delegation_candidates.sort(key=lambda row: row["seconds"], reverse=True)
        delegation_candidates = delegation_candidates[:12]

    team_capacity = None
    team_leave_trends = None
    if current_member.role in ("admin", "super_admin"):
        team_query = db.query(models.Member).filter(models.Member.id.in_(allowed_ids))
        selected_capacity_pod = None
        if capacity_pod_id:
            if current_member.role != "super_admin":
                raise HTTPException(403, "Only super admins can view capacity by pod")
            selected_capacity_pod = db.get(models.Pod, capacity_pod_id)
            if not selected_capacity_pod:
                raise HTTPException(404, "Pod not found")
            team_query = team_query.filter(models.Member.pod_id == capacity_pod_id)
        if current_member.role != "super_admin" and not _has_permission(current_member, PERMISSION_MANAGE_SUPER_ADMINS):
            # Regular admins only see Super Admins here when that visibility has been
            # explicitly delegated. _insights_allowed_member_ids already applies the
            # ordinary pod restriction and adds Super Admins only for that permission.
            team_query = team_query.filter(models.Member.role != "super_admin")
        team_members = team_query.order_by(models.Member.name).all()
        if team_members:
            team_ids = [m.id for m in team_members]
            team_tasks_all = db.query(models.TaskInstance).filter(
                models.TaskInstance.status == "submitted",
                models.TaskInstance.submitted_by_id.in_(team_ids),
            ).all()
            if selected_capacity_pod is not None:
                team_tasks_all = [t for t in team_tasks_all if getattr(t, "submitted_pod_id", None) == selected_capacity_pod.id]
            elif current_member.role == "admin" and current_member.pod_id:
                team_tasks_all = [t for t in team_tasks_all if _submitted_task_visible_to_admin_pod(t, current_member)]
            member_rows = []
            aggregate_week = {}
            total_capacity = total_utilization_capacity = total_tracked = total_billable = 0.0
            team_calamari_maps, team_calamari_meta = _calamari_daily_adjustments(db, team_members, start_date, end_date, include_leave_breakdown=True)
            for member in team_members:
                member_tasks = [t for t in team_tasks_all if t.submitted_by_id == member.id and has_actual_days and in_range(t, start_date, actual_end_date)]
                capacity_tasks = _insights_capacity_tasks(member, member_tasks, start_date, actual_end_date) if has_actual_days else []
                tracked = sum(_insights_task_seconds(t) for t in capacity_tasks)
                billable = sum(_insights_task_seconds(t) for t in capacity_tasks if _insights_is_billable(t, billing_by_type))
                member_unavailable = team_calamari_maps.get(member.id, {})
                capacity = _insights_capacity_seconds(member, start_date, end_date, member_unavailable)
                utilization_capacity = _insights_capacity_seconds(member, start_date, actual_end_date, member_unavailable) if has_actual_days else 0.0
                total_capacity += capacity
                total_utilization_capacity += utilization_capacity
                total_tracked += tracked
                total_billable += billable
                member_rows.append({
                    "member_id": member.id,
                    "name": member.name,
                    "capacity_seconds": round(capacity, 1),
                    "utilization_capacity_seconds": round(utilization_capacity, 1),
                    "tracked_seconds": round(tracked, 1),
                    "billable_seconds": round(billable, 1),
                    "overall_utilization": round((tracked / utilization_capacity) * 100, 1) if utilization_capacity > 0 else None,
                    "client_utilization": round((billable / utilization_capacity) * 100, 1) if utilization_capacity > 0 else None,
                    "available_seconds": round(max(capacity - tracked, 0.0), 1),
                    "calamari_adjustment_seconds": round(sum(member_unavailable.values()), 1),
                })
                for row in _insights_capacity_trend(member, capacity_tasks, start_date, end_date, billing_by_type, member_unavailable):
                    agg = aggregate_week.setdefault(row["period_start"], {"period_start": row["period_start"], "capacity_seconds": 0.0, "tracked_seconds": 0.0, "billable_seconds": 0.0})
                    agg["capacity_seconds"] += row["capacity_seconds"]
                    agg["tracked_seconds"] += row["tracked_seconds"]
                    agg["billable_seconds"] += row["billable_seconds"]
            team_leave_trends = _insights_leave_summary(
                team_members,
                team_calamari_meta.get("_leave_by_member", {}),
                start_date,
                end_date,
                selected_capacity_pod.name if selected_capacity_pod else None,
            )
            team_capacity = {
                "capacity_seconds": round(total_capacity, 1),
                "utilization_capacity_seconds": round(total_utilization_capacity, 1),
                "tracked_seconds": round(total_tracked, 1),
                "billable_seconds": round(total_billable, 1),
                "overall_utilization": round((total_tracked / total_utilization_capacity) * 100, 1) if total_utilization_capacity > 0 else None,
                "client_utilization": round((total_billable / total_utilization_capacity) * 100, 1) if total_utilization_capacity > 0 else None,
                "available_seconds": round(max(total_capacity - total_tracked, 0.0), 1),
                "trend_granularity": "daily" if (end_date - start_date).days <= 6 else "weekly",
                "trend": [
                    {k: (round(v, 1) if k.endswith("_seconds") else v) for k, v in row.items()}
                    for _, row in sorted(aggregate_week.items())
                ],
                "members": member_rows,
                "pod_id": selected_capacity_pod.id if selected_capacity_pod else None,
                "pod_name": selected_capacity_pod.name if selected_capacity_pod else None,
                "calamari": _calamari_public_meta(team_calamari_meta),
            }

    return {
        "member": {"id": target.id, "name": target.name},
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "actual_date_to": actual_end_date.isoformat() if has_actual_days else None,
        "summary": {
            "tracked_seconds": round(current_totals["tracked"], 1),
            "focused_seconds": round(current_totals["focused"], 1),
            "billable_seconds": round(current_billable_total_seconds, 1),
            "meeting_seconds": round(current_totals["meeting"], 1),
            "support_given_seconds": round(helped_seconds, 1),
            "support_received_seconds": round(received_seconds, 1),
            "completed_tasks": current_totals["completed"],
            "average_task_seconds": round(average_task_seconds, 1),
            "changes": {
                "tracked": _insights_change(current_totals["tracked"], previous_totals["tracked"]),
                "focused": _insights_change(current_totals["focused"], previous_totals["focused"]),
                "billable": _insights_change(current_billable_total_seconds, previous_billable_total_seconds),
                "meeting": _insights_change(current_totals["meeting"], previous_totals["meeting"]),
                "support_given": _insights_change(helped_seconds, prev_helped_seconds),
                "support_received": _insights_change(received_seconds, prev_received_seconds),
            },
            "previous": {
                "tracked_seconds": round(previous_totals["tracked"], 1),
                "focused_seconds": round(previous_totals["focused"], 1),
                "billable_seconds": round(previous_billable_total_seconds, 1),
                "meeting_seconds": round(previous_totals["meeting"], 1),
                "support_given_seconds": round(prev_helped_seconds, 1),
                "support_received_seconds": round(prev_received_seconds, 1),
            },
        },
        "capacity": ({
            "weekly_capacity_hours": round(float(getattr(target, "weekly_capacity_hours", 40.0) or 0.0), 2),
            "capacity_effective_from": target.capacity_effective_from.isoformat() if getattr(target, "capacity_effective_from", None) else None,
            "capacity_seconds": round(capacity_seconds, 1),
            "utilization_capacity_seconds": round(utilization_capacity_seconds, 1),
            "tracked_seconds": round(current_capacity_tracked_seconds, 1),
            "billable_seconds": round(current_billable_seconds, 1),
            "available_seconds": round(max(capacity_seconds - current_capacity_tracked_seconds, 0.0), 1),
            "overall_utilization": overall_utilization,
            "client_utilization": client_utilization,
            "previous_capacity_seconds": round(previous_capacity_seconds, 1),
            "previous_billable_seconds": round(previous_billable_seconds, 1),
            "trend_granularity": "daily" if (end_date - start_date).days <= 6 else "weekly",
            "trend": capacity_trend,
            "calamari_adjustment_seconds": round(sum(target_unavailable.values()), 1),
            "calamari": _calamari_public_meta(individual_calamari_meta),
        } if _has_permission(current_member, PERMISSION_INSIGHTS_LEAVE_CAPACITY) else None),
        "team_capacity": (team_capacity if (current_member.role == "super_admin" or (current_member.role == "admin" and _has_permission(current_member, PERMISSION_INSIGHTS_LEAVE_CAPACITY))) else None),
        "leave_trends": (leave_trends if _has_permission(current_member, PERMISSION_INSIGHTS_LEAVE_CAPACITY) else None),
        "team_leave_trends": (team_leave_trends if (current_member.role == "super_admin" or (current_member.role == "admin" and _has_permission(current_member, PERMISSION_INSIGHTS_LEAVE_CAPACITY))) else None),
        "support_trend": support_trend,
        "tracked_trend": tracked_trend,
        "work_mix": work_mix,
        "task_type_mix": task_type_mix,
        "top_clients": top_clients,
        "distribution_totals": {
            "work_mix_seconds": round(sum(work_mix_map.values()), 1),
            "task_type_seconds": round(sum(task_type_mix_map.values()), 1),
            "client_seconds": round(sum(top_client_map.values()), 1),
        },
        "tracking_consistency": tracking_consistency,
        "tracked_working_days": tracked_working_days,
        "working_days": working_days,
        "delegation_candidates": delegation_candidates,
    }


# ---------------------------------------------------------------
# Clients
# ---------------------------------------------------------------

@app.get("/api/clients", response_model=list[schemas.ClientOut])
def list_clients(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    return db.query(models.Client).filter(models.Client.id != _unassigned_client_id(current_member.tenant_id)).all()


def normalize_client_code(code: str) -> str:
    normalized = (code or "").strip().upper()
    if not normalized:
        raise HTTPException(400, "Client code is required")
    return normalized


def check_client_code_available(db, tenant_id, code, exclude_client_id=None):
    # Serialize code claims per tenant in production so two simultaneous requests cannot
    # both pass the availability check before either commits. The tenant id must be passed
    # explicitly; this helper is also used outside request-local variable scope.
    if engine.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:code))"), {"code": f"{tenant_id}:{code.lower()}"})
    query = db.query(models.Client).filter(func.lower(models.Client.code) == code.lower())
    if exclude_client_id:
        query = query.filter(models.Client.id != exclude_client_id)
    existing = query.first()
    if existing:
        raise HTTPException(400, f'The code "{code}" is already used by {existing.name}')


@app.post("/api/clients", response_model=schemas.ClientOut, status_code=201)
def create_client(payload: schemas.ClientCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Enter a client name")
    code = normalize_client_code(payload.code)
    check_client_code_available(db, current_member.tenant_id, code)
    client = models.Client(name=name, code=code)
    db.add(client)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, f'The code "{code}" is already in use')
    db.refresh(client)
    return client


@app.post("/api/clients/import", response_model=schemas.ClientImportResult)
def import_clients(payload: schemas.ClientImportRequest, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    rows = payload.rows or []
    if not rows:
        raise HTTPException(400, "The CSV does not contain any client rows")
    if len(rows) > 1000:
        raise HTTPException(400, "A maximum of 1,000 clients can be imported at once")

    prepared = []
    seen_codes = {}
    for index, row in enumerate(rows, start=2):
        name = (row.name or "").strip()
        if not name:
            raise HTTPException(400, f"Row {index}: client name is required")
        try:
            code = normalize_client_code(row.code)
        except HTTPException:
            raise HTTPException(400, f"Row {index}: client code is required")
        key = code.lower()
        if key in seen_codes:
            raise HTTPException(400, f'Rows {seen_codes[key]} and {index}: client code "{code}" is duplicated in the CSV')
        seen_codes[key] = index

        accounts = []
        seen_accounts = set()
        for raw_name in row.bank_accounts or []:
            account_name = (raw_name or "").strip()
            if not account_name:
                continue
            account_key = account_name.casefold()
            if account_key in seen_accounts:
                continue
            seen_accounts.add(account_key)
            accounts.append(account_name)
        if len(accounts) > 50:
            raise HTTPException(400, f"Row {index}: too many bank accounts")
        prepared.append((index, name, code, accounts))

    # Lock every incoming code in a stable order on PostgreSQL. This prevents two concurrent
    # bulk imports (or a bulk import and a normal create) from claiming the same code.
    if engine.dialect.name == "postgresql":
        for code in sorted({code.lower() for _, _, code, _ in prepared}):
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:code))"), {"code": f"{current_member.tenant_id}:{code}"})

    existing = db.query(models.Client).filter(func.lower(models.Client.code).in_(list(seen_codes.keys()))).all()
    if existing:
        conflict = existing[0]
        raise HTTPException(400, f'The code "{conflict.code}" is already used by {conflict.name}')

    imported_accounts = 0
    try:
        for _, name, code, accounts in prepared:
            client = models.Client(name=name, code=code)
            db.add(client)
            db.flush()
            for account_name in accounts:
                db.add(models.BankAccount(client_id=client.id, name=account_name))
                imported_accounts += 1
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "One or more client codes are already in use")

    return schemas.ClientImportResult(imported_clients=len(prepared), imported_bank_accounts=imported_accounts)


@app.patch("/api/clients/{client_id}", response_model=schemas.ClientOut)
def update_client(client_id: str, payload: schemas.ClientCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    client = db.get(models.Client, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    _require_expected_version(client, payload.expected_version)
    new_name = payload.name.strip()
    if not new_name:
        raise HTTPException(400, "Enter a client name")
    code = normalize_client_code(payload.code)
    check_client_code_available(db, current_member.tenant_id, code, exclude_client_id=client_id)
    client.name = new_name
    client.code = code
    # Tasks store their own copy of the client name for historical display, keep every
    # existing task in sync too, so old and new entries never show two different names for
    # what is now the same client.
    db.query(models.TaskInstance).filter(models.TaskInstance.client_id == client_id).update({"client_name": new_name})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, f'The code "{code}" is already in use')
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
        used_by_template = db.query(models.TemplateTask).filter(models.TemplateTask.role == role.name).count()
        used_by_active_task = db.query(models.TaskInstance).filter(
            models.TaskInstance.role == role.name, models.TaskInstance.status != "submitted"
        ).count()
        if used_by_template or used_by_active_task:
            raise HTTPException(400, "This role is used by a standard template or active task. Update that work first")
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
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_PODS, "You do not have permission to create pods")
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
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_PODS, "You do not have permission to delete pods")
    pod = db.get(models.Pod, pod_id)
    if pod:
        db.query(models.Member).filter(models.Member.pod_id == pod_id).update({"pod_id": None})
        db.delete(pod)
        db.commit()
    return None


@app.patch("/api/members/{member_id}/pod", response_model=schemas.MemberOut)
def update_member_pod(member_id: str, payload: schemas.MemberPodUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_PODS, "You do not have permission to change pod assignments")
    member = db.get(models.Member, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    _require_expected_version(member, payload.expected_version)
    if payload.pod_id:
        if not db.get(models.Pod, payload.pod_id):
            raise HTTPException(404, "Pod not found")
    member.pod_id = payload.pod_id
    db.commit()
    db.refresh(member)
    return member


@app.get("/api/task-types", response_model=list[schemas.TaskTypeOut])
def list_task_types(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task_types = db.query(models.TaskTypeOption).all()
    return sorted(
        task_types,
        key=lambda item: (0 if _is_builtin_task_type_name(item.name) else 1, (item.name or "").lower()),
    )


@app.post("/api/task-types", response_model=schemas.TaskTypeOut, status_code=201)
def create_task_type(payload: schemas.TaskTypeCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    name = payload.name.strip()
    if _is_builtin_task_type_name(name):
        raise HTTPException(400, f"{name} is a built-in task type")
    if db.query(models.TaskTypeOption).filter(models.TaskTypeOption.name == name).first():
        raise HTTPException(400, "That task type already exists")
    task_type = models.TaskTypeOption(name=name, is_billable=bool(payload.is_billable))
    db.add(task_type)
    db.commit()
    db.refresh(task_type)
    return task_type


@app.patch("/api/task-types/{task_type_id}/billing", response_model=schemas.TaskTypeOut)
def update_task_type_billing(task_type_id: str, payload: schemas.TaskTypeBillingUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task_type = db.get(models.TaskTypeOption, task_type_id)
    if not task_type:
        raise HTTPException(404, "Task type not found")
    if _is_builtin_task_type_name(task_type.name):
        raise HTTPException(400, f"{task_type.name} is built in and cannot be changed")
    _require_expected_version(task_type, payload.expected_version)
    task_type.is_billable = bool(payload.is_billable)
    db.commit()
    db.refresh(task_type)
    return task_type


@app.delete("/api/task-types/{task_type_id}", status_code=204)
def delete_task_type(task_type_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task_type = db.get(models.TaskTypeOption, task_type_id)
    if task_type:
        if _is_builtin_task_type_name(task_type.name):
            raise HTTPException(400, f"{task_type.name} is built in and cannot be deleted")
        used_by_template = db.query(models.TemplateTask).filter(models.TemplateTask.task_type == task_type.name).count()
        used_by_active_task = db.query(models.TaskInstance).filter(
            models.TaskInstance.task_type == task_type.name, models.TaskInstance.status != "submitted"
        ).count()
        if used_by_template or used_by_active_task:
            raise HTTPException(400, "This task type is used by a standard template or active task. Update that work first")
        db.delete(task_type)
        db.commit()
    return None


@app.get("/api/learning/categories", response_model=list[schemas.LearningCategoryOut])
def list_learning_categories(include_archived: bool = False, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    query = db.query(models.LearningCategory)
    if include_archived:
        _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_LEARNING_CATEGORIES, "You do not have permission to manage archived L&D categories")
    else:
        query = query.filter(models.LearningCategory.is_active.is_(True))
    return query.order_by(func.lower(models.LearningCategory.name)).all()


@app.post("/api/learning/categories", response_model=schemas.LearningCategoryOut, status_code=201)
def create_learning_category(payload: schemas.LearningCategoryCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_LEARNING_CATEGORIES, "You do not have permission to manage L&D categories")
    name = payload.name.strip()
    if db.query(models.LearningCategory).filter(func.lower(models.LearningCategory.name) == name.lower()).first():
        raise HTTPException(400, "That L&D category already exists")
    category = models.LearningCategory(name=name, is_active=True)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@app.patch("/api/learning/categories/{category_id}", response_model=schemas.LearningCategoryOut)
def update_learning_category(category_id: str, payload: schemas.LearningCategoryUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_LEARNING_CATEGORIES, "You do not have permission to manage L&D categories")
    category = db.get(models.LearningCategory, category_id)
    if not category:
        raise HTTPException(404, "L&D category not found")
    _require_expected_version(category, payload.expected_version)
    if payload.name is not None:
        name = payload.name.strip()
        duplicate = db.query(models.LearningCategory).filter(
            models.LearningCategory.id != category.id,
            func.lower(models.LearningCategory.name) == name.lower(),
        ).first()
        if duplicate:
            raise HTTPException(400, "That L&D category already exists")
        # LearningRecord.category is a historical snapshot string. Renaming the configured
        # category deliberately affects future submissions only and never rewrites old records.
        category.name = name
    if payload.is_active is not None:
        category.is_active = bool(payload.is_active)
    db.commit()
    db.refresh(category)
    return category


@app.delete("/api/learning/categories/{category_id}", status_code=204)
def delete_learning_category(category_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_LEARNING_CATEGORIES, "You do not have permission to manage L&D categories")
    category = db.get(models.LearningCategory, category_id)
    if not category:
        return None
    if db.query(models.LearningRecord).filter(func.lower(models.LearningRecord.category) == category.name.lower()).count():
        raise HTTPException(400, "This category has historical L&D records. Archive it instead of deleting it.")
    db.delete(category)
    db.commit()
    return None


def _learning_reference_dicts(values):
    cleaned = []
    for ref in values or []:
        title = (getattr(ref, "title", "") or "").strip()
        url = (getattr(ref, "url", "") or "").strip()
        if not url:
            continue
        if not (url.startswith("https://") or url.startswith("http://")):
            raise HTTPException(400, "Reference links must start with http:// or https://")
        cleaned.append({"title": title, "url": url})
    return cleaned


@app.get("/api/learning/library", response_model=list[schemas.LearningLibraryPersonOut])
def learning_library(keyword: str = "", category: str = "", letter: str = "", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    keyword = (keyword or "").strip()
    category = (category or "").strip()
    letter = (letter or "").strip().upper()[:1]
    if not keyword and not category and not letter:
        return []

    query = db.query(models.LearningRecord)
    if category:
        query = query.filter(func.lower(models.LearningRecord.category) == category.lower())
    if letter:
        query = query.filter(func.upper(func.substr(models.LearningRecord.topic, 1, 1)) == letter)
    records = query.order_by(models.LearningRecord.learned_at.desc()).all()
    names = {m.id: m.name for m in db.query(models.Member).all()}
    needle_text = keyword.lower()
    groups = {}
    for record in records:
        topic_text = (record.topic or "").lower()
        learned_text = (record.what_i_learned or "").lower()
        relevance = 0
        if needle_text:
            if needle_text not in topic_text and needle_text not in learned_text:
                continue
            if topic_text == needle_text:
                relevance += 5
            elif needle_text in topic_text:
                relevance += 3
            if needle_text in learned_text:
                relevance += 1
        member_name = record.member_name or names.get(record.member_id, "Unknown")
        group_key = record.member_id or f"former:{member_name.lower()}"
        group = groups.setdefault(group_key, {
            "member_id": record.member_id or group_key, "member_name": member_name, "records": [],
            "max_relevance": 0, "latest_at": record.learned_at,
        })
        group["max_relevance"] = max(group["max_relevance"], relevance)
        if record.learned_at > group["latest_at"]:
            group["latest_at"] = record.learned_at
        group["records"].append(schemas.LearningLibraryRecordOut(
            topic=record.topic, category=record.category, what_i_learned=record.what_i_learned,
            member_name=member_name, learned_at=record.learned_at,
        ))

    ordered = sorted(groups.values(), key=lambda g: (-g["max_relevance"], -len(g["records"]), -g["latest_at"].timestamp(), g["member_name"].lower()))
    return [schemas.LearningLibraryPersonOut(
        member_id=g["member_id"], member_name=g["member_name"], relevant_count=len(g["records"]),
        latest_at=g["latest_at"], records=g["records"],
    ) for g in ordered]


@app.get("/api/learning/report", response_model=list[schemas.LearningManagementRecordOut])
def learning_management_report(date_from: str = None, date_to: str = None, person_id: str = "", category: str = "", keyword: str = "", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    query = db.query(models.LearningRecord).filter(models.LearningRecord.member_id.in_(allowed_ids))
    if person_id:
        if person_id not in allowed_ids:
            raise HTTPException(403, "That person is outside your management scope")
        query = query.filter(models.LearningRecord.member_id == person_id)
    if category:
        query = query.filter(func.lower(models.LearningRecord.category) == category.lower())
    try:
        if date_from:
            query = query.filter(models.LearningRecord.learned_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        if date_to:
            query = query.filter(models.LearningRecord.learned_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if keyword.strip():
        k = f"%{keyword.strip().lower()}%"
        query = query.filter(or_(func.lower(models.LearningRecord.topic).like(k), func.lower(models.LearningRecord.what_i_learned).like(k)))
    records = query.order_by(models.LearningRecord.learned_at.desc()).all()
    names = {m.id: m.name for m in db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).all()}
    return [schemas.LearningManagementRecordOut(
        id=r.id, task_id=r.task_id, member_id=r.member_id, member_name=r.member_name or names.get(r.member_id, "Unknown"),
        learned_at=r.learned_at, duration_seconds=r.duration_seconds, category=r.category, topic=r.topic,
        what_i_learned=r.what_i_learned, tdm_references=r.tdm_references or [], article_references=r.article_references or [],
    ) for r in records]


@app.patch("/api/learning/report/{record_id}/category", response_model=schemas.LearningManagementRecordOut)
def update_learning_management_category(record_id: str, payload: schemas.LearningManagementCategoryUpdate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    record = db.get(models.LearningRecord, record_id)
    if not record:
        raise HTTPException(404, "L&D record not found")
    if not record.member_id or record.member_id not in allowed_ids:
        raise HTTPException(403, "That L&D record is outside your management scope")

    requested_category = payload.category.strip()
    configured = db.query(models.LearningCategory).filter(
        models.LearningCategory.is_active.is_(True),
        func.lower(models.LearningCategory.name) == requested_category.lower(),
    ).first()
    if not configured:
        raise HTTPException(400, "Choose an active L&D category")

    previous_category = record.category
    record.category = configured.name
    if previous_category != record.category:
        db.add(models.AuditEvent(
            tenant_id=current_member.tenant_id,
            actor_member_id=current_member.id,
            action="learning_record_category_updated",
            entity_type="LearningRecord",
            entity_id=record.id,
            changes={
                "category": {"from": previous_category, "to": record.category},
                "task_id": record.task_id,
                "member_id": record.member_id,
            },
        ))
    db.commit()
    db.refresh(record)
    return schemas.LearningManagementRecordOut(
        id=record.id, task_id=record.task_id, member_id=record.member_id, member_name=record.member_name or "Unknown",
        learned_at=record.learned_at, duration_seconds=record.duration_seconds, category=record.category, topic=record.topic,
        what_i_learned=record.what_i_learned, tdm_references=record.tdm_references or [], article_references=record.article_references or [],
    )


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
        used_by_template = db.query(models.TemplateTask).filter(models.TemplateTask.tracks_number_label == metric.name).count()
        used_by_active_task = db.query(models.TaskInstance).filter(
            models.TaskInstance.tracks_number_label == metric.name, models.TaskInstance.status != "submitted"
        ).count()
        if used_by_template or used_by_active_task:
            raise HTTPException(400, "This metric is used by a standard template or active task. Update that work first")
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
    tpl = models.Template(field=payload.field.strip(), category=(payload.category or "").strip() or None, name=payload.name.strip())
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
    _require_expected_version(tpl, payload.expected_version)
    field = payload.field.strip()
    category = (payload.category or "").strip() or None
    name = payload.name.strip()
    if not field or not name:
        raise HTTPException(400, "Enter both a field and a template name")
    tpl.field = field
    tpl.category = category
    tpl.name = name
    # Existing TaskInstance source_template_* values are snapshots. Renaming or recategorising
    # a template changes future work only; historical/in-progress generated tasks retain the
    # template identity they were created with so reports do not rewrite themselves.
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


def _validate_template_task_config(payload: schemas.TemplateTaskCreate, db: Session):
    _validate_configured_role_and_task_type(db, payload.role, payload.task_type)
    period_types = _normalise_period_types(payload.period_types)
    if payload.period_required and not period_types:
        raise HTTPException(400, "Choose at least one allowed period when a period is required")
    label = (payload.tracks_number_label or "").strip()
    if label and not db.query(models.TrackedMetric).filter(models.TrackedMetric.name == label).first():
        raise HTTPException(400, "Select a valid tracked metric")
    return period_types, label


@app.post("/api/templates/{template_id}/tasks", response_model=schemas.TemplateTaskOut, status_code=201)
def add_template_task(template_id: str, payload: schemas.TemplateTaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = db.get(models.Template, template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    period_types, tracks_label = _validate_template_task_config(payload, db)
    last_position = db.query(func.max(models.TemplateTask.position)).filter(models.TemplateTask.template_id == template_id).scalar()
    task = models.TemplateTask(
        template_id=template_id,
        name=payload.name.strip(),
        role=payload.role.strip(),
        task_type=payload.task_type.strip(),
        requires_bank_account=payload.requires_bank_account,
        tracks_number_label=tracks_label,
        needs_pay_period=False,
        period_types=period_types,
        period_required=payload.period_required,
        position=(last_position + 1) if last_position is not None else 0,
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
    _require_expected_version(task, payload.expected_version)
    period_types, tracks_label = _validate_template_task_config(payload, db)
    task.name = payload.name.strip()
    task.role = payload.role.strip()
    task.task_type = payload.task_type.strip()
    task.requires_bank_account = payload.requires_bank_account
    task.tracks_number_label = tracks_label
    task.needs_pay_period = False
    task.period_types = period_types
    task.period_required = payload.period_required
    db.commit()
    db.refresh(task)
    return task


@app.put("/api/templates/{template_id}/tasks-order", response_model=schemas.TemplateOut)
def reorder_template_tasks(template_id: str, payload: schemas.TemplateTaskReorder, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    tpl = db.get(models.Template, template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    existing = db.query(models.TemplateTask).filter(models.TemplateTask.template_id == template_id).all()
    existing_ids = {t.id for t in existing}
    if len(payload.task_ids) != len(existing_ids) or set(payload.task_ids) != existing_ids:
        raise HTTPException(400, "task_ids must contain every task in this template exactly once")
    by_id = {t.id: t for t in existing}
    for position, task_id in enumerate(payload.task_ids):
        by_id[task_id].position = position
    db.commit()
    db.expire(tpl, ["tasks"])
    db.refresh(tpl)
    return tpl


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
        can_see_super_admins = _has_permission(current_member, PERMISSION_MANAGE_SUPER_ADMINS)
        super_admin_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if super_admin_ids and not can_see_super_admins:
            query = query.filter(~models.TaskInstance.owner_id.in_(super_admin_ids))
        if current_member.pod_id:
            pod_member_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
            if can_see_super_admins:
                pod_member_ids = list(dict.fromkeys(pod_member_ids + super_admin_ids))
            query = query.filter(models.TaskInstance.owner_id.in_(pod_member_ids))
    else:
        query = query.filter(models.TaskInstance.owner_id == current_member.id)
    rows = query.order_by(models.TaskInstance.created_at.desc()).all()
    if current_member.role == "admin" and current_member.pod_id:
        rows = [
            task for task in rows
            if task.status != "submitted" or getattr(task, "submitted_pod_id", None) == current_member.pod_id
        ]
    return rows


VALID_PERIOD_TYPES = {"daily", "weekly", "fortnightly", "monthly", "bi_monthly", "quarterly", "year", "custom"}


def _validate_configured_role_and_task_type(db: Session, role: str = "", task_type: str = ""):
    role = (role or "").strip()
    task_type = (task_type or "").strip()
    if role and not db.query(models.Role).filter(models.Role.name == role).first():
        raise HTTPException(400, "Select a valid role")
    if task_type and not db.query(models.TaskTypeOption).filter(models.TaskTypeOption.name == task_type).first():
        raise HTTPException(400, "Select a valid task type")


def _normalise_period_types(values):
    values = list(values or [])
    cleaned = []
    for value in values:
        value = (value or "").strip()
        if value not in VALID_PERIOD_TYPES:
            raise HTTPException(400, "Template contains an invalid period type")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def _validate_period_selection(period_type, period_year, period_number, period_start, period_end, allowed_types, required=False, bookkeeping=False):
    allowed_types = list(allowed_types or [])
    if not period_type:
        if required:
            raise HTTPException(400, "Select a valid period")
        return
    if period_type not in allowed_types:
        raise HTTPException(400, "Select a valid period")
    if period_year is not None and not 1900 <= period_year <= 2100:
        raise HTTPException(400, "Select a valid period year")
    if period_type == "daily":
        if not period_start:
            raise HTTPException(400, "Select the date this work relates to")
    elif period_type == "custom":
        if not period_start or not period_end:
            raise HTTPException(400, "Select both dates for the custom period")
        if period_end < period_start:
            raise HTTPException(400, "Period end cannot be before period start")
    elif period_type == "year":
        if not period_year:
            raise HTTPException(400, "Select the year this work relates to")
    else:
        if not period_year or period_number is None:
            raise HTTPException(400, "Select the period and year this work relates to")
        if period_type == "weekly" and bookkeeping:
            if not period_start:
                raise HTTPException(400, "Select the bookkeeping month")
            if not 1 <= period_number <= 5:
                raise HTTPException(400, "Select Week 1 to Week 5 for weekly bookkeeping")
        else:
            limits = {"weekly": 52, "fortnightly": 26, "monthly": 12, "bi_monthly": 6, "quarterly": 4}
            if period_type in limits and not 1 <= period_number <= limits[period_type]:
                raise HTTPException(400, "Select a valid period")


def _submitted_task_visible_to_admin_pod(task: models.TaskInstance, current_member: models.Member):
    if current_member.role != "admin" or not current_member.pod_id:
        return True
    return getattr(task, "submitted_pod_id", None) == current_member.pod_id


@app.post("/api/tasks", response_model=schemas.TaskOut, status_code=201)
def create_task(payload: schemas.TaskCreate, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    owner_id = payload.owner_id or current_member.id
    if owner_id != current_member.id:
        if not is_admin_or_above(current_member.role):
            raise HTTPException(403, "Only an admin can assign a task to someone else")
        _require_member_in_scope(current_member, owner_id, db)

    role = (payload.role or "").strip()
    task_type = (payload.task_type or "").strip()
    is_internal_calendar_meeting = (
        task_type == INTERNAL_MEETING_TASK_TYPE
        and bool(payload.source_calendar_event_id)
    )

    if is_internal_calendar_meeting:
        # Calendar meetings explicitly marked Internal should never require a client.
        # Reuse the existing tenant-scoped internal support client so reporting stays
        # separate from real client work and completion follows the meeting flow.
        client = get_or_create_internal_support_client(db)
        client_id = client.id
        client_name = client.name
    else:
        client_id = payload.client_id or _unassigned_client_id(current_member.tenant_id)
        if client_id == _unassigned_client_id(current_member.tenant_id):
            client_name = UNASSIGNED_CLIENT_NAME
        else:
            client = db.get(models.Client, client_id)
            if not client or client.tenant_id != current_member.tenant_id:
                raise HTTPException(404, "Client not found")
            client_name = client.name

    if is_internal_calendar_meeting:
        _validate_configured_role_and_task_type(db, role, "")
    else:
        _validate_configured_role_and_task_type(db, role, task_type)

    helped_member_id = payload.helped_member_id if task_type.lower() == BUILTIN_HELPING_TASK_TYPE.lower() else None
    if task_type.lower() == BUILTIN_HELPING_TASK_TYPE.lower():
        if not helped_member_id:
            raise HTTPException(400, "Select who was helped/trained")
        helped_member = db.query(models.Member).filter(
            models.Member.id == helped_member_id,
            models.Member.tenant_id == current_member.tenant_id,
        ).first()
        if not helped_member:
            raise HTTPException(400, "Select a valid person who was helped/trained")
        if helped_member_id == owner_id:
            raise HTTPException(400, "The person helped/trained must be someone other than the task owner")

    source_template_task_id = payload.source_template_task_id
    # Backward compatibility for an already-open browser tab from the previous deployment:
    # resolve its template-name + task-name pair to the real server-side TemplateTask id.
    # Ambiguous/missing matches are rejected rather than trusting browser-supplied rules.
    if not source_template_task_id and payload.source_template_name:
        template_matches = db.query(models.Template).filter(models.Template.name == payload.source_template_name).all()
        if len(template_matches) == 1:
            task_matches = db.query(models.TemplateTask).filter(
                models.TemplateTask.template_id == template_matches[0].id,
                models.TemplateTask.name == payload.name.strip(),
            ).all()
            if len(task_matches) == 1:
                source_template_task_id = task_matches[0].id
        if not source_template_task_id:
            raise HTTPException(400, "Selected template changed. Refresh templates and try again")
    source_template_name = None
    source_template_field = None
    source_template_category = None
    tracks_number_label = ""
    period_types = []
    period_required = False
    bank_account_id = payload.bank_account_id
    bank_account_name = ""
    task_name = payload.name.strip()

    if source_template_task_id:
        template_task = db.get(models.TemplateTask, source_template_task_id)
        if not template_task:
            raise HTTPException(400, "Selected template task no longer exists. Refresh templates and try again")
        template = db.get(models.Template, template_task.template_id)
        if not template:
            raise HTTPException(400, "Selected template no longer exists. Refresh templates and try again")
        # Template-defined business rules are authoritative on the server. The browser may
        # choose the assignee, configured role/task type and an allowed period value, but it
        # cannot weaken metrics/period requirements or spoof template metadata.
        task_name = template_task.name
        tracks_number_label = (template_task.tracks_number_label or "").strip()
        period_types = _normalise_period_types(template_task.period_types)
        period_required = bool(template_task.period_required)
        source_template_name = template.name
        source_template_field = template.field
        source_template_category = template.category
        if template_task.requires_bank_account and not bank_account_id:
            raise HTTPException(400, f'Select a bank account for "{template_task.name}"')
    else:
        if payload.source_template_name or payload.source_template_field or payload.source_template_category:
            raise HTTPException(400, "A template task reference is required for template-created work")
        # Custom/calendar tasks do not get to invent template-only metric or period rules.
        if payload.tracks_number_label or list(payload.period_types or []) or payload.period_required or payload.needs_pay_period:
            raise HTTPException(400, "Metric and period requirements must come from a standard template")

    if bank_account_id:
        account = db.get(models.BankAccount, bank_account_id)
        if not account or account.client_id != client_id:
            raise HTTPException(400, "Select a bank account belonging to this client")
        bank_account_name = account.name

    if not task_name:
        raise HTTPException(400, "Enter a task name")

    if payload.period_type:
        _validate_period_selection(
            payload.period_type, payload.period_year, payload.period_number,
            payload.period_start, payload.period_end, period_types,
            required=False,
            bookkeeping="bookkeep" in " ".join(filter(None, [task_name, task_type, source_template_name])).lower(),
        )

    task = models.TaskInstance(
        client_id=client_id,
        client_name=client_name,
        name=task_name,
        role=role,
        task_type=task_type,
        helped_member_id=helped_member_id,
        owner_id=owner_id,
        status="todo",
        segments=[],
        bank_account_id=bank_account_id,
        bank_account_name=bank_account_name,
        tracks_number_label=tracks_number_label,
        needs_pay_period=False,
        period_types=period_types,
        period_required=period_required,
        period_type=payload.period_type,
        period_year=payload.period_year,
        period_number=payload.period_number,
        period_start=payload.period_start,
        period_end=payload.period_end,
        pay_period_type=None,
        pay_period_number=None,
        source_calendar_event_id=payload.source_calendar_event_id,
        source_template_task_id=source_template_task_id,
        source_template_name=source_template_name,
        source_template_field=source_template_field,
        source_template_category=source_template_category,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task

def _task_belongs_to_local_work_date(task: models.TaskInstance, member: models.Member, work_date):
    """Return True when the task was created or actually worked on during work_date.

    This is intentionally based on the member's configured timezone. An older task may still
    qualify when it has a real timer segment on the recovery date, but an untouched task from
    yesterday/last week cannot be used as a destination for today's forgotten time.
    """
    if task.created_at and _utc_naive_to_local(task.created_at, member).date() == work_date:
        return True
    for seg in (task.segments or []):
        if not isinstance(seg, dict) or not seg.get("start"):
            continue
        try:
            seg_start = parse_utc_naive(seg.get("start"))
        except Exception:
            continue
        if _utc_naive_to_local(seg_start, member).date() == work_date:
            return True
    return False



def _resolve_recovery_window(member, seconds: float, window_end_at=None):
    """Return an authoritative same-day recovery window.

    window_end_at lets the browser freeze a forgotten-time window at the instant a genuine
    lock/away period begins, so submitting it after unlock cannot slide that recovery block
    forward across the away interval. Legacy callers may omit it and keep the existing
    "ending now" behaviour.
    """
    recorded_at = datetime.utcnow()
    if window_end_at is None:
        window_end = recorded_at
    elif isinstance(window_end_at, datetime):
        window_end = window_end_at.astimezone(timezone.utc).replace(tzinfo=None) if window_end_at.tzinfo else window_end_at
    else:
        try:
            window_end = parse_utc_naive(window_end_at)
        except Exception:
            raise HTTPException(400, "window_end_at must be a valid timestamp")

    # Do not allow a client to place a recovery window materially in the future. A few seconds
    # are tolerated for ordinary browser/server clock skew.
    if window_end > recorded_at + timedelta(seconds=10):
        raise HTTPException(400, "Forgotten-time recovery cannot end in the future")
    if window_end > recorded_at:
        window_end = recorded_at

    start = window_end - timedelta(seconds=seconds)
    work_date = _utc_naive_to_local(recorded_at, member).date()
    if _utc_naive_to_local(window_end, member).date() != work_date or _utc_naive_to_local(start, member).date() != work_date:
        raise HTTPException(400, "Forgotten time can only be recovered for the current work date")
    return recorded_at, start, window_end, work_date




def _resolve_recovery_windows(member, total_seconds: float, window_end_at=None, recovery_windows=None):
    """Resolve one or more active/no-timer recovery slices for the current work date.

    Modern clients may send multiple slices when a recovery reminder was already active, the
    screen locked, and the same recovery pot later resumed after unlock. The away interval
    between slices is intentionally excluded. Legacy clients may omit recovery_windows and
    keep the original single contiguous window behaviour.
    """
    if not recovery_windows:
        recorded_at, start, window_end, work_date = _resolve_recovery_window(member, total_seconds, window_end_at)
        return recorded_at, [(start, window_end)], work_date

    if not isinstance(recovery_windows, list) or len(recovery_windows) > 50:
        raise HTTPException(400, "recovery_windows must be a list of up to 50 windows")

    recorded_at = datetime.utcnow()
    work_date = _utc_naive_to_local(recorded_at, member).date()
    parsed = []
    for index, raw in enumerate(recovery_windows):
        if not isinstance(raw, dict):
            raise HTTPException(400, f"Recovery window {index + 1} is invalid")
        try:
            start = parse_utc_naive(raw.get("start"))
            end = parse_utc_naive(raw.get("end"))
        except Exception:
            raise HTTPException(400, f"Recovery window {index + 1} must have valid start and end timestamps")
        if end <= start:
            raise HTTPException(400, f"Recovery window {index + 1} must end after it starts")
        if end > recorded_at + timedelta(seconds=10):
            raise HTTPException(400, "Forgotten-time recovery cannot end in the future")
        if end > recorded_at:
            end = recorded_at
        if _utc_naive_to_local(start, member).date() != work_date or _utc_naive_to_local(end, member).date() != work_date:
            raise HTTPException(400, "Forgotten time can only be recovered for the current work date")
        parsed.append((start, end))

    parsed.sort(key=lambda row: row[0])
    for index in range(1, len(parsed)):
        if parsed[index][0] < parsed[index - 1][1]:
            raise HTTPException(400, "Recovery windows cannot overlap")

    supported_seconds = sum((end - start).total_seconds() for start, end in parsed)
    # The browser allocates recovery in whole seconds while the source windows retain
    # millisecond precision. A valid whole-second recovery therefore may leave less than
    # one fractional second unused, but it must never request more time than the windows
    # actually contain. This avoids a rounded-up request exhausting the final window.
    fractional_remainder = supported_seconds - float(total_seconds)
    if fractional_remainder < -1e-6 or fractional_remainder >= 1.0:
        raise HTTPException(400, "Recovery windows must add up to the forgotten time")

    return recorded_at, parsed, work_date

def _recovery_boundary_ms(value: datetime) -> datetime:
    """Compare recovery boundaries at browser timestamp precision.

    JavaScript Date/ISO timestamps preserve milliseconds, while server-created timer segment
    timestamps may contain additional microseconds. Without normalizing that extra precision,
    a client round-trip of an exact segment end such as .123456 becomes .123000 and appears to
    overlap the segment by 456 microseconds. That is not a real time overlap; it is only a
    serialization precision difference. Genuine overlaps of 1 millisecond or more remain
    blocked.
    """
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _recovery_intervals_overlap(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return (
        _recovery_boundary_ms(start) < _recovery_boundary_ms(other_end)
        and _recovery_boundary_ms(end) > _recovery_boundary_ms(other_start)
    )


def _reject_recovery_away_overlap(db: Session, member_id: str, start: datetime, end: datetime):
    """Recovery and genuine away/inactivity time are mutually exclusive."""
    overlap = db.query(models.InactivityEvent.id).filter(
        models.InactivityEvent.member_id == member_id,
        models.InactivityEvent.started_at < end,
        models.InactivityEvent.ended_at > start,
    ).first()
    if overlap:
        raise HTTPException(409, "That forgotten-time period overlaps recorded away time. Refresh and try again.")

@app.post("/api/tasks/{task_id}/recover-time", response_model=schemas.TaskOut)
def recover_task_time(task_id: str, payload: schemas.TaskRecoverTime, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    """Add a bounded, explicitly-audited block of forgotten time to an existing task.

    The recovered block is stored as a closed timer segment so it contributes to the task's
    duration without rewriting an earlier timer segment. It is tagged separately from normal
    tracking and recorded in the immutable audit ledger as forgotten-time recovery.
    """
    _lock_timer_owner(db, current_member.id)
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.status == "submitted":
        raise HTTPException(400, "Submitted tasks cannot receive recovered time")
    if task.status == "running":
        raise HTTPException(400, "Pause the running task before recovering forgotten time")

    seconds = float(payload.seconds)
    now, start, window_end, recovery_date = _resolve_recovery_window(current_member, seconds, payload.window_end_at)

    # Forgotten-time recovery is intentionally a same-work-day correction. It must not be
    # used to backfill an older day, and the destination task must belong to today's work.
    if not _task_belongs_to_local_work_date(task, current_member, recovery_date):
        raise HTTPException(400, "Forgotten time can only be added to a task from the current work date")

    # Do not allow recovered time to overlap time already recorded for this person. This keeps
    # forgotten-time recovery additive without creating double-counted timer periods.
    owned_tasks = db.query(models.TaskInstance).filter(models.TaskInstance.owner_id == current_member.id).all()
    for owned in owned_tasks:
        for seg in (owned.segments or []):
            try:
                seg_start = parse_utc_naive(seg.get("start"))
                seg_end = parse_utc_naive(seg.get("end")) if seg.get("end") else now
            except Exception:
                continue
            if _recovery_intervals_overlap(start, window_end, seg_start, seg_end):
                raise HTTPException(409, "That forgotten-time period overlaps time already tracked. Refresh and try again.")

    _reject_recovery_away_overlap(db, current_member.id, start, window_end)

    recovered = {
        "start": start.isoformat() + "Z",
        "end": window_end.isoformat() + "Z",
        "source": "forgotten_time_recovery",
        "recovered_seconds": round(seconds, 1),
    }
    _append_time_integrity_snapshot(
        db, member=current_member, task=task, entry_source="Recovery",
        manual_duration_seconds=seconds,
        current_value_seconds=elapsed_seconds(task.segments) + seconds,
        recorded_at=now, work_date=recovery_date,
        reason_note="Forgotten time recovered",
    )
    task.segments = [*(task.segments or []), recovered]
    if not task.owner_id:
        task.owner_id = current_member.id
    if task.status == "todo":
        task.status = "paused"

    db.add(models.AuditEvent(
        tenant_id=current_member.tenant_id,
        actor_member_id=current_member.id,
        action="forgotten_time_recovered",
        entity_type="TaskInstance",
        entity_id=task.id,
        changes={
            "seconds": round(seconds, 1),
            "source": "forgotten_time_recovery",
            "start": recovered["start"],
            "end": recovered["end"],
        },
    ))
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/recover-time/batch", response_model=list[schemas.TaskOut])
def recover_task_time_batch(payload: dict, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    """Split one forgotten-time window across multiple tasks atomically.

    Allocations are ordered chronologically from the beginning of the forgotten window to now.
    Every resulting segment remains explicitly tagged as forgotten-time recovery and every task
    receives its own immutable audit event.  The entire request commits once, so a validation
    error cannot leave only part of the forgotten window recovered.
    """
    raw_allocations = payload.get("allocations") if isinstance(payload, dict) else None
    try:
        total_seconds = float(payload.get("total_seconds"))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "total_seconds must be a positive number")

    if not isinstance(raw_allocations, list) or not raw_allocations:
        raise HTTPException(400, "Add at least one recovery allocation")
    if len(raw_allocations) > 20:
        raise HTTPException(400, "Too many recovery allocations")
    if total_seconds < 1 or total_seconds > 24 * 60 * 60:
        raise HTTPException(400, "Recovered time must be between 1 second and 24 hours")

    allocations = []
    seen_task_ids = set()
    allocated_total = 0.0
    for index, raw in enumerate(raw_allocations):
        if not isinstance(raw, dict):
            raise HTTPException(400, "Each recovery allocation must be an object")
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id:
            raise HTTPException(400, f"Allocation {index + 1} is missing a task")
        if task_id in seen_task_ids:
            raise HTTPException(400, "Use each task only once in a recovery split")
        seen_task_ids.add(task_id)
        try:
            seconds = float(raw.get("seconds"))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Allocation {index + 1} has an invalid duration")
        if seconds < 1:
            raise HTTPException(400, "Every recovery allocation must be at least 1 second")
        allocated_total += seconds
        allocations.append((task_id, seconds))

    if abs(allocated_total - total_seconds) > 0.5:
        raise HTTPException(400, "Recovery allocations must add up exactly to the forgotten time")

    _lock_timer_owner(db, current_member.id)
    window_end_at = payload.get("window_end_at") if isinstance(payload, dict) else None
    recovery_windows = payload.get("recovery_windows") if isinstance(payload, dict) else None
    now, resolved_windows, recovery_date = _resolve_recovery_windows(
        current_member, total_seconds, window_end_at, recovery_windows
    )

    tasks_by_id = {}
    for task_id, _seconds in allocations:
        task = _get_task_for_update(db, task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.owner_id and task.owner_id != current_member.id:
            raise HTTPException(403, "This task belongs to someone else")
        if task.status == "submitted":
            raise HTTPException(400, "Submitted tasks cannot receive recovered time")
        if task.status == "running":
            raise HTTPException(400, "Pause the running task before recovering forgotten time")
        if not _task_belongs_to_local_work_date(task, current_member, recovery_date):
            raise HTTPException(400, "Forgotten time can only be added to tasks from the current work date")
        tasks_by_id[task_id] = task

    # Validate every active/no-timer slice before changing any task. Recovery and genuine
    # away time are mutually exclusive, and existing tracked segments cannot be overwritten.
    owned_tasks = db.query(models.TaskInstance).filter(models.TaskInstance.owner_id == current_member.id).all()
    for window_start, window_end in resolved_windows:
        for owned in owned_tasks:
            for seg in (owned.segments or []):
                if not isinstance(seg, dict) or not seg.get("start"):
                    continue
                try:
                    seg_start = parse_utc_naive(seg.get("start"))
                    seg_end = parse_utc_naive(seg.get("end")) if seg.get("end") else now
                except Exception:
                    continue
                if _recovery_intervals_overlap(window_start, window_end, seg_start, seg_end):
                    raise HTTPException(409, "That forgotten-time period overlaps time already tracked. Refresh and try again.")
        _reject_recovery_away_overlap(db, current_member.id, window_start, window_end)

    batch_id = secrets.token_urlsafe(12)
    planned_allocations = []
    prior_batch_recovery = 0.0
    window_index = 0
    cursor = resolved_windows[0][0]

    # Snapshot every logical allocation once, but physically place its recovered time only
    # inside the eligible active/no-timer slices. An allocation may therefore have more than
    # one recovered segment when a genuine away interval split the recovery pot.
    for index, (task_id, seconds) in enumerate(allocations):
        task = tasks_by_id[task_id]
        _append_time_integrity_snapshot(
            db, member=current_member, task=task, entry_source="Recovery",
            manual_duration_seconds=seconds,
            current_value_seconds=elapsed_seconds(task.segments) + seconds,
            recorded_at=now, work_date=recovery_date,
            reason_note="Forgotten time recovered",
            recovery_batch_id=batch_id, recovery_allocation_index=index,
            additional_prior_recovery_seconds=prior_batch_recovery,
        )

        remaining = float(seconds)
        parts = []
        while remaining > 1e-6:
            if window_index >= len(resolved_windows):
                raise HTTPException(400, "Recovery windows do not contain enough active time")
            window_start, window_end = resolved_windows[window_index]
            if cursor < window_start or cursor >= window_end:
                cursor = window_start
            capacity = max(0.0, (window_end - cursor).total_seconds())
            if capacity <= 1e-6:
                window_index += 1
                if window_index < len(resolved_windows):
                    cursor = resolved_windows[window_index][0]
                continue
            take = min(remaining, capacity)
            part_end = cursor + timedelta(seconds=take)
            parts.append((cursor, part_end, take))
            cursor = part_end
            remaining -= take
            if (window_end - cursor).total_seconds() <= 1e-6:
                window_index += 1
                if window_index < len(resolved_windows):
                    cursor = resolved_windows[window_index][0]

        planned_allocations.append((index, task, seconds, parts))
        prior_batch_recovery += seconds

    touched = []
    for index, task, seconds, parts in planned_allocations:
        new_segments = list(task.segments or [])
        audit_parts = []
        for part_start, part_end, part_seconds in parts:
            recovered = {
                "start": part_start.isoformat() + "Z",
                "end": part_end.isoformat() + "Z",
                "source": "forgotten_time_recovery",
                "recovered_seconds": round(part_seconds, 1),
                "recovery_batch_id": batch_id,
                "recovery_allocation_index": index,
                "recovery_total_seconds": round(total_seconds, 1),
            }
            new_segments.append(recovered)
            audit_parts.append({
                "start": recovered["start"],
                "end": recovered["end"],
                "seconds": recovered["recovered_seconds"],
            })
        task.segments = new_segments
        if not task.owner_id:
            task.owner_id = current_member.id
        if task.status == "todo":
            task.status = "paused"
        db.add(models.AuditEvent(
            tenant_id=current_member.tenant_id,
            actor_member_id=current_member.id,
            action="forgotten_time_recovered",
            entity_type="TaskInstance",
            entity_id=task.id,
            changes={
                "seconds": round(seconds, 1),
                "source": "forgotten_time_recovery",
                "start": audit_parts[0]["start"],
                "end": audit_parts[-1]["end"],
                "segments": audit_parts,
                "recovery_batch_id": batch_id,
                "recovery_allocation_index": index,
                "recovery_total_seconds": round(total_seconds, 1),
            },
        ))
        touched.append(task)

    db.commit()
    for task in touched:
        db.refresh(task)
    return touched


@app.post("/api/tasks/{task_id}/start", response_model=schemas.TaskOut)
def start_task(task_id: str, payload: schemas.TaskStart = schemas.TaskStart(), current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Treat Start as one serialized state transition per person. This keeps the existing
    # behaviour (starting B pauses A) while making two-tab starts deterministic.
    _lock_timer_owner(db, current_member.id)
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.status == "submitted":
        raise HTTPException(400, "This task has already been submitted and cannot be started again")

    if task.tracks_number_label and task.start_count is None:
        if payload.start_count is None:
            raise HTTPException(400, f"Enter the starting {task.tracks_number_label.lower()} before starting the timer")
        if payload.start_count < 0:
            raise HTTPException(400, "Starting metric cannot be negative")
        task.start_count = payload.start_count

    if not task.owner_id:
        task.owner_id = current_member.id

    # Lock all other running timers before changing them. PostgreSQL advisory locking above
    # ensures another Start for this same member cannot interleave with this operation.
    others = db.query(models.TaskInstance).filter(
        models.TaskInstance.owner_id == current_member.id,
        models.TaskInstance.status == "running",
        models.TaskInstance.id != task_id,
    ).with_for_update().all()
    for other in others:
        other.segments = close_open_segment(other.segments)
        other.status = "paused"
    # Persist pauses before setting the new row to running so the partial unique index can
    # never see two running rows even temporarily within this transaction's flush order.
    if others:
        db.flush()

    already_running_with_open_segment = (
        task.status == "running" and bool(task.segments) and not task.segments[-1].get("end")
    )
    if not already_running_with_open_segment:
        # Normal Start is server-authoritative. start_at remains only for the existing
        # explicit forgot-to-track recovery flow and is tightly bounded to the previous hour.
        start_iso = datetime.utcnow().isoformat() + "Z"
        if payload.start_at:
            try:
                parsed = datetime.fromisoformat(payload.start_at.replace("Z", "+00:00")).replace(tzinfo=None)
                now = datetime.utcnow()
                if parsed <= now and (now - parsed).total_seconds() <= 3600:
                    start_iso = parsed.isoformat() + "Z"
            except ValueError:
                pass
        task.segments = [*(task.segments or []), {"start": start_iso, "end": None}]
        try:
            event_started = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            event_started = datetime.utcnow()
        db.add(models.ClockStartEvent(member_id=current_member.id, task_id=task.id, started_at=event_started))
    task.status = "running"
    task.last_heartbeat_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        # Final backstop for non-PostgreSQL concurrency or any unexpected race around the
        # unique index. Never turn it into a 500 or allow two active timers.
        db.rollback()
        raise HTTPException(409, "Another timer was started at the same time. Refresh and try again")
    db.refresh(task)
    print(f"[timer-diagnostic] start task={task.id} status={task.status} "
          f"last_segment_start={task.segments[-1]['start'] if task.segments else None} "
          f"last_segment_end={task.segments[-1].get('end') if task.segments else None}")
    return task


@app.post("/api/tasks/{task_id}/heartbeat", response_model=schemas.TaskOut)
def heartbeat_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # A lightweight, periodic "still here" ping sent while a timer runs. This is the only
    # signal that can catch a browser disappearing outright (closed, crashed, or the machine
    # shut down), none of which leave any JS running to detect the gap the way sleep and lock
    # detection can, since those rely on the same execution context waking back up.
    task = db.get(models.TaskInstance, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.status != "running":
        # Nothing to keep alive, but not an error, the browser may not know yet that this
        # was paused or submitted elsewhere
        return task
    task.last_heartbeat_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task


@app.post("/api/tasks/{task_id}/pause", response_model=schemas.TaskOut)
def pause_task(task_id: str, payload: schemas.TaskPause = schemas.TaskPause(), current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    # Retry/double-click safety: if another tab already paused or submitted it, the requested
    # end state is already achieved and must not reopen or rewrite the task.
    if task.status in ("paused", "submitted"):
        return task
    if task.status != "running":
        raise HTTPException(400, "Only a running task can be paused")
    validated_end = _validated_pause_end_at(task, payload.end_at)
    task.segments = close_open_segment(task.segments, validated_end)
    task.status = "paused"
    db.commit()
    db.refresh(task)
    open_count = sum(1 for s in (task.segments or []) if not s.get("end"))
    print(f"[timer-diagnostic] pause task={task.id} status={task.status} "
          f"last_segment_start={task.segments[-1]['start'] if task.segments else None} "
          f"last_segment_end={task.segments[-1].get('end') if task.segments else None} "
          f"open_segments_remaining={open_count}")
    return task


@app.post("/api/tasks/{task_id}/pause-beacon", response_model=schemas.TaskOut)
def pause_task_beacon(task_id: str, payload: schemas.TaskPauseBeacon, db: Session = Depends(get_db)):
    # navigator.sendBeacon cannot set the normal Authorization header, so authenticate the
    # supplied session token exactly as before, then use the same locked/idempotent transition
    # as a normal pause. The timestamp remains bounded by server-known timer state.
    session = db.get(models.Session, payload.token)
    if not session or not session.tenant_id:
        raise HTTPException(401, "Session no longer valid")
    db.info["tenant_id"] = session.tenant_id
    current_member = db.query(models.Member).filter(
        models.Member.id == session.member_id, models.Member.tenant_id == session.tenant_id
    ).first()
    if not current_member:
        raise HTTPException(401, "Account no longer exists")
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    if task.status in ("paused", "submitted"):
        return task
    if task.status != "running":
        raise HTTPException(400, "Only a running task can be paused")
    validated_end = _validated_pause_end_at(task, payload.end_at)
    task.segments = close_open_segment(task.segments, validated_end)
    task.status = "paused"
    db.commit()
    db.refresh(task)
    print(f"[timer-diagnostic] pause-beacon task={task.id} status={task.status} "
          f"last_segment_end={task.segments[-1].get('end') if task.segments else None}")
    return task


@app.post("/api/tasks/{task_id}/reset", response_model=schemas.TaskOut)
def reset_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # For a mistaken click, wipes all tracked time back to zero and returns the task to
    # To do, rather than deleting the task itself. The owner can fix their own mistake, and
    # an admin can step in too if someone needs help undoing it.
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _require_task_in_scope(current_member, task, db, owner_can_access=True)
    # A retry after a successful Reset is a no-op rather than a misleading failure.
    if task.status == "todo" and not (task.segments or []) and task.start_count is None and task.end_count is None:
        return task
    if task.status not in ("running", "paused"):
        raise HTTPException(400, "Only a running or paused task can be reset")
    task.segments = []
    task.status = "todo"
    task.start_count = None
    task.end_count = None
    db.commit()
    db.refresh(task)
    return task


def is_bookkeeping_task(task):
    text_value = " ".join(filter(None, [
        getattr(task, "name", None),
        getattr(task, "task_type", None),
        getattr(task, "source_template_name", None),
    ])).lower()
    return "bookkeep" in text_value or "book keeping" in text_value


@app.post("/api/tasks/{task_id}/submit", response_model=schemas.TaskOut)
def submit_task(task_id: str, payload: schemas.TaskSubmit, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = _get_task_for_update(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.owner_id and task.owner_id != current_member.id:
        raise HTTPException(403, "This task belongs to someone else")
    # Complete/Submit is idempotent. If a retry or second tab arrives after the first commit,
    # return the already-submitted record without changing its timestamp, metrics or note.
    if task.status == "submitted":
        return task
    submission_task_type = (payload.task_type if payload.task_type is not None else task.task_type or "").strip()
    is_learning_submission = _is_learning_task_type(submission_task_type)
    if is_learning_submission:
        learning_category = (payload.learning_category or "").strip()
        learning_topic = (payload.learning_topic or "").strip()
        learning_notes = (payload.what_i_learned or "").strip()
        if not learning_category:
            raise HTTPException(400, "Select a Major Category before completing L&D")
        if not learning_topic:
            raise HTTPException(400, "Enter the L&D topic before completing")
        if not learning_notes:
            raise HTTPException(400, "Enter What I Learned before completing L&D")
        learning_note_words = [word for word in learning_notes.split() if any(ch.isalnum() for ch in word)]
        if len(learning_note_words) < MIN_LEARNING_NOTE_WORDS:
            raise HTTPException(400, f"What I Learned must contain at least {MIN_LEARNING_NOTE_WORDS} words")
        if len(learning_note_words) > MAX_LEARNING_NOTE_WORDS:
            raise HTTPException(400, f"What I Learned must contain no more than {MAX_LEARNING_NOTE_WORDS} words")
        if not db.query(models.LearningCategory).filter(models.LearningCategory.is_active.is_(True), func.lower(models.LearningCategory.name) == learning_category.lower()).first():
            raise HTTPException(400, "Select a valid L&D Major Category")
        tdm_references = _learning_reference_dicts(payload.tdm_references)
        article_references = _learning_reference_dicts(payload.article_references)
    else:
        learning_category = learning_topic = learning_notes = ""
        tdm_references = []
        article_references = []
    if task.tracks_number_label and payload.end_count is None:
        raise HTTPException(400, f"Enter the ending {task.tracks_number_label.lower()} before submitting")
    if payload.end_count is not None and payload.end_count < 0:
        raise HTTPException(400, "Ending metric cannot be negative")
    if task.client_id == _unassigned_client_id(current_member.tenant_id) and not is_learning_submission:
        if not payload.client_id or payload.client_id == _unassigned_client_id(current_member.tenant_id):
            raise HTTPException(400, "Select a client before completing this meeting")
        client = db.get(models.Client, payload.client_id)
        if not client:
            raise HTTPException(400, "Selected client was not found")
        task.client_id = client.id
        task.client_name = client.name
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
    proposed_role = payload.role if payload.role is not None else task.role
    proposed_task_type = submission_task_type
    _validate_configured_role_and_task_type(db, proposed_role, proposed_task_type)
    if payload.role is not None:
        task.role = payload.role.strip()
    if payload.task_type is not None:
        task.task_type = payload.task_type.strip()

    allowed_period_types = list(task.period_types or []) if list(task.period_types or []) else (["weekly", "fortnightly", "monthly"] if task.needs_pay_period else [])
    period_required = bool(task.period_required or (task.needs_pay_period and not list(task.period_types or [])))
    _validate_period_selection(
        payload.period_type, payload.period_year, payload.period_number,
        payload.period_start, payload.period_end, allowed_period_types,
        required=period_required, bookkeeping=is_bookkeeping_task(task),
    )
    if payload.period_type:
        task.period_type = payload.period_type
        task.period_year = payload.period_year
        task.period_number = payload.period_number
        task.period_start = payload.period_start
        task.period_end = payload.period_end
        # Keep the existing payroll fields populated for backwards compatibility.
        if task.needs_pay_period and not list(task.period_types or []):
            task.pay_period_type = payload.period_type
            task.pay_period_number = payload.period_number

    task.submitted_at = datetime.utcnow()
    task.submitted_by_id = current_member.id
    task.submitted_pod_id = current_member.pod_id
    task.pushed_to_karbon = False

    final_seconds_at_submit = float(task.adjusted_seconds if task.adjusted_seconds is not None else tracked_seconds)
    if task.adjusted_seconds is None:
        integrity_source = "Automatic"
        integrity_manual_seconds = 0.0
    elif tracked_seconds < 1:
        integrity_source = "Raw Manual"
        integrity_manual_seconds = max(final_seconds_at_submit, 0.0)
    else:
        integrity_source = "Manual Adjustment"
        # Only manually-added time can create unsupported positive variance. A reduction is
        # still preserved as a Manual Adjustment row through Original/Current value, but it
        # does not consume active-time availability or create unreconciled time.
        integrity_manual_seconds = max(final_seconds_at_submit - tracked_seconds, 0.0)
    _append_time_integrity_snapshot(
        db, member=current_member, task=task, entry_source=integrity_source,
        manual_duration_seconds=integrity_manual_seconds,
        current_value_seconds=final_seconds_at_submit, recorded_at=task.submitted_at,
        reason_note=task.note or "",
    )

    if is_learning_submission:
        final_seconds = float(task.adjusted_seconds if task.adjusted_seconds is not None else tracked_seconds)
        learning_record = db.query(models.LearningRecord).filter(models.LearningRecord.task_id == task.id).first()
        if learning_record is None:
            learning_record = models.LearningRecord(task_id=task.id, member_id=task.owner_id or current_member.id)
            db.add(learning_record)
        learning_record.member_id = task.owner_id or current_member.id
        owner_member = db.get(models.Member, learning_record.member_id) if learning_record.member_id else None
        learning_record.member_name = owner_member.name if owner_member else current_member.name
        learning_record.category = learning_category
        learning_record.topic = learning_topic
        learning_record.what_i_learned = learning_notes
        learning_record.tdm_references = tdm_references
        learning_record.article_references = article_references
        learning_record.duration_seconds = max(final_seconds, 0.0)
        learning_record.learned_at = task.submitted_at

    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/reassign", response_model=schemas.TaskOut)
def reassign_task(task_id: str, payload: schemas.TaskReassign, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    require_admin(current_member)
    task = db.get(models.TaskInstance, task_id)
    _require_task_in_scope(current_member, task, db, owner_can_access=True)
    if task.status == "running":
        raise HTTPException(400, "Pause the timer before reassigning this task")
    _require_member_in_scope(current_member, payload.owner_id, db)
    task.owner_id = payload.owner_id
    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/toggle-pushed", response_model=schemas.TaskOut)
def toggle_pushed(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    _require_task_in_scope(current_member, task, db, owner_can_access=True)
    task.pushed_to_karbon = not task.pushed_to_karbon
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    task = db.get(models.TaskInstance, task_id)
    if not task:
        return None
    linked_help_event = db.query(models.HelpEvent).filter(models.HelpEvent.task_id == task_id).first()
    if is_admin_or_above(current_member.role):
        _require_task_in_scope(current_member, task, db, owner_can_access=True)
    else:
        if task.owner_id != current_member.id:
            raise HTTPException(403, "This task belongs to someone else")
        if task.status == "submitted":
            # A task created from logging help given or received can still be removed by the
            # person who logged it, but only for a short window afterward, matching the
            # 30-minute "changed my mind" allowance for these specifically. Past that, or for
            # any other submitted task, only an admin can remove it, unchanged from before.
            within_grace_window = (
                linked_help_event is not None
                and (datetime.utcnow() - linked_help_event.created_at).total_seconds() <= 1800
            )
            if not within_grace_window:
                raise HTTPException(403, "Only an admin can delete a task that has already been submitted")

    # Timer-start diagnostics deliberately reference the task that was started. These rows
    # are operational diagnostics rather than submitted time, so remove them with an
    # unsubmitted task instead of allowing their FK to turn a normal delete into a 500.
    db.query(models.ClockStartEvent).filter(
        models.ClockStartEvent.task_id == task_id
    ).delete(synchronize_session=False)

    # Inactivity audit history can remain useful after the task itself is removed. Keep the
    # inactivity event, but detach its optional task reference so the task can be deleted.
    db.query(models.InactivityEvent).filter(
        models.InactivityEvent.task_id == task_id
    ).update({models.InactivityEvent.task_id: None}, synchronize_session=False)

    # Help rows are task-derived records. Delete every linked row (not just the first one)
    # before deleting the task so no dependent record can retain a dangling task_id.
    db.query(models.HelpEvent).filter(
        models.HelpEvent.task_id == task_id
    ).delete(synchronize_session=False)

    # L&D knowledge rows are task-derived too. Remove the linked knowledge record before
    # deleting the task so PostgreSQL foreign keys never turn an intentional delete into a 500.
    db.query(models.LearningRecord).filter(
        models.LearningRecord.task_id == task_id
    ).delete(synchronize_session=False)

    db.delete(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _log_event("task_delete_blocked_by_reference", task_id=task_id, tenant_id=current_member.tenant_id)
        raise HTTPException(409, "This task is still referenced by another ClockBook record. Refresh and try again")
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
        try:
            _require_task_in_scope(current_member, task, db, owner_can_access=True)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
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


@app.get("/api/admin/diagnostics/invariants")
def invariant_diagnostics(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    """Detect impossible/high-risk states without silently repairing them.

    The endpoint is tenant-scoped by the ORM guard and restricted to Super Admins because
    it exposes operational record identifiers. Repair remains an explicit separate action.
    """
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a Super Admin can run invariant diagnostics")

    findings = []
    running_by_owner = {}
    calendar_map = {}
    tasks = db.query(models.TaskInstance).all()
    for task in tasks:
        if task.status == "running" and task.owner_id:
            running_by_owner.setdefault(task.owner_id, []).append(task.id)
        if task.source_calendar_event_id:
            calendar_map.setdefault(task.source_calendar_event_id, []).append(task.id)

        segments = list(task.segments or [])
        open_indexes = [i for i, seg in enumerate(segments) if isinstance(seg, dict) and not seg.get("end")]
        if len(open_indexes) > 1 or (open_indexes and open_indexes[-1] != len(segments) - 1):
            findings.append({"type": "invalid_open_segments", "task_id": task.id, "indexes": open_indexes})
        if task.status == "submitted" and open_indexes:
            findings.append({"type": "submitted_task_has_open_segment", "task_id": task.id})

        last_end = None
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict) or not seg.get("start"):
                findings.append({"type": "invalid_segment_shape", "task_id": task.id, "segment_index": i})
                continue
            try:
                start = parse_utc_naive(seg["start"])
                end = parse_utc_naive(seg["end"]) if seg.get("end") else None
            except Exception:
                findings.append({"type": "invalid_segment_timestamp", "task_id": task.id, "segment_index": i})
                continue
            if end is not None and end < start:
                findings.append({"type": "negative_segment", "task_id": task.id, "segment_index": i})
            if last_end is not None and start < last_end:
                findings.append({"type": "overlapping_segments", "task_id": task.id, "segment_index": i})
            if end is not None:
                last_end = end

        if task.owner_id and db.get(models.Member, task.owner_id) is None:
            findings.append({"type": "missing_owner", "task_id": task.id, "owner_id": task.owner_id})
        if task.client_id and db.get(models.Client, task.client_id) is None:
            findings.append({"type": "missing_client", "task_id": task.id, "client_id": task.client_id})

    for owner_id, task_ids in running_by_owner.items():
        if len(task_ids) > 1:
            findings.append({"type": "duplicate_running_timers", "owner_id": owner_id, "task_ids": task_ids})
    for event_id, task_ids in calendar_map.items():
        if len(task_ids) > 1:
            findings.append({"type": "duplicate_calendar_mapping", "calendar_event_id": event_id, "task_ids": task_ids})
    for member in db.query(models.Member).all():
        if float(member.weekly_capacity_hours or 0) < 0:
            findings.append({"type": "negative_capacity", "member_id": member.id})

    return {
        "ok": not findings,
        "checked_tasks": len(tasks),
        "checked_members": db.query(models.Member).count(),
        "finding_count": len(findings),
        "findings": findings[:1000],
        "truncated": len(findings) > 1000,
    }


@app.post("/api/tasks/{task_id}/repair-segments")
def repair_task_segments(task_id: str, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Closes every orphaned segment, using the next segment's own start time, since that is
    # the moment the gap this segment represents actually ended. If the very last segment is
    # also stuck open on a task that isn't running, that mirrors the exact bug already fixed
    # for new tasks, closed here using submitted_at for a submitted task, or right now for a
    # paused one, since there is no way to recover the true original moment.
    require_admin(current_member)
    task = db.get(models.TaskInstance, task_id)
    _require_task_in_scope(current_member, task, db, owner_can_access=True)
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
# Karbon reconciliation + daily audit activity
# ---------------------------------------------------------------



def _normalise_calamari_tenant(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("https://", "").replace("http://", "").strip("/")
    if value.endswith(".calamari.io"):
        value = value[:-len(".calamari.io")]
    if "/" in value:
        value = value.split("/", 1)[0]
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise HTTPException(400, "Enter the Calamari workspace name, for example aroundfinance")
    return value


def _calamari_credentials(db: Session):
    tenant = _setting_value(db, "calamari_tenant").strip()
    key_enc = _setting_value(db, "calamari_api_key_encrypted")
    mode = _setting_value(db, "calamari_config_mode").strip().lower()
    if mode == "disabled" or not tenant or not key_enc:
        return None
    try:
        return tenant, _decrypt_secret(key_enc)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Stored Calamari credentials could not be decrypted")


def _calamari_post(tenant: str, api_key: str, path: str, payload=None):
    url = f"https://{tenant}.calamari.io/api/{path.lstrip('/')}"
    try:
        # Calamari documents a 10 requests/second limit. Keep this integration below
        # that ceiling even when a team capacity report needs several holiday calendars.
        time.sleep(0.12)
        with httpx.Client(timeout=25.0, auth=("calamari", api_key)) as client:
            response = client.post(url, json=payload or {})
        if response.status_code == 401:
            raise HTTPException(502, "Calamari rejected the configured API key")
        if response.status_code == 403:
            raise HTTPException(502, "Calamari denied this request. Check that the API key includes Absence Requests and Holidays scopes.")
        if response.status_code == 429:
            raise HTTPException(502, "Calamari API rate limit reached. Try again shortly.")
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("code") or body.get("message") or body.get("error") or ""
            except Exception:
                pass
            suffix = f" ({detail})" if detail else ""
            raise HTTPException(502, f"Calamari API returned {response.status_code}{suffix}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Calamari: {str(exc)}")


def _calamari_daily_adjustments(db: Session, members, start_date, end_date, include_leave_breakdown: bool = False):
    """Return per-member unavailable seconds without exposing absence reasons/types.

    Approved/accepted TIMEOFF reduces capacity. WORK-category requests (for example remote
    work) do not. Public holidays reduce capacity as full or half days. Combined reductions
    are capped at that member's scheduled daily capacity, so leave on a public holiday is
    never double-counted.
    """
    result = {m.id: {} for m in members}
    leave_result = {m.id: {} for m in members} if include_leave_breakdown else None
    meta = {"connected": False, "adjustment_seconds": 0.0, "warnings": []}
    creds = _calamari_credentials(db)
    if not creds or not members:
        return result, meta
    tenant, api_key = creds
    meta["connected"] = True
    emails = {(m.email or "").strip().lower(): m for m in members if (m.email or "").strip()}
    if not emails:
        meta["warnings"].append("No ClockBook email addresses are available for Calamari matching.")
        return result, meta

    # One organisation-level request for absences avoids one API call per employee.
    try:
        absence_rows = _calamari_post(tenant, api_key, "leave/request/v1/find-advanced", {
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        }) or []
    except HTTPException as exc:
        meta["warnings"].append(str(exc.detail))
        absence_rows = []

    for row in absence_rows if isinstance(absence_rows, list) else []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").upper()
        if status not in ("ACCEPTED", "APPROVED"):
            continue
        if str(row.get("absenceCategory") or "").upper() != "TIMEOFF":
            continue
        email = str(row.get("employeeEmail") or "").strip().lower()
        member = emails.get(email)
        if not member:
            continue
        try:
            row_start = datetime.strptime(str(row.get("from"))[:10], "%Y-%m-%d").date()
            row_end = datetime.strptime(str(row.get("to"))[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        # Capacity/unavailable metrics respect the member's Capacity From date.
        # Leave Trends are historical reporting, so keep a separate copy of the
        # selected-period range before applying that capacity boundary.
        trend_row_start = max(row_start, start_date)
        trend_row_end = min(row_end, end_date)

        effective_start = _insights_capacity_effective_start(member, start_date)
        capacity_row_start = max(row_start, effective_start)
        capacity_row_end = min(row_end, end_date)

        daily_hours = max(float(getattr(member, "weekly_capacity_hours", 40.0) or 0.0), 0.0) / 5.0
        daily_seconds = daily_hours * 3600.0
        unit = str(row.get("entitlementAmountUnit") or "DAYS").upper()
        first_amount = float(row.get("amountFirstDay") or 0.0)
        last_amount = float(row.get("amountLastDay") or 0.0)
        total_amount = float(row.get("entitlementAmount") or 0.0)

        def add_absence_days(range_start, range_end, destination):
            if destination is None or range_end < range_start:
                return
            cursor = range_start
            business_dates = []
            while cursor <= range_end:
                if cursor.weekday() < 5:
                    business_dates.append(cursor)
                cursor += timedelta(days=1)
            for i, day in enumerate(business_dates):
                if unit == "HOURS":
                    if len(business_dates) == 1:
                        hours = total_amount or first_amount or last_amount
                    elif i == 0:
                        hours = first_amount or min(total_amount, daily_hours)
                    elif i == len(business_dates) - 1:
                        hours = last_amount or min(total_amount, daily_hours)
                    else:
                        hours = daily_hours
                    seconds = min(max(hours, 0.0) * 3600.0, daily_seconds)
                else:
                    if len(business_dates) == 1:
                        fraction = first_amount or last_amount or total_amount or 1.0
                    elif i == 0:
                        fraction = first_amount or 1.0
                    elif i == len(business_dates) - 1:
                        fraction = last_amount or 1.0
                    else:
                        fraction = 1.0
                    seconds = min(max(fraction, 0.0), 1.0) * daily_seconds
                key = day.isoformat()
                destination[member.id][key] = min(destination[member.id].get(key, 0.0) + seconds, daily_seconds)

        add_absence_days(capacity_row_start, capacity_row_end, result)
        if leave_result is not None:
            add_absence_days(trend_row_start, trend_row_end, leave_result)

    # Holiday calendars are employee-specific, so Calamari exposes them per employee.
    # A failure for one email is surfaced as a warning instead of silently reducing everyone.
    for email, member in emails.items():
        daily_hours = max(float(getattr(member, "weekly_capacity_hours", 40.0) or 0.0), 0.0) / 5.0
        daily_seconds = daily_hours * 3600.0
        try:
            holiday_rows = _calamari_post(tenant, api_key, "holiday/v1/find", {
                "employee": email,
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
            }) or []
        except HTTPException as exc:
            msg = str(exc.detail)
            if "400" in msg and "INVALID_EMPLOYEE" in msg:
                meta["warnings"].append(f"{member.name} could not be matched to Calamari by email.")
            else:
                meta["warnings"].append(f"{member.name}: {msg}")
            continue
        for row in holiday_rows if isinstance(holiday_rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                h_start = datetime.strptime(str(row.get("start"))[:10], "%Y-%m-%d").date()
                h_end = datetime.strptime(str(row.get("end"))[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            effective_start = _insights_capacity_effective_start(member, start_date)
            cursor = max(h_start, effective_start)
            h_end = min(h_end, end_date)
            while cursor <= h_end:
                if cursor.weekday() < 5:
                    seconds = daily_seconds * (0.5 if bool(row.get("halfDay")) else 1.0)
                    key = cursor.isoformat()
                    result[member.id][key] = min(result[member.id].get(key, 0.0) + seconds, daily_seconds)
                cursor += timedelta(days=1)

    meta["adjustment_seconds"] = round(sum(sum(v.values()) for v in result.values()), 1)
    if leave_result is not None:
        # Internal-only aggregate used by Insights. No leave reason/type is retained or exposed.
        meta["_leave_by_member"] = leave_result
    # Avoid repeating identical API errors once per member in the UI.
    meta["warnings"] = list(dict.fromkeys(meta["warnings"]))[:8]
    return result, meta


def _calamari_public_meta(meta):
    """Return Calamari status metadata without internal reporting aggregates."""
    return {k: v for k, v in (meta or {}).items() if not str(k).startswith("_")}


def _insights_leave_summary(members, leave_by_member, start_date, end_date, pod_name=None):
    """Aggregate approved Calamari TIMEOFF for Insights without exposing leave reasons."""
    members = list(members or [])
    leave_by_member = leave_by_member or {}
    days = (end_date - start_date).days + 1
    granularity = "daily" if days <= 14 else ("weekly" if days <= 120 else "monthly")

    def bucket_start(day):
        if granularity == "daily":
            return day
        if granularity == "weekly":
            return day - timedelta(days=day.weekday())
        return day.replace(day=1)

    trend = {}
    cursor = start_date
    while cursor <= end_date:
        key = bucket_start(cursor).isoformat()
        trend.setdefault(key, {"period_start": key, "leave_seconds": 0.0})
        cursor += timedelta(days=1)

    total_seconds = 0.0
    equivalent_days = 0.0
    impacted_dates = set()
    member_rows = []
    for member in members:
        daily_seconds = max(float(getattr(member, "weekly_capacity_hours", 40.0) or 0.0), 0.0) / 5.0 * 3600.0
        member_seconds = 0.0
        member_dates = set()
        for day_key, seconds_raw in (leave_by_member.get(member.id, {}) or {}).items():
            try:
                day = datetime.strptime(day_key, "%Y-%m-%d").date()
            except Exception:
                continue
            if not (start_date <= day <= end_date):
                continue
            seconds = max(float(seconds_raw or 0.0), 0.0)
            if seconds <= 0:
                continue
            member_seconds += seconds
            member_dates.add(day_key)
            impacted_dates.add(day_key)
            key = bucket_start(day).isoformat()
            row = trend.setdefault(key, {"period_start": key, "leave_seconds": 0.0})
            row["leave_seconds"] += seconds
        member_days = (member_seconds / daily_seconds) if daily_seconds > 0 else 0.0
        total_seconds += member_seconds
        equivalent_days += member_days
        if member_seconds > 0:
            member_rows.append({
                "member_id": member.id,
                "name": member.name,
                "leave_seconds": round(member_seconds, 1),
                "equivalent_days": round(member_days, 2),
                "dates_affected": len(member_dates),
            })

    trend_rows = []
    for _, row in sorted(trend.items()):
        trend_rows.append({"period_start": row["period_start"], "leave_seconds": round(row["leave_seconds"], 1)})
    peak = max(trend_rows, key=lambda r: r["leave_seconds"], default=None)
    member_rows.sort(key=lambda r: (-r["leave_seconds"], r["name"].lower()))
    return {
        "leave_seconds": round(total_seconds, 1),
        "equivalent_days": round(equivalent_days, 2),
        "dates_affected": len(impacted_dates),
        "people_with_leave": len(member_rows),
        "trend_granularity": granularity,
        "trend": trend_rows,
        "peak_period_start": peak["period_start"] if peak and peak["leave_seconds"] > 0 else None,
        "peak_leave_seconds": peak["leave_seconds"] if peak and peak["leave_seconds"] > 0 else 0.0,
        "members": member_rows,
        "pod_name": pod_name,
    }

def _member_zone(member):
    try:
        return ZoneInfo((getattr(member, "timezone_name", None) or "Asia/Colombo").strip())
    except Exception:
        return ZoneInfo("UTC")


def _utc_naive_to_local(dt, member):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.astimezone(_member_zone(member))


PRESENCE_CONTINUITY_GAP = timedelta(minutes=10)


def _local_workday_utc_bounds(work_date, member):
    zone = _member_zone(member)
    local_start = datetime.combine(work_date, datetime.min.time()).replace(tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _merge_time_ranges(ranges):
    clean = sorted((start, end) for start, end in ranges if start is not None and end is not None and end > start)
    merged = []
    for start, end in clean:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(start, end) for start, end in merged]


def _record_presence_observation(db: Session, member: models.Member, observed_at: datetime):
    """Record compact active-presence evidence without changing timer/idle behaviour.

    Heartbeats/actions within ten minutes extend one observed interval. A larger gap starts a
    new interval, so future integrity calculations do not treat browser/laptop-off gaps as active.
    The existing DailyPresenceEvent remains intact for the existing Audit report.
    """
    local_now = _utc_naive_to_local(observed_at, member)
    work_date = local_now.date()
    daily = db.query(models.DailyPresenceEvent).filter(
        models.DailyPresenceEvent.member_id == member.id,
        models.DailyPresenceEvent.work_date == work_date,
    ).first()
    latest = db.query(models.ActivePresenceInterval).filter(
        models.ActivePresenceInterval.member_id == member.id,
        models.ActivePresenceInterval.work_date == work_date,
    ).order_by(models.ActivePresenceInterval.ended_at.desc()).first()

    if latest is not None and observed_at >= latest.ended_at and observed_at - latest.ended_at <= PRESENCE_CONTINUITY_GAP:
        latest.ended_at = observed_at
    elif latest is None:
        # On the first deployment heartbeat, carry forward an already-continuous legacy day only
        # when the previous five-minute heartbeat is still recent. This avoids falsely discarding
        # the whole morning while still refusing to bridge a real browser/laptop-off gap.
        start_at = observed_at
        if daily and daily.first_seen_at and daily.last_seen_at and observed_at >= daily.last_seen_at and observed_at - daily.last_seen_at <= PRESENCE_CONTINUITY_GAP:
            start_at = daily.first_seen_at
        db.add(models.ActivePresenceInterval(
            member_id=member.id, work_date=work_date, started_at=start_at, ended_at=observed_at,
        ))
    else:
        db.add(models.ActivePresenceInterval(
            member_id=member.id, work_date=work_date, started_at=observed_at, ended_at=observed_at,
        ))

    if daily is None:
        daily = models.DailyPresenceEvent(
            member_id=member.id, work_date=work_date, first_seen_at=observed_at, last_seen_at=observed_at,
        )
        db.add(daily)
    else:
        if observed_at < daily.first_seen_at:
            daily.first_seen_at = observed_at
        if observed_at > daily.last_seen_at:
            daily.last_seen_at = observed_at
    return work_date


def _time_integrity_work_date(task: models.TaskInstance, member: models.Member, recorded_at: datetime):
    starts = []
    for seg in (task.segments or []):
        if not isinstance(seg, dict) or not seg.get("start"):
            continue
        try:
            starts.append(parse_utc_naive(seg.get("start")))
        except Exception:
            continue
    anchor = min(starts) if starts else (task.created_at or recorded_at)
    return _utc_naive_to_local(anchor, member).date()


def _time_integrity_presence_seconds(db: Session, member: models.Member, work_date, recorded_at: datetime):
    day_start, day_end = _local_workday_utc_bounds(work_date, member)
    clip_end = min(recorded_at, day_end)
    if clip_end <= day_start:
        return 0.0

    intervals = db.query(models.ActivePresenceInterval).filter(
        models.ActivePresenceInterval.member_id == member.id,
        models.ActivePresenceInterval.work_date == work_date,
        models.ActivePresenceInterval.started_at < clip_end,
        models.ActivePresenceInterval.ended_at >= day_start,
    ).all()
    active_ranges = _merge_time_ranges([
        (max(row.started_at, day_start), min(row.ended_at, clip_end)) for row in intervals
    ])
    if not active_ranges:
        # Legacy fallback for a day that predates interval capture. It still uses net active
        # presence by subtracting away periods; it never uses login-to-shutdown totals.
        daily = db.query(models.DailyPresenceEvent).filter(
            models.DailyPresenceEvent.member_id == member.id,
            models.DailyPresenceEvent.work_date == work_date,
        ).first()
        if daily and daily.first_seen_at and daily.last_seen_at:
            start = max(daily.first_seen_at, day_start)
            end = min(daily.last_seen_at, clip_end)
            if end > start:
                active_ranges = [(start, end)]
    if not active_ranges:
        return 0.0

    inactivity = db.query(models.InactivityEvent).filter(
        models.InactivityEvent.member_id == member.id,
        models.InactivityEvent.started_at < clip_end,
        models.InactivityEvent.ended_at > day_start,
    ).all()

    # Keep Time Integrity consistent with the inactivity/audit reports: a sleep/lock period
    # that has been explicitly classified as Helping/Receiving Help is explained work, not
    # unexplained inactivity. Only help evidence that existed at this snapshot's RecordedAt
    # can affect the calculation; later classifications must not rewrite historical evidence.
    linked_help_seconds = {}
    inactivity_ids = [row.id for row in inactivity]
    if inactivity_ids:
        help_query = db.query(models.HelpEvent.inactivity_event_id, models.HelpEvent.seconds).filter(
            models.HelpEvent.source == "sleep_alert",
            models.HelpEvent.inactivity_event_id.in_(inactivity_ids),
        )
        if recorded_at is not None:
            help_query = help_query.filter(models.HelpEvent.created_at <= recorded_at)
        for inactivity_event_id, help_seconds in help_query.all():
            if inactivity_event_id:
                linked_help_seconds[inactivity_event_id] = linked_help_seconds.get(inactivity_event_id, 0.0) + max(float(help_seconds or 0), 0.0)

    active_seconds = sum((end - start).total_seconds() for start, end in active_ranges)
    unexplained_away_overlap = 0.0
    for row in inactivity:
        event_start = max(row.started_at, day_start)
        event_end = min(row.ended_at, clip_end)
        if event_end <= event_start:
            continue
        event_overlap = 0.0
        for a_start, a_end in active_ranges:
            overlap_start = max(a_start, event_start)
            overlap_end = min(a_end, event_end)
            if overlap_end > overlap_start:
                event_overlap += (overlap_end - overlap_start).total_seconds()
        explained_help = min(event_overlap, linked_help_seconds.get(row.id, 0.0))
        unexplained_away_overlap += max(event_overlap - explained_help, 0.0)

    # Inactivity events should not overlap in normal operation, but cap the reporting-only
    # deduction defensively so inconsistent legacy rows can never make Net Active Presence
    # negative or deduct more than the observed active range itself.
    unexplained_away_overlap = min(unexplained_away_overlap, active_seconds)
    return max(active_seconds - unexplained_away_overlap, 0.0)


def _time_integrity_task_bounds(task: models.TaskInstance | None, member: models.Member | None, work_date, recorded_at: datetime | None = None):
    """Return the first timer start and last closed timer end for this task on the audit work date.

    Recovery segments are included because they are real task segments, but an open segment is
    clipped to RecordedAt for immutable snapshots. Historical rows that pre-date these snapshot
    columns can use the same helper against the retained task segments.
    """
    if task is None or member is None or work_date is None:
        return None, None
    day_start, day_end = _local_workday_utc_bounds(work_date, member)
    clip_end = min(recorded_at, day_end) if recorded_at is not None else day_end
    starts = []
    ends = []
    for seg in (task.segments or []):
        if not isinstance(seg, dict) or not seg.get("start"):
            continue
        try:
            start = parse_utc_naive(seg.get("start"))
            end = parse_utc_naive(seg.get("end")) if seg.get("end") else clip_end
        except Exception:
            continue
        overlap_start = max(start, day_start)
        overlap_end = min(end, clip_end)
        if overlap_end <= overlap_start:
            continue
        starts.append(overlap_start)
        if seg.get("end") or recorded_at is not None:
            ends.append(overlap_end)
    return (min(starts) if starts else None, max(ends) if ends else None)


def _time_integrity_task_segments(task: models.TaskInstance | None, member: models.Member | None, work_date, recorded_at: datetime | None = None):
    """Return the task's individual timer/recovery slices on the audit work date.

    This is report evidence only: it does not alter task segments. Older audit rows can be
    reconstructed from retained TaskInstance.segments, while every slice is clipped to the
    original audit RecordedAt so later activity cannot leak into an earlier snapshot.
    """
    if task is None or member is None or work_date is None:
        return []
    day_start, day_end = _local_workday_utc_bounds(work_date, member)
    clip_end = min(recorded_at, day_end) if recorded_at is not None else day_end
    result = []
    for index, seg in enumerate(task.segments or []):
        if not isinstance(seg, dict) or not seg.get("start"):
            continue
        try:
            start = parse_utc_naive(seg.get("start"))
            raw_end = parse_utc_naive(seg.get("end")) if seg.get("end") else None
            end = raw_end or clip_end
        except Exception:
            continue
        overlap_start = max(start, day_start)
        overlap_end = min(end, clip_end)
        if overlap_end <= overlap_start:
            continue
        result.append({
            "segment_index": index + 1,
            "started_at": overlap_start,
            "ended_at": overlap_end,
            "seconds": round((overlap_end - overlap_start).total_seconds(), 3),
            "source": seg.get("source") or "timer",
            "clipped_to_recorded_at": bool(recorded_at is not None and (raw_end is None or raw_end > clip_end)),
        })
    return result


def _time_integrity_segment_totals(db: Session, member: models.Member, work_date, recorded_at: datetime):
    day_start, day_end = _local_workday_utc_bounds(work_date, member)
    clip_end = min(recorded_at, day_end)
    automatic = 0.0
    recovered = 0.0
    tasks = db.query(models.TaskInstance).filter(models.TaskInstance.owner_id == member.id).all()
    for task in tasks:
        for seg in (task.segments or []):
            if not isinstance(seg, dict) or not seg.get("start"):
                continue
            try:
                start = parse_utc_naive(seg.get("start"))
                end = parse_utc_naive(seg.get("end")) if seg.get("end") else recorded_at
            except Exception:
                continue
            overlap_start = max(start, day_start)
            overlap_end = min(end, clip_end)
            if overlap_end <= overlap_start:
                continue
            seconds = (overlap_end - overlap_start).total_seconds()
            if seg.get("source") == "forgotten_time_recovery":
                recovered += seconds
            else:
                automatic += seconds
    return max(automatic, 0.0), max(recovered, 0.0)


def _diagnostic_iso_utc(dt: datetime | None):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None).isoformat() + "Z"


def _diagnostic_timestamp_key(value):
    try:
        return round(parse_utc_naive(value).replace(tzinfo=timezone.utc).timestamp(), 3)
    except Exception:
        return None


def _tracked_total_server_snapshot(db: Session, member: models.Member, captured_at: datetime):
    """Rebuild the Dashboard-style tracked-today total from authoritative task segments.

    This deliberately mirrors elapsedSecondsToday in the browser: a segment belongs to the
    work date of its START in the tracked person's timezone, and an open/future-ending segment
    is clipped to the capture instant. It is diagnostic evidence only and never mutates time.
    """
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(timezone.utc).replace(tzinfo=None)
    zone = _member_zone(member)
    work_date = captured_at.replace(tzinfo=timezone.utc).astimezone(zone).date()
    tasks = db.query(models.TaskInstance).filter(models.TaskInstance.owner_id == member.id).all()
    task_rows = []
    total = 0.0
    for task in tasks:
        segments = []
        task_seconds = 0.0
        for index, seg in enumerate(task.segments or []):
            if not isinstance(seg, dict) or not seg.get("start"):
                continue
            try:
                start = parse_utc_naive(seg.get("start"))
                if start.replace(tzinfo=timezone.utc).astimezone(zone).date() != work_date:
                    continue
                raw_end = parse_utc_naive(seg.get("end")) if seg.get("end") else None
            except Exception:
                continue
            effective_end = min(raw_end, captured_at) if raw_end is not None else captured_at
            if effective_end <= start or start >= captured_at:
                continue
            seconds = max((effective_end - start).total_seconds(), 0.0)
            if seconds <= 0:
                continue
            segments.append({
                "segment_index": index + 1,
                "started_at": _diagnostic_iso_utc(start),
                "ended_at": _diagnostic_iso_utc(raw_end) if raw_end is not None else None,
                "source": seg.get("source") or "timer",
                "seconds": round(seconds, 3),
            })
            task_seconds += seconds
        if segments:
            row = {
                "task_id": task.id,
                "task_name": task.name or "",
                "client_name": task.client_name or "",
                "seconds": round(task_seconds, 3),
                "segments": segments,
            }
            task_rows.append(row)
            total += task_seconds
    return work_date, round(total, 3), task_rows


def _tracked_total_snapshot_differences(browser_tasks, server_tasks):
    """Return task/segment evidence explaining why two tracked-total snapshots differ."""
    browser_by_task = {str(task.get("task_id")): task for task in browser_tasks or [] if task.get("task_id")}
    server_by_task = {str(task.get("task_id")): task for task in server_tasks or [] if task.get("task_id")}
    result = []
    for task_id in sorted(set(browser_by_task) | set(server_by_task)):
        browser = browser_by_task.get(task_id) or {"task_id": task_id, "task_name": "", "client_name": "", "seconds": 0.0, "segments": []}
        server = server_by_task.get(task_id) or {"task_id": task_id, "task_name": "", "client_name": "", "seconds": 0.0, "segments": []}
        browser_seconds = max(float(browser.get("seconds") or 0.0), 0.0)
        server_seconds = max(float(server.get("seconds") or 0.0), 0.0)
        segment_differences = []
        browser_segments = browser.get("segments") or []
        server_segments = server.get("segments") or []
        browser_by_start = {_diagnostic_timestamp_key(seg.get("started_at")): seg for seg in browser_segments if _diagnostic_timestamp_key(seg.get("started_at")) is not None}
        server_by_start = {_diagnostic_timestamp_key(seg.get("started_at")): seg for seg in server_segments if _diagnostic_timestamp_key(seg.get("started_at")) is not None}

        for start_key, seg in server_by_start.items():
            browser_seg = browser_by_start.get(start_key)
            if browser_seg is None:
                segment_differences.append({**seg, "issue": "missing_in_browser", "browser_ended_at": None, "browser_seconds": None})
                continue
            server_end_key = _diagnostic_timestamp_key(seg.get("ended_at")) if seg.get("ended_at") else None
            browser_end_key = _diagnostic_timestamp_key(browser_seg.get("ended_at")) if browser_seg.get("ended_at") else None
            if server_end_key != browser_end_key or abs(float(seg.get("seconds") or 0.0) - float(browser_seg.get("seconds") or 0.0)) >= 0.5:
                segment_differences.append({
                    **seg,
                    "issue": "different_end",
                    "browser_ended_at": browser_seg.get("ended_at"),
                    "browser_seconds": round(float(browser_seg.get("seconds") or 0.0), 3),
                })

        for start_key, seg in browser_by_start.items():
            if start_key not in server_by_start:
                segment_differences.append({
                    **seg,
                    "issue": "browser_only",
                    "browser_ended_at": seg.get("ended_at"),
                    "browser_seconds": round(float(seg.get("seconds") or 0.0), 3),
                })

        difference = server_seconds - browser_seconds
        if abs(difference) >= 0.5 or segment_differences:
            result.append({
                "task_id": task_id,
                "task_name": server.get("task_name") or browser.get("task_name") or "",
                "client_name": server.get("client_name") or browser.get("client_name") or "",
                "browser_seconds": round(browser_seconds, 3),
                "server_seconds": round(server_seconds, 3),
                "difference_seconds": round(difference, 3),
                "segment_differences": segment_differences,
            })
    result.sort(key=lambda row: abs(float(row.get("difference_seconds") or 0.0)), reverse=True)
    return result


@app.post("/api/audit/tracked-total-check")
def tracked_total_check(
    payload: schemas.TrackedTotalCheckIn,
    current_member: models.Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    """Silently preserve browser-vs-server segment evidence when Tracked today diverges."""
    captured_at = payload.captured_at
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    # The capture is client-originated but server-time-synchronised. Reject stale/future probes so
    # this endpoint cannot be used to manufacture arbitrary historical integrity evidence.
    if abs((now - captured_at).total_seconds()) > 10 * 60:
        raise HTTPException(400, "Tracked-total diagnostic capture time is outside the allowed window")

    authoritative_timezone = (current_member.timezone_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(authoritative_timezone)
    except ZoneInfoNotFoundError:
        authoritative_timezone = "UTC"

    browser_tasks = [task.model_dump() for task in payload.tasks]
    browser_total = round(sum(max(float(task.get("seconds") or 0.0), 0.0) for task in browser_tasks), 3)
    work_date, server_total, server_tasks = _tracked_total_server_snapshot(db, current_member, captured_at)
    difference = round(server_total - browser_total, 3)
    if abs(difference) < 30.0:
        return {"mismatch": False, "browser_total_seconds": browser_total, "server_total_seconds": server_total, "difference_seconds": difference}

    # Keep one detailed snapshot per five-minute window. Repeated checks inside the same window
    # add no evidence and would make the management report noisy.
    five_minutes_ago = now - timedelta(minutes=5)
    existing = db.query(models.AuditEvent).filter(
        models.AuditEvent.actor_member_id == current_member.id,
        models.AuditEvent.action == "tracked_total_mismatch_detailed",
        models.AuditEvent.created_at >= five_minutes_ago,
    ).order_by(models.AuditEvent.created_at.desc()).first()
    if existing is None:
        task_differences = _tracked_total_snapshot_differences(browser_tasks, server_tasks)
        event = models.AuditEvent(
            tenant_id=current_member.tenant_id,
            actor_member_id=current_member.id,
            action="tracked_total_mismatch_detailed",
            entity_type="TrackedTotalDiagnostic",
            entity_id=current_member.id,
            changes={
                "member_id": current_member.id,
                "member_name": current_member.name or "",
                "work_date": work_date.isoformat(),
                "captured_at": _diagnostic_iso_utc(captured_at),
                "timezone_name": authoritative_timezone,
                "browser_total_seconds": browser_total,
                "server_total_seconds": server_total,
                "difference_seconds": difference,
                "task_differences": task_differences,
            },
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_id = event.id
    else:
        event_id = existing.id
    return {"mismatch": True, "event_id": event_id, "browser_total_seconds": browser_total, "server_total_seconds": server_total, "difference_seconds": difference}


def _time_integrity_prior_manual_seconds(db: Session, member_id: str, work_date, recorded_at: datetime):
    rows = db.query(models.TimeIntegrityAuditEntry).filter(
        models.TimeIntegrityAuditEntry.member_id == member_id,
        models.TimeIntegrityAuditEntry.work_date == work_date,
        models.TimeIntegrityAuditEntry.recorded_at < recorded_at,
        models.TimeIntegrityAuditEntry.entry_source.in_(["Raw Manual", "Manual Adjustment"]),
    ).order_by(models.TimeIntegrityAuditEntry.entry_group_id, models.TimeIntegrityAuditEntry.revision).all()
    latest = {}
    for row in rows:
        previous = latest.get(row.entry_group_id)
        if previous is None or row.revision > previous.revision:
            latest[row.entry_group_id] = row
    return sum(max(float(row.manual_duration_seconds or 0.0), 0.0) for row in latest.values())


def _append_time_integrity_snapshot(
    db: Session, *, member: models.Member, task: models.TaskInstance, entry_source: str,
    manual_duration_seconds: float, current_value_seconds: float, recorded_at: datetime,
    work_date=None, reason_note: str = "", recovery_batch_id: str | None = None,
    recovery_allocation_index: int | None = None, additional_prior_recovery_seconds: float = 0.0,
    entry_group_id: str | None = None, event_kind: str = "recorded",
):
    """Append an immutable Time Integrity Audit snapshot. Never update older snapshots."""
    _record_presence_observation(db, member, recorded_at)
    # SessionLocal disables autoflush; flush the evidence row so multiple snapshots created in
    # one atomic batch observe the same presence record instead of staging duplicates.
    db.flush()
    work_date = work_date or _time_integrity_work_date(task, member, recorded_at)
    task_started_at, task_ended_at = _time_integrity_task_bounds(task, member, work_date, recorded_at)
    net_active = _time_integrity_presence_seconds(db, member, work_date, recorded_at)
    automatic, recovered = _time_integrity_segment_totals(db, member, work_date, recorded_at)
    recovered += max(float(additional_prior_recovery_seconds or 0.0), 0.0)
    prior_manual = _time_integrity_prior_manual_seconds(db, member.id, work_date, recorded_at)
    available = max(net_active - automatic - recovered - prior_manual, 0.0)
    manual_duration = max(float(manual_duration_seconds or 0.0), 0.0)
    unreconciled = max(manual_duration - available, 0.0)

    group_id = entry_group_id or models.gen_id("tiag")
    previous_versions = db.query(models.TimeIntegrityAuditEntry).filter(
        models.TimeIntegrityAuditEntry.entry_group_id == group_id
    ).order_by(models.TimeIntegrityAuditEntry.revision.desc()).all()
    revision = (previous_versions[0].revision + 1) if previous_versions else 1
    original_value = previous_versions[-1].original_value_seconds if previous_versions else float(current_value_seconds or 0.0)
    row = models.TimeIntegrityAuditEntry(
        entry_group_id=group_id, revision=revision, event_kind=event_kind, task_id=task.id,
        member_id=member.id, member_name=member.name or "", submitted_pod_id=member.pod_id, work_date=work_date,
        client_id=task.client_id, client_name=task.client_name or "", task_name=task.name or "",
        entry_source=entry_source, recorded_at=recorded_at,
        recorded_timezone_name=(member.timezone_name or "UTC").strip() or "UTC",
        task_started_at=task_started_at, task_ended_at=task_ended_at,
        net_active_presence_seconds=round(net_active, 3),
        automatically_tracked_seconds=round(automatic, 3),
        recovered_allocated_seconds=round(recovered, 3),
        prior_manual_allocated_seconds=round(prior_manual, 3),
        available_unallocated_active_seconds=round(available, 3),
        manual_duration_seconds=round(manual_duration, 3),
        unreconciled_manual_seconds=round(unreconciled, 3),
        original_value_seconds=round(float(original_value or 0.0), 3),
        current_value_seconds=round(float(current_value_seconds or 0.0), 3),
        reason_note=(reason_note or "")[:4000],
        recovery_batch_id=recovery_batch_id, recovery_allocation_index=recovery_allocation_index,
    )
    db.add(row)
    if entry_source != "Automatic":
        db.add(models.AuditEvent(
            actor_member_id=member.id, action="time_integrity_snapshot_recorded",
            entity_type="TimeIntegrityAuditEntry", entity_id=row.id,
            changes={
                "task_id": task.id, "entry_source": entry_source,
                "manual_duration_seconds": round(manual_duration, 1),
                "unreconciled_manual_seconds": round(unreconciled, 1),
                "recorded_at": recorded_at.isoformat() + "Z",
                "recorded_timezone_name": (member.timezone_name or "UTC").strip() or "UTC",
            },
        ))
    return row


def _setting_value(db: Session, key: str) -> str:
    setting = db.query(models.TenantSetting).filter(models.TenantSetting.key == key).first()
    return setting.value if setting else ""


def _set_setting_value(db: Session, key: str, value: str):
    setting = db.query(models.TenantSetting).filter(models.TenantSetting.key == key).first()
    if setting is None:
        db.add(models.TenantSetting(key=key, value=value))
    else:
        setting.value = value


DEFAULT_DELEGATION_EXCLUSIONS = [
    "Admin",
    "Support given",
    "Support received",
    BUILTIN_LEARNING_TASK_TYPE,
]


def _delegation_exclusions(db: Session):
    raw = _setting_value(db, "delegation_suggestion_exclusions").strip()
    if not raw:
        return list(DEFAULT_DELEGATION_EXCLUSIONS)
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return list(DEFAULT_DELEGATION_EXCLUSIONS)
    if not isinstance(values, list):
        return list(DEFAULT_DELEGATION_EXCLUSIONS)
    cleaned = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label[:120])
    return cleaned


def _set_delegation_exclusions(db: Session, values):
    cleaned = []
    seen = set()
    for value in values or []:
        label = str(value or "").strip()
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label[:120])
    _set_setting_value(db, "delegation_suggestion_exclusions", json.dumps(cleaned))
    db.commit()
    return cleaned


def _integration_revision(db: Session, integration: str) -> int:
    raw = _setting_value(db, f"{integration}_config_revision").strip()
    try:
        return max(int(raw or 0), 0)
    except ValueError:
        return 0


def _require_integration_revision(db: Session, integration: str, expected_version, connected: bool):
    current = _integration_revision(db, integration)
    # New, never-configured integrations can be connected from a fresh screen without a
    # revision token. Replacing/disconnecting existing config must prove which version the
    # admin actually loaded so two admins cannot silently overwrite each other.
    if connected and expected_version is None:
        raise HTTPException(409, "This integration screen is out of date. Refresh and try again.")
    if expected_version is not None and int(expected_version) != current:
        raise HTTPException(409, "This integration was changed by someone else. Refresh and try again.")
    return current


def _bump_integration_revision(db: Session, integration: str, current: int) -> int:
    next_value = int(current) + 1
    _set_setting_value(db, f"{integration}_config_revision", str(next_value))
    return next_value


def _integration_master_key() -> bytes:
    raw = (os.environ.get("CLOCKBOOK_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise HTTPException(503, "Secure integration storage is not configured. Add CLOCKBOOK_ENCRYPTION_KEY once at deployment level.")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _encrypt_secret(value: str) -> str:
    master = _integration_master_key()
    enc_key = hmac.new(master, b"clockbook-integrations-enc", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"clockbook-integrations-mac", hashlib.sha256).digest()
    nonce = secrets.token_bytes(16)
    data = value.encode("utf-8")
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def _decrypt_secret(token: str) -> str:
    master = _integration_master_key()
    enc_key = hmac.new(master, b"clockbook-integrations-enc", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"clockbook-integrations-mac", hashlib.sha256).digest()
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, cipher, tag = raw[:16], raw[16:-32], raw[-32:]
        expected = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("invalid tag")
        stream = bytearray()
        counter = 0
        while len(stream) < len(cipher):
            stream.extend(hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
            counter += 1
        data = bytes(a ^ b for a, b in zip(cipher, stream))
        return data.decode("utf-8")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Stored integration credentials could not be decrypted")


def _karbon_credentials(db: Session):
    mode = _setting_value(db, "karbon_config_mode").strip().lower()
    if mode == "disabled":
        raise HTTPException(503, "Karbon is not connected")
    token_enc = _setting_value(db, "karbon_application_id_encrypted")
    access_enc = _setting_value(db, "karbon_access_key_encrypted")
    if token_enc and access_enc:
        return _decrypt_secret(token_enc), _decrypt_secret(access_enc), "settings"
    if mode != "settings" and _current_tenant_id(db) == AROUND_TENANT_ID:
        token = (os.environ.get("KARBON_TOKEN") or "").strip()
        access_key = (os.environ.get("KARBON_ACCESS_KEY") or "").strip()
        if token and access_key:
            return token, access_key, "environment"
    raise HTTPException(503, "Karbon is not connected. A Super Admin can connect it in Settings > Integrations.")


def _karbon_headers(db: Session):
    token, access_key, _ = _karbon_credentials(db)
    return {"Authorization": f"Bearer {token}", "AccessKey": access_key}


def _karbon_get_all(path, params=None, db: Session = None, headers=None):
    url = f"https://api.karbonhq.com/v3/{path.lstrip('/')}"
    rows = []
    try:
        request_headers = headers or _karbon_headers(db)
        with httpx.Client(timeout=25.0, headers=request_headers) as client:
            next_url = url
            next_params = params
            while next_url:
                response = client.get(next_url, params=next_params)
                if response.status_code in (401, 403):
                    raise HTTPException(502, "Karbon rejected the configured API credentials")
                if response.status_code >= 400:
                    raise HTTPException(502, f"Karbon API returned {response.status_code}")
                payload = response.json()
                values = payload.get("value", []) if isinstance(payload, dict) else []
                rows.extend(values if isinstance(values, list) else [])
                next_url = payload.get("@odata.nextLink") if isinstance(payload, dict) else None
                next_params = None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Karbon: {str(exc)}")
    return rows


def _karbon_connected_for_workspace(db: Session) -> bool:
    mode = _setting_value(db, "karbon_config_mode").strip().lower()
    token_enc = _setting_value(db, "karbon_application_id_encrypted")
    access_enc = _setting_value(db, "karbon_access_key_encrypted")
    if token_enc and access_enc:
        return True
    if mode != "disabled" and _current_tenant_id(db) == AROUND_TENANT_ID:
        return bool((os.environ.get("KARBON_TOKEN") or "").strip() and (os.environ.get("KARBON_ACCESS_KEY") or "").strip())
    return False


def _calamari_connected_for_workspace(db: Session) -> bool:
    mode = _setting_value(db, "calamari_config_mode").strip().lower()
    return bool(mode != "disabled" and _setting_value(db, "calamari_tenant").strip() and _setting_value(db, "calamari_api_key_encrypted"))


@app.get("/api/integrations/status")
def get_integration_status(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Non-secret capability flags are intentionally available to every member so the UI can
    # hide features a workspace has not chosen to connect. Credentials remain Super Admin-only.
    return {
        "karbon_connected": _karbon_connected_for_workspace(db),
        "calamari_connected": _calamari_connected_for_workspace(db),
    }


@app.get("/api/integrations/karbon")
def get_karbon_integration(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    mode = _setting_value(db, "karbon_config_mode").strip().lower()
    token_enc = _setting_value(db, "karbon_application_id_encrypted")
    access_enc = _setting_value(db, "karbon_access_key_encrypted")
    if token_enc and access_enc:
        try:
            token = _decrypt_secret(token_enc)
            access_key = _decrypt_secret(access_enc)
            return {"connected": True, "source": "settings", "application_id_hint": token[-4:] if token else "", "access_key_hint": access_key[-4:] if access_key else "", "version": _integration_revision(db, "karbon")}
        except HTTPException as exc:
            return {"connected": False, "source": "settings", "error": exc.detail, "version": _integration_revision(db, "karbon")}
    if mode != "disabled" and _current_tenant_id(db) == AROUND_TENANT_ID:
        token = (os.environ.get("KARBON_TOKEN") or "").strip()
        access_key = (os.environ.get("KARBON_ACCESS_KEY") or "").strip()
        if token and access_key:
            return {"connected": True, "source": "environment", "application_id_hint": token[-4:], "access_key_hint": access_key[-4:], "version": _integration_revision(db, "karbon")}
    return {"connected": False, "source": "settings", "version": _integration_revision(db, "karbon")}


@app.put("/api/integrations/karbon")
def save_karbon_integration(payload: schemas.KarbonIntegrationSave, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    application_id = payload.application_id.strip()
    access_key = payload.access_key.strip()
    connected_before = _karbon_connected_for_workspace(db)
    revision = _require_integration_revision(db, "karbon", payload.expected_version, connected_before)
    if not application_id or not access_key:
        raise HTTPException(400, "Application ID and Access Key are required")
    headers = {"Authorization": f"Bearer {application_id}", "AccessKey": access_key}
    _karbon_get_all("Users", {"$top": 1}, headers=headers)
    _set_setting_value(db, "karbon_application_id_encrypted", _encrypt_secret(application_id))
    _set_setting_value(db, "karbon_access_key_encrypted", _encrypt_secret(access_key))
    _set_setting_value(db, "karbon_config_mode", "settings")
    new_revision = _bump_integration_revision(db, "karbon", revision)
    db.commit()
    return {"connected": True, "source": "settings", "application_id_hint": application_id[-4:], "access_key_hint": access_key[-4:], "version": new_revision}


@app.post("/api/integrations/karbon/test")
def test_karbon_integration(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    _karbon_get_all("Users", {"$top": 1}, db=db)
    return {"ok": True}


@app.delete("/api/integrations/karbon")
def disconnect_karbon_integration(expected_version: int | None = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    connected_before = _karbon_connected_for_workspace(db)
    revision = _require_integration_revision(db, "karbon", expected_version, connected_before)
    _set_setting_value(db, "karbon_application_id_encrypted", "")
    _set_setting_value(db, "karbon_access_key_encrypted", "")
    _set_setting_value(db, "karbon_config_mode", "disabled")
    new_revision = _bump_integration_revision(db, "karbon", revision)
    db.commit()
    return {"connected": False, "source": "settings", "version": new_revision}




@app.get("/api/integrations/calamari")
def get_calamari_integration(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    tenant = _setting_value(db, "calamari_tenant").strip()
    key_enc = _setting_value(db, "calamari_api_key_encrypted")
    mode = _setting_value(db, "calamari_config_mode").strip().lower()
    if mode != "disabled" and tenant and key_enc:
        try:
            key = _decrypt_secret(key_enc)
            return {"connected": True, "tenant": tenant, "api_key_hint": key[-4:] if key else "", "version": _integration_revision(db, "calamari")}
        except HTTPException as exc:
            return {"connected": False, "tenant": tenant, "error": exc.detail, "version": _integration_revision(db, "calamari")}
    return {"connected": False, "tenant": tenant, "version": _integration_revision(db, "calamari")}


def _test_calamari_credentials(tenant: str, api_key: str):
    today = datetime.now(timezone.utc).date().isoformat()
    # This endpoint verifies the key plus the Absence Requests scope without depending
    # on any particular employee being present in Calamari.
    _calamari_post(tenant, api_key, "leave/request/v1/find-advanced", {"from": today, "to": today})
    return True


@app.put("/api/integrations/calamari")
def save_calamari_integration(payload: schemas.CalamariIntegrationSave, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    tenant = _normalise_calamari_tenant(payload.tenant)
    api_key = payload.api_key.strip()
    connected_before = _calamari_connected_for_workspace(db)
    revision = _require_integration_revision(db, "calamari", payload.expected_version, connected_before)
    if not api_key:
        raise HTTPException(400, "Calamari API key is required")
    _test_calamari_credentials(tenant, api_key)
    _set_setting_value(db, "calamari_tenant", tenant)
    _set_setting_value(db, "calamari_api_key_encrypted", _encrypt_secret(api_key))
    _set_setting_value(db, "calamari_config_mode", "settings")
    new_revision = _bump_integration_revision(db, "calamari", revision)
    db.commit()
    return {"connected": True, "tenant": tenant, "api_key_hint": api_key[-4:], "version": new_revision}


@app.post("/api/integrations/calamari/test")
def test_calamari_integration(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    creds = _calamari_credentials(db)
    if not creds:
        raise HTTPException(503, "Calamari is not connected")
    tenant, api_key = creds
    _test_calamari_credentials(tenant, api_key)
    # Verify Holidays scope too when there is an email we can test against. INVALID_EMPLOYEE
    # means the auth/scope call succeeded but this particular ClockBook email is not in Calamari.
    sample = db.query(models.Member).filter(models.Member.email.isnot(None)).order_by(models.Member.name).first()
    holiday_scope_verified = False
    if sample and sample.email:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            _calamari_post(tenant, api_key, "holiday/v1/find", {"employee": sample.email, "from": today, "to": today})
            holiday_scope_verified = True
        except HTTPException as exc:
            if "INVALID_EMPLOYEE" not in str(exc.detail):
                raise
    return {"ok": True, "holiday_scope_verified": holiday_scope_verified}


@app.delete("/api/integrations/calamari")
def disconnect_calamari_integration(expected_version: int | None = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_MANAGE_INTEGRATIONS, "You do not have permission to manage integrations")
    connected_before = _calamari_connected_for_workspace(db)
    revision = _require_integration_revision(db, "calamari", expected_version, connected_before)
    _set_setting_value(db, "calamari_api_key_encrypted", "")
    _set_setting_value(db, "calamari_config_mode", "disabled")
    new_revision = _bump_integration_revision(db, "calamari", revision)
    db.commit()
    return {"connected": False, "tenant": _setting_value(db, "calamari_tenant").strip(), "version": new_revision}

@app.get("/api/karbon/reconciliation")
def karbon_reconciliation(member_id: str = None, date_from: str = None, date_to: str = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    target_id = member_id or current_member.id
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    if target_id not in allowed_ids:
        raise HTTPException(403, "You cannot view Karbon time for that person")
    member = db.get(models.Member, target_id)
    if not member or not member.email:
        raise HTTPException(400, "This ClockBook user needs an email address before they can be matched to Karbon")
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else (datetime.utcnow().date() - timedelta(days=datetime.utcnow().weekday()))
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else start_date + timedelta(days=6)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if end_date < start_date:
        raise HTTPException(400, "End date must be on or after start date")

    safe_email = member.email.replace("'", "''")
    users = _karbon_get_all("Users", {"$filter": f"EmailAddress eq '{safe_email}'", "$top": 10}, db=db)
    if not users:
        raise HTTPException(404, f"No Karbon user matched {member.email}")
    karbon_user = users[0]

    # Karbon has used a few different names for the user identifier across
    # its API responses/documentation. Resolve the identifier defensively
    # instead of assuming the list endpoint always returns `UserKey`.
    user_key = (
        karbon_user.get("UserKey")
        or karbon_user.get("UserId")
        or karbon_user.get("UserProfileKey")
        or karbon_user.get("Key")
        or karbon_user.get("Id")
    )
    if not user_key and isinstance(karbon_user, dict):
        lower_keys = {str(k).lower(): v for k, v in karbon_user.items()}
        for candidate in ("userkey", "userid", "userprofilekey", "key", "id"):
            if lower_keys.get(candidate):
                user_key = lower_keys[candidate]
                break
    if not user_key:
        available_fields = ", ".join(sorted(str(k) for k in karbon_user.keys())) if isinstance(karbon_user, dict) else "unknown response shape"
        raise HTTPException(502, f"Karbon user matched by email but no user identifier was returned. Available fields: {available_fields}")

    karbon_filter = (
        f"UserKey eq '{str(user_key).replace(chr(39), chr(39)*2)}' and "
        f"Date ge {start_date.isoformat()}T00:00:00Z and Date le {end_date.isoformat()}T23:59:59Z"
    )
    karbon_entries = _karbon_get_all("IndividualTimeEntries", {"$filter": karbon_filter, "$orderby": "Date", "$top": 1000}, db=db)
    karbon_by_day = {}
    for entry in karbon_entries:
        raw = entry.get("Date")
        try:
            day = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
        except Exception:
            continue
        karbon_by_day[day] = karbon_by_day.get(day, 0) + int(entry.get("Minutes") or 0)

    clockbook_by_day = {}
    tasks = db.query(models.TaskInstance).filter(models.TaskInstance.status == "submitted", models.TaskInstance.submitted_by_id == target_id).all()
    for task in tasks:
        dt = _insights_task_work_date(task)
        if not dt:
            continue
        local_dt = _utc_naive_to_local(dt, member)
        if not local_dt or not (start_date <= local_dt.date() <= end_date):
            continue
        day = local_dt.date().isoformat()
        clockbook_by_day[day] = clockbook_by_day.get(day, 0.0) + _insights_task_seconds(task) / 60.0

    saved_notes = {
        n.work_date.isoformat(): n.note or ""
        for n in db.query(models.KarbonReconciliationNote).filter(
            models.KarbonReconciliationNote.member_id == target_id,
            models.KarbonReconciliationNote.work_date >= start_date,
            models.KarbonReconciliationNote.work_date <= end_date,
        ).all()
    }

    # Super Admin-only audit context for the Daily comparison. This deliberately reuses
    # the same 3-hour shutdown inference as Daily start activity, but does not alter any
    # Karbon/ClockBook reconciliation calculation. Non-Super-Admin responses never include
    # this field, so it cannot be recovered by inspecting the browser API response.
    login_to_shutdown_by_day = {}
    net_login_to_shutdown_by_day = {}
    inactivity_seconds_by_day = {}
    if current_member.role == "super_admin":
        utc_start = datetime.combine(start_date - timedelta(days=1), datetime.min.time())
        utc_end = datetime.combine(end_date + timedelta(days=2), datetime.min.time())
        login_events = db.query(models.LoginEvent).filter(
            models.LoginEvent.member_id == target_id,
            models.LoginEvent.created_at >= utc_start,
            models.LoginEvent.created_at < utc_end,
        ).all()
        first_login_by_day = {}
        for event in login_events:
            local = _utc_naive_to_local(event.created_at, member)
            if local and start_date <= local.date() <= end_date:
                key = local.date().isoformat()
                previous = first_login_by_day.get(key)
                if previous is None or local < previous:
                    first_login_by_day[key] = local

        presence_rows = db.query(models.DailyPresenceEvent).filter(
            models.DailyPresenceEvent.member_id == target_id,
            models.DailyPresenceEvent.work_date >= start_date,
            models.DailyPresenceEvent.work_date <= end_date + timedelta(days=1),
        ).order_by(models.DailyPresenceEvent.work_date, models.DailyPresenceEvent.first_seen_at).all()
        presence_by_day = {row.work_date.isoformat(): row for row in presence_rows if start_date <= row.work_date <= end_date}

        # Net span for the Super Admin Karbon check only. Reuse recorded inactivity
        # events and the same linked-help treatment as the inactivity audit report,
        # so time already explained as colleague help is not deducted as inactivity.
        inactivity_events = db.query(models.InactivityEvent).filter(
            models.InactivityEvent.member_id == target_id,
            models.InactivityEvent.started_at < utc_end,
            models.InactivityEvent.ended_at >= utc_start,
        ).all()
        inactivity_help_seconds = {}
        inactivity_ids = [event.id for event in inactivity_events]
        if inactivity_ids:
            for inactivity_event_id, help_seconds in (
                db.query(models.HelpEvent.inactivity_event_id, models.HelpEvent.seconds)
                .filter(
                    models.HelpEvent.source == "sleep_alert",
                    models.HelpEvent.inactivity_event_id.in_(inactivity_ids),
                )
                .all()
            ):
                if inactivity_event_id:
                    inactivity_help_seconds[inactivity_event_id] = inactivity_help_seconds.get(inactivity_event_id, 0.0) + max(float(help_seconds or 0), 0.0)

        now_utc = datetime.utcnow()
        shutdown_gap = timedelta(hours=3)
        for day_key, first_login_local in first_login_by_day.items():
            presence = presence_by_day.get(day_key)
            if not presence or not presence.last_seen_at:
                continue
            next_seen_utc = None
            for candidate in presence_rows:
                if candidate.work_date > presence.work_date:
                    next_seen_utc = candidate.first_seen_at
                    break
            gap_end = next_seen_utc or now_utc
            if gap_end - presence.last_seen_at < shutdown_gap:
                continue
            shutdown_local = _utc_naive_to_local(presence.last_seen_at, member)
            if shutdown_local and shutdown_local >= first_login_local:
                gross_span_seconds = (shutdown_local - first_login_local).total_seconds()
                login_to_shutdown_by_day[day_key] = gross_span_seconds

                inactive_seconds = 0.0
                for event in inactivity_events:
                    event_start_local = _utc_naive_to_local(event.started_at, member)
                    event_end_local = _utc_naive_to_local(event.ended_at, member)
                    if not event_start_local or not event_end_local:
                        continue
                    overlap_start = max(first_login_local, event_start_local)
                    overlap_end = min(shutdown_local, event_end_local)
                    if overlap_end <= overlap_start:
                        continue
                    overlap_seconds = (overlap_end - overlap_start).total_seconds()
                    explained_help = min(overlap_seconds, inactivity_help_seconds.get(event.id, 0.0))
                    inactive_seconds += max(overlap_seconds - explained_help, 0.0)

                inactivity_seconds_by_day[day_key] = inactive_seconds
                net_login_to_shutdown_by_day[day_key] = max(gross_span_seconds - inactive_seconds, 0.0)

    rows = []
    cursor = start_date
    while cursor <= end_date:
        key = cursor.isoformat()
        cb = round(clockbook_by_day.get(key, 0.0))
        kb = int(karbon_by_day.get(key, 0))
        row = {
            "date": key,
            "clockbook_minutes": cb,
            "karbon_minutes": kb,
            "difference_minutes": kb - cb,
            "note": saved_notes.get(key, ""),
        }
        if current_member.role == "super_admin":
            row["first_login_to_shutdown_seconds"] = login_to_shutdown_by_day.get(key)
            row["inactivity_seconds"] = inactivity_seconds_by_day.get(key)
            row["net_first_login_to_shutdown_seconds"] = net_login_to_shutdown_by_day.get(key)
        rows.append(row)
        cursor += timedelta(days=1)
    cb_total = sum(r["clockbook_minutes"] for r in rows)
    kb_total = sum(r["karbon_minutes"] for r in rows)
    return {
        "member_id": member.id,
        "member_name": member.name,
        "member_email": member.email,
        "karbon_user_key": user_key,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "timezone_name": member.timezone_name or "UTC",
        "clockbook_minutes": cb_total,
        "karbon_minutes": kb_total,
        "difference_minutes": kb_total - cb_total,
        "tolerance_minutes": 10,
        "rows": rows,
    }


@app.put("/api/karbon/reconciliation/note")
def save_karbon_reconciliation_note(payload: schemas.KarbonReconciliationNoteSave, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    allowed_ids = _insights_allowed_member_ids(current_member, db)
    if payload.member_id not in allowed_ids:
        raise HTTPException(403, "You cannot add a Karbon reconciliation note for that person")

    target = db.get(models.Member, payload.member_id)
    if not target:
        raise HTTPException(404, "Staff member not found")

    note_text = (payload.note or "").strip()
    existing = db.query(models.KarbonReconciliationNote).filter(
        models.KarbonReconciliationNote.member_id == payload.member_id,
        models.KarbonReconciliationNote.work_date == payload.date,
    ).first()

    if not note_text:
        if existing:
            db.delete(existing)
            db.commit()
        return {"member_id": payload.member_id, "date": payload.date.isoformat(), "note": ""}

    if existing:
        existing.note = note_text
        existing.created_by_id = current_member.id
        existing.updated_at = datetime.utcnow()
    else:
        existing = models.KarbonReconciliationNote(
            member_id=payload.member_id,
            work_date=payload.date,
            note=note_text,
            created_by_id=current_member.id,
        )
        db.add(existing)
    db.commit()
    return {"member_id": payload.member_id, "date": payload.date.isoformat(), "note": note_text}


@app.get("/api/reports/time-integrity-audit", response_model=schemas.TimeIntegrityAuditResponse)
def time_integrity_audit_report(
    date_from: str = None, date_to: str = None, member_id: str = None, pod_id: str = None,
    client_id: str = None, entry_source: str = None, only_unreconciled: bool = False,
    edited: str = "all", entry_timing: str = "all", recorded_location: str = None,
    current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db),
):
    _require_delegated_admin_permission(
        current_member, PERMISSION_TIME_INTEGRITY_AUDIT,
        "You do not have access to the Time Integrity Audit",
    )

    def _csv_values(raw: str | None):
        if not raw:
            return []
        values = []
        seen = set()
        for value in str(raw).split(","):
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
        return values

    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if start_date and end_date and end_date < start_date:
        raise HTTPException(400, "End date must be on or after start date")

    member_ids = _csv_values(member_id)
    pod_ids = _csv_values(pod_id)
    client_ids = _csv_values(client_id)
    source_values = _csv_values(entry_source)
    edited_values = _csv_values(edited) if edited and edited != "all" else []
    timing_values = _csv_values(entry_timing) if entry_timing and entry_timing != "all" else []
    location_values = _csv_values(recorded_location)

    allowed_sources = {"Automatic", "Recovery", "Raw Manual", "Manual Adjustment"}
    if any(value not in allowed_sources for value in source_values):
        raise HTTPException(400, "Unknown entry source")
    if any(value not in {"yes", "no"} for value in edited_values):
        raise HTTPException(400, "edited must contain only yes or no")
    if any(value not in {"same_day", "later_day"} for value in timing_values):
        raise HTTPException(400, "entry_timing must contain only same_day or later_day")

    allowed_ids = _insights_allowed_member_ids(current_member, db)
    if current_member.role != "super_admin":
        disallowed_members = [value for value in member_ids if value not in allowed_ids]
        if disallowed_members:
            raise HTTPException(403, "A selected staff member is outside your permitted team scope")

    query = db.query(models.TimeIntegrityAuditEntry)
    if current_member.role != "super_admin":
        query = query.filter(models.TimeIntegrityAuditEntry.member_id.in_(allowed_ids))
    # Historical pod scope follows the pod captured when the time entry was recorded, matching
    # the existing submitted-work controls instead of granting retroactive visibility after moves.
    if current_member.role == "admin" and current_member.pod_id:
        query = query.filter(models.TimeIntegrityAuditEntry.submitted_pod_id == current_member.pod_id)
    if start_date:
        query = query.filter(models.TimeIntegrityAuditEntry.work_date >= start_date)
    if end_date:
        query = query.filter(models.TimeIntegrityAuditEntry.work_date <= end_date)
    if member_ids:
        query = query.filter(models.TimeIntegrityAuditEntry.member_id.in_(member_ids))
    if pod_ids:
        if current_member.role == "admin" and current_member.pod_id and any(value != current_member.pod_id for value in pod_ids):
            raise HTTPException(403, "A selected pod is outside your permitted team scope")
        query = query.filter(models.TimeIntegrityAuditEntry.submitted_pod_id.in_(pod_ids))
    if client_ids:
        query = query.filter(models.TimeIntegrityAuditEntry.client_id.in_(client_ids))
    if source_values:
        query = query.filter(models.TimeIntegrityAuditEntry.entry_source.in_(source_values))

    raw_rows = query.order_by(
        models.TimeIntegrityAuditEntry.work_date.desc(),
        models.TimeIntegrityAuditEntry.recorded_at.desc(),
        models.TimeIntegrityAuditEntry.revision.asc(),
    ).all()
    grouped = {}
    for row in raw_rows:
        grouped.setdefault(row.entry_group_id, []).append(row)

    member_rows = db.query(models.Member).all() if current_member.role == "super_admin" else db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).all()
    members_by_id = {member.id: member for member in member_rows}
    task_ids = {row.task_id for row in raw_rows if row.task_id}
    task_rows = db.query(models.TaskInstance).filter(models.TaskInstance.id.in_(task_ids)).all() if task_ids else []
    tasks_by_id = {task.id: task for task in task_rows}
    result_rows = []
    repeated_flags = {}
    manual_delays = []
    for versions in grouped.values():
        versions = sorted(versions, key=lambda row: row.revision)
        original = versions[0]
        latest = versions[-1]
        later_edited = len(versions) > 1
        if edited_values:
            edited_key = "yes" if later_edited else "no"
            if edited_key not in edited_values:
                continue
        if only_unreconciled and float(original.unreconciled_manual_seconds or 0.0) <= 0:
            continue

        member = members_by_id.get(original.member_id)
        recorded_timezone_name = (getattr(original, "recorded_timezone_name", None) or (member.timezone_name if member else None) or "UTC").strip() or "UTC"
        try:
            recorded_zone = ZoneInfo(recorded_timezone_name)
        except ZoneInfoNotFoundError:
            recorded_timezone_name = "UTC"
            recorded_zone = timezone.utc
        local_recorded = original.recorded_at.replace(tzinfo=timezone.utc).astimezone(recorded_zone)
        staff_name = member.name if member else (original.member_name or "Former staff member")
        task_started_at = getattr(original, "task_started_at", None)
        task_ended_at = getattr(original, "task_ended_at", None)
        timer_segments = _time_integrity_task_segments(
            tasks_by_id.get(original.task_id), member, original.work_date, original.recorded_at
        ) if member is not None and original.task_id else []
        if (task_started_at is None or task_ended_at is None) and member is not None and original.task_id:
            historical_start, historical_end = _time_integrity_task_bounds(
                tasks_by_id.get(original.task_id), member, original.work_date, original.recorded_at
            )
            if task_started_at is None:
                task_started_at = historical_start
            if task_ended_at is None:
                task_ended_at = historical_end

        if location_values and recorded_timezone_name not in location_values:
            continue
        later_day = bool(local_recorded.date() > original.work_date)
        timing_key = "later_day" if later_day else "same_day"
        if timing_values and timing_key not in timing_values:
            continue

        unreconciled = max(float(original.unreconciled_manual_seconds or 0.0), 0.0)
        if unreconciled > 0:
            repeated_flags[original.member_id] = repeated_flags.get(original.member_id, 0) + 1
        manual_duration = max(float(original.manual_duration_seconds or 0.0), 0.0)
        if original.entry_source != "Automatic" and manual_duration > 0:
            manual_delays.append(max((local_recorded.date() - original.work_date).days, 0))
        result_rows.append(schemas.TimeIntegrityAuditRow(
            id=original.id, entry_group_id=original.entry_group_id, task_id=original.task_id,
            member_id=original.member_id, staff_member=staff_name, pod_id=original.submitted_pod_id,
            work_date=original.work_date, client_id=original.client_id, client=original.client_name or "",
            task=original.task_name or "", entry_source=original.entry_source,
            manual_duration_seconds=manual_duration, recorded_at=original.recorded_at,
            recorded_timezone_name=recorded_timezone_name,
            task_started_at=task_started_at, task_ended_at=task_ended_at, timer_segments=timer_segments,
            net_active_presence_seconds=max(float(original.net_active_presence_seconds or 0.0), 0.0),
            automatically_tracked_seconds=max(float(original.automatically_tracked_seconds or 0.0), 0.0),
            recovered_allocated_seconds=max(float(original.recovered_allocated_seconds or 0.0), 0.0),
            prior_manual_allocated_seconds=max(float(original.prior_manual_allocated_seconds or 0.0), 0.0),
            available_unallocated_active_seconds=max(float(original.available_unallocated_active_seconds or 0.0), 0.0),
            unreconciled_manual_seconds=unreconciled, later_edited=later_edited,
            original_value_seconds=max(float(original.original_value_seconds or 0.0), 0.0),
            current_value_seconds=max(float(latest.current_value_seconds or 0.0), 0.0),
            last_edited_at=latest.recorded_at if later_edited else None,
            reason_note=latest.reason_note or original.reason_note or "",
            review_status="Review" if unreconciled > 0 else "Reconciled",
            entry_timing="Later day" if later_day else "Same day",
            recovery_batch_id=original.recovery_batch_id,
            recovery_allocation_index=original.recovery_allocation_index,
        ))

    result_rows.sort(key=lambda row: (row.work_date, row.recorded_at), reverse=True)
    repeated_ids = {member_id for member_id, count in repeated_flags.items() if count > 1}
    name_by_id = {row.member_id: row.staff_member for row in result_rows}
    repeated_names = sorted(name_by_id[mid] for mid in repeated_ids if mid in name_by_id)
    manual_rows = [row for row in result_rows if row.entry_source != "Automatic"]
    summary = schemas.TimeIntegrityAuditSummary(
        total_manual_seconds=round(sum(row.manual_duration_seconds for row in manual_rows), 1),
        total_unreconciled_manual_seconds=round(sum(row.unreconciled_manual_seconds for row in result_rows), 1),
        flagged_entries=sum(1 for row in result_rows if row.unreconciled_manual_seconds > 0),
        later_edited_entries=sum(1 for row in result_rows if row.later_edited),
        average_delay_days=round(sum(manual_delays) / len(manual_delays), 2) if manual_delays else 0.0,
        repeated_unreconciled_staff=len(repeated_ids),
        repeated_unreconciled_staff_names=repeated_names,
    )

    # Full retained timer evidence for the selected day/person range. The row-level Segments
    # column above is intentionally clipped to that immutable audit snapshot; this timeline is
    # separate so management can reconstruct how a day's tracked total accumulated.
    if start_date is not None:
        timeline_from = start_date
    elif result_rows:
        timeline_from = min(row.work_date for row in result_rows)
    else:
        timeline_from = datetime.utcnow().date()
    if end_date is not None:
        timeline_to = end_date
    elif result_rows:
        timeline_to = max(row.work_date for row in result_rows)
    else:
        timeline_to = timeline_from

    timeline_member_ids = set(member_ids or allowed_ids)
    timeline_tasks_query = db.query(models.TaskInstance).filter(models.TaskInstance.owner_id.in_(timeline_member_ids))
    if client_ids:
        timeline_tasks_query = timeline_tasks_query.filter(models.TaskInstance.client_id.in_(client_ids))
    timeline_tasks = timeline_tasks_query.all()
    daily_segments = []
    now_utc = datetime.utcnow()
    for task in timeline_tasks:
        member = members_by_id.get(task.owner_id)
        if member is None:
            continue
        if current_member.role == "admin" and current_member.pod_id:
            task_scope_pod = task.submitted_pod_id or member.pod_id
            if task_scope_pod != current_member.pod_id:
                continue
        if pod_ids:
            task_scope_pod = task.submitted_pod_id or member.pod_id
            if task_scope_pod not in pod_ids:
                continue
        for index, seg in enumerate(task.segments or []):
            if not isinstance(seg, dict) or not seg.get("start"):
                continue
            try:
                seg_start = parse_utc_naive(seg.get("start"))
                seg_end = parse_utc_naive(seg.get("end")) if seg.get("end") else now_utc
            except Exception:
                continue
            if seg_end <= seg_start:
                continue
            local_start_date = _utc_naive_to_local(seg_start, member).date()
            local_end_date = _utc_naive_to_local(max(seg_start, seg_end - timedelta(microseconds=1)), member).date()
            slice_date = max(local_start_date, timeline_from)
            last_date = min(local_end_date, timeline_to)
            while slice_date <= last_date:
                day_start, day_end = _local_workday_utc_bounds(slice_date, member)
                overlap_start = max(seg_start, day_start)
                overlap_end = min(seg_end, day_end, now_utc)
                if overlap_end > overlap_start:
                    daily_segments.append(schemas.TimeIntegrityDailySegment(
                        member_id=member.id, staff_member=member.name or "", work_date=slice_date,
                        task_id=task.id, client=task.client_name or "", task=task.name or "",
                        segment_index=index + 1, started_at=overlap_start, ended_at=overlap_end,
                        seconds=round((overlap_end - overlap_start).total_seconds(), 3),
                        source=seg.get("source") or "timer", task_status=task.status or "",
                    ))
                slice_date += timedelta(days=1)
    daily_segments.sort(key=lambda row: (row.work_date, row.started_at, row.staff_member), reverse=True)

    diagnostic_query = db.query(models.AuditEvent).filter(
        models.AuditEvent.action == "tracked_total_mismatch_detailed",
        models.AuditEvent.entity_type == "TrackedTotalDiagnostic",
    )
    if current_member.role != "super_admin":
        diagnostic_query = diagnostic_query.filter(models.AuditEvent.actor_member_id.in_(allowed_ids))
    if member_ids:
        diagnostic_query = diagnostic_query.filter(models.AuditEvent.actor_member_id.in_(member_ids))
    diagnostic_events = diagnostic_query.order_by(models.AuditEvent.created_at.desc()).limit(1000).all()
    tracked_total_diagnostics = []
    for event in diagnostic_events:
        changes = event.changes if isinstance(event.changes, dict) else {}
        try:
            diagnostic_date = datetime.strptime(str(changes.get("work_date") or ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date and diagnostic_date < start_date:
            continue
        if end_date and diagnostic_date > end_date:
            continue
        member = members_by_id.get(event.actor_member_id)
        if member is None:
            continue
        try:
            captured_at = parse_utc_naive(changes.get("captured_at"))
        except Exception:
            captured_at = event.created_at
        tracked_total_diagnostics.append(schemas.TrackedTotalDiagnosticRow(
            id=event.id,
            member_id=event.actor_member_id,
            staff_member=member.name or changes.get("member_name") or "Former staff member",
            work_date=diagnostic_date,
            captured_at=captured_at,
            timezone_name=str(changes.get("timezone_name") or member.timezone_name or "UTC"),
            browser_total_seconds=max(float(changes.get("browser_total_seconds") or 0.0), 0.0),
            server_total_seconds=max(float(changes.get("server_total_seconds") or 0.0), 0.0),
            difference_seconds=float(changes.get("difference_seconds") or 0.0),
            task_differences=changes.get("task_differences") or [],
        ))

    return schemas.TimeIntegrityAuditResponse(
        rows=result_rows, daily_segments=daily_segments, summary=summary,
        tracked_total_diagnostics=tracked_total_diagnostics
    )


@app.get("/api/audit/changes", response_model=list[schemas.AuditEventOut])
def audit_changes(
    limit: int = 200,
    entity_type: str | None = None,
    current_member: models.Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    if current_member.role != "super_admin":
        raise HTTPException(403, "Only a super admin can view the change audit")
    limit = max(1, min(int(limit), 500))
    query = db.query(models.AuditEvent)
    if entity_type:
        query = query.filter(models.AuditEvent.entity_type == entity_type[:80])
    return query.order_by(models.AuditEvent.created_at.desc()).limit(limit).all()


@app.post("/api/audit/presence-heartbeat", status_code=204)
def audit_presence_heartbeat(current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    # Silent presence signal used by the existing Audit report and the Time Integrity Audit.
    # It remains independent of timers, lock/sleep detection and notifications.
    now_utc = datetime.utcnow()
    try:
        _record_presence_observation(db, current_member, now_utc)
        db.commit()
    except IntegrityError:
        # Multiple tabs can race on the existing one-row-per-day DailyPresenceEvent. Retry
        # after the winner commits; ActivePresenceInterval itself is intentionally appendable.
        db.rollback()
        _record_presence_observation(db, current_member, now_utc)
        db.commit()
    return None


@app.get("/api/audit/activity-summary")
def audit_activity_summary(date_from: str = None, date_to: str = None, current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    _require_delegated_admin_permission(current_member, PERMISSION_REPORT_AUDIT, "You do not have access to the Audit report")
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else datetime.utcnow().date()
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else start_date
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if end_date < start_date:
        raise HTTPException(400, "End date must be on or after start date")

    allowed_ids = _insights_allowed_member_ids(current_member, db)
    members = db.query(models.Member).filter(models.Member.id.in_(allowed_ids)).order_by(models.Member.name).all()
    # Pull a generous UTC envelope; grouping is done in each user's configured local zone.
    utc_start = datetime.combine(start_date - timedelta(days=1), datetime.min.time())
    utc_end = datetime.combine(end_date + timedelta(days=2), datetime.min.time())
    login_events = db.query(models.LoginEvent).filter(models.LoginEvent.member_id.in_(allowed_ids), models.LoginEvent.created_at >= utc_start, models.LoginEvent.created_at < utc_end).all()
    clock_events = db.query(models.ClockStartEvent).filter(models.ClockStartEvent.member_id.in_(allowed_ids), models.ClockStartEvent.started_at >= utc_start, models.ClockStartEvent.started_at < utc_end).all()
    # Pull one extra local day of presence so a heartbeat just after midnight can prove the
    # previous day was continuous rather than incorrectly marking 23:xx as a shutdown.
    presence_rows = db.query(models.DailyPresenceEvent).filter(
        models.DailyPresenceEvent.member_id.in_(allowed_ids),
        models.DailyPresenceEvent.work_date >= start_date,
        models.DailyPresenceEvent.work_date <= end_date + timedelta(days=1),
    ).all()

    # Reporting-only inactivity context for the Daily start activity table. Keep the
    # original gross login-to-shutdown span intact, and derive a separate net span by
    # subtracting recorded unexplained inactivity. Help already classified from a
    # sleep/lock prompt is treated as explained time and is not deducted again.
    inactivity_rows = db.query(models.InactivityEvent).filter(
        models.InactivityEvent.started_at < utc_end,
        models.InactivityEvent.ended_at >= utc_start,
    ).all()
    inactivity_by_member = {}
    for event in inactivity_rows:
        inactivity_by_member.setdefault(event.member_id, []).append(event)

    inactivity_help_seconds = {}
    inactivity_ids = [event.id for event in inactivity_rows]
    if inactivity_ids:
        for inactivity_event_id, help_seconds in (
            db.query(models.HelpEvent.inactivity_event_id, models.HelpEvent.seconds)
            .filter(
                models.HelpEvent.source == "sleep_alert",
                models.HelpEvent.inactivity_event_id.in_(inactivity_ids),
            )
            .all()
        ):
            if inactivity_event_id:
                inactivity_help_seconds[inactivity_event_id] = inactivity_help_seconds.get(inactivity_event_id, 0.0) + max(float(help_seconds or 0), 0.0)

    logins_by_member = {}
    clocks_by_member = {}
    presence_by_member = {}
    for event in login_events:
        logins_by_member.setdefault(event.member_id, []).append(event.created_at)
    for event in clock_events:
        clocks_by_member.setdefault(event.member_id, []).append(event.started_at)
    for event in presence_rows:
        presence_by_member.setdefault(event.member_id, []).append(event)

    rows = []
    now_utc = datetime.utcnow()
    shutdown_gap = timedelta(hours=3)
    for member in members:
        per_day = {}
        for dt in logins_by_member.get(member.id, []):
            local = _utc_naive_to_local(dt, member)
            if local and start_date <= local.date() <= end_date:
                bucket = per_day.setdefault(local.date().isoformat(), {"first_login": None, "first_clock": None, "presence": None})
                if bucket["first_login"] is None or local < bucket["first_login"]:
                    bucket["first_login"] = local
        for dt in clocks_by_member.get(member.id, []):
            local = _utc_naive_to_local(dt, member)
            if local and start_date <= local.date() <= end_date:
                bucket = per_day.setdefault(local.date().isoformat(), {"first_login": None, "first_clock": None, "presence": None})
                if bucket["first_clock"] is None or local < bucket["first_clock"]:
                    bucket["first_clock"] = local

        member_presence = sorted(presence_by_member.get(member.id, []), key=lambda r: (r.work_date, r.first_seen_at))
        for presence in member_presence:
            if start_date <= presence.work_date <= end_date:
                bucket = per_day.setdefault(presence.work_date.isoformat(), {"first_login": None, "first_clock": None, "presence": None})
                bucket["presence"] = presence

        for day, bucket in sorted(per_day.items()):
            shutdown_utc = None
            presence = bucket.get("presence")
            if presence and presence.last_seen_at:
                next_seen_utc = None
                for candidate in member_presence:
                    if candidate.work_date > presence.work_date:
                        next_seen_utc = candidate.first_seen_at
                        break
                gap_end = next_seen_utc or now_utc
                if gap_end - presence.last_seen_at >= shutdown_gap:
                    shutdown_utc = presence.last_seen_at

            shutdown_local = _utc_naive_to_local(shutdown_utc, member) if shutdown_utc else None
            first_login_to_shutdown_seconds = None
            inactivity_seconds = None
            net_first_login_to_shutdown_seconds = None
            if bucket["first_login"] and shutdown_local and shutdown_local >= bucket["first_login"]:
                first_login_to_shutdown_seconds = (shutdown_local - bucket["first_login"]).total_seconds()

                inactive = 0.0
                for event in inactivity_by_member.get(member.id, []):
                    event_start_local = _utc_naive_to_local(event.started_at, member)
                    event_end_local = _utc_naive_to_local(event.ended_at, member)
                    if not event_start_local or not event_end_local:
                        continue
                    overlap_start = max(bucket["first_login"], event_start_local)
                    overlap_end = min(shutdown_local, event_end_local)
                    if overlap_end <= overlap_start:
                        continue
                    overlap_seconds = (overlap_end - overlap_start).total_seconds()
                    explained_help = min(overlap_seconds, inactivity_help_seconds.get(event.id, 0.0))
                    inactive += max(overlap_seconds - explained_help, 0.0)

                inactivity_seconds = inactive
                net_first_login_to_shutdown_seconds = max(first_login_to_shutdown_seconds - inactive, 0.0)

            rows.append({
                "member_id": member.id,
                "member_name": member.name,
                "date": day,
                "timezone_name": member.timezone_name or "UTC",
                "first_login_at": bucket["first_login"].isoformat() if bucket["first_login"] else None,
                "first_clock_at": bucket["first_clock"].isoformat() if bucket["first_clock"] else None,
                "laptop_turned_off_at": shutdown_local.isoformat() if shutdown_local else None,
                "first_login_to_shutdown_seconds": first_login_to_shutdown_seconds,
                "inactivity_seconds": inactivity_seconds,
                "net_first_login_to_shutdown_seconds": net_first_login_to_shutdown_seconds,
            })
    return rows


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


def period_label(task):
    type_ = task.period_type
    year = task.period_year
    number = task.period_number
    if not type_:
        # Preserve display of historical payroll entries created before the generic period model.
        type_ = task.pay_period_type
        number = task.pay_period_number
    if not type_:
        return ""
    if type_ == "daily":
        return task.period_start or ""
    if type_ == "weekly":
        if is_bookkeeping_task(task) and task.period_start and number:
            try:
                month = datetime.fromisoformat(task.period_start).strftime("%b %Y")
            except ValueError:
                month = task.period_start[:7]
            return f"{month} · Week {number}"
        return f"Week {number}, {year}" if year else (f"Week {number}" if number else "")
    if type_ == "fortnightly":
        return f"Fortnight {number}, {year}" if year else (f"Fortnight {number}" if number else "")
    if type_ == "monthly" and number:
        return datetime(year, number, 1).strftime("%b %Y") if year else f"Month {number}"
    if type_ == "bi_monthly" and number and year:
        start_month = ((number - 1) * 2) + 1
        end_month = min(start_month + 1, 12)
        return f"{datetime(year, start_month, 1).strftime('%b')}–{datetime(year, end_month, 1).strftime('%b %Y')}"
    if type_ == "quarterly" and number and year:
        return f"Q{number} {year}"
    if type_ == "year" and year:
        return str(year)
    if type_ == "custom" and task.period_start and task.period_end:
        return f"{task.period_start} to {task.period_end}"
    return ""


def period_key(task):
    return "|".join([
        task.period_type or task.pay_period_type or "",
        str(task.period_year or ""),
        str(task.period_number or task.pay_period_number or ""),
        task.period_start or "",
        task.period_end or "",
    ])


def _hide_tracked_time_for_member(rows, member):
    """Remove original tracked-time detail unless this Admin was explicitly delegated access.

    Duration remains available. Staff can still see their own export detail, and Super Admins
    always retain tracked-time visibility. This is enforced on the API response, not only the UI.
    """
    if member.role != "admin" or _has_permission(member, PERMISSION_VIEW_TRACKED_TIME):
        return rows
    for row in rows:
        row["tracked_seconds"] = None
        row["tracked_hours"] = None
    return rows


def build_export_rows(db, client_id, pushed, date_from=None, date_to=None, submitted_by=None, exclude_owner_ids=None, include_owner_ids=None, submitted_pod_id=None):
    query = db.query(models.TaskInstance).filter(models.TaskInstance.status == "submitted")
    if client_id and client_id != "all":
        query = query.filter(models.TaskInstance.client_id == client_id)
    if submitted_by and submitted_by != "all":
        query = query.filter(models.TaskInstance.submitted_by_id == submitted_by)
    if exclude_owner_ids:
        query = query.filter(~models.TaskInstance.submitted_by_id.in_(exclude_owner_ids))
    if include_owner_ids is not None:
        query = query.filter(models.TaskInstance.submitted_by_id.in_(include_owner_ids))
    if submitted_pod_id is not None:
        query = query.filter(models.TaskInstance.submitted_pod_id == submitted_pod_id)
    if pushed == "pending":
        query = query.filter(models.TaskInstance.pushed_to_karbon.is_(False))
    elif pushed == "pushed":
        query = query.filter(models.TaskInstance.pushed_to_karbon.is_(True))
    from_dt = parse_utc_naive(date_from)
    to_dt = parse_utc_naive(date_to)
    tasks = query.order_by(models.TaskInstance.submitted_at.desc()).all()

    members = {m.id: m.name for m in db.query(models.Member).all()}
    rows = []
    for t in tasks:
        work_started_at = _insights_task_work_date(t)
        if from_dt and (work_started_at is None or work_started_at < from_dt):
            continue
        if to_dt and (work_started_at is None or work_started_at > to_dt):
            continue
        tracked_seconds_with_recovery = elapsed_seconds(t.segments)
        forgotten_recovered_seconds = sum(
            elapsed_seconds([seg]) for seg in (t.segments or [])
            if isinstance(seg, dict) and seg.get("source") == "forgotten_time_recovery"
        )
        automatically_tracked_seconds = max(tracked_seconds_with_recovery - forgotten_recovered_seconds, 0.0)
        has_submit_override = t.adjusted_seconds is not None
        is_adjusted = has_submit_override or forgotten_recovered_seconds > 0
        final_seconds = t.adjusted_seconds if has_submit_override else tracked_seconds_with_recovery
        adjustment_type = (
            "Manual override + forgotten time recovered" if has_submit_override and forgotten_recovered_seconds > 0
            else "Forgotten time recovered" if forgotten_recovered_seconds > 0
            else "Manual override" if has_submit_override
            else ""
        )
        change = None
        if t.start_count is not None and t.end_count is not None:
            change = t.end_count - t.start_count
        rows.append({
            "id": t.id,
            "date": work_started_at.strftime("%Y-%m-%d") if work_started_at else "",
            "work_started_at": (work_started_at.isoformat() + "Z") if work_started_at else None,
            "submitted_at": (t.submitted_at.isoformat() + "Z") if t.submitted_at else None,
            "client": t.client_name,
            "template_name": getattr(t, "source_template_name", None),
            "task": t.name,
            "role": t.role,
            "task_type": t.task_type,
            "seconds": round(final_seconds, 1),
            "tracked_seconds": round(automatically_tracked_seconds, 1) if is_adjusted else None,
            "hours": round(final_seconds / 3600, 2),
            "tracked_hours": round(automatically_tracked_seconds / 3600, 2) if is_adjusted else None,
            "adjusted": is_adjusted,
            "adjustment_type": adjustment_type,
            "forgotten_time_recovered_seconds": round(forgotten_recovered_seconds, 1),
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
            "period": period_label(t),
            "period_key": period_key(t),
            "period_type": t.period_type,
            "period_year": t.period_year,
            "period_number": t.period_number,
            "period_start": t.period_start,
            "period_end": t.period_end,
        })
    return rows


@app.get("/api/export")
def get_export(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    exclude_owner_ids = None
    include_owner_ids = None
    submitted_pod_id = None
    if current_member.role == "member":
        submitted_by = current_member.id
    elif current_member.role == "admin":
        can_see_super_admins = _has_permission(current_member, PERMISSION_MANAGE_SUPER_ADMINS)
        super_admin_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if not can_see_super_admins:
            exclude_owner_ids = super_admin_ids
        if current_member.pod_id:
            include_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
            if can_see_super_admins:
                include_owner_ids = list(dict.fromkeys(include_owner_ids + super_admin_ids))
            submitted_pod_id = current_member.pod_id
    rows = build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by, exclude_owner_ids, include_owner_ids, submitted_pod_id)
    return _hide_tracked_time_for_member(rows, current_member)


def _csv_safe_text(value):
    text_value = "" if value is None else str(value)
    # Excel/Sheets can execute cells beginning with these characters as formulas. Prefixing
    # an apostrophe keeps user-controlled names/notes literal without changing stored data.
    if text_value.startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


@app.get("/api/export.csv")
def get_export_csv(client_id: str = "all", pushed: str = "pending", date_from: str = None, date_to: str = None, submitted_by: str = "all", current_member: models.Member = Depends(get_current_member), db: Session = Depends(get_db)):
    exclude_owner_ids = None
    include_owner_ids = None
    submitted_pod_id = None
    if current_member.role == "member":
        submitted_by = current_member.id
    elif current_member.role == "admin":
        can_see_super_admins = _has_permission(current_member, PERMISSION_MANAGE_SUPER_ADMINS)
        super_admin_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.role == "super_admin").all()]
        if not can_see_super_admins:
            exclude_owner_ids = super_admin_ids
        if current_member.pod_id:
            include_owner_ids = [m.id for m in db.query(models.Member.id).filter(models.Member.pod_id == current_member.pod_id).all()]
            if can_see_super_admins:
                include_owner_ids = list(dict.fromkeys(include_owner_ids + super_admin_ids))
            submitted_pod_id = current_member.pod_id
    rows = build_export_rows(db, client_id, pushed, date_from, date_to, submitted_by, exclude_owner_ids, include_owner_ids, submitted_pod_id)
    can_view_tracked_time = current_member.role != "admin" or _has_permission(current_member, PERMISSION_VIEW_TRACKED_TIME)
    rows = _hide_tracked_time_for_member(rows, current_member)
    buffer = StringIO()
    writer = csv.writer(buffer)
    headers = ["Date", "Client", "Template", "Task", "Role", "Task Type", "Period", "Hours"]
    if can_view_tracked_time:
        headers.append("Tracked Hours")
    headers += ["Notes", "Tracked by", "Pushed to Karbon", "Bank Account", "Metric", "Start Count", "End Count", "Change"]
    writer.writerow(headers)
    for r in rows:
        values = [
            r["date"], _csv_safe_text(r["client"]), _csv_safe_text(r["template_name"] or ""), _csv_safe_text(r["task"]),
            _csv_safe_text(r["role"]), _csv_safe_text(r["task_type"]), _csv_safe_text(r["period"]), r["hours"],
        ]
        if can_view_tracked_time:
            values.append(r["tracked_hours"] if r["tracked_hours"] is not None else "")
        values += [
            _csv_safe_text(r["note"]), _csv_safe_text(r["tracked_by"]), "Yes" if r["pushed"] else "No",
            _csv_safe_text(r["bank_account"]), _csv_safe_text(r["metric"]),
            r["start_count"] if r["start_count"] is not None else "",
            r["end_count"] if r["end_count"] is not None else "",
            r["change"] if r["change"] is not None else "",
        ]
        writer.writerow(values)
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
