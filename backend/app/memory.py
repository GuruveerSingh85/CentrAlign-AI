from __future__ import annotations

import re
import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import Memory, Task, utc_now

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
    updated_at = memory.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at + age_limit <= now


def _memory_context_item(memory: Memory, now: datetime) -> dict:
    stale = _memory_is_stale(memory, now)
    return {
        "id": memory.id,
        "category": memory.category,
        "kind": memory.kind,
        "content": memory.content,
        "source": memory.source,
        "source_ref": memory.source_ref,
        "created_at": memory.created_at.isoformat(),
        "verification_status": "stale" if stale else memory.verification_status,
        "stale": stale,
    }


def retrieve_context(
    session: Session,
    owner_id: str,
    query: str,
    *,
    memory_limit: int = 8,
    history_limit: int = 3,
) -> dict:
    """Return a bounded, relevance-ranked context without serializing full history."""
    now = utc_now()
    query_terms = _terms(query)
    memories = session.exec(
        select(Memory).where(
            Memory.owner_id == owner_id,
            Memory.active == True,  # noqa: E712
        ).order_by(Memory.updated_at.desc())
    ).all()
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
        }))
    ranked_history.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)

    return {
        "memories": [
            _memory_context_item(memory, now)
            for _, memory in ranked_memories[:memory_limit]
        ],
        "task_history": [item for _, item in ranked_history[:history_limit]],
        "retrieval_status": "available",
    }


def retrieve_context_safely(session: Session, owner_id: str, query: str) -> dict:
    try:
        return retrieve_context(session, owner_id, query)
    except Exception:
        return {
            "memories": [],
            "task_history": [],
            "retrieval_status": "unavailable",
        }


def record_verified_procedure(
    session: Session,
    task: Task,
    state: dict,
    verification: dict,
) -> Memory | None:
    if verification.get("matches") is not True:
        return None
    capabilities = [
        item.get("capability")
        for item in state.get("step_results", [])
        if item.get("success") and isinstance(item.get("capability"), str)
    ]
    if not capabilities:
        return None
    memory_key = f"verified-task:{task.id}"
    existing = session.exec(
        select(Memory).where(
            Memory.owner_id == task.owner_id,
            Memory.memory_key == memory_key,
        )
    ).first()
    if existing is not None:
        return existing
    workflow_name = state.get("workflow") or "registered_tool_plan"
    content = f"Verified procedure ({workflow_name}): " + " -> ".join(capabilities)
    validate_memory_content(content)
    memory = Memory(
        owner_id=task.owner_id,
        category="procedure",
        kind="inference",
        content=content,
        memory_key=memory_key,
        source="verified_task",
        source_ref=str(task.id),
        verification_status="verified",
        verified_at=utc_now(),
    )
    session.add(memory)
    return memory