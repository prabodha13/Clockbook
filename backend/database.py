import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./clockbook.db")

# Railway hands out a URL starting with postgres://
# SQLAlchemy needs postgresql:// instead, so we adjust it here.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}

# Phase 5 operational hardening. pool_pre_ping prevents a stale Railway/Postgres
# connection from surfacing as an application error after a DB/network idle period.
# Pool sizing remains deployment-configurable and does not alter application logic.
engine_kwargs = {
    "connect_args": connect_args,
    "pool_pre_ping": True,
}
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
