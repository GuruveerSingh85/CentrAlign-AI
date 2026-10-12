
from contextlib import asynccontextmanager
import hmac

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import Field, Session, SQLModel, select

import json
import os
from typing import Any
from app.database import engine
from app import models
from app import memory_api
from app import workflow
from app.models import Task
from app.agent import create_plan
from app.tool_registry import execute_tool, read_company_policy
from app.models import ExecutionEvent
from datetime import datetime, timezone
from app.models import Vendor, Approval
from app.approval_control import approval_is_expired
from app.security import contains_secret_material
from app.workflow import (
    decide_vendor_approval,
    schedule_workflow,
    workflow_is_scheduled,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import create_db_and_tables

    create_db_and_tables(engine)
    yield


app = FastAPI(
    title="CentrAlign AI Operator",
    description="Plan, execute, and verify company tasks.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(memory_api.router)

frontend_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in frontend_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_unified_workflow(request: Request, call_next):
    path = request.url.path
    expected_token = os.getenv("API_BEARER_TOKEN")
    if (
        expected_token
        and path.startswith("/api/")
        and path != "/api/health"
        and request.method != "OPTIONS"
    ):
        scheme, _, supplied_token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied_token, expected_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "A valid bearer token is required."},
                headers={"WWW-Authenticate": "Bearer"},
            )

    trusted_user_header = os.getenv("TRUSTED_USER_HEADER", "").strip()
    identity_required = (
        trusted_user_header
        and path.startswith("/api/")
        and path != "/api/health"
        and request.method != "OPTIONS"
    )
    if identity_required:
        owner_id = request.headers.get(trusted_user_header, "").strip()
        if not owner_id or len(owner_id) > 128 or not all(
            character.isalnum() or character in "._@-" for character in owner_id
        ):
            return JSONResponse(status_code=401, content={"detail": "A trusted user identity is required."})
        request.state.owner_id = owner_id
    else:
        request.state.owner_id = "default"

    manual_workflow_action = path.startswith("/api/legacy/") or (
        path.startswith("/api/tasks/")
        and path.rsplit("/", 1)[-1] in {
            "plan",
            "execute-policy",
            "execute",
            "vendor-proposal",
            "verify",
        }
    )
    if request.method == "POST" and manual_workflow_action:
        return JSONResponse(
            status_code=410,
            content={"detail": "This action is managed by POST /api/tasks/{task_id}/run."},
        )
    return await call_next(request)


class TaskCreate(SQLModel):
    objective: str


class VendorProposal(SQLModel):
    name: str
    website: str
    registration_number: str | None = None



class RunTaskRequest(SQLModel):
    vendor_name: str | None = None
    vendor_website: str | None = None
    registration_number: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


def _owned_task(session: Session, task_id: int, owner_id: str):
    task = session.get(Task, task_id)
    return task if task is not None and task.owner_id == owner_id else None


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "CentrAlign AI Operator"}


@app.post("/api/tasks", response_model=Task)
def create_task(payload: TaskCreate, request: Request):
    objective = payload.objective.strip()

    if not objective:
        raise HTTPException(status_code=400, detail="Objective cannot be empty")
    if contains_secret_material(objective):
        raise HTTPException(status_code=422, detail="Credential-like values cannot be stored in task objectives.")

    with Session(engine) as session:
        task = Task(
            objective=objective,
            status="pending",
            owner_id=request.state.owner_id,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


@app.get("/api/tasks", response_model=list[Task])
def list_tasks(background_tasks: BackgroundTasks, request: Request):
    with Session(engine) as session:
        statement = (
            select(Task)
            .where(Task.owner_id == request.state.owner_id)
            .order_by(Task.created_at.desc())
        )
        tasks = session.exec(statement).all()

    for task in tasks:
        if task.status in {"running", "processing"} and not workflow_is_scheduled(task.id):
            schedule_workflow(background_tasks, task.id)
    return tasks

@app.post("/api/tasks/{task_id}/plan")
def plan_task(task_id: int, request: Request):
    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        objective = task.objective

    try:
        plan = create_plan(objective)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Planning failed: {str(exc)}",
        ) from exc

    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        task.plan_json = json.dumps(plan)
        task.status = "planned"
        session.add(task)
        session.commit()

    return {
        "task_id": task_id,
        "status": "planned",
        "plan": plan,
    }


