from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from app.models import Approval, Task
from app.security import ActionRisk, contains_secret_material, risk_as_dict


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def proposal_digest(capability: str, parameters: dict, action: dict | None = None) -> str:
    material = {"capability": capability, "parameters": parameters}
    if action is not None:
        material["action"] = action
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def approval_expiry(now: datetime | None = None) -> datetime:
    try:
        ttl_seconds = max(60, int(os.getenv("APPROVAL_TTL_SECONDS", "3600")))
    except ValueError:
        ttl_seconds = 3600
    return (now or datetime.now(timezone.utc)) + timedelta(seconds=ttl_seconds)


def describe_action(capability: str, parameters: dict, contract: dict, risk: ActionRisk) -> dict:
    target_key = next((
        key for key in ("target", "path", "url", "recipient", "to", "resource", "vendor_id", "website")
        if key in parameters
    ), None)
    return {
        "capability": capability,
        "action": contract.get("description", capability),
        "target": parameters.get(target_key) if target_key else capability,
        "parameters": parameters,
        "expected_consequences": contract.get("expected_consequences") or contract.get("description", capability),
        "risk": risk_as_dict(risk),
    }


def new_approval(
    task: Task,
    capability: str,
    parameters: dict,
    action: dict,
    risk: ActionRisk,
) -> Approval:
    if contains_secret_material({"parameters": parameters, "display_action": action}):
        raise ValueError("Credential-like values cannot be stored in an approval proposal.")
    return Approval(
        task_id=task.id,
        action_json=canonical_json(action),
        execution_inputs_json=canonical_json(parameters),
        capability=capability,
        action_hash=proposal_digest(capability, parameters, action),
        risk_json=canonical_json(risk_as_dict(risk)),
        status="pending",
        expires_at=approval_expiry(),
    )


def approval_parameters(approval: Approval) -> dict:
    if approval.execution_inputs_json:
        value = json.loads(approval.execution_inputs_json)
    else:
        value = json.loads(approval.action_json)
    if not isinstance(value, dict):
        raise ValueError("Approval parameters must be an object.")
    return value


def approval_is_expired(approval: Approval, now: datetime | None = None) -> bool:
    if approval.expires_at is None:
        return False
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


def proposal_is_unchanged(
    approval: Approval,
    capability: str,
    parameters: dict,
    current_action: dict | None = None,
) -> bool:
    if not approval.action_hash:
        return approval.capability in {None, capability}
    if approval.capability not in {None, capability}:
        return False
    try:
        display_action = json.loads(approval.action_json)
        display_parameters = (
            {key: display_action.get(key) for key in ("name", "website", "registration_number")}
            if capability == "register_vendor"
            else display_action.get("parameters")
        )
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(display_parameters, dict) or display_parameters != parameters:
        return False
    if approval.action_hash != proposal_digest(capability, parameters, display_action):
        return False
    if current_action is not None and approval.action_hash != proposal_digest(capability, parameters, current_action):
        return False
    return True
