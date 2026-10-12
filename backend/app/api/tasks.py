import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from app.workflows import engine as workflow
from app.core.models import Approval, ExecutionEvent, Task
from app.core.security import contains_secret_material
from app.workflows.engine import schedule_workflow, workflow_is_scheduled


class TaskCreate(SQLModel):
    objective: str


class RunTaskRequest(SQLModel):
    vendor_name: str | None = None
    vendor_website: str | None = None
    registration_number: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


def _task_inputs(payload: RunTaskRequest) -> dict[str, Any]:
    inputs = {
        key: value
        for key, value in payload.inputs.items()
        if value not in (None, "")
    }
    for key, value in {
        "name": (payload.vendor_name or "").strip(),
        "website": (payload.vendor_website or "").strip(),
        "registration_number": (payload.registration_number or "").strip(),
    }.items():
        if value:
            inputs[key] = value
    if contains_secret_material(inputs):
        raise HTTPException(status_code=422, detail="Credential-like values cannot be stored in task inputs.")
    return inputs


def _merge_task_inputs(state: dict, inputs: dict[str, Any]) -> None:
    state["inputs"].update(inputs)
    state["vendor"].update({
        "name": inputs.get("name") or inputs.get("vendor_name") or state["vendor"].get("name"),
        "website": inputs.get("website") or inputs.get("vendor_website") or state["vendor"].get("website"),
        "registration_number": inputs.get("registration_number") or state["vendor"].get("registration_number"),
    })


def _owned_task(session: Session, task_id: int, owner_id: str):
    task = session.get(Task, task_id)
    return task if task is not None and task.owner_id == owner_id else None


