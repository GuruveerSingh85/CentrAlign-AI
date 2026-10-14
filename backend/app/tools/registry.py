
from __future__ import annotations

import json
import errno
import inspect
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.database import engine
from app.core.models import Approval, ExecutionEvent, Task, Vendor
from app.services.approval_control import (
    approval_is_expired,
    approval_parameters,
    describe_action,
    proposal_digest,
    proposal_is_unchanged,
)
from app.core.security import classify_action, contains_secret_material, resolve_permissions, risk_as_dict

POLICY_PATH = Path(__file__).resolve().parents[2] / "data" / "procurement_policy.txt"
logger = logging.getLogger(__name__)


def _schema_error(value: object, schema: dict, path: str = "inputs") -> str | None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object."
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} is required."
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties", True) is False:
                    return f"{path}.{key} is not allowed."
                continue
            error = _schema_error(item, properties[key], f"{path}.{key}")
            if error:
                return error
    elif expected == "array":
        if not isinstance(value, list):
            return f"{path} must be an array."
        if len(value) < schema.get("minItems", 0):
            return f"{path} has too few items."
        if len(value) > schema.get("maxItems", float("inf")):
            return f"{path} has too many items."
        if "items" in schema:
            for index, item in enumerate(value):
                error = _schema_error(item, schema["items"], f"{path}[{index}]")
                if error:
                    return error
    elif expected == "string":
        if not isinstance(value, str):
            return f"{path} must be a string."
        if len(value) < schema.get("minLength", 0):
            return f"{path} is too short."
        if len(value) > schema.get("maxLength", float("inf")):
            return f"{path} is too long."
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{path} has an invalid format."
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path} must be an integer."
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            return f"{path} is outside the allowed range."
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{path} must be a number."
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            return f"{path} is outside the allowed range."
    elif expected == "boolean" and not isinstance(value, bool):
        return f"{path} must be a boolean."
    elif expected == "null" and value is not None:
        return f"{path} must be null."
    elif expected not in {None, "object", "array", "string", "integer", "number", "boolean", "null"}:
        return f"{path} has an invalid registered schema."

    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not an allowed value."
    return None


def _schema_definition_error(schema: object, path: str = "input_schema") -> str | None:
    if not isinstance(schema, dict):
        return f"{path} must be an object."
    schema_type = schema.get("type")
    supported_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if schema_type not in supported_types:
        return f"{path}.type is unsupported."
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return f"{path} has invalid object properties or required fields."
        if not all(isinstance(key, str) and key in properties for key in required):
            return f"{path}.required must name declared properties."
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            return f"{path}.additionalProperties must be boolean."
        for name, property_schema in properties.items():
            error = _schema_definition_error(property_schema, f"{path}.properties.{name}")
            if error:
                return error
    elif schema_type == "array":
        if "items" not in schema:
            return f"{path}.items is required for array schemas."
        for key in ("minItems", "maxItems"):
            if key in schema and (not isinstance(schema[key], int) or schema[key] < 0):
                return f"{path}.{key} must be a non-negative integer."
        error = _schema_definition_error(schema["items"], f"{path}.items")
        if error:
            return error
    elif schema_type == "string":
        for key in ("minLength", "maxLength"):
            if key in schema and (not isinstance(schema[key], int) or schema[key] < 0):
                return f"{path}.{key} must be a non-negative integer."
        if "pattern" in schema:
            try:
                re.compile(schema["pattern"])
            except (TypeError, re.error):
                return f"{path}.pattern must be a valid regular expression."
    elif schema_type in {"integer", "number"}:
        for key in ("minimum", "maximum"):
            if key in schema and (not isinstance(schema[key], (int, float)) or isinstance(schema[key], bool)):
                return f"{path}.{key} must be numeric."
    if "enum" in schema and not isinstance(schema["enum"], list):
        return f"{path}.enum must be a list."
    return None


def _configured_permissions() -> set[str]:
    return resolve_permissions()


