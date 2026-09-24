from datetime import datetime, date
from typing import Optional, List, Literal
from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field, field_serializer


class BaseModel(PydanticBaseModel):
    # Requests reject unexpected fields instead of silently accepting browser-supplied data.
    model_config = ConfigDict(extra="forbid")


class Segment(BaseModel):
    start: str = Field(min_length=1, max_length=64)
    end: Optional[str] = Field(default=None, max_length=64)


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    tenant_id: Optional[str] = None
    name: str
    email: Optional[str] = None
    color_idx: int
    role: str
    pod_id: Optional[str] = None
    google_calendar_connected: bool = False
    slack_connected: bool = False
    slack_email: Optional[str] = None
    notification_channel: str = "browser"
    weekly_capacity_hours: float = 40.0
    capacity_effective_from: Optional[date] = None
    timezone_name: str = "Asia/Colombo"
    can_view_leave_capacity_insights: bool = False
    staff_tour_completed: bool = False


class PodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str


class SlackConnect(BaseModel):
    slack_email: str = Field(min_length=3, max_length=320)


class NotificationChannelUpdate(BaseModel):
    channel: Literal["browser", "slack"]


class PodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class KarbonIntegrationSave(BaseModel):
    application_id: str = Field(min_length=1, max_length=512)
    access_key: str = Field(min_length=1, max_length=512)
    expected_version: Optional[int] = Field(default=None, ge=0)


class CalamariIntegrationSave(BaseModel):
    tenant: str = Field(min_length=1, max_length=240)
    api_key: str = Field(min_length=1, max_length=512)
    expected_version: Optional[int] = Field(default=None, ge=0)


class KarbonReconciliationNoteSave(BaseModel):
    member_id: str = Field(min_length=1, max_length=128)
    date: date
    note: str = Field(default="", max_length=4000)


class MemberPodUpdate(BaseModel):
    pod_id: Optional[str] = Field(default=None, max_length=128)
    expected_version: int = Field(ge=1)


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class MemberRoleUpdate(BaseModel):
    role: Literal["member", "admin", "super_admin"]
    expected_version: int = Field(ge=1)


class MemberCapacityUpdate(BaseModel):
    weekly_capacity_hours: float = Field(ge=0, le=168)
    capacity_effective_from: Optional[date] = None
    expected_version: int = Field(ge=1)


class MemberTimezoneUpdate(BaseModel):
    timezone_name: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)


class MemberInsightsPermissionUpdate(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)


class StaffTourUpdate(BaseModel):
    completed: bool = True


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    tenant_id: Optional[str] = Field(default=None, max_length=128)


class LoginResponse(BaseModel):
    token: str
    member: MemberOut


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    slug: str
    status: str = "active"


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: Optional[str] = Field(default=None, max_length=160)


class WorkspaceBrandingUpdate(BaseModel):
    logo_data_url: Optional[str] = Field(default=None, max_length=500000)


class TenantInvitationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=320)
    role: Literal["member", "admin", "super_admin"] = "member"


class TenantInvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    name: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime


class TenantInvitationCreated(TenantInvitationOut):
    token: str
    email_status: str = "not_configured"
    email_error: Optional[str] = None
    provider_message_id: Optional[str] = None


class TenantInvitationPublic(BaseModel):
    workspace_name: str
    email: str
    name: str
    role: str
    existing_user: bool = False
    expires_at: datetime


class TenantInvitationAccept(BaseModel):
    password: str = Field(min_length=8, max_length=256)
    name: Optional[str] = Field(default=None, max_length=160)


class ClaimAccountRequest(BaseModel):
    member_id: Optional[str] = Field(default=None, max_length=128)
    name: Optional[str] = Field(default=None, max_length=160)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str
    code: Optional[str] = None


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    code: str = Field(min_length=1, max_length=80)
    expected_version: Optional[int] = Field(default=None, ge=1)


class ClientImportRow(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    code: str = Field(min_length=1, max_length=80)
    bank_accounts: List[str] = Field(default_factory=list, max_length=50)


class ClientImportRequest(BaseModel):
    rows: List[ClientImportRow] = Field(min_length=1, max_length=5000)


class ClientImportResult(BaseModel):
    imported_clients: int
    imported_bank_accounts: int


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TaskTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str
    is_billable: bool = False


class TaskTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    is_billable: bool = False


class TaskTypeBillingUpdate(BaseModel):
    is_billable: bool
    expected_version: int = Field(ge=1)


class TrackedMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str


class TrackedMetricCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    client_id: str
    name: str


class BankAccountCreate(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=240)


class TemplateTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str
    role: str
    task_type: str
    requires_bank_account: bool
    tracks_number_label: str
    needs_pay_period: bool = False
    period_types: List[str] = Field(default_factory=list, max_length=16)
    period_required: bool = False
    position: int = 0


class TemplateTaskCreate(BaseModel):
    expected_version: Optional[int] = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=240)
    role: str = Field(default="", max_length=160)
    task_type: str = Field(default="", max_length=160)
    requires_bank_account: bool = False
    tracks_number_label: str = Field(default="", max_length=160)
    needs_pay_period: bool = False
    period_types: List[str] = Field(default_factory=list, max_length=16)
    period_required: bool = False


