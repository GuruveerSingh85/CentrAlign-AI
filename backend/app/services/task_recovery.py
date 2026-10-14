import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.models import Approval, Task
from app.workflows.engine import _add_event, _save_state, _state_from_task, schedule_workflow

logger = logging.getLogger(__name__)


def _recover_interrupted_approvals(database_engine: Engine) -> None:
    now = datetime.now(timezone.utc)
    with Session(database_engine) as session:
        approvals = session.exec(
            select(Approval).where(Approval.status.in_({"executing", "executed", "failed"}))
        ).all()
        for approval in approvals:
            task = session.get(Task, approval.task_id)
            if task is None:
                continue
            state = _state_from_task(task)
            step_number = state.get("current_step")
            record = next((
                item for item in reversed(state.get("step_results", []))
                if item.get("step") == step_number
                and item.get("capability") == approval.capability
            ), None)
            if (
                record
                and record.get("success")
                and step_number in state.get("completed_steps", [])
            ):
                approval.status = "executed"
                _add_event(session, task.id, "approval_execution_recovered", "A completed approved action checkpoint was found after restart.", {
                    "approval_id": approval.id,
                    "capability": approval.capability,
                    "step": step_number,
                })
                session.add(approval)
                continue

            message = "The backend stopped while an approved action was executing; its outcome is unknown and it was not replayed."
            result = {
                "success": False,
                "error": message,
                "failure_type": "interrupted_outcome_unknown",
                "outcome_unknown": True,
                "approval_id": approval.id,
            }
            if record is None:
                record = {
                    "step": step_number,
                    "capability": approval.capability,
                    "inputs": {},
                    "started_at": now.isoformat(),
                }
                state.setdefault("step_results", []).append(record)
            record.update({
                "status": "failed",
                "success": False,
                "result": result,
                "outputs": result,
                "finished_at": now.isoformat(),
            })
            state["error"] = message
            state["final_summary"] = message
            if task.status != "cancelled":
                task.status = "partially_completed" if state.get("completed_steps") else "failed"
            approval.status = "outcome_unknown"
            approval.decided_at = now
            _save_state(task, state)
            _add_event(session, task.id, "approval_execution_interrupted", message, {
                "approval_id": approval.id,
                "capability": approval.capability,
                "step": step_number,
                "outcome_unknown": True,
            })
            session.add(approval)
            session.add(task)
        session.commit()


def schedule_startup_recovery(engine_provider: Callable[[], Engine]) -> BackgroundTasks:
    background_tasks = BackgroundTasks()
    database_engine = engine_provider()
    _recover_interrupted_approvals(database_engine)
    now = datetime.now(timezone.utc)
    with Session(database_engine) as session:
        task_ids = session.exec(
            select(Task.id).where(
                Task.status.in_({"running", "processing"}),
                or_(
                    Task.execution_token.is_(None),
                    Task.execution_lease_until.is_(None),
                    Task.execution_lease_until <= now,
                ),
            )
        ).all()
    for task_id in task_ids:
        schedule_workflow(background_tasks, task_id, database_engine=database_engine)
    return background_tasks


async def run_recovery_loop(
    engine_provider: Callable[[], Engine],
    poll_interval_seconds: float = 5,
) -> None:
    while True:
        await asyncio.sleep(poll_interval_seconds)
        try:
            recovery_tasks = schedule_startup_recovery(engine_provider)
            if recovery_tasks.tasks:
                await recovery_tasks()
        except Exception:
            logger.exception("Periodic task recovery scan failed.")