from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, field_serializer


class Segment(BaseModel):
    start: str
    end: Optional[str] = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: Optional[str] = None
    color_idx: int
    role: str
    pod_id: Optional[str] = None
    google_calendar_connected: bool = False
    slack_connected: bool = False
    slack_email: Optional[str] = None
    notification_channel: str = "browser"


class PodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class SlackConnect(BaseModel):
    slack_email: str


class NotificationChannelUpdate(BaseModel):
    channel: str  # "browser" or "slack"


class PodCreate(BaseModel):
    name: str


class MemberPodUpdate(BaseModel):
    pod_id: Optional[str] = None


class MemberCreate(BaseModel):
    name: str
    email: str
    password: str


class MemberRoleUpdate(BaseModel):
    role: str


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    member: MemberOut


class ClaimAccountRequest(BaseModel):
    member_id: Optional[str] = None
    name: Optional[str] = None
    email: str
    password: str


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: Optional[str] = None


class ClientCreate(BaseModel):
    name: str
    code: Optional[str] = None


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class RoleCreate(BaseModel):
    name: str


class TaskTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class TaskTypeCreate(BaseModel):
    name: str


class TrackedMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class TrackedMetricCreate(BaseModel):
    name: str


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    name: str


class BankAccountCreate(BaseModel):
    client_id: str
    name: str


class TemplateTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    role: str
    task_type: str
    requires_bank_account: bool
    tracks_number_label: str
    needs_pay_period: bool = False
    position: int = 0


class TemplateTaskCreate(BaseModel):
    name: str
    role: str = ""
    task_type: str = ""
    requires_bank_account: bool = False
    tracks_number_label: str = ""
    needs_pay_period: bool = False


class TemplateTaskReorder(BaseModel):
    task_ids: List[str]


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    field: str
    name: str
    tasks: List[TemplateTaskOut] = []


class TemplateCreate(BaseModel):
    field: str
    name: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    client_name: str
    name: str
    role: str
    task_type: str
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
    pay_period_type: Optional[str] = None
    pay_period_number: Optional[int] = None
    source_calendar_event_id: Optional[str] = None
    source_template_name: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None

    @field_serializer("created_at", "submitted_at", "last_heartbeat_at")
    def serialize_as_utc(self, value: Optional[datetime], _info):
        # Stored as naive UTC in the database, this marks it as UTC for the browser
        # so it is not mistaken for local time
        if value is None:
            return None
        return value.isoformat() + "Z"


class TaskCreate(BaseModel):
    client_id: str
    client_name: str
    name: str
    role: str = ""
    task_type: str = ""
    owner_id: Optional[str] = None
    bank_account_id: Optional[str] = None
    bank_account_name: str = ""
    tracks_number_label: str = ""
    pay_period_type: Optional[str] = None
    pay_period_number: Optional[int] = None
    source_calendar_event_id: Optional[str] = None
    source_template_name: Optional[str] = None


class TaskPause(BaseModel):
    end_at: Optional[str] = None


class TaskPauseBeacon(BaseModel):
    token: str
    end_at: Optional[str] = None


class TaskSubmit(BaseModel):
    note: str = ""
    client_id: Optional[str] = None
    end_count: Optional[int] = None
    adjusted_seconds: Optional[float] = None
    role: Optional[str] = None
    task_type: Optional[str] = None


class TaskStart(BaseModel):
    start_count: Optional[int] = None
    start_at: Optional[str] = None


class TaskReassign(BaseModel):
    owner_id: str


class AdHocMeetingCreate(BaseModel):
    colleague_id: Optional[str] = None


class AdHocMeetingFinish(BaseModel):
    colleague_id: Optional[str] = None
    interaction: str  # "general", "helped", or "received"
    context: str


class QuickMeetingCreate(BaseModel):
    summary: str
    attendee_member_ids: List[str] = []
    external_emails: List[str] = []
    client_id: Optional[str] = None
    duration_minutes: int = 30


class HelpEventCreate(BaseModel):
    colleague_id: str
    direction: str  # "helped" or "received"
    seconds: float
    source: str = "idle_prompt"
    adjusted: bool = False
    context: str
    inactivity_event_id: Optional[str] = None


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
    kind: str
    started_at: datetime
    ended_at: datetime
    task_id: Optional[str] = None


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
