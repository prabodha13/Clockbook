from datetime import datetime, date, timezone
from typing import Optional, List, Literal
from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field, field_serializer


class BaseModel(PydanticBaseModel):
    # Requests reject unexpected fields instead of silently accepting browser-supplied data.
    model_config = ConfigDict(extra="forbid")


class Segment(BaseModel):
    start: str = Field(min_length=1, max_length=64)
    end: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=64)
    recovered_seconds: Optional[float] = Field(default=None, ge=0, le=28800)
    # Audit metadata used by split forgotten-time recovery. These fields are output-safe
    # metadata only; normal timer segments simply leave them null.
    recovery_batch_id: Optional[str] = Field(default=None, max_length=128)
    recovery_allocation_index: Optional[int] = Field(default=None, ge=0, le=1000)
    recovery_total_seconds: Optional[float] = Field(default=None, ge=0, le=86400)


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
    timezone_name: Optional[str] = None
    work_arrangement: Literal["office", "remote"] = "office"
    can_view_leave_capacity_insights: bool = False
    additional_permissions: List[str] = Field(default_factory=list)
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
    timezone_name: Optional[str] = Field(default=None, max_length=80)
    expected_version: int = Field(ge=1)


class InitialTimezoneSet(BaseModel):
    timezone_name: str = Field(min_length=1, max_length=80)


class MemberWorkArrangementUpdate(BaseModel):
    work_arrangement: Literal["office", "remote"]
    expected_version: int = Field(ge=1)


class MemberInsightsPermissionUpdate(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)


class MemberAdditionalPermissionsUpdate(BaseModel):
    permissions: List[str] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=1)


class StaffTourUpdate(BaseModel):
    completed: bool = True


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    tenant_id: Optional[str] = Field(default=None, max_length=128)
    # Browser/device instance is used only to keep one active ClockBook instance per user.
    # takeover is explicit so a second browser cannot silently kick out the first one.
    instance_id: Optional[str] = Field(default=None, max_length=128)
    takeover: bool = False


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


class LearningCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int = 1
    name: str
    is_active: bool = True


class LearningCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class LearningCategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    is_active: Optional[bool] = None
    expected_version: int = Field(ge=1)


class LearningReference(BaseModel):
    title: str = Field(default="", max_length=240)
    url: str = Field(min_length=1, max_length=2000)


class LearningLibraryRecordOut(BaseModel):
    topic: str
    category: str
    what_i_learned: str
    member_name: str
    learned_at: datetime


class LearningLibraryPersonOut(BaseModel):
    member_id: str
    member_name: str
    relevant_count: int
    latest_at: datetime
    records: List[LearningLibraryRecordOut]


class LearningManagementRecordOut(BaseModel):
    id: str
    task_id: str
    member_id: Optional[str] = None
    member_name: str
    learned_at: datetime
    duration_seconds: float
    category: str
    topic: str
    what_i_learned: str
    tdm_references: List[LearningReference] = Field(default_factory=list)
    article_references: List[LearningReference] = Field(default_factory=list)


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
    client_id: str = Field(default="", max_length=128)
    client_name: str = Field(default="", max_length=240)
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
    learning_category: Optional[str] = Field(default=None, max_length=160)
    learning_topic: Optional[str] = Field(default=None, max_length=240)
    what_i_learned: Optional[str] = Field(default=None, max_length=8000)
    tdm_references: List[LearningReference] = Field(default_factory=list, max_length=50)
    article_references: List[LearningReference] = Field(default_factory=list, max_length=50)
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


class TaskRecoverTime(BaseModel):
    seconds: float = Field(gt=0, le=28800)
    window_end_at: Optional[datetime] = None


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


class DelegationSuggestionExclusionsUpdate(BaseModel):
    exclusions: List[str] = Field(default_factory=list, max_length=100)


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    actor_member_id: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    changes: dict
    created_at: datetime


class TimeIntegrityTaskSegment(BaseModel):
    segment_index: int
    started_at: datetime
    ended_at: datetime
    seconds: float = 0.0
    source: str = "timer"
    clipped_to_recorded_at: bool = False

    @field_serializer("started_at", "ended_at")
    def serialize_segment_utc(self, value: datetime, _info):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"