def _failure_from_exception(exc: Exception) -> dict:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "deadlineexceeded" in name:
        failure_type = "timeout"
        error = "Tool execution timed out."
    elif "429" in message or "rate limit" in message or "resource_exhausted" in message:
        failure_type = "rate_limited"
        error = "The service rate limit was reached."
    elif any(token in name for token in ("connectionerror", "connecterror", "networkerror")) or any(
        token in message for token in ("503", "temporarily unavailable", "service unavailable")
    ):
        failure_type = "service_unavailable"
        error = "The service is temporarily unavailable."
    else:
        failure_type = "tool_error"
        error = "Tool execution failed. See backend logs for details."
    return {"success": False, "error": error, "failure_type": failure_type, "requires_input": False}


def _object_schema(inputs: dict, *, required: tuple[str, ...] = (), additional: bool = False) -> dict:
    properties = {}
    for name, description in inputs.items():
        if isinstance(description, dict):
            properties[name] = description
        else:
            properties[name] = {"type": "string", "description": str(description)}
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additional,
    }


def _normalize_context(context: dict | None = None) -> dict:
    payload = (context or {}).copy()
    if not isinstance(payload, dict):
        return {}
    return payload


def read_company_policy() -> dict:
    """Read the procurement policy file from disk and return it as executable evidence."""
    try:
        policy_text = POLICY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {
            "success": False,
            "error": "Procurement policy file not found.",
            "failure_type": "policy_file_missing",
            "requires_input": True,
        }
    except OSError as exc:
        logger.exception("Unable to read procurement policy file")
        return {
            "success": False,
            "error": "Could not read the procurement policy file.",
            "failure_type": (
                "transient_io_error"
                if exc.errno in {errno.EAGAIN, errno.EINTR, errno.EBUSY}
                else "policy_read_error"
            ),
            "requires_input": True,
        }

    return {
        "success": True,
        "policy_text": policy_text,
        "source": str(POLICY_PATH),
        "requires_input": False,
    }


def check_policy(context: dict | None = None) -> dict:
    """Validate policy requirements and vendor details without inventing facts."""
    payload = _normalize_context(context)
    name = str(payload.get("name") or payload.get("vendor_name") or "").strip()
    website = str(payload.get("website") or payload.get("vendor_website") or "").strip()
    policy_text = str(payload.get("policy_text") or "")
    registration_number = (
        str(payload.get("registration_number") or payload.get("vendor_registration_number") or "").strip()
        or None
    )

    missing_information = []
    if not name:
        missing_information.append("vendor_name")
    if not website:
        missing_information.append("vendor_website")

    if missing_information:
        return {
            "success": False,
            "error": "Vendor name and website are required. Missing or uncertain information must not be invented.",
            "missing_information": missing_information,
            "requires_input": True,
        }

    if website:
        parsed = urlparse(website)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                "success": False,
                "error": "Vendor website must be a valid http(s) URL.",
                "missing_information": [],
                "failure_type": "invalid_vendor_website",
                "requires_input": False,
            }

    if not policy_text.strip():
        return {
            "success": False,
            "error": "Procurement policy evidence is required before validation.",
            "failure_type": "missing_policy_evidence",
            "requires_input": False,
        }

    normalized_policy = " ".join(policy_text.lower().split())
    required_policy_phrases = (
        ("legal name", "website"),
        ("duplicate",),
        ("missing", "invent"),
        ("approval",),
        ("retrieved", "checked"),
        ("assistance",),
    )
    unmet_policy_requirements = [
        index + 1
        for index, phrases in enumerate(required_policy_phrases)
        if not all(phrase in normalized_policy for phrase in phrases)
    ]

    if unmet_policy_requirements:
        return {
            "success": False,
            "error": "The procurement policy is missing required vendor safeguards.",
            "failure_type": "policy_requirements_not_met",
            "unmet_requirements": unmet_policy_requirements,
            "requires_input": False,
        }

    return {
        "success": True,
        "policy_status": "compliant",
        "policy_requirements_checked": len(required_policy_phrases),
        "vendor": {
            "name": name,
            "website": website,
            "registration_number": registration_number,
        },
        "missing_information": [],
        "requires_input": False,
        "message": "Required vendor details are present and no missing facts were invented.",
    }