@app.post("/api/tasks/{task_id}/execute-policy")
def execute_policy(task_id: int, request: Request):
    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if not task:
            raise HTTPException(
                status_code=404,
                detail="Task not found"
            )

        # Execute the registered policy-reading tool
        result = execute_tool("read_company_policy")

        # Record what actually happened
        event = ExecutionEvent(
            task_id=task.id,
            event_type="tool_execution",
            message=(
                "Procurement policy read successfully"
                if result.get("success")
                else "Failed to read procurement policy"
            ),
            details_json=json.dumps(result)
        )

        session.add(event)

        # Save the observed result on the task
        task.result_json = json.dumps(result)
        task.status = (
            "executed"
            if result.get("success")
            else "needs_input"
        )

        session.add(task)
        session.commit()

        return {
            "task_id": task.id,
            "status": task.status,
            "result": result
        }


@app.post("/api/tasks/{task_id}/execute")
def execute_task_plan(task_id: int, request: Request):
    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        if not task.plan_json:
            raise HTTPException(
                status_code=400,
                detail="Task has no plan. Plan it first.",
            )

        try:
            plan = json.loads(task.plan_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Saved task plan is invalid JSON.",
            ) from exc

        steps = plan.get("steps", [])

        if not steps:
            raise HTTPException(
                status_code=400,
                detail="The plan contains no executable steps.",
            )

        results = []

        for step in steps:
            capability = step.get("capability")
            step_number = step.get("step")
            action = step.get("action", "")

            # Require human approval before sensitive actions.
            if step.get("requires_approval", False):
                event = ExecutionEvent(
                    task_id=task.id,
                    event_type="approval_required",
                    message=(
                        f"Step {step_number} requires human approval."
                    ),
                    details_json=json.dumps({
                        "capability": capability,
                        "action": action,
                    }),
                )
                session.add(event)

                task.status = "awaiting_approval"
                session.add(task)
                session.commit()

                return {
                    "task_id": task.id,
                    "status": task.status,
                    "results": results,
                    "approval_required": {
                        "step": step_number,
                        "capability": capability,
                        "action": action,
                    },
                }

            # Only capabilities explicitly registered in Python can run.
            if not isinstance(capability, str):
                result = {
                    "success": False,
                    "error": "Step has no valid capability.",
                    "requires_input": True,
                }
            else:
                result = execute_tool(capability)

            results.append({
                "step": step_number,
                "capability": capability,
                "result": result,
            })

            event = ExecutionEvent(
                task_id=task.id,
                event_type="tool_execution",
                message=(
                    f"Step {step_number} succeeded."
                    if result.get("success")
                    else f"Step {step_number} failed."
                ),
                details_json=json.dumps({
                    "capability": capability,
                    "action": action,
                    "result": result,
                }),
            )
            session.add(event)

            # Stop immediately if the tool fails.
            if not result.get("success"):
                task.status = "needs_input"
                task.result_json = json.dumps(results)
                session.add(task)
                session.commit()

                return {
                    "task_id": task.id,
                    "status": task.status,
                    "results": results,
                }

        task.status = "executed"
        task.result_json = json.dumps(results)
        session.add(task)
        session.commit()

        return {
            "task_id": task.id,
            "status": task.status,
            "results": results,
        }


@app.post("/api/tasks/{task_id}/vendor-proposal")
def propose_vendor(task_id: int, payload: VendorProposal, request: Request):
    name = payload.name.strip()
    website = payload.website.strip()
    registration_number = (
        payload.registration_number.strip()
        if payload.registration_number
        else None
    )

    if not name or not website:
        raise HTTPException(
            status_code=400,
            detail="Vendor legal name and website are required.",
        )

    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found.",
            )

        if registration_number:
            duplicate = session.exec(
                select(Vendor).where(
                    Vendor.registration_number == registration_number
                )
            ).first()

            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail="Registration number already exists.",
                )

        proposal = {
            "name": name,
            "website": website,
            "registration_number": registration_number,
        }

        approval = Approval(
            task_id=task_id,
            action_json=json.dumps(proposal),
            status="pending",
        )

        session.add(approval)
        task.status = "awaiting_approval"
        session.add(task)
        session.commit()
        session.refresh(approval)

        return {
            "approval_id": approval.id,
            "task_id": task_id,
            "status": "awaiting_approval",
            "proposed_vendor": proposal,
            "message": "Vendor registration requires human approval.",
        }


