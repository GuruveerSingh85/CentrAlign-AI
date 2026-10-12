import { useCallback, useEffect, useRef, useState } from "react";
import {
  getApprovalProposal,
  getLatestActivity,
  getRecoveryAttempts,
  getTaskStatusMessage,
  getVerificationSummary,
  getWorkflowProgress,
  normalizeStatusClass,
  shouldPollTask,
} from "./workflowPresentation.js";
import "./App.css";

const API = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api";

function StatusBadge({ status }) {
  const value = String(status || "pending").replaceAll("_", " ");
  const className = `status-badge status-${normalizeStatusClass(status)}`;

  return (
    <span className={className}>
      <span className="status-dot" />
      {value}
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

function formatRunNotice(result) {
  if (!result) return "The workflow completed.";

  switch (result.status) {
    case "awaiting_approval":
    case "waiting_for_approval":
      return "A human approval is required before the proposed sensitive action can run.";
    case "needs_input":
    case "waiting_for_input":
      return "The agent paused for missing information and is waiting for the required details.";
    case "executed":
      return "The workflow finished execution. Verification is pending.";
    case "completed":
    case "verified":
      return "The autonomous workflow finished and the result was verified.";
    case "processing":
    case "running":
      return "Task started. The dashboard is monitoring backend progress.";
    case "partially_completed":
      return "Execution stopped after partial progress; completed evidence is retained.";
    case "cancelled":
      return "The task was cancelled; completed evidence is retained.";
    default:
      return `Task status: ${result.status}`;
  }
}

export default function App() {
  const [objective, setObjective] = useState("");
  const [vendor, setVendor] = useState({
    name: "",
    website: "",
    registration_number: "",
  });
  const [clarification, setClarification] = useState({});
  const [tasks, setTasks] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [plan, setPlan] = useState(null);
  const [events, setEvents] = useState([]);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [activeView, setActiveView] = useState("tasks");
  const [memories, setMemories] = useState([]);
  const [memoryDraft, setMemoryDraft] = useState({
    category: "preference",
    kind: "user_instruction",
    content: "",
    memory_key: "",
    source: "user",
    verification_status: "unverified",
  });
  const [editingMemoryId, setEditingMemoryId] = useState(null);
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryPreview, setMemoryPreview] = useState(null);
  const actionLock = useRef(false);

  const selectedTask = tasks.find((task) => task.id === selectedId);

  const loadTasks = useCallback(async () => {
    const data = await apiRequest("/tasks");
    setTasks(data);
    return data;
  }, []);

  const loadApprovals = useCallback(async () => {
    const data = await apiRequest("/approvals?status=pending");
    setApprovals(data);
    return data;
  }, []);

  const loadMemories = useCallback(async () => {
    const data = await apiRequest("/memories");
    setMemories(data);
    return data;
  }, []);

  const loadTaskDetails = useCallback(async (taskId) => {
    if (!taskId) {
      setPlan(null);
      setEvents([]);
      setResult(null);
      setClarification({});
      return;
    }

    try {
      const taskList = await apiRequest("/tasks");
      const current = taskList.find((task) => task.id === taskId);

      if (!current) {
        setPlan(null);
        setEvents([]);
        setResult(null);
        return;
      }

      if (current.plan_json) {
        try {
          setPlan(JSON.parse(current.plan_json));
        } catch {
          setPlan(null);
        }
      } else {
        setPlan(null);
      }

      if (current.result_json) {
        try {
          setResult(JSON.parse(current.result_json));
        } catch {
          setResult(current.result_json);
        }
      } else {
        setResult(null);
      }

      const eventData = await apiRequest(`/tasks/${taskId}/events`);
      setEvents(eventData.events || []);
    } catch (err) {
      setError(err.message || "Unable to load task details.");
    }
  }, []);

  const refreshAll = useCallback(async () => {
    const taskData = await loadTasks();
    await loadApprovals();

    if (selectedId && !taskData.some((task) => task.id === selectedId)) {
      setSelectedId(null);
      setPlan(null);
      setEvents([]);
      setResult(null);
      setClarification({});
      return;
    }

    if (selectedId) {
      await loadTaskDetails(selectedId);
    }
  }, [loadTasks, loadApprovals, selectedId, loadTaskDetails]);

  useEffect(() => {
    async function bootstrap() {
      setLoading(true);
      try {
        const taskData = await loadTasks();
        await loadApprovals();

        if (!selectedId && taskData.length > 0) {
          setSelectedId(taskData[0].id);
          await loadTaskDetails(taskData[0].id);
        }
      } catch (err) {
        setError(err.message || "Unable to connect to FastAPI on port 8000.");
      } finally {
        setLoading(false);
      }
    }

    bootstrap();
  }, [loadTasks, loadApprovals, selectedId, loadTaskDetails]);

  useEffect(() => {
    if (!tasks.some((task) => shouldPollTask(task.status))) {
      return undefined;
    }

    let cancelled = false;
    let timeoutId;
    const poll = async () => {
      try {
        await refreshAll();
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to refresh task state.");
      }
      if (!cancelled) timeoutId = setTimeout(poll, 2500);
    };

    timeoutId = setTimeout(poll, 2500);

    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [tasks, refreshAll]);

  async function runAutonomousTask(event) {
    event.preventDefault();
    if (!objective.trim() || actionLock.current) return;

    actionLock.current = true;
    setBusyAction("running");
    setLoading(true);
    setError("");
    setNotice("");

    try {
      const createdTask = await apiRequest("/tasks", {
        method: "POST",
        body: JSON.stringify({ objective: objective.trim() }),
      });

      setSelectedId(createdTask.id);
      setObjective("");
      setVendor({ name: "", website: "", registration_number: "" });
      setClarification({});
      setPlan(null);
      setEvents([]);
      setResult(null);

      const runResult = await apiRequest(`/tasks/${createdTask.id}/run`, {
        method: "POST",
        body: JSON.stringify({}),
      });

      setResult(runResult);
      setNotice(formatRunNotice(runResult));
      await Promise.all([loadTasks(), loadApprovals()]);
      await loadTaskDetails(createdTask.id);
    } catch (err) {
      setError(err.message || "The task could not be processed.");
    } finally {
      actionLock.current = false;
      setBusyAction("");
      setLoading(false);
    }
  }

  async function resumeTask(event) {
    event.preventDefault();
    if (!selectedTask || actionLock.current) return;

    actionLock.current = true;
    setBusyAction("resuming");
    setError("");
    setNotice("");

    try {
      const payload = {
        vendor_name: vendor.name || null,
        vendor_website: vendor.website || null,
        registration_number: vendor.registration_number || null,
        inputs: clarification,
      };

      const resumed = await apiRequest(`/tasks/${selectedTask.id}/run`, {
        method: "POST",
        body: JSON.stringify(payload),
      });

      setResult(resumed);
      setNotice(formatRunNotice(resumed));
      setVendor({ name: "", website: "", registration_number: "" });
      setClarification({});
      await Promise.all([loadTasks(), loadApprovals()]);
      await loadTaskDetails(selectedTask.id);
    } catch (err) {
      setError(err.message || "The task could not resume.");
    } finally {
      actionLock.current = false;
      setBusyAction("");
    }
  }

  async function decideApproval(approvalId, decision) {
    setBusyAction(decision === "approve" ? "approving" : "rejecting");
    setError("");
    setNotice("");

    try {
      const data = await apiRequest(
        `/approvals/${approvalId}/decide?decision=${decision}`,
        { method: "POST" }
      );

      setNotice(
        decision === "approve"
          ? "The vendor proposal was approved and registered."
          : "The vendor proposal was rejected."
      );

      await Promise.all([loadTasks(), loadApprovals()]);
      if (data.task_id) {
        setSelectedId(data.task_id);
        setVendor({ name: "", website: "", registration_number: "" });
        setClarification({});
        await loadTaskDetails(data.task_id);
      }
    } catch (err) {
      setError(err.message || "The approval decision failed.");
    } finally {
      setBusyAction("");
    }
  }

  async function cancelTask(taskId) {
    setBusyAction("cancelling");
    setError("");
    setNotice("");
    try {
      await apiRequest(`/tasks/${taskId}/cancel`, { method: "POST" });
      setNotice("Task stopped. No further workflow actions will start.");
      await refreshAll();
    } catch (err) {
      setError(err.message || "The task could not be stopped.");
    } finally {
      setBusyAction("");
    }
  }

  async function saveMemory(event) {
    event.preventDefault();
    setBusyAction("saving-memory");
    setError("");
    setNotice("");
    const payload = {
      ...memoryDraft,
      memory_key: memoryDraft.memory_key.trim() || null,
    };
    try {
      await apiRequest(
        editingMemoryId ? `/memories/${editingMemoryId}` : "/memories",
        {
          method: editingMemoryId ? "PATCH" : "POST",
          body: JSON.stringify(payload),
        }
      );
      setMemoryDraft({
        category: "preference",
        kind: "user_instruction",
        content: "",
        memory_key: "",
        source: "user",
        verification_status: "unverified",
      });
      setEditingMemoryId(null);
      await loadMemories();
      setNotice("Memory saved.");
    } catch (err) {
      setError(err.message || "The memory could not be saved.");
    } finally {
      setBusyAction("");
    }
  }

  function editMemory(memory) {
    setEditingMemoryId(memory.id);
    setMemoryDraft({
      category: memory.category,
      kind: memory.kind,
      content: memory.content,
      memory_key: memory.memory_key || "",
      source: memory.source,
      verification_status: memory.verification_status === "stale"
        ? "unverified"
        : memory.verification_status,
    });
  }

  async function removeMemory(memoryId) {
    setBusyAction("deleting-memory");
    setError("");
    try {
      await apiRequest(`/memories/${memoryId}`, { method: "DELETE" });
      await loadMemories();
      setNotice("Memory deleted.");
      if (editingMemoryId === memoryId) setEditingMemoryId(null);
    } catch (err) {
      setError(err.message || "The memory could not be deleted.");
    } finally {
      setBusyAction("");
    }
  }

  async function previewMemoryContext(event) {
    event.preventDefault();
    if (!memoryQuery.trim()) return;
    setBusyAction("previewing-memory");
    setError("");
    try {
      const result = await apiRequest(
        `/memories/context?query=${encodeURIComponent(memoryQuery.trim())}`
      );
      setMemoryPreview(result);
    } catch (err) {
      setError(err.message || "Relevant memory could not be retrieved.");
    } finally {
      setBusyAction("");
    }
  }

  const pendingTasks = tasks.filter((task) =>
    shouldPollTask(task.status)
  ).length;

  const completedTasks = tasks.filter((task) =>
    ["completed", "verified"].includes(String(task.status || "").toLowerCase())
  ).length;

  const missingInformation = Array.isArray(result?.missing_information)
    ? result.missing_information
    : [];
  const verificationSummary = getVerificationSummary(result);
  const latestActivity = getLatestActivity(events);
  const recoveryAttempts = getRecoveryAttempts(result);

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
        <button className={`nav-item ${activeView === "tasks" ? "active" : ""}`} type="button" onClick={() => setActiveView("tasks")}>
          ▦ &nbsp; Task operator
        </button>
        <button
          className={`nav-item ${activeView === "memories" ? "active" : ""}`}
          type="button"
          onClick={() => {
            setActiveView("memories");
            loadMemories().catch((err) => setError(err.message || "Unable to load memories."));
          }}
        >
          ◉ &nbsp; Memory
        </button>
        <div className="nav-item">✓ &nbsp; Approval inbox ({approvals.length})</div>

        <div className="sidebar-note">
          <p>Turn business requests into verified actions, approvals, and evidence.</p>
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
            <h1>{activeView === "memories" ? "Persistent memory" : "Single-command AI dashboard"}</h1>
            <p>{activeView === "memories" ? "Review saved context and its provenance." : "One objective, registered tools, and evidence for every reported outcome."}</p>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={() => activeView === "memories" ? loadMemories() : refreshAll()}
            disabled={!!busyAction || loading}
          >
            ↻ &nbsp; Refresh
          </button>
        </header>

        {error && (
          <div className="message error-message" role="alert">⚠ &nbsp; {error}</div>
        )}
        {notice && (
          <div className="message success-message" role="status">✓ &nbsp; {notice}</div>
        )}

        <section className={`stats-grid ${activeView === "memories" ? "view-hidden" : ""}`}>
          <div className="stat-card">
            <div className="stat-icon purple">▤</div>
            <div>
              <span>Total tasks</span>
              <strong>{tasks.length}</strong>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-icon amber">◷</div>
            <div>
              <span>Active tasks</span>
              <strong>{pendingTasks}</strong>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-icon green">✓</div>
            <div>
              <span>Completed</span>
              <strong>{completedTasks}</strong>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-icon amber">✋</div>
            <div>
              <span>Pending approvals</span>
              <strong>{approvals.length}</strong>
            </div>
          </div>
        </section>

        {activeView === "memories" && (
          <section className="memory-view">
            <div className="workspace-grid">
              <div className="panel">
                <div className="history-heading">
                  <div>
                    <h2>{editingMemoryId ? "Correct memory" : "Add memory"}</h2>
                    <p>Preferences, business context, and reusable procedures.</p>
                  </div>
                </div>
                <form onSubmit={saveMemory}>
                  <div className="memory-form-grid">
                    <label>
                      Category
                      <select value={memoryDraft.category} onChange={(event) => setMemoryDraft((current) => ({ ...current, category: event.target.value }))}>
                        <option value="preference">Preference</option>
                        <option value="business_context">Business context</option>
                        <option value="procedure">Procedure</option>
                      </select>
                    </label>
                    <label>
                      Type
                      <select value={memoryDraft.kind} onChange={(event) => setMemoryDraft((current) => ({ ...current, kind: event.target.value }))}>
                        <option value="user_instruction">User instruction</option>
                        <option value="fact">Fact</option>
                        <option value="assumption">Assumption</option>
                        <option value="inference">Inferred conclusion</option>
                      </select>
                    </label>
                    <label>
                      Verification
                      <select value={memoryDraft.verification_status} onChange={(event) => setMemoryDraft((current) => ({ ...current, verification_status: event.target.value }))}>
                        <option value="unverified">Unverified</option>
                        <option value="verified">Verified</option>
                      </select>
                    </label>
                    <label>
                      Source
                      <input maxLength={100} value={memoryDraft.source} onChange={(event) => setMemoryDraft((current) => ({ ...current, source: event.target.value }))} required />
                    </label>
                    <label>
                      Conflict key
                      <input maxLength={128} value={memoryDraft.memory_key} onChange={(event) => setMemoryDraft((current) => ({ ...current, memory_key: event.target.value }))} placeholder="Optional" />
                    </label>
                  </div>
                  <label htmlFor="memory-content">Memory</label>
                  <textarea
                    id="memory-content"
                    maxLength={4000}
                    rows={5}
                    value={memoryDraft.content}
                    onChange={(event) => setMemoryDraft((current) => ({ ...current, content: event.target.value }))}
                    required
                  />
                  <div className="task-actions">
                    <button className="primary-button" type="submit" disabled={!!busyAction}>
                      {editingMemoryId ? "Save correction" : "Add memory"}
                    </button>
                    {editingMemoryId && (
                      <button className="secondary-button" type="button" onClick={() => { setEditingMemoryId(null); setMemoryDraft({ category: "preference", kind: "user_instruction", content: "", memory_key: "", source: "user", verification_status: "unverified" }); }}>
                        Cancel
                      </button>
                    )}
                  </div>
                </form>
              </div>

              <div className="panel">
                <div className="history-heading">
                  <div>
                    <h2>Retrieval preview</h2>
                    <p>Inspect the compact context selected for a task.</p>
                  </div>
                </div>
                <form onSubmit={previewMemoryContext}>
                  <label htmlFor="memory-query">Task objective</label>
                  <input id="memory-query" value={memoryQuery} onChange={(event) => setMemoryQuery(event.target.value)} placeholder="Describe a task to preview" required />
                  <button className="secondary-button memory-preview-button" type="submit" disabled={!!busyAction}>Preview context</button>
                </form>
                {memoryPreview && (
                  <div className="memory-preview-results">
                    <div className="task-meta">Retrieval: {memoryPreview.retrieval_status}</div>
                    {[...(memoryPreview.memories || []), ...(memoryPreview.task_history || [])].length === 0 ? (
                      <p className="muted-text">No relevant context found.</p>
                    ) : (
                      [...(memoryPreview.memories || []), ...(memoryPreview.task_history || [])].map((item, index) => (
                        <article className="memory-item" key={`${item.id || item.task_id}-${index}`}>
                          <div className="task-card-top">
                            <strong>{item.category || `Task #${item.task_id}`}</strong>
                            <span className={`memory-status ${item.verification_status || item.status}`}>{(item.verification_status || item.status || "unknown").replaceAll("_", " ")}</span>
                          </div>
                          <p>{item.content || item.objective}</p>
                        </article>
                      ))
                    )}
                  </div>
                )}
              </div>
            </div>

            <div className="panel memory-list-panel">
              <div className="history-heading">
                <div>
                  <h2>Stored memories</h2>
                  <p>Source and verification state remain attached to each record.</p>
                </div>
                <span className="task-count">{memories.length}</span>
              </div>
              {memories.length === 0 ? (
                <div className="empty-state">No memories stored.</div>
              ) : memories.map((memory) => (
                <article className={`memory-item ${memory.active ? "" : "memory-inactive"}`} key={memory.id}>
                  <div className="task-card-top">
                    <strong>{memory.category.replaceAll("_", " ")} · {memory.kind.replaceAll("_", " ")}</strong>
                    <span className={`memory-status ${memory.verification_status}`}>{memory.verification_status.replaceAll("_", " ")}</span>
                  </div>
                  <p>{memory.content}</p>
                  <div className="memory-provenance">
                    <span>Source: {memory.source}{memory.source_ref ? ` · ${memory.source_ref}` : ""}</span>
                    <span>{memory.created_at ? new Date(memory.created_at).toLocaleString() : ""}</span>
                  </div>
                  <div className="task-actions">
                    <button className="secondary-button" type="button" onClick={() => editMemory(memory)}>Edit</button>
                    <button className="secondary-button danger-button" type="button" onClick={() => removeMemory(memory.id)} disabled={!!busyAction}>Delete</button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        <section className={`workspace-grid ${activeView === "memories" ? "view-hidden" : ""}`}>
          <div className="panel">
            <div className="panel-heading">
              <div className="heading-icon">✧</div>
              <div>
                <h2>Run autonomous task</h2>
                <p>Describe the outcome and let the workflow plan, execute, and verify.</p>
              </div>
            </div>

            <form onSubmit={runAutonomousTask}>
              <label htmlFor="objective">Objective</label>
              <textarea
                id="objective"
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                placeholder="Example: Register Acme Supply under the procurement policy."
                rows={5}
                maxLength={5000}
                required
              />

              <div className="form-hint">
                <span>Use a precise business outcome.</span>
                <span>{objective.length}/5000</span>
              </div>

              <button
                className="primary-button"
                type="submit"
                disabled={loading || !!busyAction || !objective.trim()}
              >
                {busyAction ? "Running..." : "Run autonomous workflow"}
              </button>
            </form>

            <div className="capability-note">
              <strong>Execution safeguards</strong>
              <p>Only registered backend tools may run. Objectives requiring unavailable integrations are marked unsupported; vendor registration pauses for approval.</p>
            </div>

            <div className="history-heading">
              <div>
                <h2>Task history</h2>
                <p>Select a task to review its plan and evidence.</p>
              </div>
              <span className="task-count">{tasks.length}</span>
            </div>

            {loading && tasks.length === 0 ? (
              <div className="empty-state">Loading tasks...</div>
            ) : tasks.length === 0 ? (
              <div className="empty-state">Run your first objective to begin the workflow.</div>
            ) : (
              <div className="task-list">
                {tasks.map((task) => (
                  <article
                    className={`task-card ${selectedId === task.id ? "selected-task" : ""}`}
                    key={task.id}
                  >
                    <button
                      className="task-select"
                      type="button"
                      onClick={() => {
                        setSelectedId(task.id);
                        setVendor({ name: "", website: "", registration_number: "" });
                        setClarification({});
                        setError("");
                        loadTaskDetails(task.id);
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
              {selectedTask && (
                <div className="task-actions">
                  <StatusBadge status={selectedTask.status} />
                  {["pending", "planned", "waiting_for_input", "needs_input", "running", "processing", "partially_completed", "waiting_for_approval", "awaiting_approval"].includes(selectedTask.status) && (
                    <button
                      className="secondary-button"
                      type="button"
                      disabled={!!busyAction}
                      onClick={() => cancelTask(selectedTask.id)}
                    >
                      Stop task
                    </button>
                  )}
                </div>
              )}
            </div>

            {selectedTask ? (
              <>
                <h3>Objective</h3>
                <p className="task-objective">{selectedTask.objective}</p>

                <div className="section-divider" />

                <h3>Current status</h3>
                <p className="muted-text">{getTaskStatusMessage(selectedTask.status, result)}</p>

                {plan?.steps?.length > 0 && (
                  <div className="workflow-progress" aria-label="Workflow progress">
                    {getWorkflowProgress(plan, result, selectedTask.status).map((step) => (
                      <div className={`workflow-step ${step.complete ? "complete" : ""} ${step.waitingForApproval || step.waitingForInput ? "waiting" : ""}`} key={step.step}>
                        <span>{step.complete ? "✓" : step.step}</span>
                        <div>
                          <strong>{step.action}</strong>
                          <small>{step.label}</small>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {latestActivity && (
                  <div className="current-action">
                    <span className="eyebrow">LATEST ACTIVITY</span>
                    <p>{latestActivity}</p>
                  </div>
                )}

                {recoveryAttempts.length > 0 && (
                  <div className="recovery-summary">
                    <strong>Recovery attempts ({recoveryAttempts.length})</strong>
                    {recoveryAttempts.map((attempt, index) => (
                      <p key={`${attempt.capability}-${attempt.failedAttempt}-${index}`}>
                        {attempt.capability}: retry {attempt.retryAttempt} of {attempt.maxAttempts} after {attempt.error}
                      </p>
                    ))}
                  </div>
                )}

                <div className="section-divider" />

                <h3>Generated plan</h3>
                {!plan ? (
                  <p className="muted-text">The plan will appear here after the workflow starts running.</p>
                ) : (
                  <details className="plan-details">
                    <summary>View validated plan ({plan.steps.length} steps)</summary>
                    <p><strong>Goal:</strong> {plan.goal}</p>
                    {plan.supported === false && <p><strong>Supported:</strong> No</p>}
                    {(plan.unavailable_capabilities || []).length > 0 && (
                      <p><strong>Unavailable capabilities:</strong> {plan.unavailable_capabilities.join(", ")}</p>
                    )}
                    {(plan.limitations || []).map((limitation, index) => (
                      <p className="muted-text" key={`limitation-${index}`}>{limitation}</p>
                    ))}
                    {(plan.steps || []).map((step) => (
                      <div className="evidence-card" key={`${step.step}-${step.action}`}>
                        <strong>Step {step.step}: {step.action}</strong>
                        <p>Capability: <code>{step.capability}</code></p>
                        <p>Depends on: {(step.depends_on || []).length ? step.depends_on.join(", ") : "None"}</p>
                        {Object.keys(step.inputs || {}).length > 0 && (
                          <p>Inputs: <code>{JSON.stringify(step.inputs)}</code></p>
                        )}
                        <p>Approval required: {step.requires_approval ? "Yes" : "No"}</p>
                        <p className="muted-text">{step.expected_result}</p>
                        {(step.success_criteria || []).map((criterion, index) => (
                          <p className="muted-text" key={`${step.step}-criterion-${index}`}>Success: {criterion}</p>
                        ))}
                      </div>
                    ))}
                    {(plan.risks || []).length > 0 && (
                      <div className="plan-risks">
                        <strong>Plan risks</strong>
                        <ul>{plan.risks.map((risk, index) => <li key={index}>{risk}</li>)}</ul>
                      </div>
                    )}
                  </details>
                )}

                <div className="section-divider" />

                {missingInformation.length > 0 && (
                  <div className="input-panel">
                    <h3>Missing information</h3>
                    <p className="muted-text">
                      The workflow paused for required details. Add them below and continue the same task without creating a duplicate.
                    </p>
                    <form onSubmit={resumeTask}>
                      {missingInformation.includes("vendor_name") && (
                        <>
                          <label htmlFor="resume-name">Vendor legal name</label>
                          <input
                            id="resume-name"
                            value={vendor.name}
                            onChange={(event) => setVendor((current) => ({ ...current, name: event.target.value }))}
                            placeholder="Required legal name"
                            required
                          />
                        </>
                      )}

                      {missingInformation.includes("vendor_website") && (
                        <>
                          <label htmlFor="resume-website">Vendor website</label>
                          <input
                            id="resume-website"
                            type="url"
                            value={vendor.website}
                            onChange={(event) => setVendor((current) => ({ ...current, website: event.target.value }))}
                            placeholder="https://example.com"
                            required
                          />
                        </>
                      )}

                      {missingInformation.includes("registration_number") && (
                        <>
                          <label htmlFor="resume-reg">Registration number</label>
                          <input
                            id="resume-reg"
                            value={vendor.registration_number}
                            onChange={(event) => setVendor((current) => ({ ...current, registration_number: event.target.value }))}
                            required
                          />
                        </>
                      )}

                      {missingInformation
                        .filter((field) => !["vendor_name", "vendor_website", "registration_number"].includes(field))
                        .map((field) => (
                          <div key={field}>
                            <label htmlFor={`resume-${field.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`}>{field.replaceAll("_", " ")}</label>
                            <input
                              id={`resume-${field.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`}
                              value={clarification[field] || ""}
                              onChange={(event) => setClarification((current) => ({ ...current, [field]: event.target.value }))}
                              required
                            />
                          </div>
                        ))}

                      <button className="primary-button" type="submit" disabled={!!busyAction}>
                        Resume task
                      </button>
                    </form>
                  </div>
                )}

                <div className="section-divider" />

                <h3>Independent verification</h3>
                {verificationSummary ? (
                  <div className={`verification-result ${verificationSummary.verified ? "verified" : "failed"}`}>
                    <strong>{verificationSummary.label}</strong>
                    <p>{verificationSummary.message}</p>
                    {verificationSummary.source && <small>Source: {verificationSummary.source}</small>}
                    {verificationSummary.vendorId && <small>Vendor record: #{verificationSummary.vendorId}</small>}
                  </div>
                ) : (
                  <p className="muted-text">Verification has not completed.</p>
                )}

                <div className="section-divider" />

                <h3>Execution result</h3>
                {result ? (
                  <pre className="json-panel">{JSON.stringify(result, null, 2)}</pre>
                ) : selectedTask.result_json ? (
                  <pre className="json-panel">{selectedTask.result_json}</pre>
                ) : (
                  <p className="muted-text">No execution result has been recorded yet.</p>
                )}

                <div className="section-divider" />

                <h3>Execution evidence</h3>
                {events.length === 0 ? (
                  <p className="muted-text">No evidence is available for this task yet.</p>
                ) : (
                  events.map((event) => (
                    <div className="evidence-card" key={event.id}>
                      <div className="task-card-top">
                        <strong>{String(event.event_type || "event").replaceAll("_", " ")}</strong>
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
                <p>Select any task from the list to inspect its plan, evidence, and status.</p>
              </div>
            )}
          </div>
        </section>

        <section className={`panel approval-panel ${activeView === "memories" ? "view-hidden" : ""}`}>
          <div className="history-heading">
            <div>
              <h2>Approval inbox</h2>
              <p>Review the exact action and its consequences before deciding.</p>
            </div>
            <span className="task-count">{approvals.length}</span>
          </div>

          {approvals.length === 0 ? (
            <p className="muted-text">No pending approvals.</p>
          ) : (
            <div className="approval-list">
              {approvals.map((approval) => {
                const proposal = getApprovalProposal(approval);
                return (
                <article className="evidence-card" key={proposal.id}>
                  <div className="task-card-top">
                    <strong>Approval #{proposal.id} · Task #{proposal.taskId} · {proposal.capability || "registered action"}</strong>
                    <StatusBadge status={approval.status} />
                  </div>
                  <p><strong>Action:</strong> {proposal.action}</p>
                  <p><strong>Target:</strong> {proposal.target || "Not specified"}</p>
                  {proposal.capability === "register_vendor" ? (
                    <>
                      <p><strong>{proposal.name}</strong></p>
                      <p>{proposal.website}</p>
                      <p className="muted-text">Registration number: {proposal.registrationNumber || "Not provided"}</p>
                    </>
                  ) : (
                    <div>
                      <strong>Exact parameters</strong>
                      <pre className="approval-parameters">{JSON.stringify(proposal.parameters, null, 2)}</pre>
                    </div>
                  )}
                  {proposal.expectedConsequences && <p><strong>Expected consequences:</strong> {proposal.expectedConsequences}</p>}
                  {proposal.risk && (
                    <p className="muted-text">
                      Risk: {proposal.risk.level}; impact {proposal.risk.impact}; {proposal.risk.reversibility}; data {proposal.risk.data_sensitivity}; external consequences {proposal.risk.external_consequences}.
                    </p>
                  )}
                  {proposal.expiresAt && <p className="muted-text">Expires: {new Date(proposal.expiresAt).toLocaleString()}</p>}
                  <div className="task-actions">
                    <button
                      className="primary-button"
                      disabled={!!busyAction}
                      onClick={() => decideApproval(approval.id, "approve")}
                    >
                      {proposal.capability === "register_vendor" ? "Approve & register" : "Approve & execute"}
                    </button>
                    <button
                      className="secondary-button"
                      disabled={!!busyAction}
                      onClick={() => decideApproval(approval.id, "reject")}
                    >
                      Reject
                    </button>
                    <button
                      className="secondary-button"
                      disabled={!!busyAction}
                      onClick={() => cancelTask(approval.task_id)}
                    >
                      Cancel task
                    </button>
                  </div>
                </article>
                );
              })}
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