def register_vendor(context: dict | None = None) -> dict:
    payload = _normalize_context(context)
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, int):
        return {
            "success": False,
            "error": "A persisted approval is required before vendor registration.",
            "failure_type": "approval_required",
        }

    try:
        with Session(engine) as session:
            approval = session.get(Approval, approval_id)
            if approval is None or approval.status != "executing":
                return {
                    "success": False,
                    "error": "An explicit human approval is required before vendor registration.",
                    "failure_type": "human_approval_required",
                }
            task = session.get(Task, approval.task_id)
            if task is None or task.status not in {"waiting_for_approval", "awaiting_approval"}:
                return {
                    "success": False,
                    "error": "The associated task is not awaiting approval.",
                    "failure_type": "invalid_task_state",
                }

            try:
                state = json.loads(task.result_json or "{}")
                proposal = json.loads(approval.execution_inputs_json or approval.action_json)
                plan = json.loads(task.plan_json or "{}")
            except (json.JSONDecodeError, TypeError):
                return {
                    "success": False,
                    "error": "Saved approval or workflow state is invalid.",
                    "failure_type": "invalid_saved_state",
                }

            if state.get("approval_id") != approval_id:
                return {
                    "success": False,
                    "error": "The approval does not match the active workflow checkpoint.",
                    "failure_type": "approval_mismatch",
                }

            registration_step = next((
                step.get("step")
                for step in plan.get("steps", [])
                if step.get("capability") == "register_vendor"
            ), None)
            if not isinstance(registration_step, int):
                return {
                    "success": False,
                    "error": "The workflow has no validated vendor registration step.",
                    "failure_type": "invalid_workflow_plan",
                }

            name = str(proposal.get("name") or "").strip()
            website = str(proposal.get("website") or "").strip()
            registration_number = str(proposal.get("registration_number") or "").strip() or None
            parsed_website = urlparse(website)
            if not name or parsed_website.scheme not in {"http", "https"} or not parsed_website.netloc:
                return {
                    "success": False,
                    "error": "The approved vendor name or website is invalid.",
                    "failure_type": "invalid_vendor_details",
                }

            if registration_number:
                duplicate = session.exec(
                    select(Vendor).where(Vendor.registration_number == registration_number)
                ).first()
                if duplicate:
                    return {
                        "success": False,
                        "error": "Registration number already exists.",
                        "failure_type": "duplicate_registration_number",
                    }

            decided_at = datetime.now(timezone.utc)
            vendor = Vendor(
                name=name,
                website=website,
                registration_number=registration_number,
                status="registered",
            )
            session.add(vendor)
            session.flush()
            vendor_id = vendor.id

            state["vendor"] = {
                "name": name,
                "website": website,
                "registration_number": registration_number,
            }
            state["completed_steps"] = sorted(set(
                state.get("completed_steps", []) + [registration_step]
            ))
            registration_result = {"success": True, "vendor_id": vendor_id, "status": "registered"}
            registration_record = next((
                item for item in reversed(state.get("step_results", []))
                if item.get("step") == registration_step
                and item.get("capability") == "register_vendor"
            ), None)
            if registration_record is None:
                registration_record = {
                    "step": registration_step,
                    "capability": "register_vendor",
                    "inputs": {"approval_id": approval_id},
                    "started_at": decided_at.isoformat(),
                }
                state.setdefault("step_results", []).append(registration_record)
            registration_record.update({
                "status": "completed",
                "outputs": registration_result,
                "result": registration_result,
                "success": True,
                "finished_at": decided_at.isoformat(),
                "verification_evidence": None,
            })
            state["current_step"] = registration_step
            state["final_summary"] = "Registration was committed; independently verifying the saved vendor record."
            task.status = "running"
            task.result_json = json.dumps(state)
            task.updated_at = decided_at
            session.add(ExecutionEvent(
                task_id=task.id,
                event_type="vendor_registered",
                message="Approved vendor record committed to SQLite.",
                details_json=json.dumps({
                    "approval_id": approval_id,
                    "vendor_id": vendor_id,
                    "approved_proposal": state["vendor"],
                }),
            ))
            session.add(task)
            session.commit()

            return {
                "success": True,
                "vendor_id": vendor_id,
                "status": "registered",
                "vendor": state["vendor"],
                "approval_id": approval_id,
            }
    except IntegrityError:
        return {
            "success": False,
            "error": "Registration number already exists.",
            "failure_type": "duplicate_registration_number",
        }


