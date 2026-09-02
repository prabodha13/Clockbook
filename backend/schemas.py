from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class Segment(BaseModel):
    start: str
    end: Optional[str] = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    color_idx: int


class MemberCreate(BaseModel):
    name: str


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class ClientCreate(BaseModel):
    name: str


class TemplateTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    role: str
    task_type: str


class TemplateTaskCreate(BaseModel):
    name: str
    role: str = ""
    task_type: str = ""


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


class TaskCreate(BaseModel):
    client_id: str
    client_name: str
    name: str
    role: str = ""
    task_type: str = ""
    owner_id: Optional[str] = None


class TaskSubmit(BaseModel):
    note: str = ""


class TaskReassign(BaseModel):
    owner_id: str


class TaskStart(BaseModel):
    owner_id: str