def create_task_router(engine_provider: Callable[[], Engine]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/tasks", response_model=Task)
    def create_task(payload: TaskCreate, request: Request):
        objective = payload.objective.strip()
        if not objective:
            raise HTTPException(status_code=400, detail="Objective cannot be empty")
        if contains_secret_material(objective):
            raise HTTPException(status_code=422, detail="Credential-like values cannot be stored in task objectives.")

        with Session(engine_provider()) as session:
            task = Task(
                objective=objective,
                status="pending",
                owner_id=request.state.owner_id,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    @router.get("/api/tasks", response_model=list[Task])
    def list_tasks(request: Request):
        with Session(engine_provider()) as session:
            statement = (
                select(Task)
                .where(Task.owner_id == request.state.owner_id)
                .order_by(Task.created_at.desc())
            )
            return session.exec(statement).all()

    @router.post("/api/tasks/{task_id}/plan")
    def plan_task(
        task_id: int,
        request: Request,
        payload: RunTaskRequest = RunTaskRequest(),
    ):
        inputs = _task_inputs(payload)
        with Session(engine_provider()) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            if task.status not in {"pending", "planned", "waiting_for_input", "needs_input"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Task cannot be planned from status '{task.status}'.",
                )

            objective = task.objective
            owner_id = task.owner_id
            state = workflow._state_from_task(task)
            _merge_task_inputs(state, inputs)
            state.pop("error", None)
            state["final_summary"] = None
            workflow._save_state(task, state)
            workflow._add_event(session, task_id, "planning_started", "The backend is preparing a validated plan.", {
                "input_fields": sorted(inputs),
            })
            session.add(task)
            session.commit()

        try:
            with Session(engine_provider()) as session:
                memory_context = workflow.retrieve_context_safely(session, owner_id, objective)
            planner_plan = workflow._create_plan_with_memory(objective, inputs, memory_context)
            plan, next_workflow = workflow._validated_plan(objective, planner_plan)
        except Exception as exc:
            with Session(engine_provider()) as session:
                task = _owned_task(session, task_id, owner_id)
                if task is not None:
                    state = workflow._state_from_task(task)
                    message = f"Plan preview failed: {str(exc)}"
                    state["error"] = message
                    state["final_summary"] = message
                    workflow._save_state(task, state)
                    workflow._add_event(session, task_id, "plan_failed", message)
                    session.add(task)
                    session.commit()
            raise HTTPException(status_code=502, detail=f"Planning failed: {str(exc)}") from exc

        with Session(engine_provider()) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            if task.status not in {"pending", "planned", "waiting_for_input", "needs_input"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Task cannot be planned from status '{task.status}'.",
                )

            state = workflow._state_from_task(task)
            _merge_task_inputs(state, inputs)
            state["workflow"] = next_workflow
            state["missing_information"] = plan.get("missing_information", [])
            state["final_summary"] = (
                " ".join(plan.get("limitations", []))
                if next_workflow == "unsupported"
                else None
            )
            state.pop("error", None)
            task.plan_json = json.dumps(plan)
            task.status = (
                "unsupported" if next_workflow == "unsupported"
                else "waiting_for_input" if next_workflow == "needs_input"
                else "planned"
            )
            workflow._save_state(task, state)
            workflow._add_event(session, task_id, "plan_previewed", "A validated execution plan was prepared for review.", plan)
            task_status = task.status
            session.add(task)
            session.commit()

        return {"task_id": task_id, "status": task_status, "plan": plan}

    @router.get("/api/tasks/{task_id}/events")
    def get_task_events(task_id: int, request: Request):
        with Session(engine_provider()) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            events = session.exec(
                select(ExecutionEvent)
                .where(ExecutionEvent.task_id == task_id)
                .order_by(ExecutionEvent.created_at.asc())
            ).all()
            return {
                "task_id": task_id,
                "events": [
                    {
                        "id": event.id,
                        "event_type": event.event_type,
                        "message": event.message,
                        "details": json.loads(event.details_json) if event.details_json else None,
                        "created_at": event.created_at,
                    }
                    for event in events
                ],
            }

    @router.post("/api/tasks/{task_id}/run", status_code=202)
    def run_task(
        task_id: int,
        background_tasks: BackgroundTasks,
        request: Request,
        payload: RunTaskRequest = RunTaskRequest(),
    ):
        vendor_context = _task_inputs(payload)
        with Session(engine_provider()) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            if task.status not in {
                "pending", "planned", "waiting_for_input", "needs_input",
                "running", "processing", "partially_completed",
            }:
                raise HTTPException(
                    status_code=409,
                    detail=f"Task cannot run from status '{task.status}'.",
                )
            if task.status in {"running", "processing"} and workflow_is_scheduled(task_id):
                raise HTTPException(status_code=409, detail="This task is already running.")

            was_processing = task.status in {"running", "processing"}
            task.status = "running"
            task.updated_at = datetime.now(timezone.utc)
            session.add(task)
            session.commit()

        if not schedule_workflow(background_tasks, task_id, vendor_context):
            raise HTTPException(status_code=409, detail="This task is already running.")
        return {
            "task_id": task_id,
            "status": "running",
            "message": "Workflow resumed." if was_processing else "Workflow accepted.",
        }

    @router.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: int, request: Request):
        with Session(engine_provider()) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            if task.status in {"completed", "failed", "cancelled", "verified", "rejected", "unsupported"}:
                raise HTTPException(status_code=409, detail=f"Task cannot be cancelled from status '{task.status}'.")
            state = workflow._state_from_task(task)
            state["cancellation_requested"] = True
            state["final_summary"] = "Task cancellation was requested; completed step evidence is retained."
            task.status = "cancelled"
            approval_id = state.get("approval_id")
            if approval_id:
                approval = session.get(Approval, approval_id)
                if approval is not None and approval.status in {"pending", "approved"}:
                    approval.status = "cancelled"
                    approval.decided_at = datetime.now(timezone.utc)
                    session.add(approval)
            workflow._save_state(task, state)
            workflow._add_event(session, task_id, "workflow_cancelled", state["final_summary"], {
                "completed_steps": state.get("completed_steps", []),
                "current_step": state.get("current_step"),
            })
            session.add(task)
            session.commit()
            return {"task_id": task_id, "status": "cancelled", "completed_steps": state.get("completed_steps", [])}

    return router