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


class PodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


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


class ClientCreate(BaseModel):
    name: str


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


class TemplateTaskCreate(BaseModel):
    name: str
    role: str = ""
    task_type: str = ""
    requires_bank_account: bool = False
    tracks_number_label: str = ""
    needs_pay_period: bool = False


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

    @field_serializer("created_at", "submitted_at")
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


class TaskPause(BaseModel):
    end_at: Optional[str] = None


class TaskSubmit(BaseModel):
    note: str = ""
    end_count: Optional[int] = None
    adjusted_seconds: Optional[float] = None


class TaskStart(BaseModel):
    start_count: Optional[int] = None


class TaskReassign(BaseModel):
    owner_id: str