@app.post("/api/legacy/approvals/{approval_id}/decide")
def decide_approval(approval_id: int, decision: str):
    decision = decision.strip().lower()

    if decision not in {"approve", "reject"}:
        raise HTTPException(
            status_code=400,
            detail="Decision must be 'approve' or 'reject'.",
        )

    with Session(engine) as session:
        approval = session.get(Approval, approval_id)

        if approval is None:
            raise HTTPException(
                status_code=404,
                detail="Approval request not found.",
            )

        if approval.status != "pending":
            raise HTTPException(
                status_code=409,
                detail="This approval has already been decided.",
            )

        task = session.get(Task, approval.task_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Associated task not found.",
            )

        if decision == "reject":
            approval.status = "rejected"
            approval.decided_at = datetime.now(timezone.utc)
            task.status = "rejected"

            session.add(approval)
            session.add(task)
            session.commit()

            return {
                "approval_id": approval.id,
                "status": "rejected",
                "message": "Vendor registration was not performed.",
            }

        try:
            proposal = json.loads(approval.action_json)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Saved vendor proposal is invalid.",
            )

        name = proposal.get("name", "").strip()
        website = proposal.get("website", "").strip()
        registration_number = proposal.get("registration_number")

        if not name or not website:
            raise HTTPException(
                status_code=400,
                detail="Vendor legal name and website are required.",
            )

        if registration_number:
            registration_number = registration_number.strip() or None

        # Recheck duplicates immediately before registration.
        if registration_number:
            duplicate = session.exec(
                select(Vendor).where(
                    Vendor.registration_number == registration_number
                )
            ).first()

            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail="Registration number already exists.",
                )

        vendor = Vendor(
            name=name,
            website=website,
            registration_number=registration_number,
            status="registered",
        )

        session.add(vendor)

        # Persist the vendor before retrieving it for verification.
        session.flush()
        vendor_id = vendor.id
        session.commit()

        saved_vendor = session.get(Vendor, vendor_id)

        if saved_vendor is None:
            task.status = "verification_failed"
            session.add(task)
            session.commit()

            raise HTTPException(
                status_code=500,
                detail="Vendor was saved but could not be retrieved for verification.",
            )

        verified = (
            saved_vendor.name == name
            and saved_vendor.website == website
            and saved_vendor.registration_number == registration_number
            and saved_vendor.status == "registered"
        )

        approval.status = "approved"
        approval.decided_at = datetime.now(timezone.utc)
        task.status = "completed" if verified else "verification_failed"

        event = ExecutionEvent(
            task_id=task.id,
            event_type="vendor_verification",
            message=(
                "Vendor record verified successfully."
                if verified
                else "Vendor record verification failed."
            ),
            details_json=json.dumps({
                "vendor_id": vendor_id,
                "verified": verified,
            }),
        )

        session.add(approval)
        session.add(task)
        session.add(event)
        session.commit()

        return {
            "approval_id": approval.id,
            "approval_status": approval.status,
            "task_id": task.id,
            "task_status": task.status,
            "vendor_id": vendor_id,
            "verified": verified,
            "vendor": {
                "name": saved_vendor.name,
                "website": saved_vendor.website,
                "registration_number": saved_vendor.registration_number,
                "status": saved_vendor.status,
            },
        }


@app.get("/api/tasks/{task_id}/events")
def get_task_events(task_id: int, request: Request):
    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found.",
            )

        statement = (
            select(ExecutionEvent)
            .where(ExecutionEvent.task_id == task_id)
            .order_by(ExecutionEvent.created_at.asc())
        )

        events = session.exec(statement).all()

        return {
            "task_id": task_id,
            "events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "message": event.message,
                    "details": (
                        json.loads(event.details_json)
                        if event.details_json
                        else None
                    ),
                    "created_at": event.created_at,
                }
                for event in events
            ],
        }


