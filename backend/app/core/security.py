from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


_RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
_DEFAULT_GRANTS = {
    "procurement:read",
    "procurement:write",
    "business:read",
    "approval:decide",
}
_SECRET_KEY = re.compile(r"(?i)(?:api[_ -]?key|access[_ -]?token|password|passwd|client[_ -]?secret|private[_ -]?key)")
_SECRET_VALUE = re.compile(
    r"(?i)(?:AIza[0-9A-Za-z_-]{25,}|sk-[0-9A-Za-z_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_ -]?key|access[_ -]?token|password|passwd|client[_ -]?secret)\s*[:=]\s*\S+)"
)


@dataclass(frozen=True)
class ActionRisk:
    level: str
    impact: str
    reversibility: str
    data_sensitivity: str
    external_consequences: str
    requires_approval: bool
    autonomy_limits: dict


def _json_setting(name: str) -> dict | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain a JSON object.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object.")
    return value


def resolve_permissions(owner_id: str | None = None) -> set[str]:
    role_grants = _json_setting("TOOL_ROLE_GRANTS")
    user_roles = _json_setting("TRUSTED_USER_ROLES")
    if role_grants is not None or user_roles is not None:
        if role_grants is None or user_roles is None or owner_id is None:
            return set()
        role = user_roles.get(owner_id)
        if not isinstance(role, str):
            return set()
        scopes = role_grants.get(role, [])
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            return set()
        return {scope.strip() for scope in scopes if scope.strip()}

    configured = os.getenv("TOOL_PERMISSION_GRANTS")
    if configured is None:
        return set(_DEFAULT_GRANTS)
    return {scope.strip() for scope in configured.split(",") if scope.strip()}


def contains_secret_material(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _SECRET_KEY.search(str(key)) or contains_secret_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret_material(item) for item in value)
    if isinstance(value, str):
        return bool(_SECRET_VALUE.search(value))
    return False


def classify_action(contract: dict, parameters: dict | None = None) -> ActionRisk:
    metadata = contract.get("risk", {})
    impact = metadata.get(
        "impact",
        "low" if not contract.get("side_effect") or contract.get("permission_gated_side_effect") else "medium",
    )
    reversibility = metadata.get(
        "reversibility",
        "reversible" if not contract.get("side_effect") or contract.get("permission_gated_side_effect") else "unknown",
    )
    data_sensitivity = metadata.get("data_sensitivity", "internal")
    external_consequences = metadata.get("external_consequences", "none")
    impact_level = impact if impact in _RISK_ORDER else "high"
    level = impact_level
    if reversibility not in {"reversible", "partially_reversible", "irreversible", "unknown"}:
        level = "high"
    if reversibility in {"irreversible", "unknown"}:
        level = "high"
    if data_sensitivity not in {"public", "internal", "business", "sensitive", "confidential", "restricted", "secret"}:
        level = "high"
    if data_sensitivity in {"sensitive", "confidential", "restricted", "secret"}:
        level = "high"
    if external_consequences not in {"none", "bounded_processing", "external", "material"}:
        level = "high"
    if external_consequences in {"material", "external"}:
        level = "high"

    configured_ceiling = os.getenv("MAX_AUTONOMOUS_RISK", "low").strip().lower()
    if configured_ceiling not in _RISK_ORDER:
        configured_ceiling = "low"
    explicitly_sensitive = (
        contract.get("requires_approval", False)
        or reversibility not in {"reversible", "partially_reversible"}
        or data_sensitivity not in {"public", "internal", "business"}
        or external_consequences in {"material", "external"}
        or level == "high" and (
            data_sensitivity not in {"public", "internal", "business"}
            or external_consequences not in {"none", "bounded_processing"}
            or reversibility not in {"reversible", "partially_reversible"}
        )
    )
    limits = metadata.get("autonomy_limits", {})
    if not isinstance(limits, dict):
        limits = {}
    return ActionRisk(
        level=level,
        impact=impact,
        reversibility=reversibility,
        data_sensitivity=data_sensitivity,
        external_consequences=external_consequences,
        requires_approval=explicitly_sensitive or _RISK_ORDER[level] > _RISK_ORDER[configured_ceiling],
        autonomy_limits=limits,
    )


def risk_as_dict(risk: ActionRisk) -> dict:
    return {
        "level": risk.level,
        "impact": risk.impact,
        "reversibility": risk.reversibility,
        "data_sensitivity": risk.data_sensitivity,
        "external_consequences": risk.external_consequences,
        "requires_approval": risk.requires_approval,
        "autonomy_limits": risk.autonomy_limits,
    }
