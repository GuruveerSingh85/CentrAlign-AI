import re

from app.tool_registry import TOOLS, TOOL_CONTRACTS
from app.security import classify_action
from app.workflow_support.constants import MAX_WORKFLOW_STEPS


def _is_json_value(value) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _base_plan(objective: str, planner_plan: dict) -> dict:
    goal = planner_plan.get("goal")
    success_criteria = planner_plan.get("success_criteria") or []
    risks = planner_plan.get("risks") or []
    limitations = planner_plan.get("limitations") or []
    unavailable_capabilities = planner_plan.get("unavailable_capabilities") or []
    missing_information = planner_plan.get("missing_information") or []

    for value, label in (
        (success_criteria, "success criteria"),
        (risks, "risks"),
        (limitations, "limitations"),
        (unavailable_capabilities, "unavailable capabilities"),
        (missing_information, "missing information"),
    ):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Planner {label} must be a list of strings.")

    return {
        "goal": goal.strip() if isinstance(goal, str) and goal.strip() else objective,
        "success_criteria": success_criteria or [
            "Complete every planned registered-tool step and independently verify its outcome."
        ],
        "risks": risks,
        "limitations": limitations,
        "unavailable_capabilities": unavailable_capabilities,
        "missing_information": missing_information,
    }


def _unsupported_plan(base: dict, capability: str, reason: str) -> tuple[dict, str]:
    unavailable = list(base["unavailable_capabilities"])
    if capability and capability not in unavailable:
        unavailable.append(capability)
    limitations = list(base["limitations"])
    if reason not in limitations:
        limitations.append(reason)
    return {
        **base,
        "supported": False,
        "steps": [],
        "limitations": limitations,
        "unavailable_capabilities": unavailable,
        "planner_steps": [],
    }, "unsupported"


