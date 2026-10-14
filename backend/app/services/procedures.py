from __future__ import annotations

import hashlib
import json
import re
from sqlmodel import Session, select

from app.core.models import Memory, Task, utc_now

_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,40}", re.IGNORECASE)
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "i", "in", "is", "it", "me", "of", "on", "or", "please",
    "the", "to", "we", "with",
}
_MAX_EVIDENCE = 50


def _objective_terms(objective: str) -> list[str]:
    return sorted({
        token.casefold()
        for token in _TOKEN_PATTERN.findall(objective)
        if token.casefold() not in _STOP_WORDS
    })


def _procedure_key(task: Task, workflow: str) -> str:
    identity = "|".join((workflow, " ".join(_objective_terms(task.objective))))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"procedure:{digest}"


def _read_json(value: str | None, fallback):
    try:
        result = json.loads(value or "")
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _evidence_entry(task: Task, verification: dict, succeeded: bool) -> dict:
    return {
        "task_id": task.id,
        "recorded_at": utc_now().isoformat(),
        "outcome": "verified_success" if succeeded else "verification_failure",
        "verification_type": verification.get("type"),
        "verification_matches": verification.get("matches") is True,
        "capabilities": [
            item.get("capability")
            for item in _read_json(task.result_json, {}).get("step_results", [])
            if isinstance(item, dict) and isinstance(item.get("capability"), str)
        ] if task.result_json else [],
    }


def record_procedure_outcome(
    session: Session,
    task: Task,
    state: dict,
    verification: dict,
) -> Memory | None:
    """Create reusable records only on independent success; attach failures to existing records."""
    succeeded = verification.get("matches") is True
    capabilities = [
        item.get("capability")
        for item in state.get("step_results", [])
        if isinstance(item, dict)
        and item.get("success") is True
        and isinstance(item.get("capability"), str)
    ]
    if not capabilities and verification.get("matches") is True:
        return None

    workflow = str(state.get("workflow") or "registered_tool_plan")
    procedure_key = _procedure_key(task, workflow)
    existing = session.exec(
        select(Memory).where(
            Memory.owner_id == task.owner_id,
            Memory.category == "procedure",
            Memory.procedure_key == procedure_key,
            Memory.active == True,  # noqa: E712
        ).order_by(Memory.updated_at.desc())
    ).first()
    if existing is None and not succeeded:
        return None

    from app.services.memory import validate_memory_content

    now = utc_now()
    conditions = {
        "workflow": workflow,
        "objective_terms": _objective_terms(task.objective),
        "input_fields": sorted(
            key for key in state.get("inputs", {}) if isinstance(key, str)
        ),
    }
    content = (
        f"Verified procedure for {task.objective[:240]}: "
        + " -> ".join(capabilities)
    )
    validate_memory_content(content)
    evidence = _read_json(existing.evidence_json, {}) if existing else {}
    outcomes = evidence.get("outcomes", []) if isinstance(evidence, dict) else []
    if not isinstance(outcomes, list):
        outcomes = []
    previous_capabilities = evidence.get("capabilities", []) if isinstance(evidence, dict) else []
    previous_conditions = _read_json(existing.conditions_json, {}) if existing else {}
    versions = evidence.get("versions", []) if isinstance(evidence, dict) else []
    if not isinstance(versions, list):
        versions = []
    version = existing.procedure_version if existing else 1
    if existing and succeeded and (
        previous_capabilities != capabilities
        or previous_conditions != conditions
    ):
        versions.append({
            "version": version,
            "content": existing.content,
            "capabilities": previous_capabilities,
            "conditions": _read_json(existing.conditions_json, {}),
            "recorded_at": existing.updated_at.isoformat(),
        })
        version += 1

    outcomes.append(_evidence_entry(task, verification, succeeded))
    outcomes = outcomes[-_MAX_EVIDENCE:]
    success_count = (existing.procedure_successes if existing else 0) + int(succeeded)
    failure_count = (existing.procedure_failures if existing else 0) + int(not succeeded)
    confidence = success_count / (success_count + failure_count)
    if existing is None:
        existing = Memory(
            owner_id=task.owner_id,
            category="procedure",
            kind="inference",
            content=content,
            memory_key=procedure_key,
            source="verified_task",
            source_ref=str(task.id),
            verification_status="verified",
            verified_at=now,
        )
    else:
        existing.content = content if succeeded else existing.content
        existing.verified_at = now if succeeded else existing.verified_at
        existing.updated_at = now

    existing.procedure_key = procedure_key
    existing.procedure_version = version
    existing.procedure_successes = success_count
    existing.procedure_failures = failure_count
    existing.procedure_confidence = confidence
    existing.conditions_json = json.dumps(conditions if succeeded else _read_json(existing.conditions_json, conditions))
    existing.evidence_json = json.dumps({
        "capabilities": capabilities if succeeded else previous_capabilities,
        "versions": versions[-20:],
        "outcomes": outcomes,
    })
    session.add(existing)
    return existing


def record_verified_procedure(
    session: Session,
    task: Task,
    state: dict,
    verification: dict,
) -> Memory | None:
    return record_procedure_outcome(session, task, state, verification)