class TemplateTaskReorder(BaseModel):
    task_ids: List[str] = Field(max_length=500)


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    field: str
    category: Optional[str] = None
    name: str
    tasks: List[TemplateTaskOut] = []


class TemplateCreate(BaseModel):
    field: str = Field(min_length=1, max_length=160)
    expected_version: Optional[int] = Field(default=None, ge=1)
    category: Optional[str] = Field(default=None, max_length=160)
    name: str = Field(min_length=1, max_length=240)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    client_name: str
    name: str
    role: str
    task_type: str
    helped_member_id: Optional[str] = None
    status: str
    owner_id: Optional[str] = None
    segments: List[Segment]
    note: str
    created_at: datetime
    submitted_at: Optional[datetime] = None
    submitted_by_id: Optional[str] = None
    pushed_to_karbon: bool
    bank_account_id: Optional[str] = None
    bank_account_name: str = ""
    tracks_number_label: str = ""
    start_count: Optional[int] = None
    end_count: Optional[int] = None
    adjusted_seconds: Optional[float] = None
    pay_period_type: Optional[str] = Field(default=None, max_length=32)
    pay_period_number: Optional[int] = Field(default=None, ge=1, le=53)
    needs_pay_period: bool = False
    period_types: List[str] = Field(default_factory=list, max_length=16)
    period_required: bool = False
    period_type: Optional[str] = Field(default=None, max_length=32)
    period_year: Optional[int] = Field(default=None, ge=1900, le=2100)
    period_number: Optional[int] = Field(default=None, ge=1, le=53)
    period_start: Optional[str] = Field(default=None, max_length=32)
    period_end: Optional[str] = Field(default=None, max_length=32)
    source_calendar_event_id: Optional[str] = Field(default=None, max_length=512)
    source_template_task_id: Optional[str] = Field(default=None, max_length=128)
    source_template_name: Optional[str] = Field(default=None, max_length=240)
    source_template_field: Optional[str] = Field(default=None, max_length=160)
    source_template_category: Optional[str] = Field(default=None, max_length=160)
    submitted_pod_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None

    @field_serializer("created_at", "submitted_at", "last_heartbeat_at")
    def serialize_as_utc(self, value: Optional[datetime], _info):
        # Stored as naive UTC in the database, this marks it as UTC for the browser
        # so it is not mistaken for local time
        if value is None:
            return None
        return value.isoformat() + "Z"


class TaskCreate(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    client_name: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=240)
    role: str = Field(default="", max_length=160)
    task_type: str = Field(default="", max_length=160)
    helped_member_id: Optional[str] = Field(default=None, max_length=128)
    owner_id: Optional[str] = Field(default=None, max_length=128)
    bank_account_id: Optional[str] = Field(default=None, max_length=128)
    bank_account_name: str = Field(default="", max_length=240)
    tracks_number_label: str = Field(default="", max_length=160)
    pay_period_type: Optional[str] = Field(default=None, max_length=32)
    pay_period_number: Optional[int] = Field(default=None, ge=1, le=53)
    needs_pay_period: bool = False
    period_types: List[str] = Field(default_factory=list, max_length=16)
    period_required: bool = False
    period_type: Optional[str] = Field(default=None, max_length=32)
    period_year: Optional[int] = Field(default=None, ge=1900, le=2100)
    period_number: Optional[int] = Field(default=None, ge=1, le=53)
    period_start: Optional[str] = Field(default=None, max_length=32)
    period_end: Optional[str] = Field(default=None, max_length=32)
    source_calendar_event_id: Optional[str] = Field(default=None, max_length=512)
    source_template_task_id: Optional[str] = Field(default=None, max_length=128)
    source_template_name: Optional[str] = Field(default=None, max_length=240)
    source_template_field: Optional[str] = Field(default=None, max_length=160)
    source_template_category: Optional[str] = Field(default=None, max_length=160)


class TaskPause(BaseModel):
    end_at: Optional[str] = Field(default=None, max_length=64)


