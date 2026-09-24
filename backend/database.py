import os
from sqlalchemy import create_engine, event, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker, declarative_base, with_loader_criteria, Session

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./clockbook.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine_kwargs = {"connect_args": connect_args, "pool_pre_ping": True}
if not is_sqlite:
    engine_kwargs.update({
        "pool_size": int(os.environ.get("CLOCKBOOK_DB_POOL_SIZE", "5")),
        "max_overflow": int(os.environ.get("CLOCKBOOK_DB_MAX_OVERFLOW", "10")),
        "pool_recycle": int(os.environ.get("CLOCKBOOK_DB_POOL_RECYCLE_SECONDS", "300")),
        "pool_timeout": int(os.environ.get("CLOCKBOOK_DB_POOL_TIMEOUT_SECONDS", "30")),
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(Session, "do_orm_execute")
def _tenant_scope_orm(execute_state):
    """Defense-in-depth tenant isolation for ORM SELECT/UPDATE/DELETE statements.

    Authentication/bootstrap code deliberately runs before a tenant is selected. Once
    get_current_member resolves a session it stores tenant_id in Session.info, and every
    tenant-owned ORM statement on that request is automatically constrained thereafter.
    """
    tenant_id = execute_state.session.info.get("tenant_id")
    if not tenant_id or execute_state.session.info.get("skip_tenant_scope") or execute_state.execution_options.get("skip_tenant_scope"):
        return
    from models import TenantScopedMixin
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _tenant_guard_before_flush(session, flush_context, instances):
    tenant_id = session.info.get("tenant_id")
    if not tenant_id or session.info.get("skip_tenant_scope"):
        return
    from models import TenantScopedMixin
    for obj in session.new:
        if isinstance(obj, TenantScopedMixin):
            if not getattr(obj, "tenant_id", None):
                obj.tenant_id = tenant_id
            elif obj.tenant_id != tenant_id:
                raise ValueError("Cross-tenant insert blocked")
    for obj in session.dirty:
        if isinstance(obj, TenantScopedMixin) and getattr(obj, "tenant_id", None) != tenant_id:
            raise ValueError("Cross-tenant update blocked")
    for obj in session.deleted:
        if isinstance(obj, TenantScopedMixin) and getattr(obj, "tenant_id", None) != tenant_id:
            raise ValueError("Cross-tenant delete blocked")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_AUDIT_EXCLUDED_FIELDS = {
    "password_hash", "google_refresh_token", "token_hash", "token", "access_key", "api_key",
    "segments", "logo_data_url", "value", "note", "context"
}
_AUDIT_EXCLUDED_TYPES = {
    "AuditEvent", "Session", "RateLimitBucket", "GoogleOAuthState", "LoginEvent", "ClockStartEvent"
}

def _audit_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        text = value
        if isinstance(text, str) and len(text) > 500:
            return text[:500] + "…"
        return text
    return str(value)[:500]


@event.listens_for(Session, "before_flush", insert=True)
def _immutable_audit_before_flush(session, flush_context, instances):
    """Append-only mutation audit for authenticated requests.

    Sensitive credential/token fields and bulky timer segment payloads are deliberately
    excluded. AuditEvent itself has no mutation API, so this forms an application-level
    immutable ledger of business changes.
    """
    tenant_id = session.info.get("tenant_id")
    actor_id = session.info.get("actor_member_id")
    if not tenant_id or not actor_id or session.info.get("suppress_audit"):
        return
    from models import AuditEvent, TenantScopedMixin, TenantSetting

    # Audit records are append-only even inside application code. There is intentionally no
    # normal escape hatch for requests; maintenance scripts must opt in explicitly.
    if not session.info.get("allow_audit_maintenance"):
        for audit_row in list(session.dirty) + list(session.deleted):
            if isinstance(audit_row, AuditEvent):
                raise ValueError("Audit events are append-only")

    pending = []
    for obj, action in [(o, "create") for o in list(session.new)] + [(o, "update") for o in list(session.dirty)] + [(o, "delete") for o in list(session.deleted)]:
        if obj.__class__.__name__ in _AUDIT_EXCLUDED_TYPES or isinstance(obj, AuditEvent):
            continue
        if not isinstance(obj, TenantScopedMixin):
            continue
        state = sa_inspect(obj)
        changes = {}
        if action == "update":
            for attr in state.mapper.column_attrs:
                key = attr.key
                hist = state.attrs[key].history
                if key in _AUDIT_EXCLUDED_FIELDS:
                    # Tenant settings include integration credentials and other values that
                    # must never be copied into the audit ledger. Still record that the
                    # setting changed so integration/settings mutations remain auditable.
                    if isinstance(obj, TenantSetting) and key == "value" and hist.has_changes():
                        before = hist.deleted[0] if hist.deleted else None
                        after = hist.added[0] if hist.added else getattr(obj, key, None)
                        changes["setting_value_changed"] = {
                            "before_configured": bool(before),
                            "after_configured": bool(after),
                        }
                    continue
                if hist.has_changes():
                    changes[key] = {
                        "before": _audit_safe(hist.deleted[0] if hist.deleted else None),
                        "after": _audit_safe(hist.added[0] if hist.added else getattr(obj, key, None)),
                    }
            if not changes:
                continue
        else:
            # Keep create/delete records compact: identity plus non-sensitive scalar fields.
            for attr in state.mapper.column_attrs:
                key = attr.key
                if key in _AUDIT_EXCLUDED_FIELDS or key in {"tenant_id", "version"}:
                    continue
                value = getattr(obj, key, None)
                if isinstance(value, (type(None), bool, int, float, str)):
                    changes[key] = _audit_safe(value)
        pending.append(AuditEvent(
            tenant_id=tenant_id, actor_member_id=actor_id, action=action,
            entity_type=obj.__class__.__name__, entity_id=str(getattr(obj, "id", "") or "") or None,
            changes=changes,
        ))
    if pending:
        session.add_all(pending)
