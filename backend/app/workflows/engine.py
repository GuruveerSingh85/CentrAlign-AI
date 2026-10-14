import json
import hashlib
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import update
from sqlmodel import Session

from app.services.agent import create_plan
from app.services.approval_control import (
    approval_is_expired,
    approval_parameters,
    describe_action,
    new_approval,
    proposal_digest,
    proposal_is_unchanged,
)
from app.core.database import engine
from app.core.models import Approval, Task, Vendor
from app.services.memory import retrieve_context_safely
from app.services.task_scheduling import (
    claim_task_execution as _claim_task_execution,
    forget_scheduled_task,
    refresh_execution_lease as _refresh_execution_lease,
    release_task_execution,
    schedule_task_workflow,
    workflow_is_scheduled,
)
from app.tools.registry import TOOL_CONTRACTS, TOOLS, _schema_error, execute_tool, read_company_policy
from app.tools.browser import browser_auto_approval_allowed
from app.core.security import classify_action, resolve_permissions
from app.workflows.support.constants import (
    MAX_BROWSER_WORKFLOW_REPLANS,
    MAX_PARALLEL_STEPS,
    MAX_WORKFLOW_REPLANS,
    MAX_WORKFLOW_STEPS,
    MAX_SAFE_RETRIES,
    SAFE_RETRY_CAPABILITIES,
    TRANSIENT_FAILURE_TYPES,
    WORKFLOW_TIMEOUT_SECONDS,
    serialized_task_state,
    _scheduled_tasks,
    _task_lock,
)
from app.workflows.support.execution import (
    _decide_after_observation,
    _execute_observed_tool,
    _finish_policy_verification,
    _finish_registered_plan_verification,
    _finish_vendor_verification,
    _persist_step,
    _policy_text_from_state,
    _record_recovery_attempt,
    _write_failure,
)
from app.workflows.support.plan import _validated_plan
from app.workflows.support.state import _add_event, _new_state, _save_state, _state_from_task


def schedule_workflow(
    background_tasks: BackgroundTasks,
    task_id: int,
    context: dict | None = None,
    database_engine=None,
) -> bool:
    return schedule_task_workflow(
        background_tasks,
        task_id,
        run_task_workflow,
        context,
        database_engine or engine,
    )


