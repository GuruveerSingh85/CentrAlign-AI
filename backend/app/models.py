
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import SQLModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    objective: str
    status: str = Field(default="pending", index=True)
    plan_json: Optional[str] = None
    result_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
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
    status: str = Field(default="pending", index=True)
    decided_at: Optional[datetime] = None


class ExecutionEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    event_type: str
    message: str
    details_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
