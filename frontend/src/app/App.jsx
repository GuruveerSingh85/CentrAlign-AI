import { useCallback, useEffect, useRef, useState } from "react";
import { shouldPollTask } from "../features/workflows/workflowPresentation.js";
import { apiRequest } from "../api/api.js";
import { TaskComposer, TaskHistory } from "../features/tasks/TaskPanels.jsx";
import ExecutionMonitor from "../features/tasks/ExecutionMonitor.jsx";
import ApprovalCenter from "../features/approvals/ApprovalCenter.jsx";
import LedgerlySite from "../ledgerly/LedgerlySite.jsx";
import "./App.css";

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

function AgentDashboard() {
  const [objective, setObjective] = useState("");
  const [taskInputsText, setTaskInputsText] = useState("");
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
    scope: "user",
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

  async function previewAutonomousTask(event) {
    event.preventDefault();
    if (!objective.trim() || actionLock.current) return;

    let inputs;
    try {
      inputs = taskInputsText.trim() ? JSON.parse(taskInputsText) : {};
      if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") {
        throw new Error("Task inputs must be a JSON object.");
      }
    } catch (err) {
      setError(err.message || "Task inputs must be valid JSON.");
      return;
    }

    actionLock.current = true;
    setBusyAction("planning");
    setLoading(true);
    setError("");
    setNotice("");
    let createdTaskId = null;

    try {
      const createdTask = await apiRequest("/tasks", {
        method: "POST",
        body: JSON.stringify({ objective: objective.trim() }),
      });

      createdTaskId = createdTask.id;
      setSelectedId(createdTask.id);
      setVendor({ name: "", website: "", registration_number: "" });
      setClarification({});
      setPlan(null);
      setEvents([]);
      setResult(null);

      const preview = await apiRequest(`/tasks/${createdTask.id}/plan`, {
        method: "POST",
        body: JSON.stringify({ inputs }),
      });

      setPlan(preview.plan);
      setObjective("");
      setTaskInputsText("");
      if (preview.plan?.supported && Array.isArray(preview.plan.steps) && preview.plan.steps.length > 0
          && !(preview.plan.missing_information || []).length) {
        setBusyAction("running");
        const runResult = await apiRequest(`/tasks/${createdTask.id}/run`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setNotice(formatRunNotice(runResult));
      } else if ((preview.plan?.missing_information || []).length) {
        setNotice("The task needs more information before it can run. Add the missing details and submit again.");
      } else {
        setNotice("The plan is ready, but this task has no supported actions to run.");
      }
      await Promise.all([loadTasks(), loadApprovals()]);
      await loadTaskDetails(createdTask.id);
    } catch (err) {
      setError(err.message || "The task could not be processed.");
      if (createdTaskId) {
        setObjective("");
        setTaskInputsText("");
        await loadTasks().catch(() => {});
        await loadTaskDetails(createdTaskId);
      }
    } finally {
      actionLock.current = false;
      setBusyAction("");
      setLoading(false);
    }
  }

  async function startTask(task = selectedTask) {
    if (!task || actionLock.current) return;

    actionLock.current = true;
    setBusyAction("running");
    setError("");
    setNotice("");
    try {
      const runResult = await apiRequest(`/tasks/${task.id}/run`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      setNotice(formatRunNotice(runResult));
      await Promise.all([loadTasks(), loadApprovals()]);
      await loadTaskDetails(task.id);
    } catch (err) {
      setError(err.message || "The task could not be started.");
    } finally {
      actionLock.current = false;
      setBusyAction("");
    }
  }

  async function clearTaskHistory(task) {
    if (actionLock.current || !window.confirm(`Clear task #${task.id} and permanently delete its plan, result, and history?`)) return;

    actionLock.current = true;
    setBusyAction("clearing-history");
    setError("");
    setNotice("");
    try {
      await apiRequest(`/tasks/${task.id}`, { method: "DELETE" });
      const remainingTasks = await loadTasks();
      if (selectedId === task.id) {
        const nextTask = remainingTasks[0];
        setSelectedId(nextTask?.id ?? null);
        await loadTaskDetails(nextTask?.id);
      }
      await loadApprovals();
      setNotice(`Task #${task.id} and its history were cleared.`);
    } catch (err) {
      setError(err.message || "Task history could not be cleared.");
    } finally {
      actionLock.current = false;
      setBusyAction("");
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
    if (actionLock.current) return;
    actionLock.current = true;
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
      actionLock.current = false;
      setBusyAction("");
    }
  }

  async function replanTask(event) {
    event?.preventDefault();
    if (!selectedTask || actionLock.current) return;

    actionLock.current = true;
    setBusyAction("planning");
    setError("");
    setNotice("");
    const savedInputs = result && !Array.isArray(result) && typeof result === "object"
      ? result.inputs || {}
      : {};
    const inputs = { ...savedInputs, ...clarification };
    if (vendor.name.trim()) inputs.name = vendor.name.trim();
    if (vendor.website.trim()) inputs.website = vendor.website.trim();
    if (vendor.registration_number.trim()) {
      inputs.registration_number = vendor.registration_number.trim();
    }

    try {
      const preview = await apiRequest(`/tasks/${selectedTask.id}/plan`, {
        method: "POST",
        body: JSON.stringify({ inputs }),
      });
      setPlan(preview.plan);
      setNotice("Updated plan saved. Review the revised steps before starting execution.");
      setVendor({ name: "", website: "", registration_number: "" });
      setClarification({});
      await Promise.all([loadTasks(), loadApprovals()]);
      await loadTaskDetails(selectedTask.id);
    } catch (err) {
      setError(err.message || "The plan could not be prepared.");
      await loadTasks().catch(() => {});
      await loadTaskDetails(selectedTask.id);
    } finally {
      actionLock.current = false;
      setBusyAction("");
    }
  }

  async function cancelTask(taskId) {
    if (actionLock.current) return;
    actionLock.current = true;
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
      actionLock.current = false;
      setBusyAction("");
    }
  }

  function openTask(taskId) {
    setActiveView("tasks");
    setSelectedId(taskId);
    setError("");
    loadTaskDetails(taskId);
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
        scope: "user",
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
      scope: memory.scope || "user",
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
        <button
          className={`nav-item ${activeView === "approvals" ? "active" : ""}`}
          type="button"
          onClick={() => setActiveView("approvals")}
        >
          ✓ &nbsp; Approval inbox ({approvals.length})
        </button>
        <a className="nav-item" href="/dashboard" target="_blank" rel="noreferrer">
          ◫ &nbsp; Ledgerly mock site
        </a>

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
            <h1>{activeView === "memories" ? "Persistent memory" : activeView === "approvals" ? "Approval center" : "AI employee workspace"}</h1>
            <p>{activeView === "memories" ? "Review saved context and its provenance." : activeView === "approvals" ? "Review sensitive actions and decide from their exact persisted proposals." : "Plan work, monitor real execution, and inspect verified results."}</p>
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
                      Scope
                      <select
                        value={memoryDraft.scope}
                        disabled={!!editingMemoryId}
                        onChange={(event) => setMemoryDraft((current) => ({ ...current, scope: event.target.value }))}
                      >
                        <option value="user">User</option>
                        <option value="organization">Organization</option>
                      </select>
                    </label>
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
                      <select
                        value={memoryDraft.category === "procedure" ? "unverified" : memoryDraft.verification_status}
                        disabled={memoryDraft.category === "procedure"}
                        onChange={(event) => setMemoryDraft((current) => ({ ...current, verification_status: event.target.value }))}
                      >
                        <option value="unverified">Unverified</option>
                        {memoryDraft.category !== "procedure" && <option value="verified">Verified</option>}
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
                      <button className="secondary-button" type="button" onClick={() => { setEditingMemoryId(null); setMemoryDraft({ category: "preference", scope: "user", kind: "user_instruction", content: "", memory_key: "", source: "user", verification_status: "unverified" }); }}>
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
                    <span>{memory.scope === "organization" ? `Organization: ${memory.organization_id}` : "User scope"} · Source: {memory.source}{memory.source_ref ? ` · ${memory.source_ref}` : ""}</span>
                    <span>{memory.created_at ? new Date(memory.created_at).toLocaleString() : ""}</span>
                  </div>
                  {memory.category === "procedure" && memory.procedure_key && (
                    <p className="task-meta">
                      Version {memory.procedure_version} · {memory.procedure_successes} verified successes · {memory.procedure_failures} verification failures · confidence {Math.round(memory.procedure_confidence * 100)}%
                    </p>
                  )}
                  <div className="task-actions">
                    <button className="secondary-button" type="button" onClick={() => editMemory(memory)}>Edit</button>
                    <button className="secondary-button danger-button" type="button" onClick={() => removeMemory(memory.id)} disabled={!!busyAction}>Delete</button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        {activeView === "tasks" && (
          <section className="workspace-grid task-workspace">
            <div className="task-side-column">
              <TaskComposer
                objective={objective}
                onObjectiveChange={setObjective}
                inputsText={taskInputsText}
                onInputsChange={setTaskInputsText}
                onPreview={previewAutonomousTask}
                busyAction={busyAction}
              />
              <TaskHistory
                tasks={tasks}
                selectedId={selectedId}
                loading={loading}
                busyAction={busyAction}
                onClear={clearTaskHistory}
                onSelect={(task) => {
                  setSelectedId(task.id);
                  setVendor({ name: "", website: "", registration_number: "" });
                  setClarification({});
                  setError("");
                  loadTaskDetails(task.id);
                }}
              />
            </div>
            <ExecutionMonitor
              task={selectedTask}
              plan={plan}
              state={result}
              events={events}
              busyAction={busyAction}
              onCancel={cancelTask}
              onStart={startTask}
              onReplan={replanTask}
              onResume={resumeTask}
              vendor={vendor}
              setVendor={setVendor}
              clarification={clarification}
              setClarification={setClarification}
              onFeedbackSaved={() => loadTaskDetails(selectedId)}
            />
          </section>
        )}

        {activeView === "approvals" && (
          <ApprovalCenter
            approvals={approvals}
            busyAction={busyAction}
            onDecide={decideApproval}
            onCancel={cancelTask}
            onOpenTask={openTask}
          />
        )}

        <footer className="page-footer">
          CentrAlign AI Operator <span>·</span> Prototype v0.2
        </footer>
      </main>
    </div>
  );
}

export default function App() {
  const path = window.location.pathname;
  const isLedgerlyRoute = /^\/(dashboard|invoices|vendors|purchase-orders|payment-approvals|payment-runs|knowledge-base|activity|help)(\/|$)/.test(path);
  return isLedgerlyRoute ? <LedgerlySite /> : <AgentDashboard />;
}