def _validated_plan(objective: str, planner_plan: dict) -> tuple[dict, str]:
    if not isinstance(planner_plan, dict):
        raise ValueError("Planner output must be an object.")

    base = _base_plan(objective, planner_plan)
    planner_steps = planner_plan.get("steps")
    if not isinstance(planner_steps, list):
        raise ValueError("Planner steps must be a list.")
    if len(planner_steps) > MAX_WORKFLOW_STEPS:
        raise ValueError(f"Planner step count exceeds the limit of {MAX_WORKFLOW_STEPS}.")

    objective_lower = objective.lower()
    requests_policy_review = bool(re.search(
        r"\b(review|summari[sz]e|explain|assess|audit)\b.{0,80}\b(?:procurement\s+)?policy\b|"
        r"\b(?:procurement\s+)?policy\b.{0,80}\b(review|summari[sz]e|explain|assess|audit)\b",
        objective_lower,
    ))
    asks_for_vendor_registration = bool(re.search(
        r"\b(vendor|supplier)\b.{0,80}\b(register|registration|onboard|add|create)\b|"
        r"\b(register|registration|onboard|add|create)\b.{0,80}\b(vendor|supplier)\b",
        objective_lower,
    )) and not bool(re.search(
        r"\b(?:do not|don't|never|not to|without)\s+(?:register(?:ing)?|registration|onboard(?:ing)?|add(?:ing)?|creat(?:e|ing))\b.{0,80}\b(vendor|supplier)\b|"
        r"\b(vendor|supplier)\b.{0,80}\b(?:must not|should not|do not|don't|never)\s+(?:be\s+)?(?:registered|onboarded|added|created)\b",
        objective_lower,
    ))

    if planner_plan.get("supported") is False and not (
        asks_for_vendor_registration and "register_vendor" in TOOLS
    ):
        return _unsupported_plan(
            base,
            "",
            "The required business capability is not available in the registered tool set.",
        )

    validated_planner_steps = []
    for index, step in enumerate(planner_steps, start=1):
        if not isinstance(step, dict):
            raise ValueError("Planner output contains an invalid step.")

        capability = step.get("capability")
        if not isinstance(capability, str) or capability not in TOOLS:
            return _unsupported_plan(
                base,
                str(capability or "unidentified integration"),
                f"The plan requires '{capability or 'an unidentified integration'}', which is not registered.",
            )

        action = step.get("action")
        expected_result = step.get("expected_result")
        requires_approval = step.get("requires_approval", False)
        dependencies = step.get("depends_on", [index - 1] if index > 1 else [])
        inputs = step.get("inputs", {})
        step_success_criteria = step.get("success_criteria", [expected_result])

        if not isinstance(action, str) or not action.strip():
            raise ValueError("Planner step is missing an action description.")
        if not isinstance(expected_result, str) or not expected_result.strip():
            raise ValueError("Planner step is missing an expected result.")
        if not isinstance(requires_approval, bool):
            raise ValueError("Planner approval requirements must be boolean.")
        valid_dependencies = isinstance(dependencies, list) and all(
            isinstance(dependency, int)
            and not isinstance(dependency, bool)
            and 1 <= dependency <= len(planner_steps)
            and dependency != index
            for dependency in dependencies
        )
        if not valid_dependencies or len(dependencies) != len(set(dependencies)):
            raise ValueError("Planner dependencies must reference valid, distinct step numbers.")
        if not isinstance(inputs, dict) or not _is_json_value(inputs):
            raise ValueError("Planner step inputs must be a JSON object.")
        if not isinstance(step_success_criteria, list) or not all(
            isinstance(item, str) for item in step_success_criteria
        ):
            raise ValueError("Planner step success criteria must be a list of strings.")

        for input_value in inputs.values():
            if isinstance(input_value, dict) and "$ref" in input_value:
                reference = input_value["$ref"]
                if (
                    set(input_value) != {"$ref"}
                    or not isinstance(reference, dict)
                    or set(reference) != {"step", "field"}
                    or reference.get("step") not in dependencies
                    or not isinstance(reference.get("field"), str)
                    or not reference["field"].strip()
                ):
                    raise ValueError("Planner input references must point to a declared dependency output.")

        validated_planner_steps.append({
            "step": index,
            "action": action.strip(),
            "capability": capability,
            "depends_on": dependencies,
            "inputs": inputs,
            "requires_approval": (
                requires_approval
                or classify_action(TOOL_CONTRACTS.get(capability, {}), inputs).requires_approval
            ),
            "expected_result": expected_result.strip(),
            "success_criteria": step_success_criteria,
        })

    dependencies_by_step = {
        step["step"]: step["depends_on"] for step in validated_planner_steps
    }
    visiting = set()
    visited = set()

    def visit(step_number):
        if step_number in visiting:
            raise ValueError("Planner dependencies contain a circular dependency.")
        if step_number in visited:
            return
        visiting.add(step_number)
        for dependency in dependencies_by_step[step_number]:
            visit(dependency)
        visiting.remove(step_number)
        visited.add(step_number)

    for step_number in dependencies_by_step:
        visit(step_number)

    planner_requests_registration = any(
        step["capability"] == "register_vendor" for step in validated_planner_steps
    )
    if planner_requests_registration and not asks_for_vendor_registration:
        raise ValueError("The plan requested vendor registration that the user did not ask for.")

    if asks_for_vendor_registration:
        if not {"read_company_policy", "check_policy", "check_vendor_duplicate", "register_vendor"}.issubset(TOOLS):
            return _unsupported_plan(
                base,
                "procurement vendor registration",
                "The complete guarded vendor-registration tool chain is not available.",
            )
        steps = [
            {
                "step": 1,
                "action": "Read the current procurement policy",
                "capability": "read_company_policy",
                "depends_on": [],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "The policy file is read and its contents are saved as evidence.",
                "success_criteria": ["The current procurement policy is available as evidence."],
            },
            {
                "step": 2,
                "action": "Validate policy requirements and vendor details",
                "capability": "check_policy",
                "depends_on": [1],
                "inputs": {"policy_text": {"$ref": {"step": 1, "field": "policy_text"}}},
                "requires_approval": False,
                "expected_result": "The policy safeguards and supplied vendor details pass validation.",
                "success_criteria": ["Vendor details satisfy the procurement safeguards."],
            },
            {
                "step": 3,
                "action": "Check the vendor registration number for duplicates",
                "capability": "check_vendor_duplicate",
                "depends_on": [2],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "No existing vendor uses the proposed registration number.",
                "success_criteria": ["No vendor uses the supplied registration number."],
            },
            {
                "step": 4,
                "action": "Request explicit approval to register the vendor",
                "capability": "register_vendor",
                "depends_on": [2, 3],
                "inputs": {},
                "requires_approval": True,
                "expected_result": "A persistent approval request contains the exact proposed vendor record.",
                "success_criteria": ["The exact vendor proposal is awaiting human approval."],
            },
        ]
        workflow = "procurement_vendor_registration"
    elif not planner_steps:
        if base["missing_information"]:
            return {
                **base,
                "supported": True,
                "steps": [],
                "planner_steps": [],
            }, "needs_input"
        return _unsupported_plan(
            base,
            "",
            "No registered tool plan was provided for this objective.",
        )
    else:
        if (
            all(step["capability"] == "read_company_policy" for step in validated_planner_steps)
            or (
                requests_policy_review
                and all(step["capability"] in {"read_company_policy", "check_policy"} for step in validated_planner_steps)
            )
        ):
            steps = [{
                "step": 1,
                "action": "Read the current procurement policy",
                "capability": "read_company_policy",
                "depends_on": [],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "The policy file is read and independently compared with the saved result.",
                "success_criteria": ["The current policy text matches a fresh source read."],
            }]
            workflow = "procurement_policy_review"
        else:
            steps = validated_planner_steps
            workflow = "registered_tool_plan"

    return {
        **base,
        "supported": True,
        "steps": steps,
        "planner_steps": validated_planner_steps,
    }, workflow