def _step_context(step: dict, state: dict) -> dict:
    inherited_context = {
        **state.get("vendor", {}),
        **state.get("inputs", {}),
    }
    input_schema = TOOL_CONTRACTS.get(step.get("capability"), {}).get("input_schema", {})
    allowed_inputs = input_schema.get("properties", {})
    context = {
        key: value
        for key, value in inherited_context.items()
        if key in allowed_inputs
    }
    result_by_step = {
        item.get("step"): item.get("result", {})
        for item in state.get("step_results", [])
        if item.get("success")
    }

    def resolve_references(value):
        if isinstance(value, dict) and "$ref" in value:
            reference = value["$ref"]
            return result_by_step.get(reference["step"], {}).get(reference["field"])
        if isinstance(value, dict):
            return {key: resolve_references(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve_references(item) for item in value]
        return value

    for key, value in step.get("inputs", {}).items():
        context[key] = resolve_references(value)

    if step.get("capability") == "check_policy" and not context.get("policy_text"):
        context["policy_text"] = _policy_text_from_state(state)
    return context


def _normalize_saved_plan(plan: dict) -> dict:
    steps = plan.get("steps", [])
    normalized = []
    previous_step = None
    for step in steps:
        normalized_step = dict(step)
        if "depends_on" not in normalized_step:
            normalized_step["depends_on"] = [previous_step] if previous_step is not None else []
        normalized.append(normalized_step)
        previous_step = normalized_step.get("step")
    return {**plan, "steps": normalized}


def _create_plan_with_memory(objective: str, context: dict, memory_context: dict) -> dict:
    try:
        accepts_memory = "memory_context" in inspect.signature(create_plan).parameters
    except (TypeError, ValueError):
        accepts_memory = False
    if accepts_memory:
        return create_plan(objective, context, memory_context=memory_context)
    return create_plan(objective, context)


@serialized_task_state
def _mark_step_running(task_id: int, step: dict, inputs: dict) -> bool:
    interrupted_side_effect = None
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None or task.status == "cancelled":
            return False
        state = _state_from_task(task)
        state["current_step"] = step["step"]
        previous = next(
            (item for item in reversed(state["step_results"])
             if item.get("step") == step["step"]),
            None,
        )
        contract = TOOL_CONTRACTS.get(step["capability"], {})
        previous_result = (previous or {}).get("result") or {}
        if (
            previous
            and contract.get("side_effect")
            and (
                previous.get("status") == "running"
                or previous_result.get("outcome_unknown")
            )
        ):
            interrupted_side_effect = previous.get("inputs", {})
        else:
            record = {
                "step": step["step"],
                "capability": step["capability"],
                "depends_on": step.get("depends_on", []),
                "status": "running",
                "inputs": inputs,
                "outputs": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "verification_evidence": None,
                "success": False,
            }
            state["step_results"].append(record)
            task.status = "running"
            _save_state(task, state)
            _add_event(session, task_id, "step_started", f"Step {step['step']} started: {step['action']}.", {
                "step": step["step"],
                "capability": step["capability"],
                "depends_on": step.get("depends_on", []),
                "inputs": inputs,
            })
            session.add(task)
            session.commit()
            return True

    _persist_step(task_id, step, {
        "success": False,
        "error": "An interrupted side-effecting step has an unknown outcome and was not retried.",
        "failure_type": "interrupted_outcome_unknown",
        "outcome_unknown": True,
    }, succeeded=False, completed=False, inputs=interrupted_side_effect)
    return False


@serialized_task_state
def _request_replan(task_id: int, step: dict, result: dict) -> tuple[dict, str] | None:
    planning_error = None
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None or task.status == "cancelled":
            return None
        state = _state_from_task(task)
        plan = json.loads(task.plan_json or "{}")
        observation = {"step": step["step"], "capability": step["capability"], "result": result}
        fingerprint = hashlib.sha256(
            json.dumps(observation, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        replans = state.setdefault("replans", [])
        max_replans = (
            MAX_BROWSER_WORKFLOW_REPLANS
            if step.get("capability") in {"browser.inspect", "browser.interact"}
            else MAX_WORKFLOW_REPLANS
        )
        if fingerprint in replans or len(replans) >= max_replans:
            planning_error = {
                "step": step["step"],
                "replan_count": len(replans),
                "repeated_observation": fingerprint in replans,
            }
        else:
            replans.append(fingerprint)
        objective = task.objective
        owner_id = task.owner_id
        finished_steps = [
            item for item in plan.get("steps", [])
            if item.get("step") in state.get("completed_steps", [])
        ]
        compact_step_numbers = {
            item.get("step"): index
            for index, item in enumerate(finished_steps, start=1)
        }
        completed_plan_steps = []
        for index, item in enumerate(finished_steps, start=1):
            compact = {**item, "step": index}
            compact["depends_on"] = [
                compact_step_numbers[dependency]
                for dependency in item.get("depends_on", [])
                if dependency in compact_step_numbers
            ]
            completed_plan_steps.append(compact)
        context = {
            **state.get("inputs", {}),
            "execution_observation": observation,
            "completed_evidence": [
                item for item in state.get("step_results", []) if item.get("success")
            ],
            "completed_plan_steps": completed_plan_steps,
        }

    if planning_error:
        _write_failure(task_id, "The workflow stopped because replanning repeated without progress or exceeded its limit.", planning_error)
        return None

    with Session(engine) as session:
        memory_context = retrieve_context_safely(session, owner_id, objective)
    planner_plan = _create_plan_with_memory(objective, context, memory_context)
    replanned, next_workflow = _validated_plan(objective, planner_plan)
    if not replanned.get("supported") or next_workflow == "needs_input":
        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task is None:
                return None
            state = _state_from_task(task)
            state["replans"] = replans
            state["workflow"] = next_workflow
            state["missing_information"] = replanned.get("missing_information", [])
            state["final_summary"] = "The observed result requires additional information before execution can continue."
            task.status = "waiting_for_input" if next_workflow == "needs_input" else "failed"
            _save_state(task, state)
            _add_event(session, task_id, "plan_replanned", state["final_summary"], {
                "observation": observation,
                "plan": replanned,
            })
            session.add(task)
            session.commit()
        return None

    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        state = _state_from_task(task)
        old_plan = json.loads(task.plan_json or "{}")
        completed = set(state.get("completed_steps", []))
        preserved = [item for item in old_plan.get("steps", []) if item.get("step") in completed]
        offset = max((item.get("step", 0) for item in old_plan.get("steps", [])), default=0)
        appended = []
        remapped_steps = {}
        completed_by_signature = {
            (item.get("capability"), item.get("action"), json.dumps(item.get("inputs", {}), sort_keys=True)): item.get("step")
            for item in old_plan.get("steps", [])
            if item.get("step") in completed
        }
        for item in replanned.get("steps", []):
            signature = (
                item.get("capability"),
                item.get("action"),
                json.dumps(item.get("inputs", {}), sort_keys=True),
            )
            remapped_steps[item["step"]] = completed_by_signature.get(signature, item["step"] + offset)
        for item in replanned.get("steps", []):
            if remapped_steps[item["step"]] in completed:
                continue
            updated = {**item, "step": remapped_steps[item["step"]]}
            updated["depends_on"] = [remapped_steps[dependency] for dependency in item.get("depends_on", [])]

            def remap_references(value):
                if isinstance(value, dict) and "$ref" in value:
                    reference = {**value["$ref"]}
                    reference["step"] = remapped_steps[reference["step"]]
                    return {**value, "$ref": reference}
                if isinstance(value, dict):
                    return {key: remap_references(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [remap_references(item) for item in value]
                return value

            updated["inputs"] = remap_references(updated.get("inputs", {}))
            appended.append(updated)
        if len(preserved) + len(appended) > MAX_WORKFLOW_STEPS:
            _write_failure(task_id, "The replanned workflow exceeds the configured step limit.")
            return None
        updated_plan = {**replanned, "steps": preserved + appended}
        state["workflow"] = next_workflow
        state["replans"] = replans
        state["final_summary"] = None
        task.plan_json = json.dumps(updated_plan)
        _save_state(task, state)
        _add_event(session, task_id, "plan_replanned", "Execution results changed the next action; the remaining plan was replaced.", {
            "observation": observation,
            "plan": updated_plan,
            "preserved_steps": sorted(completed),
        })
        session.add(task)
        session.commit()
    return updated_plan, next_workflow


def run_task_workflow(
    task_id: int,
    incoming_context: dict | None = None,
    execution_token: str | None = None,
) -> None:
    if execution_token is None:
        execution_token = _claim_task_execution(task_id, engine)
        if execution_token is None:
            return
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None or task.execution_token != execution_token or task.status == "cancelled":
            return

    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_refresh_execution_lease,
        args=(task_id, execution_token, stop_heartbeat, engine),
        daemon=True,
    )
    heartbeat.start()
    try:
        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            objective = task.objective
            owner_id = task.owner_id
            organization_id = task.organization_id
            state = _state_from_task(task)
            state.setdefault("inputs", {})
            state.setdefault("vendor", {})
            for key, value in (incoming_context or {}).items():
                if value not in (None, ""):
                    state["inputs"][key] = value
                    if key in {"name", "vendor_name"}:
                        state["vendor"]["name"] = value
                    elif key in {"website", "vendor_website"}:
                        state["vendor"]["website"] = value
                    elif key == "registration_number":
                        state["vendor"][key] = value
            _save_state(task, state)
            session.add(task)
            session.commit()
            plan_json = task.plan_json

        should_create_plan = not plan_json
        if plan_json:
            plan = _normalize_saved_plan(json.loads(plan_json))
            workflow = state.get("workflow")
            if workflow is None:
                if not plan.get("supported", True):
                    workflow = "unsupported"
                elif any(step.get("capability") == "register_vendor" for step in plan.get("steps", [])):
                    workflow = "procurement_vendor_registration"
                elif all(step.get("capability") == "read_company_policy" for step in plan.get("steps", [])):
                    workflow = "procurement_policy_review"
                else:
                    workflow = "registered_tool_plan"
            should_create_plan = workflow == "needs_input" and not plan.get("steps")

        if should_create_plan:
            with Session(engine) as session:
                memory_context = retrieve_context_safely(
                    session,
                    owner_id,
                    objective,
                    organization_id=organization_id,
                )
            planner_plan = _create_plan_with_memory(
                objective, state.get("inputs", {}), memory_context
            )
            plan, workflow = _validated_plan(objective, planner_plan)
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task is None:
                    return
                state = _state_from_task(task)
                state["workflow"] = workflow
                _save_state(task, state)
                task.plan_json = json.dumps(plan)
                task.updated_at = datetime.now(timezone.utc)
                event_type = "plan_updated" if plan_json else "plan_created"
                event_message = (
                    "The plan was regenerated with newly supplied information."
                    if plan_json
                    else "A validated execution plan was created."
                )
                _add_event(session, task_id, event_type, event_message, plan)
                session.add(task)
                session.commit()

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            state = _state_from_task(task)

        if workflow == "unsupported":
            unavailable = plan.get("unavailable_capabilities", [])
            message = plan.get("limitations") or [
                "The required integration is not available in the registered tool set."
            ]
            summary = " ".join(message)
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task is None:
                    return
                state = _state_from_task(task)
                state["final_summary"] = summary
                state["unavailable_capabilities"] = unavailable
                task.status = "unsupported"
                _save_state(task, state)
                _add_event(session, task_id, "objective_unsupported", summary, {
                    "unavailable_capabilities": unavailable,
                    "limitations": message,
                })
                session.add(task)
                session.commit()
            return

        if workflow == "needs_input":
            missing = plan.get("missing_information", [])
            message = "Additional information is required before a supported plan can be created."
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task is None:
                    return
                state = _state_from_task(task)
                state["missing_information"] = missing
                state["final_summary"] = None
                task.status = "waiting_for_input"
                _save_state(task, state)
                _add_event(session, task_id, "input_required", message, {"missing_information": missing})
                session.add(task)
                session.commit()
            return

        if len(plan.get("steps", [])) > MAX_WORKFLOW_STEPS:
            _write_failure(task_id, "The saved plan exceeds the configured step limit.")
            return

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            state = _state_from_task(task)
            timestamps = state.setdefault("timestamps", {"started_at": None, "completed_at": None})
            timestamps["started_at"] = timestamps.get("started_at") or datetime.now(timezone.utc).isoformat()
            session.add(task)
            _save_state(task, state)
            session.commit()

        deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
        while True:
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task is None or task.status == "cancelled":
                    return
                state = _state_from_task(task)
            completed = set(state.get("completed_steps", []))
            pending_steps = [step for step in plan["steps"] if step["step"] not in completed]
            if not pending_steps:
                break
            if time.monotonic() >= deadline:
                _write_failure(task_id, "The workflow exceeded its configured execution time limit.", {
                    "timeout_seconds": WORKFLOW_TIMEOUT_SECONDS,
                    "completed_steps": sorted(completed),
                })
                return

            ready = [
                step for step in pending_steps
                if set(step.get("depends_on", [])).issubset(completed)
            ]
            if not ready:
                _write_failure(task_id, "The saved plan has unresolved or circular dependencies.", {
                    "pending_steps": [step["step"] for step in pending_steps],
                })
                return

            first_contract = TOOL_CONTRACTS.get(ready[0]["capability"], {})
            parallel_safe = not first_contract.get("side_effect") and not ready[0].get("requires_approval")
            batch = [ready[0]]
            if parallel_safe:
                for candidate in ready[1:]:
                    contract = TOOL_CONTRACTS.get(candidate["capability"], {})
                    if contract.get("side_effect") or candidate.get("requires_approval"):
                        continue
                    batch.append(candidate)
                    if len(batch) >= MAX_PARALLEL_STEPS:
                        break

            auto_approval_ids = {}
            for step in batch:
                capability = step["capability"]
                context = _step_context(step, state)
                previous_result = next((
                    item.get("result", {})
                    for item in reversed(state.get("step_results", []))
                    if item.get("step") == step["step"]
                    and item.get("capability") == capability
                ), {})
                if (
                    TOOL_CONTRACTS.get(capability, {}).get("side_effect")
                    and previous_result.get("outcome_unknown")
                ):
                    _persist_step(
                        task_id,
                        step,
                        {
                            **previous_result,
                            "error": "The prior side-effect outcome is unknown; reconcile it before retrying.",
                            "failure_type": "interrupted_outcome_unknown",
                            "outcome_unknown": True,
                        },
                        succeeded=False,
                        completed=False,
                        inputs=context,
                    )
                    return
                if capability == "register_vendor":
                    with Session(engine) as session:
                        task = session.get(Task, task_id)
                        if task is None:
                            return
                        state = _state_from_task(task)
                        if state.get("approval_id"):
                            task.status = "waiting_for_approval"
                            session.add(task)
                            session.commit()
                            return
                        proposal = {
                            "name": context.get("name") or context.get("vendor_name", ""),
                            "website": context.get("website") or context.get("vendor_website", ""),
                            "registration_number": context.get("registration_number"),
                        }
                        contract = TOOL_CONTRACTS.get(capability, {})
                        risk = classify_action(contract, proposal)
                        display_action = {
                            **proposal,
                            **describe_action(capability, proposal, contract, risk),
                        }
                        approval = new_approval(task, capability, proposal, display_action, risk)
                        session.add(approval)
                        session.flush()
                        state["approval_id"] = approval.id
                        state["current_step"] = step["step"]
                        state["final_summary"] = "Waiting for explicit human approval."
                        state["step_results"].append({
                            "step": step["step"],
                            "capability": capability,
                            "depends_on": step.get("depends_on", []),
                            "status": "waiting_for_approval",
                            "inputs": context,
                            "outputs": {"approval_id": approval.id, "proposed_action": proposal},
                            "result": {"approval_id": approval.id, "proposed_action": proposal},
                            "success": False,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "finished_at": None,
                            "verification_evidence": None,
                        })
                        task.status = "waiting_for_approval"
                        _save_state(task, state)
                        _add_event(session, task_id, "approval_required", "Vendor registration is paused for human approval.", {
                            "approval_id": approval.id,
                            "step": step["step"],
                            "proposed_action": display_action,
                            "action_hash": approval.action_hash,
                            "expires_at": approval.expires_at.isoformat(),
                            "policy_evidence": _policy_text_from_state(state),
                        })
                        session.add(task)
                        session.commit()
                    return
                contract = TOOL_CONTRACTS.get(capability, {})
                risk = classify_action(contract, context)
                needs_approval = bool(step.get("requires_approval") or risk.requires_approval)
                browser_auto_approved = bool(
                    capability == "browser.interact"
                    and needs_approval
                    and browser_auto_approval_allowed(context, task_id)
                )
                if needs_approval and not browser_auto_approved:
                    validation_error = _schema_error(context, contract.get("input_schema", {}))
                    if validation_error:
                        _persist_step(
                            task_id,
                            step,
                            {"success": False, "error": validation_error, "failure_type": "validation_error"},
                            succeeded=False,
                            completed=False,
                            inputs=context,
                        )
                        return
                    display_action = describe_action(capability, context, contract, risk)
                    with Session(engine) as session:
                        task = session.get(Task, task_id)
                        if task is None:
                            return
                        state = _state_from_task(task)
                        approval = new_approval(task, capability, context, display_action, risk)
                        session.add(approval)
                        session.flush()
                        state["approval_id"] = approval.id
                        state["current_step"] = step["step"]
                        state["final_summary"] = "Waiting for explicit human approval."
                        state["step_results"].append({
                            "step": step["step"],
                            "capability": capability,
                            "depends_on": step.get("depends_on", []),
                            "status": "waiting_for_approval",
                            "inputs": context,
                            "outputs": {"approval_id": approval.id, "proposed_action": display_action},
                            "result": {"approval_id": approval.id, "proposed_action": display_action},
                            "success": False,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "finished_at": None,
                            "verification_evidence": None,
                        })
                        task.status = "waiting_for_approval"
                        _save_state(task, state)
                        _add_event(session, task_id, "approval_required", "A registered action is paused for explicit human approval.", {
                            "approval_id": approval.id,
                            "capability": capability,
                            "step": step["step"],
                            "proposed_action": display_action,
                            "action_hash": approval.action_hash,
                            "expires_at": approval.expires_at.isoformat(),
                        })
                        session.add(task)
                        session.commit()
                    return
                if browser_auto_approved:
                    validation_error = _schema_error(context, contract.get("input_schema", {}))
                    if validation_error:
                        _persist_step(
                            task_id,
                            step,
                            {"success": False, "error": validation_error, "failure_type": "validation_error"},
                            succeeded=False,
                            completed=False,
                            inputs=context,
                        )
                        return
                    display_action = describe_action(capability, context, contract, risk)
                    with Session(engine) as session:
                        task = session.get(Task, task_id)
                        if task is None:
                            return
                        approval = new_approval(task, capability, context, display_action, risk)
                        approval.status = "approved"
                        approval.decided_at = datetime.now(timezone.utc)
                        approval.decided_by = "browser_auto_approval_policy"
                        session.add(approval)
                        session.flush()
                        auto_approval_ids[step["step"]] = approval.id
                        _add_event(session, task_id, "approval_autoapproved", "Browser action was approved by the configured allowlist policy.", {
                            "approval_id": approval.id,
                            "capability": capability,
                            "step": step["step"],
                            "proposed_action": display_action,
                            "action_hash": approval.action_hash,
                            "decided_by": approval.decided_by,
                        })
                        session.commit()
                if not _mark_step_running(task_id, step, context):
                    return

            def execute_step(step):
                context = _step_context(step, state)
                return _execute_observed_tool(
                    task_id,
                    step["capability"],
                    context,
                    executor=execute_tool,
                    step_number=step["step"],
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                    owner_id=owner_id,
                    approval_id=auto_approval_ids.get(step["step"]),
                )

            if len(batch) == 1:
                observed = [(batch[0], execute_step(batch[0]))]
            else:
                with ThreadPoolExecutor(max_workers=min(len(batch), MAX_PARALLEL_STEPS)) as pool:
                    futures = {pool.submit(execute_step, step): step for step in batch}
                    observed = [(futures[future], future.result()) for future in futures]

            any_failed = False
            replan_result = None
            replan_step = None
            for step, result in observed:
                succeeded = bool(result.get("success"))
                state = _persist_step(
                    task_id,
                    step,
                    result,
                    succeeded=succeeded,
                    completed=succeeded,
                    inputs=_step_context(step, state),
                )
                if not succeeded:
                    any_failed = True
                if (
                    result.get("replan_required")
                    or result.get("next_action") == "replan"
                    or (succeeded and step["capability"] == "web.search")
                ):
                    replan_result = result
                    replan_step = step
            if replan_result is not None:
                updated = _request_replan(task_id, replan_step, replan_result)
                if updated is None:
                    return
                plan, workflow = updated
                continue
            if any_failed:
                with Session(engine) as session:
                    task = session.get(Task, task_id)
                    if task is None:
                        return
                    state = _state_from_task(task)
                    if task.status == "cancelled":
                        session.add(task)
                        session.commit()
                        return
                    missing = sorted({
                        value
                        for item in state.get("step_results", [])
                        if item.get("status") == "waiting_for_input"
                        for value in item.get("result", {}).get("missing_information", [])
                    })
                    if missing:
                        task.status = "waiting_for_input"
                        state["missing_information"] = missing
                    elif state.get("completed_steps"):
                        task.status = "partially_completed"
                    else:
                        task.status = "failed"
                    _save_state(task, state)
                    session.add(task)
                    session.commit()
                return

        if workflow == "procurement_vendor_registration":
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task is None:
                    return
                state = _state_from_task(task)
                registered = any(
                    item.get("capability") == "register_vendor" and item.get("success")
                    for item in state.get("step_results", [])
                )
            if registered:
                _finish_vendor_verification(task_id)
            else:
                _write_failure(task_id, "The workflow ended without an approved vendor registration.")
        elif workflow == "procurement_policy_review":
            _finish_policy_verification(task_id)
        else:
            _finish_registered_plan_verification(task_id, plan)
    except (ValueError, TypeError, json.JSONDecodeError):
        _write_failure(task_id, "The planner returned an invalid plan; no unsupported action was run.")
    except Exception:
        _write_failure(task_id, "The workflow encountered an internal error and stopped safely.")
    finally:
        stop_heartbeat.set()
        try:
            release_task_execution(task_id, execution_token, engine)
        finally:
            forget_scheduled_task(task_id)
            try:
                with Session(engine) as session:
                    task = session.get(Task, task_id)
                    task_status = task.status if task is not None else "missing"
                if task_status not in {
                    "waiting_for_approval", "awaiting_approval", "waiting_for_input",
                    "needs_input", "planned", "pending", "running", "processing",
                }:
                    from app.tools.browser import close_browser_session

                    close_browser_session(task_id)
            except Exception:
                pass


def decide_vendor_approval(
    approval_id: int,
    decision: str,
    owner_id: str = "default",
    background_tasks: BackgroundTasks | None = None,
) -> dict:
    decision = decision.strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Decision must be approve or reject.")
    if "approval:decide" not in resolve_permissions(owner_id):
        raise HTTPException(status_code=403, detail="The authenticated role cannot decide approvals.")

    with Session(engine) as session:
        approval = session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="Approval request not found.")
        task = session.get(Task, approval.task_id)
        if task is None or task.owner_id != owner_id:
            raise HTTPException(status_code=404, detail="Associated task not found.")

        task_id = task.id
        state = _state_from_task(task)
        if task.status not in {"waiting_for_approval", "awaiting_approval"} or state.get("approval_id") != approval_id:
            raise HTTPException(status_code=409, detail="This task is not waiting for this approval.")
        if approval.status != "pending":
            raise HTTPException(status_code=409, detail="This approval has already been decided.")

        if approval_is_expired(approval):
            approval.status = "expired"
            approval.decided_at = datetime.now(timezone.utc)
            task.status = "approval_expired"
            state["final_summary"] = "The approval expired; the proposed action was not executed."
            _save_state(task, state)
            _add_event(session, task_id, "approval_expired", state["final_summary"], {
                "approval_id": approval_id,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            })
            session.add(approval)
            session.add(task)
            session.commit()
            from app.tools.browser import close_browser_session

            close_browser_session(task_id)
            raise HTTPException(status_code=410, detail="This approval has expired.")

        capability = approval.capability or "register_vendor"
        try:
            execution_parameters = approval_parameters(approval)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="The saved approval proposal is invalid.") from exc
        approval_contract = TOOL_CONTRACTS.get(capability, {})
        approval_risk = classify_action(approval_contract, execution_parameters)
        current_action = describe_action(
            capability,
            execution_parameters,
            approval_contract,
            approval_risk,
        )
        if capability == "register_vendor":
            current_action = {**execution_parameters, **current_action}
        if not proposal_is_unchanged(
            approval,
            capability,
            execution_parameters,
            current_action,
        ):
            raise HTTPException(status_code=409, detail="The approved proposal has changed; request approval again.")

        step = None
        if capability != "register_vendor":
            try:
                plan = json.loads(task.plan_json or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                raise HTTPException(status_code=409, detail="The saved task plan is invalid.") from exc
            step = next((
                item for item in plan.get("steps", [])
                if item.get("step") == state.get("current_step")
                and item.get("capability") == capability
            ), None)
            pending_record = next((
                item for item in reversed(state.get("step_results", []))
                if item.get("step") == state.get("current_step")
                and item.get("capability") == capability
                and item.get("status") == "waiting_for_approval"
            ), None)
            if (
                step is None
                or pending_record is None
                or proposal_digest(capability, pending_record.get("inputs", {}))
                != proposal_digest(capability, execution_parameters)
            ):
                raise HTTPException(status_code=409, detail="The approved action no longer matches the task plan.")

        if decision == "reject":
            updated = session.exec(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == "pending")
                .values(
                    status="rejected",
                    decided_at=datetime.now(timezone.utc),
                    decided_by=owner_id,
                )
            )
            if updated.rowcount != 1:
                raise HTTPException(status_code=409, detail="This approval has already been decided.")
            task.status = "rejected"
            state["final_summary"] = "Vendor registration was rejected; no vendor record was created."
            _save_state(task, state)
            _add_event(session, task_id, "approval_rejected", state["final_summary"], {
                "approval_id": approval_id,
                "proposed_action": json.loads(approval.action_json),
            })
            session.add(task)
            session.commit()
            from app.tools.browser import close_browser_session

            close_browser_session(task_id)
            return {"approval_id": approval_id, "task_id": task_id, "status": "rejected"}

        decided_at = datetime.now(timezone.utc)
        updated = session.exec(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status="approved", decided_at=decided_at, decided_by=owner_id)
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="This approval has already been decided.")
        _add_event(session, task_id, "approval_approved", f"{capability} was explicitly approved.", {
            "approval_id": approval_id,
            "decided_by": owner_id,
            "proposed_action": json.loads(approval.action_json),
        })
        session.commit()

    if capability != "register_vendor":
        result = execute_tool(
            capability,
            execution_parameters,
            approval_id=approval_id,
            owner_id=owner_id,
            task_id=task_id,
        )
        _persist_step(
            task_id,
            step,
            result,
            succeeded=bool(result.get("success")),
            completed=bool(result.get("success")),
            inputs=execution_parameters,
        )
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result.get("error", "The approved action failed safely."))
        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task is None or task.status == "cancelled":
                return {
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "status": "approved",
                    "action_status": "executed_after_cancellation",
                }
        if background_tasks is not None:
            schedule_workflow(background_tasks, task_id)
        else:
            run_task_workflow(task_id)
        return {
            "approval_id": approval_id,
            "task_id": task_id,
            "status": "approved",
            "action_status": "executed",
        }

    registration = execute_tool(
        "register_vendor",
        {"approval_id": approval_id},
        approval_id=approval_id,
        owner_id=owner_id,
    )
    if not registration.get("success"):
        failure_type = registration.get("failure_type")
        if failure_type != "invalid_approval_state":
            _write_failure(task_id, registration.get("error", "Vendor registration failed safely."), {
                "approval_id": approval_id,
                "failure_type": failure_type,
            })
        raise HTTPException(
            status_code=409,
            detail=registration.get("error", "Vendor registration failed safely."),
        )

    verification = _finish_vendor_verification(task_id)
    return {
        "approval_id": approval_id,
        "task_id": task_id,
        "status": "approved",
        "task_status": "completed" if verification and verification["matches"] else "failed",
        "vendor_id": registration["vendor_id"],
        "verified": bool(verification and verification["matches"]),
    }


__all__ = [
    "MAX_SAFE_RETRIES",
    "SAFE_RETRY_CAPABILITIES",
    "TRANSIENT_FAILURE_TYPES",
    "_add_event",
    "_decide_after_observation",
    "_execute_observed_tool",
    "_finish_policy_verification",
    "_finish_vendor_verification",
    "_new_state",
    "_persist_step",
    "_policy_text_from_state",
    "_record_recovery_attempt",
    "_save_state",
    "_state_from_task",
    "_validated_plan",
    "_write_failure",
    "create_plan",
    "decide_vendor_approval",
    "execute_tool",
    "read_company_policy",
    "run_task_workflow",
    "schedule_workflow",
    "workflow_is_scheduled",
]
