import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  getApprovalProposal,
  getLatestActivity,
  getRecoveryAttempts,
  getTaskStatusMessage,
  getTaskResultPresentation,
  getVerificationSummary,
  getWorkflowProgress,
  normalizeStatusClass,
  shouldPollTask,
} from "./workflowPresentation.js";

describe("workflow status presentation", () => {
  it("polls only active backend work, not human-paused or terminal tasks", () => {
    assert.equal(shouldPollTask("processing"), true);
    assert.equal(shouldPollTask("executing"), true);
    assert.equal(shouldPollTask("needs_input"), false);
    assert.equal(shouldPollTask("awaiting_approval"), false);
    assert.equal(shouldPollTask("completed"), false);
  });

  it("maps persisted checkpoints to accurate step labels", () => {
    const plan = {
      steps: [
        { step: 1, action: "Read policy" },
        { step: 2, action: "Validate vendor" },
      ],
    };
    const state = { completed_steps: [1], current_step: 2 };

    assert.deepEqual(
      getWorkflowProgress(plan, state, "needs_input").map((step) => step.label),
      ["Complete", "Waiting for information"],
    );
    assert.deepEqual(
      getWorkflowProgress(plan, state, "awaiting_approval").map((step) => step.label),
      ["Complete", "Awaiting approval"],
    );
    assert.equal(getWorkflowProgress(plan, state, "running")[1].label, "In progress");
  });

  it("presents persisted tool outputs, source links, generated files, and partial failures", () => {
    const presentation = getTaskResultPresentation({
      completed_steps: [1],
      final_summary: "One step completed before execution stopped.",
      step_results: [
        {
          step: 1,
          capability: "files.create",
          status: "completed",
          success: true,
          outputs: {
            success: true,
            path: "reports/result.md",
            size_bytes: 42,
            sha256: "digest",
            source: "https://example.com/source",
            report_markdown: "Read more: https://example.com/report-source.",
          },
        },
        {
          step: 2,
          capability: "web.fetch_page",
          status: "failed",
          success: false,
          result: { error: "The website is unavailable." },
        },
      ],
    }, {
      limitations: ["External access is restricted."],
      steps: [{ step: 1, action: "Write report" }, { step: 2, action: "Fetch source" }],
    }, "partially_completed");

    assert.equal(presentation.partial, true);
    assert.deepEqual(presentation.files, [{
      path: "reports/result.md",
      size: 42,
      digest: "digest",
    }]);
    assert.deepEqual(presentation.sources, [
      "https://example.com/source",
      "https://example.com/report-source",
    ]);
    assert.equal(presentation.reports[0], "Read more: https://example.com/report-source.");
    assert.deepEqual(presentation.limitations, ["External access is restricted."]);
    assert.equal(presentation.blockingIssues[0], "Fetch source: The website is unavailable.");
  });

  it("surfaces a saved summarizer answer when the final summary only reports unavailable verification", () => {
    const presentation = getTaskResultPresentation({
      final_summary: "One or more steps could not be independently verified.",
      step_results: [{
        step: 4,
        capability: "documents.summarize_text",
        status: "completed",
        success: true,
        outputs: { success: true, summary: "Invoice INV-2048 needs receipt evidence before approval." },
      }],
    }, { steps: [{ step: 4, action: "Summarize findings" }] }, "executed");

    assert.equal(presentation.summary, "Invoice INV-2048 needs receipt evidence before approval.");
  });

  it("renders the exact saved approval proposal without inventing details", () => {
    assert.deepEqual(getApprovalProposal({
      id: 8,
      task_id: 12,
      action: {
        name: "Acme Supply",
        website: "https://acme.example",
        registration_number: "AC-8",
      },
    }), {
      id: 8,
      taskId: 12,
      capability: "register_vendor",
      action: "Register vendor",
      target: "https://acme.example",
      parameters: {
        name: "Acme Supply",
        website: "https://acme.example",
        registration_number: "AC-8",
      },
      expectedConsequences: null,
      risk: null,
      expiresAt: null,
      name: "Acme Supply",
      website: "https://acme.example",
      registrationNumber: "AC-8",
    });
    assert.equal(getApprovalProposal({}).name, "Name not provided");
  });

  it("preserves a generic approval's exact action details", () => {
    assert.deepEqual(getApprovalProposal({
      id: 9,
      task_id: 13,
      expires_at: "2030-01-01T00:00:00Z",
      action: {
        capability: "files.edit",
        action: "Replace text in an authorized file.",
        target: "reports/quarterly.md",
        parameters: { old_text: "draft", new_text: "approved" },
        expected_consequences: "Updates the report in place.",
        risk: { level: "low", reversibility: "reversible" },
      },
    }), {
      id: 9,
      taskId: 13,
      capability: "files.edit",
      action: "Replace text in an authorized file.",
      target: "reports/quarterly.md",
      parameters: { old_text: "draft", new_text: "approved" },
      expectedConsequences: "Updates the report in place.",
      risk: { level: "low", reversibility: "reversible" },
      expiresAt: "2030-01-01T00:00:00Z",
      name: "Name not provided",
      website: "Website not provided",
      registrationNumber: null,
    });
  });

  it("shows verification only when the backend recorded a boolean result", () => {
    assert.equal(getVerificationSummary({ verification: null }), null);
    assert.deepEqual(getVerificationSummary({
      verification: {
        matches: false,
        message: "Persisted details did not match.",
        vendor_id: 44,
      },
    }), {
      verified: false,
      label: "Verification failed",
      message: "Persisted details did not match.",
      source: null,
      vendorId: 44,
    });
  });

  it("explains when independent verification is unavailable without labeling the task as failed", () => {
    assert.deepEqual(getVerificationSummary({
      verification: {
        matches: false,
        status: "unavailable",
        message: "One or more steps could not be independently verified.",
        steps: [{ status: "unavailable", message: "No independent verifier is registered for this capability." }],
      },
    }), {
      verified: null,
      label: "Independent verification unavailable",
      message: "No independent verifier is registered for this capability.",
      source: null,
      vendorId: null,
    });
  });

  it("uses the latest persisted execution event as current activity", () => {
    assert.equal(getLatestActivity([]), null);
    assert.equal(getLatestActivity([
      { message: "Policy read." },
      { message: "Vendor details validated." },
    ]), "Vendor details validated.");
  });

  it("presents only persisted retry attempts with bounded attempt counts", () => {
    assert.deepEqual(getRecoveryAttempts({
      recovery_attempts: [{
        capability: "read_company_policy",
        attempt: 1,
        max_attempts: 3,
        error: "Interrupted read",
      }],
    }), [{
      capability: "read_company_policy",
      failedAttempt: 1,
      retryAttempt: 2,
      maxAttempts: 3,
      error: "Interrupted read",
    }]);
    assert.deepEqual(getRecoveryAttempts({}), []);
  });

  it("reports failed and rejected outcomes instead of presenting them as active work", () => {
    assert.equal(
      getTaskStatusMessage("failed", { final_summary: "Duplicate registration number." }),
      "Duplicate registration number.",
    );
    assert.equal(
      getTaskStatusMessage("rejected", {}),
      "The proposal was rejected; no vendor was registered.",
    );
    assert.equal(
      getTaskStatusMessage("verification_failed", {
        verification: { message: "Saved vendor details did not match." },
      }),
      "Saved vendor details did not match.",
    );
  });

  it("assigns completed and rejected statuses distinct tones", () => {
    assert.equal(normalizeStatusClass("verified"), "completed");
    assert.equal(normalizeStatusClass("rejected"), "failed");
    assert.equal(normalizeStatusClass("unsupported"), "failed");
    assert.equal(normalizeStatusClass("awaiting_approval"), "approval");
  });

  it("explains when the registered tools cannot perform an objective", () => {
    assert.equal(
      getTaskStatusMessage("unsupported", {}),
      "The required integration is not available in the registered tool set.",
    );
  });
});
