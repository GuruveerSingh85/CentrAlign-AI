const ACTIVE_STATUSES = new Set(["processing", "running", "executing"]);

export function shouldPollTask(status) {
  return ACTIVE_STATUSES.has(String(status || "").toLowerCase());
}

export function normalizeStatusClass(status) {
  const value = String(status || "pending").toLowerCase();

  if (value === "unsupported") return "failed";
  if (value.includes("approval")) return "approval";
  if (value.includes("need") || value.includes("waiting") || value.includes("pending")) return "pending";
  if (["executed", "completed", "verified"].includes(value)) return "completed";
  if (value === "rejected" || value === "cancelled" || value.includes("fail")) return "failed";
  if (value.includes("execut") || value.includes("process") || value.includes("planned")) return "processing";

  return "pending";
}

export function getWorkflowProgress(plan, state, taskStatus) {
  return (plan?.steps || []).map((step) => {
    const complete = state?.completed_steps?.includes(step.step) || false;
    const waitingForApproval = ["awaiting_approval", "waiting_for_approval"].includes(taskStatus)
      && state?.current_step === step.step;
    const waitingForInput = ["needs_input", "waiting_for_input"].includes(taskStatus)
      && state?.current_step === step.step;

    return {
      step: step.step,
      action: step.action,
      complete,
      waitingForApproval,
      waitingForInput,
      label: complete
        ? "Complete"
        : waitingForApproval
          ? "Awaiting approval"
          : waitingForInput
            ? "Waiting for information"
            : "Pending",
    };
  });
}

export function getApprovalProposal(approval) {
  const action = approval?.action || {};
  return {
    id: approval?.id,
    taskId: approval?.task_id,
    capability: action.capability || (action.name ? "register_vendor" : null),
    action: action.action || (action.name ? "Register vendor" : "Review proposed action"),
    target: action.target || action.website || null,
    parameters: action.parameters || {
      name: action.name,
      website: action.website,
      registration_number: action.registration_number,
    },
    expectedConsequences: action.expected_consequences || null,
    risk: action.risk || null,
    expiresAt: approval?.expires_at || null,
    name: action.name || "Name not provided",
    website: action.website || "Website not provided",
    registrationNumber: action.registration_number || null,
  };
}

export function getVerificationSummary(state) {
  const verification = state?.verification;
  if (!verification || typeof verification.matches !== "boolean") return null;

  return {
    verified: verification.matches,
    label: verification.matches ? "Verified against source of truth" : "Verification failed",
    message: verification.message || "No verification detail was recorded.",
    source: verification.source || null,
    vendorId: verification.vendor_id || verification.saved_vendor?.id || null,
  };
}

export function getLatestActivity(events) {
  if (!Array.isArray(events) || events.length === 0) return null;
  return events[events.length - 1].message || null;
}

export function getRecoveryAttempts(state) {
  if (!Array.isArray(state?.recovery_attempts)) return [];
  return state.recovery_attempts.map((attempt) => ({
    capability: attempt.capability || "tool",
    failedAttempt: attempt.attempt,
    retryAttempt: Number.isInteger(attempt.attempt) ? attempt.attempt + 1 : null,
    maxAttempts: attempt.max_attempts,
    error: attempt.error || "Transient failure",
  }));
}

export function getTaskStatusMessage(status, state) {
  switch (String(status || "pending").toLowerCase()) {
    case "awaiting_approval":
    case "waiting_for_approval":
      return "This task is waiting for explicit human approval before registration.";
    case "needs_input":
    case "waiting_for_input":
      return "The workflow paused because required information is missing.";
    case "verification_failed":
      return state?.verification?.message
        || "The requested outcome could not be independently verified.";
    case "failed":
      return state?.final_summary || state?.error || "The workflow stopped after an execution failure.";
    case "rejected":
      return state?.final_summary || "The proposal was rejected; no vendor was registered.";
    case "approval_expired":
      return state?.final_summary || "The approval expired; the proposed action was not executed.";
    case "cancelled":
      return state?.final_summary || "The task was cancelled; completed step evidence is retained.";
    case "partially_completed":
      return state?.final_summary || "Execution stopped after partial progress; completed evidence is retained.";
    case "unsupported":
      return state?.final_summary || "The required integration is not available in the registered tool set.";
    case "executed":
      return "The actions completed; verification is pending.";
    case "completed":
    case "verified":
      return state?.verification?.message || "The task completed and its outcome was verified.";
    case "processing":
    case "running":
    case "executing":
      return "The backend is processing the objective through the autonomous workflow.";
    default:
      return `Task status: ${String(status || "pending").replaceAll("_", " ")}`;
  }
}
