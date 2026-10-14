import json
import inspect
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone

from sqlmodel import Session

from app.core.database import engine as default_engine
from app.core.models import Approval, Task, Vendor
from app.services.memory import record_verified_procedure
from app.tools.registry import TOOL_CONTRACTS, execute_tool, read_company_policy, verify_registered_tool
from app.workflows.support.constants import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    MAX_SAFE_RETRIES,
    SAFE_RETRY_CAPABILITIES,
    TRANSIENT_FAILURE_TYPES,
    serialized_task_state,
)
from app.workflows.support.state import _add_event, _save_state, _state_from_task


def _workflow_module():
    try:
        import app.workflows.engine as workflow_module
    except ImportError:
        return None
    return workflow_module


def _session_engine():
    workflow_module = _workflow_module()
    return getattr(workflow_module, "engine", default_engine) if workflow_module else default_engine


def _active_tool_executor(executor=None):
    workflow_module = _workflow_module()
    return executor or getattr(workflow_module, "execute_tool", execute_tool)


def _active_policy_reader():
    workflow_module = _workflow_module()
    return getattr(workflow_module, "read_company_policy", read_company_policy)


@serialized_task_state
def _write_failure(task_id: int, message: str, details: dict | None = None) -> None:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return

        if task.status == "cancelled":
            _add_event(session, task_id, "workflow_interrupted", message, details)
            session.commit()
            return

        approval_id = (details or {}).get("approval_id")
        if approval_id:
            approval = session.get(Approval, approval_id)
            if approval is not None and approval.status == "pending":
                approval.status = "failed"
                approval.decided_at = datetime.now(timezone.utc)
                session.add(approval)

        state = _state_from_task(task)
        state["error"] = message
        state["final_summary"] = message
        task.status = "partially_completed" if state.get("completed_steps") else "failed"
        state.setdefault("timestamps", {})["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(task, state)
        _add_event(session, task_id, "workflow_failed", message, details)
        session.add(task)
        session.commit()


@serialized_task_state
def _persist_step(
    task_id: int,
    step: dict,
    result: dict,
    *,
    succeeded: bool,
    completed: bool,
    inputs: dict | None = None,
) -> dict:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            raise RuntimeError("Task disappeared while workflow was running.")

        state = _state_from_task(task)
        state["current_step"] = step["step"]
        records = state["step_results"]
        record = next(
            (item for item in reversed(records)
             if item.get("step") == step["step"] and item.get("status") == "running"),
            None,
        )
        if record is None:
            record = {
                "step": step["step"],
                "capability": step["capability"],
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            records.append(record)
        record.update({
            "capability": step["capability"],
            "depends_on": step.get("depends_on", []),
            "inputs": inputs or record.get("inputs", {}),
            "outputs": result,
            "result": result,
            "success": succeeded,
            "status": "completed" if succeeded else (
                "waiting_for_input" if result.get("missing_information") else "failed"
            ),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        if completed:
            state["completed_steps"] = sorted(set(state["completed_steps"] + [step["step"]]))
        if not succeeded:
            state["missing_information"] = result.get("missing_information", [])

        if task.status == "cancelled":
            event_type = "tool_execution"
            message = f"Step {step['step']} finished after cancellation was requested."
        elif not succeeded and state["missing_information"]:
            task.status = "waiting_for_input"
            event_type = "input_required"
            message = result.get("error", "Additional information is required.")
            state["final_summary"] = None
        elif not succeeded:
            task.status = "partially_completed" if state.get("completed_steps") else "failed"
            event_type = "tool_failed"
            message = result.get("error", "A tool failed; the workflow stopped safely.")
            state["error"] = message
            state["final_summary"] = message
        elif task.status not in {"waiting_for_input", "failed", "partially_completed", "cancelled"}:
            task.status = "running"
            event_type = "tool_execution"
            message = f"Step {step['step']} succeeded: {step['action']}."
        else:
            event_type = "tool_execution"
            message = f"Step {step['step']} succeeded: {step['action']} while another step remains paused."

        _save_state(task, state)
        _add_event(session, task_id, event_type, message, {
            "step": step["step"],
            "capability": step["capability"],
            "expected_result": step["expected_result"],
            "result": result,
        })
        session.add(task)
        session.commit()
        return state


def _decide_after_observation(
    capability: str,
    result: dict,
    attempt: int,
    max_retries: int = MAX_SAFE_RETRIES,
) -> str:
    if result.get("success"):
        return "continue"
    if result.get("missing_information"):
        return "request_input"
    if result.get("requires_approval"):
        return "await_approval"
    if (
        capability in SAFE_RETRY_CAPABILITIES
        and result.get("failure_type") in TRANSIENT_FAILURE_TYPES
        and attempt <= max_retries
    ):
        return "retry"
    return "stop"


@serialized_task_state
def _record_recovery_attempt(
    task_id: int,
    capability: str,
    step_number: int | None,
    attempt: int,
    result: dict,
) -> None:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        state = _state_from_task(task)
        record = {
            "capability": capability,
            "step": step_number,
            "attempt": attempt,
            "max_attempts": MAX_SAFE_RETRIES + 1,
            "failure_type": result.get("failure_type"),
            "error": result.get("error", "Transient tool failure."),
        }
        state["recovery_attempts"].append(record)
        _save_state(task, state)
        _add_event(
            session,
            task_id,
            "recovery_attempt",
            f"Transient {capability} failure; retrying attempt {attempt + 1} of {MAX_SAFE_RETRIES + 1}.",
            {**record, "observed_result": result},
        )
        session.add(task)
        session.commit()


def _execute_observed_tool(
    task_id: int,
    capability: str,
    context: dict | None = None,
    executor=None,
    step_number: int | None = None,
    timeout_seconds: float | None = None,
    owner_id: str | None = None,
    approval_id: int | None = None,
) -> dict:
    run_tool = _active_tool_executor(executor)
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        state = _state_from_task(task) if task else {}
    attempt = sum(
        1
        for recovery in state.get("recovery_attempts", [])
        if recovery.get("capability") == capability
        and (step_number is None or recovery.get("step") == step_number)
    )

    while True:
        attempt += 1
        try:
            configured_timeout = TOOL_CONTRACTS.get(capability, {}).get("timeout_seconds")
            configured_timeout = configured_timeout or DEFAULT_TOOL_TIMEOUT_SECONDS
            execution_timeout = min(configured_timeout, timeout_seconds) if timeout_seconds else configured_timeout
            pool = ThreadPoolExecutor(max_workers=1)
            executor_kwargs = {}
            try:
                parameter_names = set(inspect.signature(run_tool).parameters)
                if owner_id is not None and "owner_id" in parameter_names:
                    executor_kwargs["owner_id"] = owner_id
                if approval_id is not None and "approval_id" in parameter_names:
                    executor_kwargs["approval_id"] = approval_id
                if "task_id" in parameter_names:
                    executor_kwargs["task_id"] = task_id
            except (TypeError, ValueError):
                pass
            future = pool.submit(run_tool, capability, context, **executor_kwargs)
            try:
                result = future.result(timeout=execution_timeout)
            except FutureTimeoutError:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                result = {
                    "success": False,
                    "error": "Tool execution exceeded its configured timeout; the outcome may be unknown.",
                    "failure_type": "timeout",
                    "outcome_unknown": True,
                }
            else:
                pool.shutdown(wait=True)
        except Exception:
            result = {
                "success": False,
                "error": "Tool raised an unexpected error; no automatic retry was attempted.",
                "failure_type": "tool_exception",
            }

        decision = _decide_after_observation(capability, result, attempt)
        if decision != "retry":
            return {**result, "attempt_count": attempt, "next_action": decision}
        _record_recovery_attempt(task_id, capability, step_number, attempt, result)
        time.sleep(min(0.25 * (2 ** (attempt - 1)), 5.0))


def _policy_text_from_state(state: dict) -> str | None:
    for item in reversed(state.get("step_results", [])):
        result = item.get("result", {})
        if item.get("capability") == "read_company_policy" and result.get("success"):
            return result.get("policy_text")
    return None


def _finish_policy_verification(task_id: int) -> None:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        state = _state_from_task(task)
        recorded_text = _policy_text_from_state(state)

    actual = _active_policy_reader()()
    matches = bool(
        actual.get("success")
        and isinstance(recorded_text, str)
        and recorded_text.strip() == actual.get("policy_text", "").strip()
    )
    verification = {
        "type": "procurement_policy",
        "matches": matches,
        "source": actual.get("source"),
        "message": (
            "The saved policy result matches a fresh read of the policy file."
            if matches
            else "The saved policy result did not match a fresh read of the policy file."
        ),
    }

    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        state = _state_from_task(task)
        policy_record = next((
            item for item in reversed(state.get("step_results", []))
            if item.get("capability") == "read_company_policy" and item.get("success")
        ), None)
        if policy_record is not None:
            policy_record["verification_evidence"] = {
                "verified": matches,
                **verification,
            }
        state["verification"] = verification
        state["final_summary"] = verification["message"]
        task.status = "completed" if matches else "failed"
        state.setdefault("timestamps", {})["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(task, state)
        _add_event(session, task_id, "verification", verification["message"], verification)
        record_verified_procedure(session, task, state, verification)
        session.add(task)
        session.commit()


def _finish_vendor_verification(task_id: int) -> dict | None:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return None

        state = _state_from_task(task)
        registered = next(
            (
                item["result"]
                for item in reversed(state.get("step_results", []))
                if item.get("capability") == "register_vendor" and item.get("success")
            ),
            None,
        )
        if not registered or not registered.get("vendor_id"):
            _write_failure(task_id, "The approved vendor record could not be identified for verification.")
            return None
        vendor_id = registered["vendor_id"]
        expected = state.get("vendor", {})

    with Session(_session_engine()) as session:
        saved_vendor = session.get(Vendor, vendor_id)
        saved = None if saved_vendor is None else {
            "id": saved_vendor.id,
            "name": saved_vendor.name,
            "website": saved_vendor.website,
            "registration_number": saved_vendor.registration_number,
            "status": saved_vendor.status,
        }

    matches = bool(
        saved
        and saved["name"] == expected.get("name")
        and saved["website"] == expected.get("website")
        and saved["registration_number"] == expected.get("registration_number")
        and saved["status"] == "registered"
    )
    verification = {
        "type": "vendor_record",
        "matches": matches,
        "vendor_id": vendor_id,
        "saved_vendor": saved,
        "message": (
            "The persisted vendor record matches the approved proposal."
            if matches
            else "The persisted vendor record does not match the approved proposal."
        ),
    }

    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        state = _state_from_task(task)
        registration_record = next((
            item for item in reversed(state.get("step_results", []))
            if item.get("capability") == "register_vendor" and item.get("success")
        ), None)
        if registration_record is not None:
            registration_record["verification_evidence"] = verification
        state["verification"] = verification
        state["final_summary"] = verification["message"]
        task.status = "completed" if matches else "failed"
        state.setdefault("timestamps", {})["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(task, state)
        _add_event(session, task_id, "vendor_verification", verification["message"], verification)
        record_verified_procedure(session, task, state, verification)
        session.add(task)
        session.commit()
    return verification


def _finish_registered_plan_verification(task_id: int, plan: dict) -> dict:
    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return {"matches": False, "message": "Task not found during verification."}
        state = _state_from_task(task)

    results_by_step = {
        item.get("step"): item
        for item in state.get("step_results", [])
    }
    step_verifications = []
    for step in plan.get("steps", []):
        observed = results_by_step.get(step.get("step"))
        if not observed or not observed.get("success"):
            step_verifications.append({
                "step": step.get("step"),
                "capability": step.get("capability"),
                "matches": False,
                "status": "mismatch",
                "message": "No successful persisted tool result exists for this step.",
            })
            continue

        verification = verify_registered_tool(
            step["capability"],
            observed.get("inputs", {}),
            observed.get("result", {}),
        )
        step_verifications.append({
            "step": step["step"],
            "capability": step["capability"],
            "matches": bool(verification.get("verified")),
            "status": verification.get(
                "status",
                "verified" if verification.get("verified") else "mismatch",
            ),
            "actual": verification.get("actual"),
            "message": verification.get("reason") or (
                "The tool result matches a fresh verification."
                if verification.get("verified")
                else "The tool result did not match a fresh verification."
            ),
        })
        observed["verification_evidence"] = step_verifications[-1]

    matches = bool(step_verifications) and all(step["matches"] for step in step_verifications)
    verification_status = (
        "verified" if matches
        else "mismatch" if any(step["status"] == "mismatch" for step in step_verifications)
        else "unavailable"
    )
    message = (
        "Every registered-tool step was independently verified."
        if matches
        else "One or more steps could not be independently verified."
    )
    verification = {
        "type": "registered_tool_plan",
        "matches": matches,
        "status": verification_status,
        "steps": step_verifications,
        "message": message,
    }

    with Session(_session_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            return verification
        state = _state_from_task(task)
        state["step_results"] = list(results_by_step.values())
        state["verification"] = verification
        state["final_summary"] = message
        task.status = (
            "completed" if matches
            else "executed" if verification_status == "unavailable"
            else "failed"
        )
        state.setdefault("timestamps", {})["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(task, state)
        _add_event(session, task_id, "verification", message, verification)
        record_verified_procedure(session, task, state, verification)
        session.add(task)
        session.commit()
    return verification
