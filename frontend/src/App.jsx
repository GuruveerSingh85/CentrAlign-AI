
import { useCallback, useEffect, useState } from "react";
import "./App.css";

const API = "http://127.0.0.1:8000/api";

function StatusBadge({ status }) {
  const value = status || "pending";
  return (
    <span className={`status-badge status-${value}`}>
      <span className="status-dot" />
      {value.replaceAll("_", " ")}
    </span>
  );
}

async function apiRequest(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "The request failed."
    );
  }

  return data;
}

export default function App() {
  const [objective, setObjective] = useState("");
  const [tasks, setTasks] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [plan, setPlan] = useState(null);
  const [events, setEvents] = useState([]);
  const [result, setResult] = useState(null);

  const [vendor, setVendor] = useState({
    name: "",
    website: "",
    registration_number: "",
  });

  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedTask = tasks.find((task) => task.id === selectedId);

  const loadTasks = useCallback(async () => {
    const data = await apiRequest("/tasks");
    setTasks(data);
    return data;
  }, []);

  const loadApprovals = useCallback(async () => {
    const data = await apiRequest("/approvals?status=pending");
    setApprovals(data);
  }, []);

  const loadTaskDetails = useCallback(async (taskId) => {
    if (!taskId) return;

    const task = await apiRequest(`/tasks`);
    const current = task.find((item) => item.id === taskId);

    if (current?.plan_json) {
      try {
        setPlan(JSON.parse(current.plan_json));
      } catch {
        setPlan(null);
      }
    } else {
      setPlan(null);
    }

    if (current?.result_json) {
      try {
        setResult(JSON.parse(current.result_json));
      } catch {
        setResult(current.result_json);
      }
    } else {
      setResult(null);
    }

    try {
      const eventData = await apiRequest(`/tasks/${taskId}/events`);
      setEvents(eventData.events || []);
    } catch {
      setEvents([]);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    const [taskData] = await Promise.all([
      loadTasks(),
      loadApprovals(),
    ]);

    if (selectedId && !taskData.some((task) => task.id === selectedId)) {
      setSelectedId(null);
      setPlan(null);
      setEvents([]);
      setResult(null);
    } else if (selectedId) {
      await loadTaskDetails(selectedId);
    }
  }, [loadTasks, loadApprovals, selectedId, loadTaskDetails]);

  useEffect(() => {
    async function initialLoad() {
      setLoading(true);
      try {
        await Promise.all([loadTasks(), loadApprovals()]);
        setError("");
      } catch (err) {
        setError(err.message || "Cannot connect to FastAPI on port 8000.");
      } finally {
        setLoading(false);
      }
    }

    initialLoad();
  }, [loadTasks, loadApprovals]);

  async function runAction(label, action) {
    setBusyAction(label);
    setError("");
    setNotice("");

    try {
      const data = await action();
      setNotice(`${label} completed successfully.`);
      await refreshAll();
      return data;
    } catch (err) {
      setError(err.message || "Something went wrong.");
      return null;
    } finally {
      setBusyAction("");
    }
  }

  async function createTask(event) {
    event.preventDefault();
    if (!objective.trim()) return;

    setLoading(true);
    setError("");
    setNotice("");

    try {
      const data = await apiRequest("/tasks", {
        method: "POST",
        body: JSON.stringify({ objective: objective.trim() }),
      });

      setObjective("");
      setSelectedId(data.id);
      setPlan(null);
      setEvents([]);
      setResult(null);
      setNotice(`Task #${data.id} created.`);
      await loadTasks();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function planTask(taskId) {
    await runAction("Planning", async () => {
      const data = await apiRequest(`/tasks/${taskId}/plan`, {
        method: "POST",
      });
      setSelectedId(taskId);
      setPlan(data.plan);
      setResult(null);
      await loadTasks();
      return data;
    });
  }

  async function executeTask(taskId) {
    await runAction("Execution", async () => {
      const data = await apiRequest(`/tasks/${taskId}/execute`, {
        method: "POST",
      });
      setSelectedId(taskId);
      setResult(data);
      await loadTaskDetails(taskId);
      return data;
    });
  }

  async function proposeVendor(event) {
    event.preventDefault();
    if (!selectedId) return;

    await runAction("Vendor proposal", async () => {
      const data = await apiRequest(
        `/tasks/${selectedId}/vendor-proposal`,
        {
          method: "POST",
          body: JSON.stringify({
            name: vendor.name,
            website: vendor.website,
            registration_number: vendor.registration_number || null,
          }),
        }
      );

      setVendor({
        name: "",
        website: "",
        registration_number: "",
      });

      setNotice(`Approval request #${data.approval_id} created.`);
      await loadApprovals();
      await loadTasks();
      return data;
    });
  }

  async function decideApproval(approvalId, decision) {
    await runAction(
      decision === "approve" ? "Approval" : "Rejection",
      async () => {
        const data = await apiRequest(
          `/approvals/${approvalId}/decide?decision=${decision}`,
          { method: "POST" }
        );

        await loadApprovals();
        await loadTasks();

        if (data.task_id) {
          setSelectedId(data.task_id);
          await loadTaskDetails(data.task_id);
        }

        return data;
      }
    );
  }

  const pendingTasks = tasks.filter((task) =>
    ["pending", "processing", "planned", "awaiting_approval", "needs_input"].includes(task.status)
  ).length;

  const completedTasks = tasks.filter((task) =>
    ["completed", "verified"].includes(task.status)
  ).length;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">✦</div>
          <div>
            <h2>CentrAlign</h2>
            <span>AI OPERATOR</span>
          </div>
        </div>

        <div className="sidebar-label">WORKSPACE</div>
        <div className="nav-item active">▦ &nbsp; Task operator</div>
        <div className="nav-item">✓ &nbsp; Approval inbox ({approvals.length})</div>

        <div className="sidebar-note">
          <div className="note-icon">✧</div>
          <p>Turn business requests into actions, verified outcomes, and useful evidence.</p>
        </div>

        <div className="sidebar-footer">
          <span className="online-dot" />
          Local development environment
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div>
            <div className="eyebrow">AUTONOMOUS WORKSPACE</div>
            <h1>Company task operator</h1>
            <p>Plan, execute, approve, and inspect real outcomes.</p>
          </div>
          <button
            className="secondary-button"
            onClick={() => runAction("Refresh", refreshAll)}
            disabled={!!busyAction}
          >
            ↻ &nbsp; Refresh
          </button>
        </header>

        {error && <div className="message error-message" role="alert">⚠ &nbsp; {error}</div>}
        {notice && <div className="message success-message" role="status">✓ &nbsp; {notice}</div>}

        <section className="stats-grid">
          <div className="stat-card">
            <div className="stat-icon purple">▤</div>
            <div><span>Total tasks</span><strong>{tasks.length}</strong></div>
          </div>
          <div className="stat-card">
            <div className="stat-icon amber">◷</div>
            <div><span>Active tasks</span><strong>{pendingTasks}</strong></div>
          </div>
          <div className="stat-card">
            <div className="stat-icon green">✓</div>
            <div><span>Completed / verified</span><strong>{completedTasks}</strong></div>
          </div>
          <div className="stat-card">
            <div className="stat-icon amber">✋</div>
            <div><span>Pending approvals</span><strong>{approvals.length}</strong></div>
          </div>
        </section>

        <section className="workspace-grid">
          <div className="panel">
            <div className="panel-heading">
              <div className="heading-icon">✧</div>
              <div>
                <h2>Give the operator a goal</h2>
                <p>Start with the outcome you want.</p>
              </div>
            </div>

            <form onSubmit={createTask}>
              <label htmlFor="objective">Your objective</label>
              <textarea
                id="objective"
                value={objective}
                onChange={(e) => setObjective(e.target.value)}
                placeholder="Example: Read the procurement policy before registering a vendor."
                rows={4}
                maxLength={5000}
                required
              />
              <div className="form-hint">
                <span>Be specific about the desired outcome.</span>
                <span>{objective.length}/5000</span>
              </div>
              <button
                className="primary-button"
                type="submit"
                disabled={loading || !!busyAction || !objective.trim()}
              >
                {loading ? "Creating..." : "+ Create task"}
              </button>
            </form>

            <div className="capability-note">
              <strong>Execution safeguards</strong>
              <p>Only registered backend tools can execute. Vendor registration requires a separate approval decision.</p>
            </div>

            <div className="history-heading">
              <div>
                <h2>Task history</h2>
                <p>Select a task to inspect or operate it.</p>
              </div>
              <span className="task-count">{tasks.length}</span>
            </div>

            {loading && tasks.length === 0 ? (
              <div className="empty-state">Loading tasks...</div>
            ) : tasks.length === 0 ? (
              <div className="empty-state">Create your first task to get started.</div>
            ) : (
              <div className="task-list">
                {tasks.map((task) => (
                  <article
                    className={`task-card ${selectedId === task.id ? "selected-task" : ""}`}
                    key={task.id}
                  >
                    <button
                      className="task-select"
                      onClick={async () => {
                        setSelectedId(task.id);
                        setError("");
                        try {
                          await loadTaskDetails(task.id);
                        } catch (err) {
                          setError(err.message);
                        }
                      }}
                    >
                      <div className="task-card-top">
                        <span className="task-id">TASK #{task.id}</span>
                        <StatusBadge status={task.status} />
                      </div>
                      <p className="task-objective">{task.objective}</p>
                      <div className="task-meta">
                        ◷ &nbsp;
                        {task.created_at
                          ? new Date(task.created_at).toLocaleString()
                          : "Date unavailable"}
                      </div>
                    </button>

                    <div className="task-actions">
                      <button
                        className="secondary-button"
                        disabled={!!busyAction}
                        onClick={() => planTask(task.id)}
                      >
                        Plan
                      </button>
                      <button
                        className="primary-button"
                        disabled={!!busyAction || !task.plan_json || task.status === "awaiting_approval"}
                        onClick={() => executeTask(task.id)}
                      >
                        Execute
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>

          <div className="panel">
            <div className="history-heading">
              <div>
                <h2>Operator workspace</h2>
                <p>{selectedTask ? `Task #${selectedTask.id}` : "Select a task from history."}</p>
              </div>
              {selectedTask && <StatusBadge status={selectedTask.status} />}
            </div>

            {selectedTask ? (
              <>
                <h3>Objective</h3>
                <p className="task-objective">{selectedTask.objective}</p>

                <div className="section-divider" />
                <h3>Generated plan</h3>

                {!plan ? (
                  <p className="muted-text">No plan loaded. Select Plan to ask Gemini to create one.</p>
                ) : (
                  <>
                    <p><strong>Goal:</strong> {plan.goal}</p>
                    <h4>Steps</h4>
                    {(plan.steps || []).map((step) => (
                      <div className="evidence-card" key={step.step}>
                        <strong>Step {step.step}: {step.action}</strong>
                        <p>Capability: <code>{step.capability}</code></p>
                        <p>Approval required: {step.requires_approval ? "Yes" : "No"}</p>
                        <p className="muted-text">{step.expected_result}</p>
                      </div>
                    ))}
                    {(plan.risks || []).length > 0 && (
                      <>
                        <h4>Risks</h4>
                        <ul>{plan.risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul>
                      </>
                    )}
                  </>
                )}

                <div className="section-divider" />
                <h3>Prepare vendor proposal</h3>
                <p className="muted-text">Use only real, verified details. Submitting creates an approval request, not a vendor record.</p>
                <form onSubmit={proposeVendor}>
                  <label htmlFor="vendor-name">Legal name</label>
                  <input
                    id="vendor-name"
                    value={vendor.name}
                    onChange={(e) => setVendor({ ...vendor, name: e.target.value })}
                    required
                  />
                  <label htmlFor="vendor-website">Website</label>
                  <input
                    id="vendor-website"
                    type="url"
                    placeholder="https://example.com"
                    value={vendor.website}
                    onChange={(e) => setVendor({ ...vendor, website: e.target.value })}
                    required
                  />
                  <label htmlFor="vendor-reg">Registration number (optional)</label>
                  <input
                    id="vendor-reg"
                    value={vendor.registration_number}
                    onChange={(e) => setVendor({ ...vendor, registration_number: e.target.value })}
                  />
                  <button className="secondary-button" disabled={!!busyAction} type="submit">
                    Submit for approval
                  </button>
                </form>

                <div className="section-divider" />
                <h3>Execution result</h3>
                {result ? (
                  <pre className="json-panel">{JSON.stringify(result, null, 2)}</pre>
                ) : selectedTask.result_json ? (
                  <pre className="json-panel">{selectedTask.result_json}</pre>
                ) : (
                  <p className="muted-text">No execution result loaded.</p>
                )}

                <div className="section-divider" />
                <h3>Execution evidence</h3>
                {events.length === 0 ? (
                  <p className="muted-text">No events recorded for this task yet.</p>
                ) : (
                  events.map((event) => (
                    <div className="evidence-card" key={event.id}>
                      <div className="task-card-top">
                        <strong>{event.event_type.replaceAll("_", " ")}</strong>
                        <span className="task-meta">
                          {event.created_at ? new Date(event.created_at).toLocaleString() : ""}
                        </span>
                      </div>
                      <p>{event.message}</p>
                      {event.details && (
                        <pre className="json-panel">{JSON.stringify(event.details, null, 2)}</pre>
                      )}
                    </div>
                  ))
                )}
              </>
            ) : (
              <div className="empty-state">
                <div className="empty-icon">✧</div>
                <h3>No task selected</h3>
                <p>Select a task to view its plan, execute it, and inspect evidence.</p>
              </div>
            )}
          </div>
        </section>

        <section className="panel approval-panel">
          <div className="history-heading">
            <div>
              <h2>Approval inbox</h2>
              <p>Vendor registration requests awaiting your decision.</p>
            </div>
            <span className="task-count">{approvals.length}</span>
          </div>

          {approvals.length === 0 ? (
            <p className="muted-text">No pending approvals.</p>
          ) : (
            <div className="approval-list">
              {approvals.map((approval) => (
                <article className="evidence-card" key={approval.id}>
                  <div className="task-card-top">
                    <strong>Approval #{approval.id} · Task #{approval.task_id}</strong>
                    <StatusBadge status={approval.status} />
                  </div>
                  <p><strong>{approval.action.name}</strong></p>
                  <p>{approval.action.website}</p>
                  <p className="muted-text">
                    Registration number: {approval.action.registration_number || "Not provided"}
                  </p>
                  <div className="task-actions">
                    <button
                      className="primary-button"
                      disabled={!!busyAction}
                      onClick={() => decideApproval(approval.id, "approve")}
                    >
                      Approve & register
                    </button>
                    <button
                      className="secondary-button"
                      disabled={!!busyAction}
                      onClick={() => decideApproval(approval.id, "reject")}
                    >
                      Reject
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <footer className="page-footer">
          CentrAlign AI Operator <span>·</span> Prototype v0.2
        </footer>
      </main>
    </div>
  );
}
