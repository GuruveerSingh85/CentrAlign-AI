from collections.abc import Callable

from fastapi import BackgroundTasks
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.models import Task
from app.workflows.engine import schedule_workflow


def schedule_startup_recovery(engine_provider: Callable[[], Engine]) -> BackgroundTasks:
    background_tasks = BackgroundTasks()
    with Session(engine_provider()) as session:
        task_ids = session.exec(
            select(Task.id).where(Task.status.in_({"running", "processing"}))
        ).all()
    for task_id in task_ids:
        schedule_workflow(background_tasks, task_id)
    return background_tasks