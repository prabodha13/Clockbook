import os
from sqlalchemy import create_engine, event
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