class TaskPauseBeacon(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    end_at: Optional[str] = Field(default=None, max_length=64)


class TaskSubmit(BaseModel):
    note: str = Field(default="", max_length=4000)
    client_id: Optional[str] = Field(default=None, max_length=128)
    end_count: Optional[int] = Field(default=None, ge=0, le=2147483647)
    adjusted_seconds: Optional[float] = Field(default=None, ge=0, le=2678400)
    role: Optional[str] = Field(default=None, max_length=160)
    task_type: Optional[str] = Field(default=None, max_length=160)
    period_type: Optional[str] = Field(default=None, max_length=32)
    period_year: Optional[int] = Field(default=None, ge=1900, le=2100)
    period_number: Optional[int] = Field(default=None, ge=1, le=53)
    period_start: Optional[str] = Field(default=None, max_length=32)
    period_end: Optional[str] = Field(default=None, max_length=32)


class TaskStart(BaseModel):
    start_count: Optional[int] = Field(default=None, ge=0, le=2147483647)
    start_at: Optional[str] = Field(default=None, max_length=64)


class TaskReassign(BaseModel):
    owner_id: str = Field(min_length=1, max_length=128)


class AdHocMeetingCreate(BaseModel):
    colleague_id: Optional[str] = Field(default=None, max_length=128)


class AdHocMeetingFinish(BaseModel):
    colleague_id: Optional[str] = Field(default=None, max_length=128)
    interaction: Literal["general", "helped", "received"]
    context: str = Field(min_length=1, max_length=4000)


class QuickMeetingCreate(BaseModel):
    summary: str = Field(min_length=1, max_length=240)
    request_id: Optional[str] = Field(default=None, max_length=128)
    attendee_member_ids: List[str] = Field(default_factory=list, max_length=100)
    external_emails: List[str] = Field(default_factory=list, max_length=100)
    client_id: Optional[str] = Field(default=None, max_length=128)
    duration_minutes: int = Field(default=30, ge=1, le=720)


class CalendarEventCreate(BaseModel):
    summary: str = Field(min_length=1, max_length=240)
    start: str = Field(min_length=1, max_length=64)
    end: str = Field(min_length=1, max_length=64)
    all_day: bool = False
    attendee_member_ids: List[str] = Field(default_factory=list, max_length=100)
    external_emails: List[str] = Field(default_factory=list, max_length=100)
    create_meet: bool = False


class CalendarEventUpdate(BaseModel):
    summary: Optional[str] = Field(default=None, max_length=240)
    start: Optional[str] = Field(default=None, max_length=64)
    end: Optional[str] = Field(default=None, max_length=64)
    all_day: Optional[bool] = None


class HelpEventCreate(BaseModel):
    colleague_id: str = Field(min_length=1, max_length=128)
    direction: Literal["helped", "received"]
    seconds: float = Field(gt=0, le=43200)
    source: str = Field(default="idle_prompt", max_length=80)
    adjusted: bool = False
    context: str = Field(default="", max_length=4000)
    inactivity_event_id: Optional[str] = Field(default=None, max_length=128)


class HelpEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    member_id: str
    colleague_id: str
    direction: str
    seconds: float
    source: str
    created_at: datetime
    task_id: Optional[str] = None
    adjusted: bool = False
    context: str = ""
    inactivity_event_id: Optional[str] = None


class HelpSummaryRow(BaseModel):
    member_id: str
    member_name: str
    helped_seconds: float
    received_seconds: float
    helped_count: int
    received_count: int


class HelpEventDetail(BaseModel):
    id: str
    member_name: str
    colleague_name: str
    direction: str
    seconds: float
    created_at: datetime
    task_id: Optional[str] = None
    adjusted: bool = False
    context: str = ""

    @field_serializer("created_at")
    def serialize_as_utc(self, value: datetime, _info):
        return value.isoformat() + "Z"


class InactivityEventCreate(BaseModel):
    kind: Literal["screen_locked", "sleep_gap", "stale_gap"]
    started_at: datetime
    ended_at: datetime
    task_id: Optional[str] = Field(default=None, max_length=128)


class InactivityEventDetail(BaseModel):
    id: str
    member_id: str
    member_name: str
    kind: str
    started_at: datetime
    ended_at: datetime
    seconds: float  # unexplained inactivity after any linked help is deducted
    original_seconds: float = 0
    help_seconds: float = 0
    task_id: Optional[str] = None

    @field_serializer("started_at", "ended_at")
    def serialize_datetime_as_utc(self, value: datetime, _info):
        return value.isoformat() + "Z"


class InactivityAuditSettingUpdate(BaseModel):
    enabled: bool


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    actor_member_id: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    changes: dict
    created_at: datetime
