import json
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.workflows import engine as workflow
from app.services.approval_control import approval_is_expired
from app.core.models import Approval, Task
from app.workflows.engine import decide_vendor_approval


def create_approval_router(engine_provider: Callable[[], Engine]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/approvals")
    def list_approvals(request: Request, status: str = "pending"):
        with Session(engine_provider()) as session:
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

            approvals = session.exec(
                select(Approval)
                .where(Approval.status == status)
                .join(Task)
                .where(Task.owner_id == request.state.owner_id)
                .order_by(Approval.id.desc())
            ).all()
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

    @router.post("/api/approvals/{approval_id}/decide")
    def decide_approval(
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

    return router