import { getApprovalProposal } from "./workflowPresentation.js";
import { StatusBadge } from "./TaskPanels.jsx";

export default function ApprovalCenter({ approvals, busyAction, onDecide, onCancel, onOpenTask }) {
  return (
    <section className="panel approval-panel">
      <div className="history-heading">
        <div>
          <h2>Approval inbox</h2>
          <p>Review the exact proposed action and consequences before deciding.</p>
        </div>
        <span className="task-count">{approvals.length}</span>
      </div>

      {approvals.length === 0 ? (
        <div className="empty-state">No pending approvals.</div>
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
                <div>
                  <strong>Exact parameters</strong>
                  <pre className="approval-parameters">{JSON.stringify(proposal.parameters, null, 2)}</pre>
                </div>
                {proposal.expectedConsequences && <p><strong>Expected consequences:</strong> {proposal.expectedConsequences}</p>}
                {proposal.risk && (
                  <p className="muted-text">
                    Risk: {proposal.risk.level}; impact {proposal.risk.impact}; {proposal.risk.reversibility}; data {proposal.risk.data_sensitivity}; external consequences {proposal.risk.external_consequences}.
                  </p>
                )}
                {proposal.expiresAt && <p className="muted-text">Expires: {new Date(proposal.expiresAt).toLocaleString()}</p>}
                <div className="task-actions approval-actions">
                  <button className="primary-button" type="button" disabled={!!busyAction} onClick={() => onDecide(approval.id, "approve")}>
                    {proposal.capability === "register_vendor" ? "Approve & register" : "Approve & execute"}
                  </button>
                  <button className="secondary-button" type="button" disabled={!!busyAction} onClick={() => onDecide(approval.id, "reject")}>Reject</button>
                  <button className="secondary-button danger-button" type="button" disabled={!!busyAction} onClick={() => onCancel(approval.task_id)}>Cancel task</button>
                  <button className="secondary-button" type="button" onClick={() => onOpenTask(approval.task_id)}>View task</button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}