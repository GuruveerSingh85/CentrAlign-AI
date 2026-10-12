
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import SQLModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: str = Field(default="default", index=True)
    objective: str
    status: str = Field(default="pending", index=True)
    plan_json: Optional[str] = None
    result_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Memory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: str = Field(index=True)
    category: str = Field(index=True)
    kind: str = Field(index=True)
    content: str
    memory_key: Optional[str] = Field(default=None, index=True)
    source: str = Field(default="user")
    source_ref: Optional[str] = None
    verification_status: str = Field(default="unverified", index=True)
    verified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class Vendor(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    website: Optional[str] = None
    registration_number: Optional[str] = Field(
        default=None,
        unique=True,
        index=True,
    )
    status: str = Field(default="registered")
    created_at: datetime = Field(default_factory=utc_now)


class Approval(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    action_json: str
    execution_inputs_json: Optional[str] = None
    capability: Optional[str] = Field(default=None, index=True)
    action_hash: Optional[str] = None
    risk_json: Optional[str] = None
    status: str = Field(default="pending", index=True)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    expires_at: Optional[datetime] = Field(default=None, index=True)


class ExecutionEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    event_type: str
    message: str
    details_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