def check_vendor_duplicate(context: dict | None = None) -> dict:
    """Check the local vendor table for a registration-number collision."""
    payload = _normalize_context(context)
    registration_number = str(payload.get("registration_number") or "").strip()
    if not registration_number:
        return {
            "success": True,
            "checked": False,
            "duplicate": False,
            "message": "No registration number was provided; duplicate lookup was skipped.",
        }

    with Session(engine) as session:
        existing = session.exec(
            select(Vendor).where(Vendor.registration_number == registration_number)
        ).first()

    if existing:
        return {
            "success": False,
            "checked": True,
            "duplicate": True,
            "error": "Registration number already exists.",
            "vendor_id": existing.id,
            "failure_type": "duplicate_registration_number",
        }

    return {
        "success": True,
        "checked": True,
        "duplicate": False,
        "message": "No existing vendor uses this registration number.",
    }


TOOLS = {
    "read_company_policy": read_company_policy,
    "check_policy": check_policy,
    "check_vendor_duplicate": check_vendor_duplicate,
    "register_vendor": register_vendor,
}

TOOL_CONTRACTS = {
    "read_company_policy": {
        "description": "Read the current procurement policy file.",
        "inputs": {},
        "input_schema": _object_schema({}),
        "outputs": ["policy_text", "source"],
        "permissions": ["procurement:read"],
        "errors": ["policy_file_missing", "transient_io_error", "policy_read_error"],
        "requires_approval": False,
        "side_effect": False,
        "verification": "A fresh read of the policy file.",
    },
    "check_policy": {
        "description": "Validate vendor details against the procurement policy safeguards.",
        "inputs": {
            "name": "Required vendor legal name.",
            "website": "Required vendor http(s) website.",
            "registration_number": "Optional vendor registration number.",
            "policy_text": "Required policy evidence, normally from read_company_policy.",
        },
        "input_schema": _object_schema({
            "name": {"type": "string"},
            "vendor_name": {"type": "string"},
            "website": {"type": "string"},
            "vendor_website": {"type": "string"},
            "registration_number": {"type": "string"},
            "vendor_registration_number": {"type": "string"},
            "policy_text": {"type": "string"},
        }),
        "outputs": ["policy_status", "policy_requirements_checked", "vendor"],
        "permissions": ["procurement:read"],
        "errors": ["invalid_vendor_website", "missing_policy_evidence", "policy_requirements_not_met"],
        "requires_approval": False,
        "side_effect": False,
        "verification": "Repeat validation against the same inputs and policy evidence.",
    },
    "check_vendor_duplicate": {
        "description": "Check the local vendor table for a registration-number collision.",
        "inputs": {"registration_number": "Optional vendor registration number."},
        "input_schema": _object_schema({
            "name": {"type": "string"},
            "vendor_name": {"type": "string"},
            "website": {"type": "string"},
            "vendor_website": {"type": "string"},
            "registration_number": {"type": "string"},
            "policy_reference": {"type": "string"},
        }),
        "outputs": ["checked", "duplicate", "vendor_id"],
        "permissions": ["procurement:read"],
        "errors": ["duplicate_registration_number", "transient_database_error", "tool_error"],
        "requires_approval": False,
        "side_effect": False,
        "verification": "Repeat the duplicate lookup against the current vendor table.",
    },
    "register_vendor": {
        "description": "Register a vendor only after its persisted human approval is approved.",
        "expected_consequences": "Creates one vendor record in the procurement database after rechecking duplicates.",
        "inputs": {"approval_id": "Internal persisted approval request identifier."},
        "input_schema": _object_schema({
            "approval_id": {"type": "integer"},
            "name": {"type": "string"},
            "website": {"type": "string"},
            "registration_number": {"type": "string"},
        }),
        "outputs": ["vendor_id", "status", "vendor"],
        "permissions": ["procurement:write"],
        "errors": ["approval_required", "invalid_approval_state", "duplicate_registration_number", "tool_error"],
        "requires_approval": True,
        "side_effect": True,
        "risk": {
            "impact": "high",
            "reversibility": "irreversible",
            "data_sensitivity": "business",
            "external_consequences": "material",
            "autonomy_limits": {"one_vendor_per_approval": True},
        },
        "verification": "Retrieve the saved vendor record and compare it with the approved proposal.",
    },
}


