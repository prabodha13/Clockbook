import uuid
import secrets
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Integer, Float, JSON, Text
from sqlalchemy.orm import relationship

from database import Base


def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Pod(Base):
    # A team/pod grouping for staff. When a regular admin is assigned to a pod, they only
    # see task and time data for people in that same pod, super admins always see everyone
    # regardless of pod, and an admin with no pod assigned keeps seeing everyone too.
    __tablename__ = "pods"
    id = Column(String, primary_key=True, default=lambda: gen_id("pod"))
    name = Column(String, nullable=False, unique=True)


class Member(Base):
    __tablename__ = "members"
    id = Column(String, primary_key=True, default=lambda: gen_id("mem"))
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=True)
    password_hash = Column(String, nullable=True)
    color_idx = Column(Integer, default=0)
    role = Column(String, default="member")  # "admin" or "member"
    pod_id = Column(String, ForeignKey("pods.id"), nullable=True)
    # Never returned by any API response, only a computed "connected" boolean is. This is
    # the one credential Google gives that keeps working long-term, used to fetch a fresh
    # short-lived access token each time a calendar check actually needs to happen.
    google_refresh_token = Column(String, nullable=True)

    @property
    def google_calendar_connected(self):
        return bool(self.google_refresh_token)


class Session(Base):
    __tablename__ = "sessions"
    token = Column(String, primary_key=True)
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class GoogleOAuthState(Base):
    # A short-lived, single-use marker created the moment someone clicks "Connect Calendar",
    # so that when Google redirects back with just a code and this same state value, and
    # nothing else identifying who they are, we can safely look up which member started it.
    # Consumed and deleted the moment it is used, so it cannot be replayed.
    __tablename__ = "google_oauth_states"
    state = Column(String, primary_key=True, default=lambda: secrets.token_urlsafe(32))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class DismissedSuggestion(Base):
    # Marks a calendar event as "not needed" for a specific person, purely a reminder they
    # chose to clear, never turned into a task. Kept separate from source_calendar_event_id
    # on tasks, since dismissing is the opposite action, deciding nothing should be created.
    __tablename__ = "dismissed_suggestions"
    id = Column(String, primary_key=True, default=lambda: gen_id("dsm"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    calendar_event_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Client(Base):
    __tablename__ = "clients"
    id = Column(String, primary_key=True, default=lambda: gen_id("cli"))
    name = Column(String, nullable=False)
    code = Column(String, nullable=True)


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
    needs_pay_period = Column(Boolean, default=False)  # asks which weekly/fortnightly/monthly period this covers
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
    pay_period_type = Column(String, nullable=True)  # "weekly", "fortnightly", or "monthly"
    pay_period_number = Column(Integer, nullable=True)  # 1-52, 1-26, or 1-12 respectively
    source_calendar_event_id = Column(String, nullable=True)  # ties this task back to the Google Calendar event it came from, so that event stops being suggested again once it has produced a task
    source_template_name = Column(String, nullable=True)  # which template this task came from, if any, kept in sync if that template is later renamed
