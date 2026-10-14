from __future__ import annotations

import re
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.core.models import ExecutionEvent, Memory, Task, utc_now
from app.services.procedures import record_verified_procedure as _record_verified_procedure

_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,40}", re.IGNORECASE)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|password|passwd|client[_ -]?secret|"
    r"authorization\s*:\s*bearer)\s*[:=]\s*\S+"
)
_ALLOWED_CATEGORIES = {"preference", "business_context", "procedure"}
_ALLOWED_KINDS = {"fact", "assumption", "user_instruction", "inference"}
_ALLOWED_VERIFICATION = {"verified", "unverified", "conflicted"}
_STALE_AFTER = {
    "business_context": timedelta(days=180),
    "procedure": timedelta(days=365),
}


def validate_memory_content(content: str) -> None:
    if not isinstance(content, str):
        raise ValueError("Memory content must be text.")
    if not content.strip():
        raise ValueError("Memory content cannot be empty.")
    if len(content) > 4000:
        raise ValueError("Memory content cannot exceed 4,000 characters.")
    if _CREDENTIAL_PATTERN.search(content):
        raise ValueError("Credentials and secrets cannot be stored as memory.")


def _terms(value: str) -> set[str]:
    return {term.casefold() for term in _TOKEN_PATTERN.findall(value)}


def _score(query_terms: set[str], content: str) -> int:
    content_terms = _terms(content)
    if not query_terms or not content_terms:
        return 0
    overlap = query_terms & content_terms
    return len(overlap) * 4 + (3 if query_terms <= content_terms else 0)


def _safe_summary(value: object) -> str:
    return _CREDENTIAL_PATTERN.sub("[redacted]", str(value))[:500]


def _memory_is_stale(memory: Memory, now: datetime) -> bool:
    expires_at = memory.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return True
    age_limit = _STALE_AFTER.get(memory.category)
    if age_limit is None:
        return False
    freshness_at = (
        memory.verified_at
        if memory.category == "procedure" and memory.verified_at is not None
        else memory.updated_at
    )
    if freshness_at.tzinfo is None:
        freshness_at = freshness_at.replace(tzinfo=timezone.utc)
    return freshness_at + age_limit <= now


def _memory_context_item(memory: Memory, now: datetime) -> dict:
    stale = _memory_is_stale(memory, now)
    item = {
        "id": memory.id,
        "category": memory.category,
        "kind": memory.kind,
        "content": memory.content,
        "source": memory.source,
        "source_ref": memory.source_ref,
        "scope": memory.scope,
        "organization_id": memory.organization_id,
        "created_at": memory.created_at.isoformat(),
        "verification_status": "stale" if stale else memory.verification_status,
        "stale": stale,
        "conflicted": memory.verification_status == "conflicted",
    }
    if memory.category == "procedure":
        item.update({
            "procedure_key": memory.procedure_key,
            "procedure_version": memory.procedure_version,
            "successes": memory.procedure_successes,
            "failures": memory.procedure_failures,
            "confidence": memory.procedure_confidence,
            "conditions": _json_object(memory.conditions_json),
            "evidence": _json_object(memory.evidence_json),
        })
    return item