def get_registered_tool_contracts() -> list[dict]:
    """Return contracts only for tools that are currently executable."""
    contracts = []
    for capability, tool in TOOLS.items():
        contract = TOOL_CONTRACTS.get(capability, {})
        risk = classify_action(contract, {})
        description = contract.get("description") or tool.__doc__ or capability
        if capability in {"api.read", "api.write"}:
            from app.tools.api import configured_operations

            operations = configured_operations(mutation=capability == "api.write")
            if operations:
                description += " Configured operations and their input schemas: " + json.dumps(
                    operations, ensure_ascii=True, sort_keys=True,
                )
        contracts.append({
            "capability": capability,
            "description": description,
            "inputs": contract.get("inputs", {}),
            "input_schema": contract.get("input_schema", _object_schema(contract.get("inputs", {}))),
            "outputs": contract.get("outputs", []),
            "output_format": contract.get("output_format", "JSON object with a boolean success field."),
            "permissions": contract.get("permissions", []),
            "errors": contract.get("errors", ["validation_error", "permission_denied", "tool_error"]),
            "requires_approval": risk.requires_approval,
            "side_effect": contract.get("side_effect", False),
            "permission_gated_side_effect": contract.get("permission_gated_side_effect", False),
            "risk": risk_as_dict(risk),
            "expected_consequences": contract.get("expected_consequences", contract.get("description", capability)),
            "timeout_seconds": contract.get("timeout_seconds"),
            "verification": contract.get("verification") or (
                "No independent verifier is registered; the outcome will not be reported as verified."
            ),
        })
    return contracts


TOOL_VERIFIERS: dict[str, Callable[[dict, dict], dict]] = {}


