import { normalizeStatusClass } from "../workflows/workflowPresentation.js";

export function StatusBadge({ status }) {
  const value = String(status || "pending").replaceAll("_", " ");
  return (
    <span className={`status-badge status-${normalizeStatusClass(status)}`}>
      <span className="status-dot" />
      {value}
    </span>
  );
}

export function TaskComposer({
  objective,
  onObjectiveChange,
  inputsText,
  onInputsChange,
  onPreview,
  busyAction,
}) {
  return (
    <div className="panel">
      <div className="panel-heading">
        <div className="heading-icon">✧</div>
        <div>
          <h2>New task</h2>
          <p>Describe the outcome and provide context. Website tasks use the configured default site when you omit its URL.</p>
        </div>
      </div>

      <form onSubmit={onPreview}>
        <label htmlFor="objective">Objective</label>
        <textarea
          id="objective"
          value={objective}
          onChange={(event) => onObjectiveChange(event.target.value)}
          placeholder="Example: Register Acme Supply under the procurement policy."
          rows={5}
          maxLength={5000}
          required
        />
        <div className="form-hint">
          <span>Use a precise business outcome.</span>
          <span>{objective.length}/5000</span>
        </div>

        <details className="input-details">
          <summary>Required inputs and context</summary>
          <label htmlFor="task-inputs">Structured inputs (JSON)</label>
          <textarea
            id="task-inputs"
            className="inputs-editor"
            value={inputsText}
            onChange={(event) => onInputsChange(event.target.value)}
            placeholder={'{\n  "name": "Acme Supply",\n  "website": "https://acme.example"\n}'}
            rows={5}
            spellCheck="false"
          />
          <p className="muted-text">Inputs are sent to the planner and retained with this task. Do not include credentials or secrets.</p>
        </details>

        <button
          className="primary-button"
          type="submit"
          disabled={!!busyAction || !objective.trim()}
        >
          {busyAction === "planning" ? "Preparing plan..." : busyAction === "running" ? "Running task..." : "Create and run task"}
        </button>
      </form>

      <div className="capability-note">
        <strong>Execution safeguards</strong>
        <p>Only registered backend tools may run. Browser actions on configured allowlisted sites can run automatically when enabled; other sensitive actions still pause for approval.</p>
      </div>
    </div>
  );
}

export function TaskHistory({ tasks, selectedId, loading, onSelect, onClear, busyAction }) {
  return (
    <div className="panel history-panel">
      <div className="history-heading">
        <div>
          <h2>Task history</h2>
          <p>Open a task to review its plan, evidence, and outcome.</p>
        </div>
        <span className="task-count">{tasks.length}</span>
      </div>

      {loading && tasks.length === 0 ? (
        <div className="empty-state">Loading tasks...</div>
      ) : tasks.length === 0 ? (
        <div className="empty-state">No tasks yet. Preview a plan to begin.</div>
      ) : (
        <div className="task-list">
          {tasks.map((task) => (
            <article className={`task-card ${selectedId === task.id ? "selected-task" : ""}`} key={task.id}>
              <button className="task-select" type="button" onClick={() => onSelect(task)}>
                <div className="task-card-top">
                  <span className="task-id">TASK #{task.id}</span>
                  <StatusBadge status={task.status} />
                </div>
                <p className="task-objective">{task.objective}</p>
                <div className="task-meta">
                  {task.created_at ? new Date(task.created_at).toLocaleString() : "Date unavailable"}
                </div>
              </button>
              <div className="task-actions">
                <button
                  className="secondary-button danger-button"
                  type="button"
                  onClick={() => onClear(task)}
                  disabled={!!busyAction || ["running", "processing", "waiting_for_input", "needs_input", "waiting_for_approval", "awaiting_approval", "partially_completed"].includes(task.status)}
                  aria-label={`Clear history for task ${task.id}`}
                >
                  Clear history
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
