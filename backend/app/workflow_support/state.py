import json
from datetime import datetime, timezone

from sqlmodel import Session

from app.database import engine as default_engine
from app.models import ExecutionEvent, Task


def _session_engine():
    try:
        import app.workflow as workflow_module
    except ImportError:
        return default_engine
    return getattr(workflow_module, "engine", default_engine)


def _new_state() -> dict:
    return {
        "workflow": None,
        "inputs": {},
        "vendor": {},
        "completed_steps": [],
        "step_results": [],
        "recovery_attempts": [],
        "current_step": None,
        "missing_information": [],
        "approval_id": None,
        "verification": None,
        "final_summary": None,
        "timestamps": {"started_at": None, "completed_at": None},
        "replans": [],
        "cancellation_requested": False,
    }


def _state_from_task(task: Task) -> dict:
    if not task.result_json:
        return _new_state()
    try:
        state = json.loads(task.result_json)
    except (json.JSONDecodeError, TypeError):
        return _new_state()
    if not isinstance(state, dict) or "step_results" not in state:
        return _new_state()
    return {**_new_state(), **state}


def _save_state(task: Task, state: dict) -> None:
    state.setdefault("timestamps", {"started_at": None, "completed_at": None})
    task.result_json = json.dumps(state)
    task.updated_at = datetime.now(timezone.utc)


def _add_event(
    session: Session,
    task_id: int,
    event_type: str,
    message: str,
    details: dict | None = None,
) -> None:
    session.add(
        ExecutionEvent(
            task_id=task_id,
            event_type=event_type,
            message=message,
            details_json=json.dumps(details) if details is not None else None,
        )
    )


def _session() -> Session:
    return Session(_session_engine())