@app.get("/api/approvals")
def list_approvals(request: Request, status: str = "pending"):
    with Session(engine) as session:
        if status == "pending":
            expired = session.exec(
                select(Approval).join(Task).where(
                    Approval.status == "pending",
                    Task.owner_id == request.state.owner_id,
                )
            ).all()
            for approval in expired:
                if approval_is_expired(approval):
                    approval.status = "expired"
                    approval.decided_at = datetime.now(timezone.utc)
                    task = session.get(Task, approval.task_id)
                    if task is not None and task.status in {"waiting_for_approval", "awaiting_approval"}:
                        task.status = "approval_expired"
                        workflow._add_event(session, task.id, "approval_expired", "The approval expired; the proposed action was not executed.", {
                            "approval_id": approval.id,
                            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                        })
                        session.add(task)
                    session.add(approval)
            session.commit()
        statement = select(Approval).where(
            Approval.status == status
        ).join(Task).where(
            Task.owner_id == request.state.owner_id
        ).order_by(Approval.id.desc())

        approvals = session.exec(statement).all()

        return [
            {
                "id": approval.id,
                "task_id": approval.task_id,
                "status": approval.status,
                "action": json.loads(approval.action_json),
                "decided_at": approval.decided_at,
                "decided_by": approval.decided_by,
                "expires_at": approval.expires_at,
                "action_hash": approval.action_hash,
            }
            for approval in approvals
        ]


@app.post("/api/tasks/{task_id}/verify")
def verify_task(task_id: int, request: Request):
    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)

        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")

        policy_result = read_company_policy()
        if not policy_result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=policy_result.get("error", "Unable to read procurement policy."),
            )

        saved_policy_text = None
        if task.result_json:
            try:
                saved_result = json.loads(task.result_json)
            except json.JSONDecodeError:
                saved_result = {"raw": task.result_json}

            if isinstance(saved_result, dict):
                for key in ("policy_text", "content", "result", "body"):
                    value = saved_result.get(key)
                    if isinstance(value, str):
                        saved_policy_text = value
                        break
                if saved_policy_text is None:
                    nested_result = saved_result.get("result")
                    if isinstance(nested_result, dict):
                        for key in ("policy_text", "content", "body"):
                            value = nested_result.get(key)
                            if isinstance(value, str):
                                saved_policy_text = value
                                break

        actual_policy_text = policy_result["policy_text"]
        verification_matches = (
            isinstance(saved_policy_text, str)
            and saved_policy_text.strip() == actual_policy_text.strip()
        )

        task.status = "verified" if verification_matches else "verification_failed"
        session.add(task)
        session.add(
            ExecutionEvent(
                task_id=task.id,
                event_type="verification",
                message=(
                    "Policy verification succeeded."
                    if verification_matches
                    else "Policy verification failed."
                ),
                details_json=json.dumps({
                    "matches": verification_matches,
                    "source_file": policy_result["source"],
                    "stored_policy_preview": (saved_policy_text[:200] if isinstance(saved_policy_text, str) else None),
                }),
            )
        )
        session.commit()

        return {
            "task_id": task_id,
            "status": task.status,
            "verification": {
                "status": "verified" if verification_matches else "failed",
                "matches": verification_matches,
                "source": policy_result["source"],
                "message": (
                    "Stored policy content matches the current policy file."
                    if verification_matches
                    else "Stored policy content does not match the current policy file."
                ),
            },
        }


