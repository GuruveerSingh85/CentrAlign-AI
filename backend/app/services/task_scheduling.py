import uuid
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks
from sqlalchemy import or_, update
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.core.database import engine as default_engine
from app.core.models import Task
from app.workflows.support.constants import (
    EXECUTION_HEARTBEAT_SECONDS,
    EXECUTION_LEASE_SECONDS,
    _scheduled_tasks,
    _task_lock,
)


def claim_task_execution(task_id: int, database_engine: Engine | None = None) -> str | None:
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with Session(database_engine or default_engine) as session:
        result = session.exec(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status.in_({
                    "pending", "planned", "waiting_for_input", "needs_input",
                    "running", "processing", "partially_completed",
                }),
                or_(
                    Task.execution_token.is_(None),
                    Task.execution_lease_until.is_(None),
                    Task.execution_lease_until <= now,
                ),
            )
            .values(
                status="running",
                execution_token=token,
                execution_lease_until=now + timedelta(seconds=EXECUTION_LEASE_SECONDS),
                updated_at=now,
            )
        )
        session.commit()
        return token if result.rowcount == 1 else None


def refresh_execution_lease(
    task_id: int,
    token: str,
    stop_event,
    database_engine: Engine | None = None,
) -> None:
    while not stop_event.wait(EXECUTION_HEARTBEAT_SECONDS):
        now = datetime.now(timezone.utc)
        try:
            with Session(database_engine or default_engine) as session:
                session.exec(
                    update(Task)
                    .where(Task.id == task_id, Task.execution_token == token)
                    .values(execution_lease_until=now + timedelta(seconds=EXECUTION_LEASE_SECONDS))
                )
                session.commit()
        except Exception:
            continue


def release_task_execution(
    task_id: int,
    token: str,
    database_engine: Engine | None = None,
) -> None:
    with Session(database_engine or default_engine) as session:
        session.exec(
            update(Task)
            .where(Task.id == task_id, Task.execution_token == token)
            .values(execution_token=None, execution_lease_until=None)
        )
        session.commit()


def schedule_task_workflow(
    background_tasks: BackgroundTasks,
    task_id: int,
    workflow_runner,
    context: dict | None = None,
    database_engine: Engine | None = None,
) -> bool:
    with _task_lock:
        if task_id in _scheduled_tasks:
            return False
        token = claim_task_execution(task_id, database_engine)
        if token is None:
            return False
        _scheduled_tasks.add(task_id)
    background_tasks.add_task(workflow_runner, task_id, context or {}, token)
    return True


def workflow_is_scheduled(task_id: int) -> bool:
    with _task_lock:
        return task_id in _scheduled_tasks


def forget_scheduled_task(task_id: int) -> None:
    with _task_lock:
        _scheduled_tasks.discard(task_id)