class TimeIntegrityAuditRow(BaseModel):
    id: str
    entry_group_id: str
    task_id: Optional[str] = None
    member_id: str
    staff_member: str
    pod_id: Optional[str] = None
    work_date: date
    client_id: Optional[str] = None
    client: str
    task: str
    entry_source: Literal["Automatic", "Recovery", "Raw Manual", "Manual Adjustment"]
    manual_duration_seconds: float = 0.0
    recorded_at: datetime
    recorded_timezone_name: str = "UTC"
    task_started_at: Optional[datetime] = None
    task_ended_at: Optional[datetime] = None
    timer_segments: List[TimeIntegrityTaskSegment] = Field(default_factory=list)
    net_active_presence_seconds: float = 0.0
    automatically_tracked_seconds: float = 0.0
    recovered_allocated_seconds: float = 0.0
    prior_manual_allocated_seconds: float = 0.0
    available_unallocated_active_seconds: float = 0.0
    unreconciled_manual_seconds: float = 0.0
    later_edited: bool = False
    original_value_seconds: float = 0.0
    current_value_seconds: float = 0.0
    last_edited_at: Optional[datetime] = None
    reason_note: str = ""
    review_status: str
    entry_timing: Literal["Same day", "Later day"]
    recovery_batch_id: Optional[str] = None
    recovery_allocation_index: Optional[int] = None

    @field_serializer("recorded_at", "last_edited_at", "task_started_at", "task_ended_at")
    def serialize_time_integrity_utc(self, value: Optional[datetime], _info):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"


class TrackedTotalBrowserSegment(BaseModel):
    segment_index: int = Field(ge=1, le=10000)
    started_at: str = Field(min_length=1, max_length=64)
    ended_at: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=64)
    seconds: float = Field(default=0.0, ge=0, le=172800)


class TrackedTotalBrowserTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    task_name: str = Field(default="", max_length=500)
    client_name: str = Field(default="", max_length=500)
    seconds: float = Field(default=0.0, ge=0, le=172800)
    segments: List[TrackedTotalBrowserSegment] = Field(default_factory=list, max_length=500)


class TrackedTotalCheckIn(BaseModel):
    captured_at: datetime
    timezone_name: str = Field(min_length=1, max_length=100)
    browser_total_seconds: float = Field(default=0.0, ge=0, le=172800)
    tasks: List[TrackedTotalBrowserTask] = Field(default_factory=list, max_length=1000)


class TrackedTotalDiagnosticSegment(BaseModel):
    segment_index: int
    started_at: str
    ended_at: Optional[str] = None
    source: Optional[str] = None
    seconds: float = 0.0
    issue: Literal["missing_in_browser", "different_end", "browser_only"]
    browser_ended_at: Optional[str] = None
    browser_seconds: Optional[float] = None


class TrackedTotalDiagnosticTask(BaseModel):
    task_id: str
    task_name: str = ""
    client_name: str = ""
    browser_seconds: float = 0.0
    server_seconds: float = 0.0
    difference_seconds: float = 0.0
    segment_differences: List[TrackedTotalDiagnosticSegment] = Field(default_factory=list)


class TrackedTotalDiagnosticRow(BaseModel):
    id: str
    member_id: str
    staff_member: str
    work_date: date
    captured_at: datetime
    timezone_name: str = "UTC"
    browser_total_seconds: float = 0.0
    server_total_seconds: float = 0.0
    difference_seconds: float = 0.0
    task_differences: List[TrackedTotalDiagnosticTask] = Field(default_factory=list)

    @field_serializer("captured_at")
    def serialize_captured_at_utc(self, value: datetime, _info):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"


class TimeIntegrityAuditSummary(BaseModel):
    total_manual_seconds: float = 0.0
    total_unreconciled_manual_seconds: float = 0.0
    flagged_entries: int = 0
    later_edited_entries: int = 0
    average_delay_days: float = 0.0
    repeated_unreconciled_staff: int = 0
    repeated_unreconciled_staff_names: List[str] = Field(default_factory=list)


class TimeIntegrityAuditResponse(BaseModel):
    rows: List[TimeIntegrityAuditRow] = Field(default_factory=list)
    summary: TimeIntegrityAuditSummary
    tracked_total_diagnostics: List[TrackedTotalDiagnosticRow] = Field(default_factory=list)

