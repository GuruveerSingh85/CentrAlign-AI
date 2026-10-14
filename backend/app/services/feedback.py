from __future__ import annotations

import json

from sqlmodel import Session, select

from app.core.models import ExecutionEvent, Memory, Task, utc_now
from app.core.security import contains_secret_material

FEEDBACK_TYPES = {"correction", "failure", "usefulness", "success"}
_FEEDBACK_ELIGIBLE_STATUSES = {
    "completed", "failed", "cancelled", "verified", "rejected", "unsupported",
    "partially_completed",
    "approval_expired",
}


def record_task_feedback(
    session: Session,
    task: Task,
    owner_id: str,
    feedback_type: str,
    content: str,
) -> ExecutionEvent:
    if task.owner_id != owner_id:
        raise ValueError("Task not found.")
    if task.status not in _FEEDBACK_ELIGIBLE_STATUSES:
        raise ValueError("Feedback is available after task execution has ended.")
    if feedback_type not in FEEDBACK_TYPES:
        raise ValueError("Unsupported feedback type.")
    if not isinstance(content, str) or not content.strip() or len(content.strip()) > 1000:
        raise ValueError("Feedback must contain 1 to 1,000 characters.")
    if contains_secret_material(content):
        raise ValueError("Credential-like values cannot be stored as feedback.")

    try:
        state = json.loads(task.result_json or "{}")
    except (ValueError, TypeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    try:
        plan = json.loads(task.plan_json or "{}")
    except (ValueError, TypeError):
        plan = {}
    if not isinstance(plan, dict):
        plan = {}

    steps = state.get("step_results", [])
    capabilities = list(dict.fromkeys(
        item.get("capability")
        for item in steps
        if isinstance(item, dict) and isinstance(item.get("capability"), str)
    )) if isinstance(steps, list) else []
    verification = state.get("verification")
    evidence = {
        "type": verification.get("type"),
        "matches": verification.get("matches"),
        "message": str(verification.get("message", ""))[:300],
    } if isinstance(verification, dict) else None
    relevant_terms = set(task.objective.casefold().split())
    procedures = session.exec(
        select(Memory).where(
            Memory.owner_id == owner_id,
            Memory.category == "procedure",
            Memory.verification_status == "verified",
            Memory.active == True,  # noqa: E712
        )
    ).all()
    procedure_refs = [
        memory.id
        for memory in procedures
        if memory.id is not None
        and any(term in memory.content.casefold() for term in relevant_terms if len(term) > 2)
    ][:10]

    event = ExecutionEvent(
        task_id=task.id,
        event_type="user_feedback",
        message=f"User provided {feedback_type} feedback.",
        details_json=json.dumps({
            "feedback_type": feedback_type,
            "content": content.strip(),
            "task_id": task.id,
            "owner_id": owner_id,
            "capabilities": capabilities,
            "planned_capabilities": [
                step.get("capability")
                for step in plan.get("steps", [])
                if isinstance(step, dict) and isinstance(step.get("capability"), str)
            ][:20],
            "procedure_refs": procedure_refs,
            "verification_evidence": evidence,
            "submitted_at": utc_now().isoformat(),
            "authority": "user_feedback_unverified",
        }),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event