def register_tool(
    capability: str,
    handler: Callable,
    *,
    description: str,
    inputs: dict | None = None,
    input_schema: dict | None = None,
    outputs: list[str] | None = None,
    permissions: list[str] | tuple[str, ...] = ("business:read",),
    errors: list[str] | None = None,
    output_format: str = "JSON object with a boolean success field.",
    timeout_seconds: float | None = None,
    requires_approval: bool = False,
    side_effect: bool = False,
    permission_gated_side_effect: bool = False,
    risk: dict | None = None,
    expected_consequences: str | None = None,
    verification: str | None = None,
    verifier: Callable[[dict, dict], dict] | None = None,
) -> None:
    """Register an executable capability and its planning and verification contract."""
    if not capability or capability in TOOLS:
        raise ValueError("Capability names must be non-empty and unique.")
    if not callable(handler) or not isinstance(description, str) or not description.strip():
        raise ValueError("A callable handler and non-empty description are required.")
    if verifier is not None and not callable(verifier):
        raise ValueError("A tool verifier must be callable.")
    if not isinstance(permissions, (list, tuple)) or not all(
        isinstance(permission, str) and permission.strip() for permission in permissions
    ):
        raise ValueError("Tool permissions must be a list of non-empty permission names.")
    schema = input_schema or _object_schema(inputs or {})
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or not isinstance(schema.get("properties", {}), dict)
        or not isinstance(schema.get("required", []), list)
        or not all(key in schema.get("properties", {}) for key in schema.get("required", []))
        or _schema_definition_error(schema)
    ):
        raise ValueError("Tool input_schema must be a valid object schema.")
    if not isinstance(output_format, str) or not output_format.strip():
        raise ValueError("A non-empty tool output format is required.")
    if not isinstance(outputs, (list, type(None))) or (
        outputs is not None and not all(isinstance(output, str) for output in outputs)
    ):
        raise ValueError("Tool outputs must be a list of field names.")
    if not isinstance(errors, (list, type(None))) or (
        errors is not None and not all(isinstance(error, str) for error in errors)
    ):
        raise ValueError("Tool errors must be a list of failure type names.")
    if not all(isinstance(flag, bool) for flag in (requires_approval, side_effect, permission_gated_side_effect)):
        raise ValueError("Tool approval and side-effect flags must be boolean.")
    if timeout_seconds is not None and (not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0):
        raise ValueError("Tool timeout_seconds must be a positive number.")
    if risk is not None and not isinstance(risk, dict):
        raise ValueError("Tool risk metadata must be an object.")
    if expected_consequences is not None and (
        not isinstance(expected_consequences, str) or not expected_consequences.strip()
    ):
        raise ValueError("Expected consequences must be non-empty text.")

    TOOLS[capability] = handler
    TOOL_CONTRACTS[capability] = {
        "description": description.strip(),
        "inputs": inputs or {},
        "input_schema": schema,
        "outputs": outputs or [],
        "output_format": output_format.strip(),
        "permissions": list(permissions),
        "errors": errors or ["validation_error", "permission_denied", "tool_error"],
        "timeout_seconds": timeout_seconds,
        "requires_approval": requires_approval,
        "side_effect": side_effect,
        "permission_gated_side_effect": permission_gated_side_effect,
        "risk": risk or {},
        "expected_consequences": expected_consequences or description.strip(),
        "verification": verification,
    }
    if verifier is not None:
        TOOL_VERIFIERS[capability] = verifier


def verify_registered_tool(
    capability: str,
    context: dict | None,
    observed_result: dict,
) -> dict:
    """Run the verifier registered for a tool, or report that verification is unavailable."""
    verifier = TOOL_VERIFIERS.get(capability)
    if verifier is None:
        return {
            "verified": False,
            "status": "unavailable",
            "reason": "No independent verifier is registered for this capability.",
        }
    try:
        result = verifier(context or {}, observed_result)
        result.setdefault("status", "verified" if result.get("verified") else "mismatch")
        return result
    except Exception:
        logger.exception("Registered verifier for %s failed", capability)
        return {
            "verified": False,
            "status": "unavailable",
            "reason": "The registered verifier failed safely.",
        }


def _verify_policy_read(_context: dict, observed_result: dict) -> dict:
    actual = read_company_policy()
    matches = bool(
        actual.get("success")
        and observed_result.get("success")
        and actual.get("policy_text", "").strip()
        == observed_result.get("policy_text", "").strip()
    )
    return {"verified": matches, "actual": actual}


def _verify_registered_result(capability: str, context: dict, observed_result: dict) -> dict:
    actual = execute_tool(capability, context)
    output_fields = TOOL_CONTRACTS[capability]["outputs"]
    matches = bool(
        actual.get("success") == observed_result.get("success")
        and all(actual.get(field) == observed_result.get(field) for field in output_fields)
    )
    return {"verified": matches, "actual": actual}


def _verify_policy_check(context: dict, observed_result: dict) -> dict:
    return _verify_registered_result("check_policy", context, observed_result)


def _verify_vendor_duplicate(context: dict, observed_result: dict) -> dict:
    return _verify_registered_result("check_vendor_duplicate", context, observed_result)


TOOL_VERIFIERS.update({
    "read_company_policy": _verify_policy_read,
    "check_policy": _verify_policy_check,
    "check_vendor_duplicate": _verify_vendor_duplicate,
})