@app.post("/api/legacy/tasks/{task_id}/run")
def run_task(task_id: int, request: Request, payload: RunTaskRequest = RunTaskRequest()):
    vendor_context = {
        "name": (payload.vendor_name or "").strip(),
        "website": (payload.vendor_website or "").strip(),
        "registration_number": (
            (payload.registration_number or "").strip() or None
        ),
    }

    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")

        objective = task.objective
        existing_plan = task.plan_json

    if existing_plan:
        try:
            plan = json.loads(existing_plan)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Saved plan is invalid JSON.",
            ) from exc
    else:
        try:
            plan = create_plan(objective)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Planning failed: {exc}",
            ) from exc

        with Session(engine) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            task.plan_json = json.dumps(plan)
            task.status = "planned"
            session.add(task)
            session.commit()

    steps = plan.get("steps", [])
    if not steps:
        raise HTTPException(
            status_code=400,
            detail="Plan contains no steps.",
        )

    results = []

    for step in steps:
        capability = step.get("capability")
        step_number = step.get("step")
        action = step.get("action", "")

        # Never register a vendor directly through the planner.
        if capability == "register_vendor":
            if not vendor_context["name"] or not vendor_context["website"]:
                with Session(engine) as session:
                    task = _owned_task(session, task_id, request.state.owner_id)
                    task.status = "needs_input"
                    session.add(task)
                    session.commit()

                return {
                    "task_id": task_id,
                    "status": "needs_input",
                    "message": "Provide the vendor's legal name and website.",
                    "required_fields": ["vendor_name", "vendor_website"],
                    "results": results,
                }

            with Session(engine) as session:
                if vendor_context["registration_number"]:
                    duplicate = session.exec(
                        select(Vendor).where(
                            Vendor.registration_number
                            == vendor_context["registration_number"]
                        )
                    ).first()

                    if duplicate:
                        raise HTTPException(
                            status_code=409,
                            detail="Vendor registration number already exists.",
                        )

                approval = Approval(
                    task_id=task_id,
                    action_json=json.dumps(vendor_context),
                    status="pending",
                )
                session.add(approval)

                task = _owned_task(session, task_id, request.state.owner_id)
                task.status = "awaiting_approval"
                session.add(task)

                session.add(ExecutionEvent(
                    task_id=task_id,
                    event_type="approval_required",
                    message="Vendor registration is awaiting human approval.",
                    details_json=json.dumps({
                        "step": step_number,
                        "vendor": vendor_context,
                    }),
                ))

                session.commit()
                session.refresh(approval)

                return {
                    "task_id": task_id,
                    "status": "awaiting_approval",
                    "approval_id": approval.id,
                    "message": "Review the vendor proposal in the approval inbox.",
                    "results": results,
                }

        # Respect approval flags on other steps, too.
        if step.get("requires_approval", False):
            with Session(engine) as session:
                task = _owned_task(session, task_id, request.state.owner_id)
                task.status = "awaiting_approval"
                session.add(task)
                session.commit()

            return {
                "task_id": task_id,
                "status": "awaiting_approval",
                "approval_required": {
                    "step": step_number,
                    "capability": capability,
                    "action": action,
                },
                "results": results,
            }

        if not isinstance(capability, str):
            result = {
                "success": False,
                "error": "Missing or invalid capability.",
                "requires_input": True,
            }
        elif capability == "check_policy":
            result = execute_tool(capability, context=vendor_context)
        else:
            result = execute_tool(capability)

        results.append({
            "step": step_number,
            "capability": capability,
            "result": result,
        })

        with Session(engine) as session:
            task = _owned_task(session, task_id, request.state.owner_id)
            task.result_json = json.dumps(results)

            session.add(ExecutionEvent(
                task_id=task_id,
                event_type="tool_execution",
                message=(
                    f"Step {step_number} succeeded."
                    if result.get("success")
                    else f"Step {step_number} needs input or failed."
                ),
                details_json=json.dumps({
                    "capability": capability,
                    "action": action,
                    "result": result,
                }),
            ))

            if not result.get("success"):
                task.status = "needs_input"
            else:
                task.status = "executing"

            session.add(task)
            session.commit()

        if not result.get("success"):
            return {
                "task_id": task_id,
                "status": "needs_input",
                "plan": plan,
                "results": results,
                "missing_information": result.get(
                    "missing_information", []
                ),
                "error": result.get("error"),
            }

    with Session(engine) as session:
        task = _owned_task(session, task_id, request.state.owner_id)
        task.status = "executed"
        task.result_json = json.dumps(results)
        session.add(task)
        session.commit()

    return {
        "task_id": task_id,
        "status": "executed",
        "plan": plan,
        "results": results,
        "verification": {
            "status": "pending",
            "message": "Use POST /api/tasks/{task_id}/verify to compare the saved policy result against the current procurement policy file.",
        },
    }


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval_unified(
    approval_id: int,
    decision: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    return decide_vendor_approval(
        approval_id,
        decision,
        request.state.owner_id,
        background_tasks,
    )


@app.post("/api/tasks/{task_id}/run", status_code=202)
def run_task_unified(
    task_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    payload: RunTaskRequest = RunTaskRequest(),
):
    vendor_context = {
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
            vendor_context[key] = value
    if contains_secret_material(vendor_context):
        raise HTTPException(status_code=422, detail="Credential-like values cannot be stored in task inputs.")

    with Session(engine) as session:
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

    scheduled = schedule_workflow(background_tasks, task_id, vendor_context)
    if not scheduled:
        raise HTTPException(status_code=409, detail="This task is already running.")

    return {
        "task_id": task_id,
        "status": "running",
        "message": "Workflow resumed." if was_processing else "Workflow accepted.",
    }


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request):
    with Session(engine) as session:
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

