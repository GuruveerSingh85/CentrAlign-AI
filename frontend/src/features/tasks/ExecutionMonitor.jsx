import { useState } from "react";
import { apiRequest } from "../../api/api.js";
import {
  getLatestActivity,
  getRecoveryAttempts,
  getTaskResultPresentation,
  getTaskStatusMessage,
  getVerificationSummary,
  getWorkflowProgress,
} from "../workflows/workflowPresentation.js";
import { StatusBadge } from "./TaskPanels.jsx";

function renderInlineSummary(text, keyPrefix) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={key}>{part.slice(1, -1)}</code>;
    return part;
  });
}

function renderHumanSummary(summary) {
  const blocks = String(summary).trim().split(/\n\s*\n/).filter(Boolean);
  return blocks.map((block, index) => {
    const lines = block.split(/\r?\n/).filter(Boolean);
    const heading = lines[0].match(/^#{1,4}\s+(.+)$/);
    if (heading && lines.length === 1) return <h4 key={`heading-${index}`}>{renderInlineSummary(heading[1], `heading-${index}`)}</h4>;
    if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
      return <ul key={`list-${index}`}>{lines.map((line, itemIndex) => <li key={`item-${itemIndex}`}>{renderInlineSummary(line.replace(/^\s*[-*]\s+/, ""), `list-${index}-${itemIndex}`)}</li>)}</ul>;
    }
    if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
      return <ol key={`ordered-${index}`}>{lines.map((line, itemIndex) => <li key={`item-${itemIndex}`}>{renderInlineSummary(line.replace(/^\s*\d+[.)]\s+/, ""), `ordered-${index}-${itemIndex}`)}</li>)}</ol>;
    }
    return <p key={`paragraph-${index}`}>{lines.map((line, lineIndex) => <span key={`line-${lineIndex}`}>{lineIndex > 0 && <br />}{renderInlineSummary(line, `paragraph-${index}-${lineIndex}`)}</span>)}</p>;
  });
}

