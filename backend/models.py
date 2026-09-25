import uuid
import secrets
from datetime import datetime, date
from sqlalchemy import Column, String, Boolean, DateTime, Date, ForeignKey, Integer, Float, JSON, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declared_attr

from database import Base


def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TenantScopedMixin:
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)


class VersionedMixin:
    """Optimistic-locking guard for mutable business records.

    SQLAlchemy includes the current version in UPDATE/DELETE statements and raises
    StaleDataError if another request changed the same row after it was loaded.
    """
    version = Column(Integer, nullable=False, default=1)

    @declared_attr
    def __mapper_args__(cls):
        return {"version_id_col": cls.version}


class Tenant(VersionedMixin, Base):
    __tablename__ = "tenants"
    id = Column(String, primary_key=True, default=lambda: gen_id("tenant"))
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class User(VersionedMixin, Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: gen_id("usr"))
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=True)
    default_tenant_id = Column(String, ForeignKey("tenants.id"), nullable=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SystemSetting(Base):
    # Deployment/global settings only. Tenant-owned settings live in TenantSetting.
    __tablename__ = "system_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False, default="")


class TenantSetting(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "tenant_settings"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_settings_tenant_key"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("tset"))
    key = Column(String, nullable=False)
    value = Column(String, nullable=False, default="")


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    key = Column(String, primary_key=True)
    window_start = Column(DateTime, nullable=False)
    count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Pod(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "pods"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_pods_tenant_name"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("pod"))
    name = Column(String, nullable=False)


class Member(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_members_tenant_user"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("mem"))
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    # Kept as a tenant-profile snapshot for current API/UI compatibility. Authentication
    # is performed against User; this field is synchronized when credentials change.
    email = Column(String, nullable=True, index=True)
    password_hash = Column(String, nullable=True)  # legacy compatibility; User is authoritative
    color_idx = Column(Integer, default=0)
    role = Column(String, default="member")
    pod_id = Column(String, ForeignKey("pods.id"), nullable=True)
    google_refresh_token = Column(String, nullable=True)
    slack_email = Column(String, nullable=True)
    slack_user_id = Column(String, nullable=True)
    notification_channel = Column(String, default="browser")
    weekly_capacity_hours = Column(Float, default=40.0)
    capacity_effective_from = Column(Date, default=date.today)
    timezone_name = Column(String, default="Asia/Colombo")
    can_view_leave_capacity_insights = Column(Boolean, default=False)
    staff_tour_completed = Column(Boolean, default=False)

    @property
    def google_calendar_connected(self):
        return bool(self.google_refresh_token)

    @property
    def slack_connected(self):
        return bool(self.slack_user_id)


class TenantInvitation(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "tenant_invitations"
    __table_args__ = (
        Index("ix_tenant_invitations_tenant_email", "tenant_id", "email"),
        Index("ix_tenant_invitations_token_hash", "token_hash", unique=True),
    )
    id = Column(String, primary_key=True, default=lambda: gen_id("inv"))
    email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="member")
    token_hash = Column(String, nullable=False)
    invited_by_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    @property
    def status(self):
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at < datetime.utcnow():
            return "expired"
        return "pending"


class AuditEvent(TenantScopedMixin, Base):
    __tablename__ = "audit_events"
    id = Column(String, primary_key=True, default=lambda: gen_id("audit"))
    actor_member_id = Column(String, ForeignKey("members.id"), nullable=True, index=True)
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(String, nullable=True, index=True)
    changes = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Session(TenantScopedMixin, Base):
    __tablename__ = "sessions"
    token = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class LoginEvent(TenantScopedMixin, Base):
    __tablename__ = "login_events"
    id = Column(String, primary_key=True, default=lambda: gen_id("login"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DailyPresenceEvent(TenantScopedMixin, Base):
    __tablename__ = "daily_presence_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "member_id", "work_date", name="uq_daily_presence_tenant_member_date"),
        Index("ix_daily_presence_tenant_member_date", "tenant_id", "member_id", "work_date"),
    )
    id = Column(String, primary_key=True, default=lambda: gen_id("presence"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    work_date = Column(Date, nullable=False)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ClockStartEvent(TenantScopedMixin, Base):
    __tablename__ = "clock_start_events"
    id = Column(String, primary_key=True, default=lambda: gen_id("clk"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class KarbonReconciliationNote(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "karbon_reconciliation_notes"
    id = Column(String, primary_key=True, default=lambda: gen_id("krn"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    work_date = Column(Date, nullable=False)
    note = Column(Text, nullable=False, default="")
    created_by_id = Column(String, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class GoogleOAuthState(TenantScopedMixin, Base):
    __tablename__ = "google_oauth_states"
    state = Column(String, primary_key=True, default=lambda: secrets.token_urlsafe(32))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    code_verifier = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DismissedSuggestion(TenantScopedMixin, Base):
    __tablename__ = "dismissed_suggestions"
    id = Column(String, primary_key=True, default=lambda: gen_id("dsm"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    calendar_event_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Client(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_clients_tenant_code"),
        Index("ix_clients_tenant_name", "tenant_id", "name"),
    )
    id = Column(String, primary_key=True, default=lambda: gen_id("cli"))
    name = Column(String, nullable=False)
    code = Column(String, nullable=True)


class BankAccount(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "bank_accounts"
    __table_args__ = (Index("ix_bank_accounts_tenant_client", "tenant_id", "client_id"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("bank"))
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    name = Column(String, nullable=False)


class Role(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("role"))
    name = Column(String, nullable=False)


class TaskTypeOption(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "task_type_options"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_task_types_tenant_name"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("tto"))
    name = Column(String, nullable=False)
    is_billable = Column(Boolean, nullable=False, default=False)


class LearningCategory(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "learning_categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_learning_categories_tenant_name"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("ldcat"))
    name = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)


class TrackedMetric(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "tracked_metrics"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_metrics_tenant_name"),)
    id = Column(String, primary_key=True, default=lambda: gen_id("metric"))
    name = Column(String, nullable=False)


class Template(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "templates"
    id = Column(String, primary_key=True, default=lambda: gen_id("tpl"))
    field = Column(String, nullable=False)
    category = Column(String, nullable=True)
    name = Column(String, nullable=False)
    tasks = relationship(
        "TemplateTask",
        backref="template",
        cascade="all, delete-orphan",
        order_by="TemplateTask.position, TemplateTask.created_at",
    )


class TemplateTask(TenantScopedMixin, VersionedMixin, Base):
    __tablename__ = "template_tasks"
    id = Column(String, primary_key=True, default=lambda: gen_id("tt"))
    template_id = Column(String, ForeignKey("templates.id"), nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, default="")
    task_type = Column(String, default="")
    requires_bank_account = Column(Boolean, default=False)
    tracks_number_label = Column(String, default="")
    needs_pay_period = Column(Boolean, default=False)
    period_types = Column(JSON, default=list)
    period_required = Column(Boolean, default=False)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskInstance(TenantScopedMixin, Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, default=lambda: gen_id("task"))
    client_id = Column(String, ForeignKey("clients.id"), nullable=False)
    client_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, default="")
    task_type = Column(String, default="")
    helped_member_id = Column(String, ForeignKey("members.id"), nullable=True)
    status = Column(String, default="todo")
    owner_id = Column(String, ForeignKey("members.id"), nullable=True)
    segments = Column(JSON, default=list)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime, nullable=True)
    submitted_by_id = Column(String, ForeignKey("members.id"), nullable=True)
    pushed_to_karbon = Column(Boolean, default=False)
    bank_account_id = Column(String, ForeignKey("bank_accounts.id"), nullable=True)
    bank_account_name = Column(String, default="")
    tracks_number_label = Column(String, default="")
    start_count = Column(Integer, nullable=True)
    end_count = Column(Integer, nullable=True)
    adjusted_seconds = Column(Float, nullable=True)
    pay_period_type = Column(String, nullable=True)
    pay_period_number = Column(Integer, nullable=True)
    needs_pay_period = Column(Boolean, default=False)
    period_types = Column(JSON, default=list)
    period_required = Column(Boolean, default=False)
    period_type = Column(String, nullable=True)
    period_year = Column(Integer, nullable=True)
    period_number = Column(Integer, nullable=True)
    period_start = Column(String, nullable=True)
    period_end = Column(String, nullable=True)
    source_calendar_event_id = Column(String, nullable=True)
    quick_meeting_request_id = Column(String, nullable=True)
    calendar_event_deleted_at = Column(DateTime, nullable=True)
    source_template_task_id = Column(String, nullable=True)
    source_template_name = Column(String, nullable=True)
    source_template_field = Column(String, nullable=True)
    source_template_category = Column(String, nullable=True)
    submitted_pod_id = Column(String, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)


class LearningRecord(TenantScopedMixin, Base):
    __tablename__ = "learning_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_learning_records_tenant_task"),
        Index("ix_learning_records_tenant_member_date", "tenant_id", "member_id", "learned_at"),
        Index("ix_learning_records_tenant_category_date", "tenant_id", "category", "learned_at"),
    )
    id = Column(String, primary_key=True, default=lambda: gen_id("ldr"))
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    member_id = Column(String, ForeignKey("members.id", ondelete="SET NULL"), nullable=True)
    member_name = Column(String, nullable=False, default="Unknown")
    category = Column(String, nullable=False)
    topic = Column(String, nullable=False)
    what_i_learned = Column(Text, nullable=False)
    tdm_references = Column(JSON, default=list)
    article_references = Column(JSON, default=list)
    duration_seconds = Column(Float, nullable=False, default=0.0)
    learned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class HelpEvent(TenantScopedMixin, Base):
    __tablename__ = "help_events"
    id = Column(String, primary_key=True, default=lambda: gen_id("help"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    colleague_id = Column(String, ForeignKey("members.id"), nullable=False)
    direction = Column(String, nullable=False)
    seconds = Column(Float, nullable=False)
    source = Column(String, default="idle_prompt")
    created_at = Column(DateTime, default=datetime.utcnow)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    adjusted = Column(Boolean, default=False)
    context = Column(Text, default="")
    inactivity_event_id = Column(String, nullable=True)


class InactivityEvent(TenantScopedMixin, Base):
    __tablename__ = "inactivity_events"
    id = Column(String, primary_key=True, default=lambda: gen_id("away"))
    member_id = Column(String, ForeignKey("members.id"), nullable=False)
    kind = Column(String, nullable=False)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    seconds = Column(Float, nullable=False)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