def _json_object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def retrieve_context(
    session: Session,
    owner_id: str,
    query: str,
    *,
    memory_limit: int = 8,
    history_limit: int = 3,
    organization_id: str | None = None,
) -> dict:
    """Return a bounded, relevance-ranked context without serializing full history."""
    now = utc_now()
    query_terms = _terms(query)
    memory_scope = and_(Memory.scope == "user", Memory.owner_id == owner_id)
    if organization_id:
        memory_scope = or_(
            memory_scope,
            and_(
                Memory.scope == "organization",
                Memory.organization_id == organization_id,
            ),
        )
    memories = session.exec(select(Memory).where(
        memory_scope,
        Memory.active == True,  # noqa: E712
    ).order_by(Memory.updated_at.desc())).all()
    ranked_memories = []
    for memory in memories:
        score = _score(query_terms, memory.content)
        if memory.category == "preference":
            score += 2
        if memory.kind == "user_instruction":
            score += 2
        if memory.verification_status == "verified":
            score += 1
        if _memory_is_stale(memory, now):
            score -= 3
        if score > 0:
            ranked_memories.append((score, memory))
    ranked_memories.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)

    tasks = session.exec(
        select(Task)
        .where(Task.owner_id == owner_id)
        .order_by(Task.updated_at.desc())
        .limit(100)
    ).all()
    feedback_by_task: dict[int, list[dict]] = {}
    task_ids = [task.id for task in tasks if task.id is not None]
    if task_ids:
        feedback_events = session.exec(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.task_id.in_(task_ids),
                ExecutionEvent.event_type == "user_feedback",
            )
            .order_by(ExecutionEvent.created_at.desc())
        ).all()
        for event in feedback_events:
            try:
                details = json.loads(event.details_json or "{}")
            except (ValueError, TypeError):
                continue
            if (
                not isinstance(details, dict)
                or details.get("authority") != "user_feedback_unverified"
            ):
                continue
            feedback_by_task.setdefault(event.task_id, []).append({
                "type": details.get("feedback_type"),
                "content": _safe_summary(details.get("content", "")),
                "capabilities": details.get("capabilities", [])[:20],
                "procedure_refs": details.get("procedure_refs", [])[:10],
                "verification_evidence": details.get("verification_evidence"),
                "authority": "unverified_user_feedback",
                "created_at": event.created_at.isoformat(),
            })
    ranked_history = []
    for task in tasks:
        score = _score(query_terms, task.objective)
        if score <= 0:
            continue
        state = {}
        result_summary = ""
        if task.result_json:
            try:
                parsed = json.loads(task.result_json)
                if isinstance(parsed, dict):
                    state = parsed
                elif isinstance(parsed, list):
                    outcomes = []
                    for record in parsed:
                        if not isinstance(record, dict):
                            continue
                        result = record.get("result")
                        if not isinstance(result, dict):
                            continue
                        capability = record.get("capability") or "tool"
                        if result.get("success"):
                            outcomes.append(f"{capability} succeeded")
                        elif result.get("error"):
                            outcomes.append(
                                f"{capability} failed: {_safe_summary(result['error'])}"
                            )
                    result_summary = "; ".join(outcomes)[:500]
            except (ValueError, TypeError):
                pass
        verification = state.get("verification") or {}
        ranked_history.append((score, {
            "task_id": task.id,
            "objective": task.objective[:500],
            "status": task.status,
            "summary": _safe_summary(
                state.get("final_summary")
                or state.get("error")
                or result_summary
            ),
            "verification_status": (
                "verified" if verification.get("matches") is True
                else "failed" if verification.get("matches") is False
                else "verified" if task.status == "verified"
                else "failed" if task.status == "verification_failed"
                else "unverified"
            ),
            "updated_at": task.updated_at.isoformat(),
            "feedback": feedback_by_task.get(task.id, [])[:3],
        }))
    ranked_history.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)

    selected_memories = [
        _memory_context_item(memory, now)
        for _, memory in ranked_memories[:memory_limit]
    ]
    verified_procedures = [
        item for item in selected_memories
        if item["category"] == "procedure"
        and item["verification_status"] == "verified"
        and not item["stale"]
        and not item["conflicted"]
        and item["procedure_key"]
    ]

    return {
        "memories": selected_memories,
        "verified_procedures": verified_procedures,
        "task_history": [item for _, item in ranked_history[:history_limit]],
        "retrieval_status": "available",
    }


def retrieve_context_safely(
    session: Session,
    owner_id: str,
    query: str,
    *,
    organization_id: str | None = None,
) -> dict:
    try:
        return retrieve_context(
            session,
            owner_id,
            query,
            organization_id=organization_id,
        )
    except Exception:
        return {
            "memories": [],
            "verified_procedures": [],
            "task_history": [],
            "retrieval_status": "unavailable",
        }


def record_verified_procedure(
    session: Session,
    task: Task,
    state: dict,
    verification: dict,
) -> Memory | None:
    return _record_verified_procedure(session, task, state, verification)