
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, SQLModel, select

import json
from app.database import engine
from app import models
from app.models import Task
from app.agent import create_plan
from app.tool_registry import execute_tool
from app.models import ExecutionEvent
from datetime import datetime, timezone
from app.models import Vendor, Approval

@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(
    title="CentrAlign AI Operator",
    description="Plan, execute, and verify company tasks.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "CentrAlign AI Operator"}


@app.post("/api/tasks", response_model=Task)
def create_task(payload: TaskCreate):
    objective = payload.objective.strip()

    if not objective:
        raise HTTPException(status_code=400, detail="Objective cannot be empty")

    with Session(engine) as session:
        task = Task(objective=objective, status="pending")
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


@app.get("/api/tasks", response_model=list[Task])
def list_tasks():
    with Session(engine) as session:
        statement = select(Task).order_by(Task.created_at.desc())
        return session.exec(statement).all()

@app.post("/api/tasks/{task_id}/plan")
def plan_task(task_id: int):
    with Session(engine) as session:
        task = session.get(Task, task_id)

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
        task = session.get(Task, task_id)

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
def execute_policy(task_id: int):
    with Session(engine) as session:
        task = session.get(Task, task_id)

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
def execute_task_plan(task_id: int):
    with Session(engine) as session:
        task = session.get(Task, task_id)

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
def propose_vendor(task_id: int, payload: VendorProposal):
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
        task = session.get(Task, task_id)

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


@app.post("/api/approvals/{approval_id}/decide")
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
def get_task_events(task_id: int):
    with Session(engine) as session:
        task = session.get(Task, task_id)

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
def list_approvals(status: str = "pending"):
    with Session(engine) as session:
        statement = select(Approval).where(
            Approval.status == status
        ).order_by(Approval.id.desc())

        approvals = session.exec(statement).all()

        return [
            {
                "id": approval.id,
                "task_id": approval.task_id,
                "status": approval.status,
                "action": json.loads(approval.action_json),
                "decided_at": approval.decided_at,
            }
            for approval in approvals
        ]



@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: int, payload: RunTaskRequest = RunTaskRequest()):
    vendor_context = {
        "name": (payload.vendor_name or "").strip(),
        "website": (payload.vendor_website or "").strip(),
        "registration_number": (
            (payload.registration_number or "").strip() or None
        ),
    }

    with Session(engine) as session:
        task = session.get(Task, task_id)
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
            task = session.get(Task, task_id)
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
                    task = session.get(Task, task_id)
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

                task = session.get(Task, task_id)
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
                task = session.get(Task, task_id)
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
            task = session.get(Task, task_id)
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
        task = session.get(Task, task_id)
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
            "status": "not_implemented",
            "message": "Independent verification is still required.",
        },
    }

