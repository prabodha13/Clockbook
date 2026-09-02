import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Integer, Float, JSON, Text
from sqlalchemy.orm import relationship

from database import Base


def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Member(Base):
    __tablename__ = "members"
    id = Column(String, primary_key=True, default=lambda: gen_id("mem"))
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=True)
    password_hash = Column(String, nullable=True)
    color_idx = Column(Integer, default=0)
    role = Column(String, default="member")  # "admin" or "member"


class Session(Base):
    __tablename__ = "sessions"
    token = Column(String, primary_key=True)
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Client(Base):
    __tablename__ = "clients"
    id = Column(String, primary_key=True, default=lambda: gen_id("cli"))
    name = Column(String, nullable=False)


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id = Column(String, primary_key=True, default=lambda: gen_id("bank"))
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    name = Column(String, nullable=False)


class Role(Base):
    __tablename__ = "roles"
    id = Column(String, primary_key=True, default=lambda: gen_id("role"))
    name = Column(String, nullable=False, unique=True)


class TaskTypeOption(Base):
    __tablename__ = "task_type_options"
    id = Column(String, primary_key=True, default=lambda: gen_id("tto"))
    name = Column(String, nullable=False, unique=True)


class TrackedMetric(Base):
    __tablename__ = "tracked_metrics"
    id = Column(String, primary_key=True, default=lambda: gen_id("metric"))
    name = Column(String, nullable=False, unique=True)


class Template(Base):
    __tablename__ = "templates"
    id = Column(String, primary_key=True, default=lambda: gen_id("tpl"))
    field = Column(String, nullable=False)
    name = Column(String, nullable=False)
    tasks = relationship(
        "TemplateTask",
        backref="template",
        cascade="all, delete-orphan",
        order_by="TemplateTask.created_at",
    )


class TemplateTask(Base):
    __tablename__ = "template_tasks"
    id = Column(String, primary_key=True, default=lambda: gen_id("tt"))
    template_id = Column(String, ForeignKey("templates.id"), nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, default="")
    task_type = Column(String, default="")
    requires_bank_account = Column(Boolean, default=False)
    tracks_number_label = Column(String, default="")  # e.g. "Unreconciled transactions", blank means not tracked
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskInstance(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, default=lambda: gen_id("task"))
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    client_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, default="")
    task_type = Column(String, default="")
    status = Column(String, default="todo")  # todo, running, paused, submitted
    owner_id = Column(String, ForeignKey("members.id"), nullable=True)
    segments = Column(JSON, default=list)  # list of {"start": iso string, "end": iso string or null}
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime, nullable=True)
    submitted_by_id = Column(String, ForeignKey("members.id"), nullable=True)
    pushed_to_karbon = Column(Boolean, default=False)
    bank_account_id = Column(String, ForeignKey("bank_accounts.id"), nullable=True)
    bank_account_name = Column(String, default="")
    tracks_number_label = Column(String, default="")  # copied from the template task at creation time
    start_count = Column(Integer, nullable=True)
    end_count = Column(Integer, nullable=True)
    adjusted_seconds = Column(Float, nullable=True)  # only set when the person edits the tracked time at submit