function humanizeLabel(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatValue(value) {
  if (value == null || value === "") return "Not provided";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.map(formatValue).join(", ") : "None";
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${humanizeLabel(key)}: ${formatValue(item)}`).join(" · ");
  return String(value);
}

function OutputSummary({ output }) {
  if (!output) return null;
  const usefulFields = new Set(["summary", "message", "status", "success", "error", "url", "title", "path", "count", "total", "size_bytes"]);
  const entries = typeof output === "object" && !Array.isArray(output)
    ? Object.entries(output).filter(([key, value]) => usefulFields.has(key.toLowerCase()) && value != null && value !== "")
    : typeof output === "string" ? [["Result", output]] : [];
  if (!entries.length) return <p className="muted-text">The action completed. Detailed execution data is hidden.</p>;
  return <dl className="readable-details">{entries.map(([key, value]) => <div key={key}><dt>{humanizeLabel(key)}</dt><dd>{formatValue(value)}</dd></div>)}</dl>;
}

function TaskResults({ state, plan, status }) {
  const presentation = getTaskResultPresentation(state, plan, status);
  const verification = getVerificationSummary(state);

  return (
    <section className="result-section" aria-labelledby="result-heading">
      <h3 id="result-heading">Results and verification</h3>
      {presentation.summary && (
        <div className={`human-readable-result ${presentation.partial ? "partial" : ""}`}>
          <strong>{presentation.partial ? "Completed work so far" : "Agent's answer"}</strong>
          <div className="human-readable-output">{renderHumanSummary(presentation.summary)}</div>
        </div>
      )}
      {verification && (
        <div className={`verification-result ${verification.verified === true ? "verified" : verification.verified === false ? "failed" : "notice"}`}>
          <strong>{verification.label}</strong>
          <p>{verification.message}</p>
          {verification.source && <small>Source: {verification.source}</small>}
          {verification.vendorId && <small>Vendor record: #{verification.vendorId}</small>}
        </div>
      )}

      {presentation.files.length > 0 && (
        <div className="result-group">
          <h4>Generated files</h4>
          {presentation.files.map((file) => (
            <div className="result-file" key={`${file.path}-${file.digest}`}>
              <strong>{file.path}</strong>
              <span>{file.size ? `${file.size.toLocaleString()} bytes` : "Backend file reference"}</span>
            </div>
          ))}
          <p className="muted-text">Files are stored in the backend's authorized file area; this API does not expose file downloads.</p>
        </div>
      )}

      {presentation.sources.length > 0 && (
        <div className="result-group">
          <h4>Sources</h4>
          <ul className="source-list">
            {presentation.sources.map((source) => (
              <li key={source}><a href={source} target="_blank" rel="noreferrer">{source}</a></li>
            ))}
          </ul>
        </div>
      )}

      {presentation.steps.length > 0 && (
        <div className="result-group">
          <h4>Action summary</h4>
          {presentation.steps.map((step, index) => (
            <details className="result-step" key={`${step.step}-${step.capability}-${index}`}>
              <summary>
                <span>{step.action}</span>
                <StatusBadge status={step.status} />
              </summary>
              {step.capability && <p className="task-meta">Tool: {step.capability}</p>}
              {step.output && <OutputSummary output={step.output} />}
            </details>
          ))}
        </div>
      )}

      {presentation.reports.map((report, index) => (
        <details className="result-group" key={`report-${index}`}>
          <summary>Generated report {presentation.reports.length > 1 ? index + 1 : ""}</summary>
          <pre className="report-output">{report}</pre>
        </details>
      ))}

      {presentation.blockingIssues.length > 0 && (
        <div className="blocking-issues">
          <h4>Blocking issues</h4>
          <ul>{presentation.blockingIssues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
        </div>
      )}
      {presentation.limitations.length > 0 && (
        <div className="result-group">
          <h4>Limitations</h4>
          <ul>{presentation.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
        </div>
      )}
      {!presentation.summary && !verification && presentation.steps.length === 0 && (
        <p className="muted-text">No execution result has been recorded yet.</p>
      )}
    </section>
  );
}

export default function ExecutionMonitor({
  task,
  plan,
  state,
  events,
  busyAction,
  onCancel,
  onStart,
  onReplan,
  onResume,
  vendor,
  setVendor,
  clarification,
  setClarification,
  onFeedbackSaved,
}) {
  const [feedbackType, setFeedbackType] = useState("usefulness");
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState("");
  const [feedbackError, setFeedbackError] = useState("");
  const [sendingFeedback, setSendingFeedback] = useState(false);

  async function submitFeedback(event) {
    event.preventDefault();
    if (!task || !feedbackText.trim()) return;
    setSendingFeedback(true);
    setFeedbackNotice("");
    setFeedbackError("");
    try {
      await apiRequest(`/tasks/${task.id}/feedback`, {
        method: "POST",
        body: JSON.stringify({
          feedback_type: feedbackType,
          content: feedbackText.trim(),
        }),
      });
      setFeedbackText("");
      setFeedbackNotice("Feedback recorded.");
      await onFeedbackSaved?.();
    } catch (error) {
      setFeedbackError(error.message || "Feedback could not be recorded.");
    } finally {
      setSendingFeedback(false);
    }
  }

  if (!task) {
    return (
      <div className="panel execution-panel">
        <div className="empty-state">
          <div className="empty-icon">✧</div>
          <h3>No task selected</h3>
          <p>Select a task from history to inspect its persisted plan, progress, and evidence.</p>
        </div>
      </div>
    );
  }

  const missingInformation = Array.isArray(state?.missing_information)
    ? state.missing_information
    : plan?.missing_information || [];
  const latestActivity = getLatestActivity(events);
  const recoveryAttempts = getRecoveryAttempts(state);
  const hasExecutablePlan = (plan?.steps || []).length > 0;
  const needsPlan = !hasExecutablePlan
    && ["pending", "planned", "waiting_for_input", "needs_input"].includes(task.status);
  const canCancel = [
    "pending", "planned", "waiting_for_input", "needs_input", "running", "processing",
    "partially_completed", "waiting_for_approval", "awaiting_approval",
  ].includes(task.status);
  const canStart = task.status === "planned";
  const canRetry = task.status === "partially_completed";
  const canResume = hasExecutablePlan
    && ["waiting_for_input", "needs_input"].includes(task.status)
    && missingInformation.length > 0;
  const canPreviewWithInputs = needsPlan
    && ["waiting_for_input", "needs_input"].includes(task.status)
    && missingInformation.length > 0;
  const canRetryPlanning = needsPlan && missingInformation.length === 0;

  return (
    <div className="panel execution-panel">
      <div className="history-heading">
        <div>
          <h2>Execution monitor</h2>
          <p>Task #{task.id} · backend-persisted status and events</p>
        </div>
        <div className="task-actions">
          <StatusBadge status={task.status} />
          {canCancel && (
            <button className="secondary-button danger-button" type="button" onClick={() => onCancel(task.id)} disabled={!!busyAction}>
              Cancel task
            </button>
          )}
        </div>
      </div>

      <section>
        <h3>Objective</h3>
        <p className="task-objective">{task.objective}</p>
        <p className="muted-text">{getTaskStatusMessage(task.status, state)}</p>
        {Object.keys(state?.inputs || {}).length > 0 && (
          <details className="result-group">
            <summary>Inputs retained with this task</summary>
            <dl className="readable-details">{Object.entries(state.inputs).map(([key, value]) => <div key={key}><dt>{humanizeLabel(key)}</dt><dd>{formatValue(value)}</dd></div>)}</dl>
          </details>
        )}
      </section>

      {plan?.steps?.length > 0 && (
        <section className="monitor-section">
          <h3>Plan and progress</h3>
          <p className="task-objective"><strong>Goal:</strong> {plan.goal || task.objective}</p>
          <div className="workflow-progress" aria-label="Workflow progress">
            {getWorkflowProgress(plan, state, task.status).map((step) => (
              <div className={`workflow-step ${step.complete ? "complete" : ""} ${step.active ? "active" : ""} ${step.waitingForApproval || step.waitingForInput ? "waiting" : ""}`} key={step.step}>
                <span>{step.complete ? "✓" : step.step}</span>
                <div>
                  <strong>{step.action}</strong>
                  <small>{step.label}</small>
                  <small>Tool: {plan.steps.find((item) => item.step === step.step)?.capability || "Not specified"}</small>
                  <small>Depends on: {(plan.steps.find((item) => item.step === step.step)?.depends_on || []).join(", ") || "None"}</small>
                  <small>Expected: {plan.steps.find((item) => item.step === step.step)?.expected_result || "Not specified"}</small>
                  <small>Approval required: {plan.steps.find((item) => item.step === step.step)?.requires_approval ? "Yes" : "No"}</small>
                </div>
              </div>
            ))}
          </div>
          {(plan.risks || []).length > 0 && (
            <div className="plan-risks"><strong>Plan risks</strong><ul>{plan.risks.map((risk) => <li key={risk}>{risk}</li>)}</ul></div>
          )}
          {(plan.limitations || []).map((limitation) => <p className="muted-text" key={limitation}>{limitation}</p>)}
        </section>
      )}

      {latestActivity && (
        <div className="current-action">
          <span className="eyebrow">LATEST BACKEND EVENT</span>
          <p>{latestActivity}</p>
        </div>
      )}

      {recoveryAttempts.length > 0 && (
        <div className="recovery-summary">
          <strong>Recorded safe retries ({recoveryAttempts.length})</strong>
          {recoveryAttempts.map((attempt, index) => (
            <p key={`${attempt.capability}-${attempt.failedAttempt}-${index}`}>
              {attempt.capability}: retry {attempt.retryAttempt} of {attempt.maxAttempts} after {attempt.error}
            </p>
          ))}
        </div>
      )}

      {canStart && (
        <button className="secondary-button monitor-action" type="button" onClick={() => onStart(task)} disabled={!!busyAction}>
          Start reviewed plan
        </button>
      )}
      {canRetry && (
        <button className="secondary-button monitor-action" type="button" onClick={() => onStart(task)} disabled={!!busyAction}>
          Retry remaining steps
        </button>
      )}

      {canRetryPlanning && (
        <button className="secondary-button monitor-action" type="button" onClick={() => onReplan()} disabled={!!busyAction}>
          {busyAction === "planning" ? "Preparing plan..." : "Retry plan preview"}
        </button>
      )}

      {(canResume || canPreviewWithInputs) && (
        <section className="input-panel monitor-section">
          <h3>{canPreviewWithInputs ? "Inputs needed to finish planning" : "Required inputs"}</h3>
          <p className="muted-text">
            {canPreviewWithInputs
              ? "Add the missing information and review the updated plan before execution."
              : "Add only the missing information; the same task and completed evidence will be retained."}
          </p>
          <form onSubmit={canPreviewWithInputs ? onReplan : onResume}>
            {missingInformation.map((field) => {
              const vendorField = {
                vendor_name: "name",
                vendor_website: "website",
                registration_number: "registration_number",
              }[field];
              const value = vendorField ? vendor[vendorField] : clarification[field] || "";
              const label = field.replaceAll("_", " ");
              return (
                <label key={field} htmlFor={`resume-${field}`}>
                  {label}
                  <input
                    id={`resume-${field}`}
                    type={field.includes("website") ? "url" : "text"}
                    value={value}
                    onChange={(event) => vendorField
                      ? setVendor((current) => ({ ...current, [vendorField]: event.target.value }))
                      : setClarification((current) => ({ ...current, [field]: event.target.value }))}
                    required
                  />
                </label>
              );
            })}
            <button className="primary-button" type="submit" disabled={!!busyAction}>
              {canPreviewWithInputs
                ? busyAction === "planning" ? "Preparing plan..." : "Preview updated plan"
                : busyAction === "resuming" ? "Resuming..." : "Resume task"}
            </button>
          </form>
        </section>
      )}

      {task.status === "waiting_for_approval" || task.status === "awaiting_approval" ? (
        <div className="blocking-issues"><strong>Approval required</strong><p>Review the exact proposed action in the approval inbox before execution can continue.</p></div>
      ) : null}

      <TaskResults state={state} plan={plan} status={task.status} />

      {["completed", "failed", "cancelled", "verified", "rejected", "unsupported", "approval_expired", "partially_completed"].includes(task.status) && (
        <section className="monitor-section">
          <h3>Feedback on this task</h3>
          <form className="feedback-form" onSubmit={submitFeedback}>
            <label htmlFor="task-feedback-type">Feedback type</label>
            <select
              id="task-feedback-type"
              value={feedbackType}
              onChange={(event) => setFeedbackType(event.target.value)}
            >
              <option value="correction">Correction</option>
              <option value="failure">Failure</option>
              <option value="usefulness">Usefulness</option>
              <option value="success">Success</option>
            </select>
            <label htmlFor="task-feedback-text">Feedback</label>
            <textarea
              id="task-feedback-text"
              maxLength={1000}
              rows={3}
              value={feedbackText}
              onChange={(event) => setFeedbackText(event.target.value)}
              placeholder="Describe what was useful or what should change"
              required
            />
            {feedbackError && <p className="form-error">{feedbackError}</p>}
            {feedbackNotice && <p className="form-success">{feedbackNotice}</p>}
            <button className="secondary-button" type="submit" disabled={sendingFeedback}>
              {sendingFeedback ? "Recording..." : "Submit feedback"}
            </button>
          </form>
        </section>
      )}

      <section className="monitor-section">
        <h3>Execution events</h3>
        {events.length === 0 ? (
          <p className="muted-text">No backend events recorded yet.</p>
        ) : events.map((event) => (
          <article className="evidence-card" key={event.id}>
            <div className="task-card-top">
              <strong>{String(event.event_type || "event").replaceAll("_", " ")}</strong>
              <span className="task-meta">{event.created_at ? new Date(event.created_at).toLocaleString() : ""}</span>
            </div>
            <p>{event.message}</p>
          </article>
        ))}
      </section>
    </div>
  );
}