def execute_tool(
    capability: str,
    context: dict | None = None,
    *,
    permissions: set[str] | list[str] | tuple[str, ...] | None = None,
    approval_id: int | None = None,
    owner_id: str | None = None,
    task_id: int | None = None,
) -> dict:
    """Execute only explicitly registered tools. Always return a structured failure payload for unsupported or broken actions."""
    tool = TOOLS.get(capability)

    if tool is None:
        return {
            "success": False,
            "error": f"Unsupported capability: {capability}",
            "failure_type": "unsupported_capability",
            "requires_input": False,
        }

    arguments = context if context is not None else {}
    if not isinstance(arguments, dict):
        return {
            "success": False,
            "error": "Tool inputs must be a JSON object.",
            "failure_type": "validation_error",
            "requires_input": False,
        }

    contract = TOOL_CONTRACTS.get(capability, {})
    validation_error = _schema_error(arguments, contract.get("input_schema", _object_schema({})))
    if validation_error:
        return {
            "success": False,
            "error": validation_error,
            "failure_type": "validation_error",
            "requires_input": False,
        }
    if contains_secret_material(arguments):
        return {
            "success": False,
            "error": "Credential-like values cannot be passed to registered tools.",
            "failure_type": "secret_material_blocked",
            "requires_input": False,
        }

    if permissions is not None and (
        not isinstance(permissions, (set, list, tuple))
        or not all(isinstance(permission, str) for permission in permissions)
    ):
        return {
            "success": False,
            "error": "Execution permissions must be a collection of permission names.",
            "failure_type": "permission_denied",
            "requires_input": False,
        }
    granted_permissions = resolve_permissions(owner_id) if permissions is None else set(permissions)
    required_permissions = set(contract.get("permissions", []))
    if not required_permissions.issubset(granted_permissions):
        return {
            "success": False,
            "error": "The configured execution role is not authorized for this tool.",
            "failure_type": "permission_denied",
            "requires_input": False,
        }

    claimed_approval_id = None
    audit_task_id = task_id
    action_risk = classify_action(contract, arguments)
    if action_risk.requires_approval:
        if not isinstance(approval_id, int):
            return {
                "success": False,
                "error": "An explicit human approval is required before this action.",
                "failure_type": "approval_required",
                "requires_input": False,
            }
        with Session(engine) as session:
            approval = session.get(Approval, approval_id)
            task = session.get(Task, approval.task_id) if approval else None
            try:
                approved_parameters = approval_parameters(approval) if approval else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                approved_parameters = {}
                proposal_valid = False
            else:
                execution_parameters = approved_parameters if capability == "register_vendor" else arguments
                current_action = None
                if approval:
                    current_action = describe_action(capability, execution_parameters, contract, action_risk)
                    if capability == "register_vendor":
                        current_action = {**execution_parameters, **current_action}
                    proposal_valid = proposal_is_unchanged(
                        approval,
                        capability,
                        execution_parameters,
                        current_action,
                    )
                else:
                    proposal_valid = False
            if (
                approval is None
                or approval.status != "approved"
                or task is None
                or task.status == "cancelled"
                or owner_id is None
                or task.owner_id != owner_id
                or (approval.capability and approval.capability != capability)
                or approval_is_expired(approval)
                or not proposal_valid
            ):
                return {
                    "success": False,
                    "error": "The approval is missing, expired, altered, mismatched, or already consumed.",
                    "failure_type": "approval_proposal_mismatch" if approval and not proposal_valid else "invalid_approval_state",
                    "requires_input": False,
                }
            claimed = session.exec(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == "approved")
                .values(status="executing")
            )
            if claimed.rowcount != 1:
                return {
                    "success": False,
                    "error": "The approval has already been consumed.",
                    "failure_type": "invalid_approval_state",
                    "requires_input": False,
                }
            session.commit()
            claimed_approval_id = approval_id
            audit_task_id = task.id

    if audit_task_id is not None:
        with Session(engine) as session:
            task = session.get(Task, audit_task_id)
            if task is None or task.status == "cancelled":
                if claimed_approval_id is not None:
                    approval = session.get(Approval, claimed_approval_id)
                    if approval is not None and approval.status == "executing":
                        approval.status = "cancelled"
                        session.add(approval)
                        session.add(ExecutionEvent(
                            task_id=audit_task_id,
                            event_type="approval_execution_cancelled",
                            message="The approved action was stopped before its handler started.",
                            details_json=json.dumps({"approval_id": claimed_approval_id, "capability": capability}),
                        ))
                        session.commit()
                return {
                    "success": False,
                    "error": "The task was cancelled before this action could start.",
                    "failure_type": "task_cancelled",
                    "requires_input": False,
                }
            session.add(ExecutionEvent(
                task_id=audit_task_id,
                event_type="tool_invocation",
                message=f"Registered tool {capability} was invoked.",
                details_json=json.dumps({
                    "capability": capability,
                    "parameters_hash": proposal_digest(capability, arguments),
                    "approval_id": claimed_approval_id,
                }),
            ))
            session.commit()

    def finish_approval(status: str, result: dict) -> None:
        if claimed_approval_id is None:
            return
        with Session(engine) as session:
            approval = session.get(Approval, claimed_approval_id)
            if approval is not None and approval.status == "executing":
                approval.status = status
                session.add(approval)
                session.add(ExecutionEvent(
                    task_id=approval.task_id,
                    event_type="approval_execution_outcome",
                    message=f"Approved action {status}.",
                    details_json=json.dumps({
                        "approval_id": claimed_approval_id,
                        "capability": capability,
                        "success": bool(result.get("success")),
                        "failure_type": result.get("failure_type"),
                        "outcome_unknown": bool(result.get("outcome_unknown")),
                    }),
                ))
                session.commit()

    def audit_outcome(result: dict) -> None:
        if audit_task_id is None:
            return
        with Session(engine) as session:
            task = session.get(Task, audit_task_id)
            if task is not None:
                session.add(ExecutionEvent(
                    task_id=audit_task_id,
                    event_type="tool_outcome",
                    message=f"Registered tool {capability} {'succeeded' if result.get('success') else 'failed'}.",
                    details_json=json.dumps({
                        "capability": capability,
                        "success": bool(result.get("success")),
                        "failure_type": result.get("failure_type"),
                        "outcome_unknown": bool(result.get("outcome_unknown")),
                    }),
                ))
                session.commit()

    try:
        tool_arguments = arguments
        if capability in {"browser.inspect", "browser.interact"} and task_id is not None:
            tool_arguments = {**arguments, "_task_id": task_id}
        parameters = inspect.signature(tool).parameters
        if "context" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters.values()
        ):
            result = tool(tool_arguments)
        else:
            result = tool()
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise TypeError("Tool handlers must return an object with a boolean success field.")
        if contains_secret_material(result):
            result = {
                "success": False,
                "error": "Credential-like output was blocked before it could be returned or audited.",
                "failure_type": "secret_material_blocked",
                "requires_input": False,
            }
        json.dumps(result, allow_nan=False)
        finish_approval("executed" if result.get("success") else "failed", result)
        audit_outcome(result)
        return result
    except OperationalError as exc:
        logger.exception("Registered tool %s encountered a database error", capability)
        failure_type = (
            "transient_database_error"
            if any(token in str(exc).lower() for token in ("locked", "busy"))
            else "tool_error"
        )
        result = {
            "success": False,
            "error": "Database operation failed.",
            "failure_type": failure_type,
            "requires_input": False,
        }
        finish_approval("failed", result)
        audit_outcome(result)
        return result
    except Exception as exc:
        logger.exception("Registered tool %s failed", capability)
        result = _failure_from_exception(exc)
        finish_approval("failed", result)
        audit_outcome(result)
        return result


from app.tools.documents import register_document_tools
from app.tools.api import register_api_tools
from app.tools.browser import register_browser_tools
from app.tools.files import register_file_tools
from app.tools.web import register_web_search_tool, register_web_tools

register_file_tools(register_tool)
register_document_tools(register_tool)
register_web_search_tool(register_tool)
register_web_tools(register_tool)
register_api_tools(register_tool)
register_browser_tools(register_tool)
