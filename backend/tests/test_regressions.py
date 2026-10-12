import errno
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app import main, tool_registry, workflow
from app.models import Task, Vendor
from app.tool_registry import execute_tool, check_policy, get_registered_tool_contracts


class ToolRegistryRegressionTests(unittest.TestCase):
    def test_execute_tool_reads_policy(self):
        result = execute_tool("read_company_policy")
        self.assertTrue(result["success"])
        self.assertIn("Sample Procurement Policy", result["policy_text"])

    def test_check_policy_requires_vendor_name_and_website(self):
        result = check_policy({"name": "", "website": "https://example.com"})
        self.assertFalse(result["success"])
        self.assertIn("vendor_name", result["missing_information"])

    def test_execute_tool_rejects_unsupported_capabilities(self):
        result = execute_tool("not_a_real_tool")
        self.assertFalse(result["success"])
        self.assertIn("Unsupported capability", result["error"])

    def test_tool_contract_catalog_tracks_executable_registry(self):
        contracts = get_registered_tool_contracts()
        self.assertEqual(
            {contract["capability"] for contract in contracts},
            set(tool_registry.TOOLS),
        )
        self.assertTrue(all(contract["description"] for contract in contracts))

    def test_register_vendor_tool_requires_persisted_approval(self):
        result = execute_tool("register_vendor", {
            "name": "Unapproved Vendor",
            "website": "https://vendor.example",
        })
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_type"], "approval_required")

    def test_only_known_transient_file_and_database_errors_are_retryable(self):
        with self.assertLogs("app.tool_registry", level="ERROR"):
            with patch.object(
                type(tool_registry.POLICY_PATH),
                "read_text",
                side_effect=OSError(errno.EINTR, "interrupted"),
            ):
                policy_result = tool_registry.read_company_policy()
            self.assertEqual(policy_result["failure_type"], "transient_io_error")

            locked_database_error = OperationalError(
                "SELECT vendor", {}, RuntimeError("database is locked")
            )
            with patch.dict(tool_registry.TOOLS, {
                "check_vendor_duplicate": Mock(side_effect=locked_database_error),
            }):
                transient_result = execute_tool(
                    "check_vendor_duplicate", {"registration_number": "TEST"}
                )
            self.assertEqual(transient_result["failure_type"], "transient_database_error")

            permanent_database_error = OperationalError(
                "SELECT vendor", {}, RuntimeError("no such table")
            )
            with patch.dict(tool_registry.TOOLS, {
                "check_vendor_duplicate": Mock(side_effect=permanent_database_error),
            }):
                permanent_result = execute_tool(
                    "check_vendor_duplicate", {"registration_number": "TEST"}
                )
            self.assertEqual(permanent_result["failure_type"], "tool_error")


class AutonomousWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "workflow-test.db"
        self.test_engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        self.patchers = [
            patch.object(main, "engine", self.test_engine),
            patch.object(workflow, "engine", self.test_engine),
            patch.object(tool_registry, "engine", self.test_engine),
        ]
        for patcher in self.patchers:
            patcher.start()
        workflow._scheduled_tasks.clear()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.planner_patcher = patch(
            "app.workflow.create_plan",
            return_value={
                "goal": "Complete the procurement request safely.",
                "success_criteria": ["Use the procurement policy and verify the result."],
                "steps": [
                    {
                        "step": 1,
                        "action": "Read procurement policy",
                        "capability": "read_company_policy",
                        "requires_approval": False,
                        "expected_result": "Policy content is available.",
                    },
                    {
                        "step": 2,
                        "action": "Validate vendor",
                        "capability": "check_policy",
                        "requires_approval": False,
                        "expected_result": "Vendor details comply.",
                    },
                    {
                        "step": 3,
                        "action": "Check duplicate number",
                        "capability": "check_vendor_duplicate",
                        "requires_approval": False,
                        "expected_result": "No duplicate exists.",
                    },
                    {
                        "step": 4,
                        "action": "Register vendor",
                        "capability": "register_vendor",
                        "requires_approval": True,
                        "expected_result": "Human approval is recorded.",
                    },
                ],
                "risks": [],
            },
        )
        self.planner_patcher.start()

    def tearDown(self):
        self.planner_patcher.stop()
        self.client.__exit__(None, None, None)
        workflow._scheduled_tasks.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.test_engine.dispose()
        self.temp_dir.cleanup()

    def create_task(self, objective):
        response = self.client.post("/api/tasks", json={"objective": objective})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_plan_preview_accepts_and_persists_supplied_inputs(self):
        task_id = self.create_task("Register Acme Supply as a vendor.")
        inputs = {
            "name": "Acme Supply",
            "website": "https://acme.example",
            "registration_number": "AC-100",
        }

        with patch("app.workflow.create_plan", return_value=self.policy_plan()) as planner:
            response = self.client.post(
                f"/api/tasks/{task_id}/plan",
                json={"inputs": inputs},
            )

        self.assertEqual(response.status_code, 200, response.text)
        planner.assert_called_once()
        self.assertEqual(
            planner.call_args.args[:2],
            ("Register Acme Supply as a vendor.", inputs),
        )
        self.assertEqual(response.json()["status"], "planned")
        self.assertEqual(len(response.json()["plan"]["steps"]), 4)
        saved_task = next(
            task for task in self.client.get("/api/tasks").json()
            if task["id"] == task_id
        )
        self.assertEqual(json.loads(saved_task["plan_json"]), response.json()["plan"])
        self.assertEqual(json.loads(saved_task["result_json"])["inputs"], inputs)
        event_types = [
            event["event_type"]
            for event in self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
        ]
        self.assertIn("plan_previewed", event_types)

    def test_failed_plan_preview_keeps_inputs_for_retry(self):
        task_id = self.create_task("Review the procurement policy.")
        inputs = {"policy_version": "2026"}

        with patch("app.workflow.create_plan", side_effect=RuntimeError("planner unavailable")):
            response = self.client.post(
                f"/api/tasks/{task_id}/plan",
                json={"inputs": inputs},
            )

        self.assertEqual(response.status_code, 502)
        saved_task = next(
            task for task in self.client.get("/api/tasks").json()
            if task["id"] == task_id
        )
        self.assertEqual(json.loads(saved_task["result_json"])["inputs"], inputs)
        self.assertIn(
            "plan_failed",
            [
                event["event_type"]
                for event in self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
            ],
        )

    @staticmethod
    def policy_plan():
        return {
            "goal": "Review the current procurement policy.",
            "steps": [{
                "step": 1,
                "action": "Read procurement policy",
                "capability": "read_company_policy",
                "requires_approval": False,
                "expected_result": "Policy content is available.",
            }],
        }

    def test_negated_vendor_registration_objective_stays_policy_review(self):
        objective = (
            "Review the current procurement policy and vendor safeguards. "
            "Do not register a vendor."
        )
        planner_plan = {
            "goal": "Review the policy and safeguards without registering a vendor.",
            "success_criteria": ["Review the policy safeguards."],
            "steps": [
                {
                    "step": 1,
                    "action": "Read the procurement policy",
                    "capability": "read_company_policy",
                    "requires_approval": False,
                    "expected_result": "The policy contents are available.",
                },
                {
                    "step": 2,
                    "action": "Check the vendor safeguards",
                    "capability": "check_policy",
                    "requires_approval": False,
                    "expected_result": "The safeguards are reviewed.",
                },
            ],
            "risks": [],
        }

        validated_plan, workflow_name = workflow._validated_plan(objective, planner_plan)

        self.assertEqual(workflow_name, "procurement_policy_review")
        self.assertEqual(
            [step["capability"] for step in validated_plan["steps"]],
            ["read_company_policy"],
        )

    def read_task(self, task_id):
        with Session(self.test_engine) as session:
            return session.get(Task, task_id)

    def read_vendors(self):
        with Session(self.test_engine) as session:
            return session.exec(select(Vendor)).all()

    def start_vendor_task(self, registration_number="TEST-100", include_details=True):
        task_id = self.create_task("Register a vendor under the procurement policy.")
        payload = {}
        if include_details:
            payload = {
                "vendor_name": "Test Vendor",
                "vendor_website": "https://vendor.example",
                "registration_number": registration_number,
            }
        response = self.client.post(f"/api/tasks/{task_id}/run", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return task_id

    def register_sensitive_action(self, capability):
        handler = Mock(return_value={"success": True, "updated": True})
        tool_registry.register_tool(
            capability,
            handler,
            description="Update the specified business record.",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "minLength": 1},
                    "amount": {"type": "number", "minimum": 0},
                },
                "required": ["target", "amount"],
                "additionalProperties": False,
            },
            outputs=["updated"],
            permissions=["procurement:write"],
            requires_approval=True,
            side_effect=True,
            risk={
                "impact": "high",
                "reversibility": "irreversible",
                "data_sensitivity": "business",
                "external_consequences": "material",
                "autonomy_limits": {"approval_per_action": True},
            },
            expected_consequences="Changes the targeted business record by the proposed amount.",
            verifier=lambda _context, result: {"verified": result.get("updated") is True},
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)
        return handler

    def test_policy_read_is_independently_verified(self):
        with patch("app.workflow.create_plan", return_value=self.policy_plan()):
            task_id = self.create_task("Review the procurement policy.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})
        self.assertEqual(response.status_code, 202, response.text)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertTrue(state["verification"]["matches"])

    def test_missing_information_pauses_and_resumes_without_replaying_policy(self):
        calls = []
        original_execute = workflow.execute_tool

        def capture_execute(capability, context=None):
            calls.append(capability)
            return original_execute(capability, context)

        with patch("app.workflow.execute_tool", side_effect=capture_execute):
            task_id = self.start_vendor_task(include_details=False)
            first_state = json.loads(self.read_task(task_id).result_json)
            self.assertEqual(self.read_task(task_id).status, "waiting_for_input")
            self.assertEqual(first_state["missing_information"], ["vendor_name", "vendor_website"])

            response = self.client.post(
                f"/api/tasks/{task_id}/run",
                json={
                    "vendor_name": "Resumed Vendor",
                    "vendor_website": "https://resumed.example",
                    "registration_number": "RESUME-1",
                },
            )
            self.assertEqual(response.status_code, 202, response.text)

        self.assertEqual(calls.count("read_company_policy"), 1)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_approval")
        state = json.loads(self.read_task(task_id).result_json)
        self.assertEqual(state["completed_steps"], [1, 2, 3])

    def test_transient_read_retries_are_bounded_and_persisted(self):
        observed = []
        responses = [
            {"success": False, "error": "busy", "failure_type": "transient_io_error"},
            {"success": False, "error": "busy", "failure_type": "transient_io_error"},
            {"success": True, "policy_text": "policy"},
        ]
        task_id = self.create_task("Review the procurement policy.")
        result = workflow._execute_observed_tool(
            task_id,
            "read_company_policy",
            executor=lambda capability, context: observed.append(capability) or responses.pop(0),
            step_number=1,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(observed, ["read_company_policy"] * 3)
        state = json.loads(self.read_task(task_id).result_json)
        self.assertEqual(len(state["recovery_attempts"]), 2)
        self.assertEqual(
            len(self.client.get(f"/api/tasks/{task_id}/events").json()["events"]),
            2,
        )

    def test_transient_read_stops_after_two_retries(self):
        task_id = self.create_task("Review the procurement policy.")
        result = workflow._execute_observed_tool(
            task_id,
            "read_company_policy",
            executor=lambda capability, context: {
                "success": False,
                "error": "still busy",
                "failure_type": "transient_io_error",
            },
            step_number=1,
        )
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(result["next_action"], "stop")
        state = json.loads(self.read_task(task_id).result_json)
        self.assertEqual(len(state["recovery_attempts"]), 2)

    def test_retry_budget_survives_restart_from_persisted_step_state(self):
        task_id = self.create_task("Review the procurement policy.")
        task = self.read_task(task_id)
        state = workflow._new_state()
        state["recovery_attempts"] = [
            {
                "capability": "read_company_policy",
                "step": 1,
                "attempt": 1,
                "max_attempts": 3,
                "failure_type": "transient_io_error",
                "error": "temporary",
            },
            {
                "capability": "read_company_policy",
                "step": 1,
                "attempt": 2,
                "max_attempts": 3,
                "failure_type": "transient_io_error",
                "error": "temporary",
            },
        ]
        with Session(self.test_engine) as session:
            saved_task = session.get(Task, task_id)
            workflow._save_state(saved_task, state)
            session.add(saved_task)
            session.commit()

        calls = []
        result = workflow._execute_observed_tool(
            task_id,
            "read_company_policy",
            executor=lambda capability, context: calls.append(capability) or {
                "success": False,
                "failure_type": "transient_io_error",
                "error": "still interrupted",
            },
            step_number=1,
        )
        self.assertEqual(calls, ["read_company_policy"])
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(result["next_action"], "stop")

    def test_real_workflow_retries_then_verifies(self):
        original_execute = workflow.execute_tool
        attempts = []

        def fail_twice_then_execute(capability, context=None):
            if capability == "read_company_policy":
                attempts.append(capability)
                if len(attempts) < 3:
                    return {
                        "success": False,
                        "error": "temporary interruption",
                        "failure_type": "transient_io_error",
                    }
            return original_execute(capability, context)

        with patch("app.workflow.create_plan", return_value=self.policy_plan()), patch(
            "app.workflow.execute_tool", side_effect=fail_twice_then_execute
        ):
            task_id = self.create_task("Review the procurement policy.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.read_task(task_id).status, "completed")
        self.assertEqual(len(attempts), 3)
        event_types = [
            event["event_type"]
            for event in self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
        ]
        self.assertEqual(event_types.count("recovery_attempt"), 2)

    def test_non_transient_and_unsafe_tool_failures_are_not_retried(self):
        for capability, failure_type in (
            ("read_company_policy", "policy_file_missing"),
            ("register_vendor", "transient_database_error"),
        ):
            with self.subTest(capability=capability):
                calls = []
                task_id = self.create_task("Register a vendor under the procurement policy.")
                result = workflow._execute_observed_tool(
                    task_id,
                    capability,
                    executor=lambda name, context: calls.append(name) or {
                        "success": False,
                        "error": "controlled failure",
                        "failure_type": failure_type,
                    },
                )
                self.assertEqual(calls, [capability])
                self.assertEqual(result["next_action"], "stop")

    def test_observation_decision_requests_input_and_preserves_approval_gate(self):
        self.assertEqual(workflow._decide_after_observation(
            "check_policy",
            {"success": False, "missing_information": ["vendor_name"]},
            1,
        ), "request_input")
        self.assertEqual(workflow._decide_after_observation(
            "register_vendor",
            {"success": False, "requires_approval": True},
            1,
        ), "await_approval")
        self.assertEqual(workflow._decide_after_observation(
            "read_company_policy",
            {"success": False, "failure_type": "transient_io_error"},
            3,
        ), "stop")

    def test_vendor_is_registered_and_verified_only_after_approval(self):
        task_id = self.start_vendor_task("APPROVE-1")
        task = self.read_task(task_id)
        self.assertEqual(task.status, "waiting_for_approval")
        state = json.loads(task.result_json)
        approval_id = state["approval_id"]
        approval = self.client.get("/api/approvals?status=pending").json()[0]
        self.assertEqual(approval["action"]["name"], "Test Vendor")
        self.assertEqual(approval["action"]["registration_number"], "APPROVE-1")
        self.assertEqual(self.read_vendors(), [])

        decision = self.client.post(
            f"/api/approvals/{approval_id}/decide?decision=approve"
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertTrue(decision.json()["verified"])
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertEqual(state["verification"]["type"], "vendor_record")
        self.assertEqual(len(self.read_vendors()), 1)

    def test_sensitive_registered_action_requires_approval_and_resumes(self):
        capability = "test_sensitive_record_update"
        handler = self.register_sensitive_action(capability)
        planner_plan = {"goal": "Update a business record.", "steps": [{
            "step": 1,
            "action": "Update record 42 by 25",
            "capability": capability,
            "depends_on": [],
            "inputs": {"target": "record-42", "amount": 25},
            "requires_approval": False,
            "expected_result": "The approved record update is applied.",
        }]}
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Update record 42 by 25.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_approval")
        handler.assert_not_called()
        approval = next(
            item for item in self.client.get("/api/approvals?status=pending").json()
            if item["task_id"] == task_id
        )
        self.assertEqual(approval["action"]["target"], "record-42")
        self.assertEqual(approval["action"]["parameters"], {"target": "record-42", "amount": 25})
        self.assertIn("expected_consequences", approval["action"])
        self.assertEqual(approval["action"]["risk"]["reversibility"], "irreversible")

        decision = self.client.post(f"/api/approvals/{approval['id']}/decide?decision=approve")

        self.assertEqual(decision.status_code, 200, decision.text)
        handler.assert_called_once_with({"target": "record-42", "amount": 25})
        self.assertEqual(self.read_task(task_id).status, "completed")
        event_types = [
            event["event_type"]
            for event in self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
        ]
        self.assertIn("approval_approved", event_types)
        self.assertIn("tool_invocation", event_types)
        self.assertIn("tool_outcome", event_types)

    def test_changed_approval_display_proposal_is_rejected(self):
        capability = "test_sensitive_changed_proposal"
        handler = self.register_sensitive_action(capability)
        planner_plan = {"goal": "Update a business record.", "steps": [{
            "step": 1,
            "action": "Update record 42 by 25",
            "capability": capability,
            "depends_on": [],
            "inputs": {"target": "record-42", "amount": 25},
            "requires_approval": False,
            "expected_result": "The approved record update is applied.",
        }]}
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Update record 42 by 25.")
            self.client.post(f"/api/tasks/{task_id}/run", json={})
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
        with Session(self.test_engine) as session:
            approval = session.get(workflow.Approval, approval_id)
            proposal = json.loads(approval.action_json)
            proposal["parameters"]["amount"] = 250
            approval.action_json = json.dumps(proposal)
            session.add(approval)
            session.commit()

        response = self.client.post(f"/api/approvals/{approval_id}/decide?decision=approve")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_approval")
        handler.assert_not_called()

    def test_cancelled_vendor_approval_cannot_be_executed(self):
        task_id = self.start_vendor_task("CANCEL-APPROVAL-1")
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]

        cancelled = self.client.post(f"/api/tasks/{task_id}/cancel")
        decision = self.client.post(f"/api/approvals/{approval_id}/decide?decision=approve")

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(decision.status_code, 409)
        self.assertEqual(self.read_task(task_id).status, "cancelled")
        self.assertEqual(self.read_vendors(), [])
        with Session(self.test_engine) as session:
            self.assertEqual(session.get(workflow.Approval, approval_id).status, "cancelled")

    def test_task_owner_is_required_for_api_execution_and_audit_access(self):
        with patch.dict(os.environ, {"TRUSTED_USER_HEADER": "X-Authenticated-User"}):
            created = self.client.post(
                "/api/tasks",
                json={"objective": "Review the procurement policy."},
                headers={"X-Authenticated-User": "alice"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            task_id = created.json()["id"]
            foreign_events = self.client.get(
                f"/api/tasks/{task_id}/events",
                headers={"X-Authenticated-User": "bob"},
            )
            foreign_cancel = self.client.post(
                f"/api/tasks/{task_id}/cancel",
                headers={"X-Authenticated-User": "bob"},
            )

        self.assertEqual(foreign_events.status_code, 404)
        self.assertEqual(foreign_cancel.status_code, 404)

    def test_dispatcher_blocks_new_actions_after_task_cancellation(self):
        capability = "test_stopped_task_action"
        handler = Mock(return_value={"success": True, "value": "ran"})
        tool_registry.register_tool(
            capability,
            handler,
            description="Run a task-scoped operation.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            permissions=["business:read"],
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        task_id = self.create_task("Run a task-scoped operation.")
        self.client.post(f"/api/tasks/{task_id}/cancel")

        result = execute_tool(capability, {}, task_id=task_id)

        self.assertEqual(result["failure_type"], "task_cancelled")
        handler.assert_not_called()

    def test_expired_approval_is_not_executed(self):
        task_id = self.start_vendor_task("EXPIRED-APPROVAL-1")
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
        with Session(self.test_engine) as session:
            approval = session.get(workflow.Approval, approval_id)
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.add(approval)
            session.commit()

        response = self.client.post(f"/api/approvals/{approval_id}/decide?decision=approve")

        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.read_task(task_id).status, "approval_expired")
        self.assertEqual(self.read_vendors(), [])

    def test_role_without_approval_scope_cannot_approve_sensitive_action(self):
        with patch.dict(os.environ, {
            "TOOL_ROLE_GRANTS": '{"reader":["procurement:read"]}',
            "TRUSTED_USER_ROLES": '{"default":"reader"}',
        }, clear=False):
            task_id = self.start_vendor_task("ROLE-GATE-1")
            approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
            response = self.client.post(f"/api/approvals/{approval_id}/decide?decision=approve")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.read_vendors(), [])

    def test_dispatcher_cannot_use_a_pending_vendor_approval(self):
        task = Task(
            objective="Register a vendor under the procurement policy.",
            status="waiting_for_approval",
            plan_json=json.dumps({"steps": [{"step": 4, "capability": "register_vendor"}]}),
        )
        with Session(self.test_engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            approval = workflow.Approval(
                task_id=task.id,
                action_json=json.dumps({
                    "name": "Direct Call Vendor",
                    "website": "https://direct-call.example",
                    "registration_number": "DIRECT-CALL-1",
                }),
                status="pending",
            )
            session.add(approval)
            session.commit()
            session.refresh(approval)
            approval_id = approval.id
            state = workflow._new_state()
            state["workflow"] = "procurement_vendor_registration"
            state["approval_id"] = approval_id
            workflow._save_state(task, state)
            session.add(task)
            session.commit()

        result = execute_tool("register_vendor", {"approval_id": approval_id})

        self.assertFalse(result["success"])
        self.assertEqual(result["failure_type"], "approval_required")
        self.assertEqual(self.read_vendors(), [])

    def test_rejection_never_creates_vendor(self):
        task_id = self.start_vendor_task("REJECT-1")
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
        response = self.client.post(
            f"/api/approvals/{approval_id}/decide?decision=reject"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read_task(task_id).status, "rejected")
        self.assertEqual(self.read_vendors(), [])

    def test_duplicate_registration_number_stops_before_approval(self):
        with Session(self.test_engine) as session:
            session.add(Vendor(name="Existing", website="https://existing.example", registration_number="DUP-1"))
            session.commit()
        task_id = self.start_vendor_task("DUP-1")
        self.assertEqual(self.read_task(task_id).status, "partially_completed")
        state = json.loads(self.read_task(task_id).result_json)
        self.assertEqual(state["error"], "Registration number already exists.")
        self.assertEqual(len(self.read_vendors()), 1)

    def test_registration_rechecks_duplicate_after_approval(self):
        task_id = self.start_vendor_task("LATE-DUP-1")
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
        with Session(self.test_engine) as session:
            session.add(Vendor(
                name="Added while approval was pending",
                website="https://racing.example",
                registration_number="LATE-DUP-1",
            ))
            session.commit()

        decision = self.client.post(
            f"/api/approvals/{approval_id}/decide?decision=approve"
        )
        self.assertEqual(decision.status_code, 409)
        self.assertEqual(self.read_task(task_id).status, "partially_completed")
        self.assertEqual(len(self.read_vendors()), 1)
        self.assertEqual(self.client.get("/api/approvals?status=pending").json(), [])

    def test_processing_task_recovers_from_saved_plan_after_reload(self):
        task = Task(
            objective="Review the procurement policy.",
            status="processing",
            plan_json=json.dumps({
                "goal": "Review the policy.",
                "success_criteria": ["Compare the saved policy result to source."],
                "steps": [{
                    "step": 1,
                    "action": "Read the current procurement policy",
                    "capability": "read_company_policy",
                    "requires_approval": False,
                    "expected_result": "Policy file is read.",
                }],
                "risks": [],
            }),
            result_json=json.dumps({
                "workflow": "procurement_policy_review",
                "vendor": {},
                "completed_steps": [],
                "step_results": [],
                "current_step": None,
                "missing_information": [],
                "approval_id": None,
                "verification": None,
                "final_summary": None,
            }),
        )
        with Session(self.test_engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read_task(task_id).status, "completed")

    def test_restart_resume_does_not_repeat_completed_policy_read(self):
        objective = "Register a vendor under the procurement policy."
        policy_evidence = tool_registry.read_company_policy()
        plan = {
            "goal": "Register a vendor safely.",
            "success_criteria": ["Get required vendor details and approval."],
            "steps": [
                {"step": 1, "action": "Read procurement policy", "capability": "read_company_policy", "requires_approval": False, "expected_result": "Policy is read."},
                {"step": 2, "action": "Validate vendor", "capability": "check_policy", "requires_approval": False, "expected_result": "Vendor details comply."},
                {"step": 3, "action": "Check duplicate", "capability": "check_vendor_duplicate", "requires_approval": False, "expected_result": "No duplicate exists."},
                {"step": 4, "action": "Register vendor", "capability": "register_vendor", "requires_approval": True, "expected_result": "Approval was granted."},
            ],
            "risks": [],
        }
        task = Task(
            objective=objective,
            status="processing",
            plan_json=json.dumps(plan),
            result_json=json.dumps({
                "workflow": "procurement_vendor_registration",
                "vendor": {},
                "completed_steps": [1],
                "step_results": [{
                    "step": 1,
                    "capability": "read_company_policy",
                    "result": policy_evidence,
                    "success": True,
                }],
                "current_step": 1,
                "missing_information": [],
                "approval_id": None,
                "verification": None,
                "final_summary": None,
            }),
        )
        with Session(self.test_engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        called_capabilities = []
        original_execute = workflow.execute_tool

        def record_execute(capability, context=None):
            called_capabilities.append(capability)
            return original_execute(capability, context)

        with patch("app.workflow.execute_tool", side_effect=record_execute):
            response = self.client.get("/api/tasks")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_input")
        self.assertNotIn("read_company_policy", called_capabilities)
        state = json.loads(self.read_task(task_id).result_json)
        self.assertIn(1, state["completed_steps"])

    def test_unsupported_planner_capability_fails_safely(self):
        with patch("app.workflow.create_plan", return_value={
            "goal": "Run a shell command",
            "steps": [{"step": 1, "capability": "run_shell", "action": "Execute shell"}],
        }):
            task_id = self.create_task("Do an unsupported action.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.read_task(task_id).status, "unsupported")
        state = json.loads(self.read_task(task_id).result_json)
        self.assertFalse(json.loads(self.read_task(task_id).plan_json)["supported"])
        self.assertIn("run_shell", state["unavailable_capabilities"])
        self.assertEqual(self.read_vendors(), [])

    def test_registered_multi_step_plan_runs_and_verifies_each_tool(self):
        planner_plan = {
            "goal": "Check the vendor number against the current local vendor records.",
            "success_criteria": ["The current vendor table has been checked."],
            "steps": [
                {
                    "step": 1,
                    "action": "Read current policy context",
                    "capability": "read_company_policy",
                    "depends_on": [],
                    "inputs": {},
                    "requires_approval": False,
                    "expected_result": "Policy context is available.",
                },
                {
                    "step": 2,
                    "action": "Check the vendor number for an existing record",
                    "capability": "check_vendor_duplicate",
                    "depends_on": [1],
                    "inputs": {
                        "registration_number": "MULTI-STEP-1",
                        "policy_reference": {"$ref": {"step": 1, "field": "policy_text"}},
                    },
                    "requires_approval": False,
                    "expected_result": "The local vendor table is checked.",
                },
            ],
            "risks": [],
        }
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Check vendor number MULTI-STEP-1 for a duplicate.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202, response.text)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        plan = json.loads(task.plan_json)
        self.assertEqual(plan["steps"][1]["depends_on"], [1])
        state = json.loads(task.result_json)
        self.assertEqual(state["completed_steps"], [1, 2])
        self.assertTrue(state["verification"]["matches"])
        self.assertEqual(len(state["verification"]["steps"]), 2)
        self.assertIn("Sample Procurement Policy", state["step_results"][1]["inputs"]["policy_reference"])

    def test_registered_tool_can_be_added_with_its_own_verifier(self):
        capability = "test_format_business_note"
        tool_registry.register_tool(
            capability,
            lambda context: {"success": True, "summary": f"Prepared: {context['subject']}"},
            description="Prepare a short note for a business subject.",
            inputs={"subject": "The business subject to summarize."},
            outputs=["summary"],
            verification="Recreate the note from the supplied subject.",
            verifier=lambda context, result: {
                "verified": result.get("summary") == f"Prepared: {context.get('subject')}"
            },
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)

        planner_plan = {
            "goal": "Prepare a note about the quarter close.",
            "steps": [{
                "step": 1,
                "action": "Prepare the note",
                "capability": capability,
                "depends_on": [],
                "inputs": {"subject": "quarter close"},
                "requires_approval": False,
                "expected_result": "A note is returned.",
                "success_criteria": ["The note identifies the requested subject."],
            }],
        }
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Prepare a note about the quarter close.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202, response.text)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertEqual(state["step_results"][0]["result"]["summary"], "Prepared: quarter close")
        self.assertTrue(state["verification"]["matches"])

    def test_custom_approval_required_tool_pauses_for_human_approval(self):
        capability = "test_update_forecast"
        handler = Mock(return_value={"success": True, "updated": True})
        tool_registry.register_tool(
            capability,
            handler,
            description="Update a business forecast.",
            requires_approval=True,
            side_effect=True,
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)

        planner_plan = {
            "goal": "Update the quarterly forecast.",
            "steps": [{
                "step": 1,
                "action": "Update the forecast",
                "capability": capability,
                "depends_on": [],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "The forecast is updated.",
            }],
        }
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Update the quarterly forecast.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_approval")
        handler.assert_not_called()
        approval = next(
            item for item in self.client.get("/api/approvals?status=pending").json()
            if item["task_id"] == task_id
        )
        self.assertEqual(approval["action"]["capability"], capability)

    def test_generic_missing_inputs_resume_the_same_registered_tool_plan(self):
        planner_plan = {
            "goal": "Validate vendor details against procurement safeguards.",
            "steps": [
                {
                    "step": 1,
                    "action": "Read the procurement policy",
                    "capability": "read_company_policy",
                    "depends_on": [],
                    "inputs": {},
                    "requires_approval": False,
                    "expected_result": "Policy evidence is available.",
                },
                {
                    "step": 2,
                    "action": "Validate the supplied vendor details",
                    "capability": "check_policy",
                    "depends_on": [1],
                    "inputs": {},
                    "requires_approval": False,
                    "expected_result": "The details meet the policy safeguards.",
                },
            ],
        }
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Validate supplier details against procurement safeguards.")
            initial = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(initial.status_code, 202, initial.text)
        self.assertEqual(self.read_task(task_id).status, "waiting_for_input")
        first_state = json.loads(self.read_task(task_id).result_json)
        self.assertEqual(first_state["missing_information"], ["vendor_name", "vendor_website"])

        resumed = self.client.post(
            f"/api/tasks/{task_id}/run",
            json={"inputs": {
                "vendor_name": "Clarified Supplier",
                "vendor_website": "https://supplier.example",
            }},
        )
        self.assertEqual(resumed.status_code, 202, resumed.text)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertEqual(state["completed_steps"], [1, 2])
        self.assertEqual(state["step_results"][-1]["inputs"]["vendor_name"], "Clarified Supplier")

    def test_empty_clarification_plan_is_replanned_with_supplied_context(self):
        initial_plan = {
            "goal": "Review the policy for a specific business unit.",
            "supported": True,
            "steps": [],
            "missing_information": ["business_unit"],
            "risks": [],
        }
        completed_plan = {
            "goal": "Review the procurement policy for operations.",
            "steps": [{
                "step": 1,
                "action": "Read current procurement policy",
                "capability": "read_company_policy",
                "depends_on": [],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "The policy text is available.",
            }],
        }
        planning_contexts = []

        def plan_with_context(objective, provided_information=None):
            planning_contexts.append(provided_information or {})
            return initial_plan if len(planning_contexts) == 1 else completed_plan

        with patch("app.workflow.create_plan", side_effect=plan_with_context):
            task_id = self.create_task("Review procurement policy for a business unit.")
            initial = self.client.post(f"/api/tasks/{task_id}/run", json={})
            self.assertEqual(initial.status_code, 202, initial.text)
            self.assertEqual(self.read_task(task_id).status, "waiting_for_input")

            resumed = self.client.post(
                f"/api/tasks/{task_id}/run",
                json={"inputs": {"business_unit": "operations"}},
            )

        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual(planning_contexts, [{}, {"business_unit": "operations"}])
        self.assertEqual(self.read_task(task_id).status, "completed")

    def test_plan_rejects_forward_or_undeclared_input_references(self):
        with self.assertRaisesRegex(ValueError, "dependencies"):
            workflow._validated_plan("Check records", {
                "goal": "Check records",
                "steps": [{
                    "action": "Read policy",
                    "capability": "read_company_policy",
                    "depends_on": [2],
                    "inputs": {},
                    "expected_result": "Policy is read.",
                }],
            })

    def test_tool_failure_does_not_complete_task(self):
        with patch("app.workflow.execute_tool", return_value={
            "success": False,
            "error": "Controlled test failure.",
            "failure_type": "test_failure",
        }), patch("app.workflow.create_plan", return_value=self.policy_plan()):
            task_id = self.create_task("Review the procurement policy.")
            self.client.post(f"/api/tasks/{task_id}/run", json={})
        self.assertEqual(self.read_task(task_id).status, "failed")

    def test_policy_verification_mismatch_is_not_success(self):
        with patch("app.workflow.read_company_policy", return_value={
            "success": True,
            "policy_text": "Changed policy source",
            "source": "controlled-test-source",
        }), patch("app.workflow.create_plan", return_value=self.policy_plan()):
            task_id = self.create_task("Review the procurement policy.")
            self.client.post(f"/api/tasks/{task_id}/run", json={})
        task = self.read_task(task_id)
        self.assertEqual(task.status, "failed")
        self.assertFalse(json.loads(task.result_json)["verification"]["matches"])

    def test_repeated_run_and_approval_cannot_duplicate_vendor(self):
        task_id = self.start_vendor_task("ONCE-1")
        approval_id = json.loads(self.read_task(task_id).result_json)["approval_id"]
        approved = self.client.post(
            f"/api/approvals/{approval_id}/decide?decision=approve"
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        repeated_decision = self.client.post(
            f"/api/approvals/{approval_id}/decide?decision=approve"
        )
        repeated_run = self.client.post(f"/api/tasks/{task_id}/run", json={})
        self.assertEqual(repeated_decision.status_code, 409)
        self.assertEqual(repeated_run.status_code, 409)
        self.assertEqual(len(self.read_vendors()), 1)

    def test_validated_plan_rejects_circular_dependencies(self):
        with self.assertRaisesRegex(ValueError, "circular"):
            workflow._validated_plan("Check independent records", {
                "goal": "Check independent records",
                "steps": [
                    {
                        "step": 1,
                        "action": "Read policy",
                        "capability": "read_company_policy",
                        "depends_on": [2],
                        "expected_result": "Policy is read.",
                    },
                    {
                        "step": 2,
                        "action": "Check a vendor number",
                        "capability": "check_vendor_duplicate",
                        "depends_on": [1],
                        "expected_result": "The number is checked.",
                    },
                ],
            })

    def test_independent_read_only_steps_execute_in_parallel_and_are_verified(self):
        capability = "test_parallel_read"
        barrier = threading.Barrier(2)
        tool_registry.register_tool(
            capability,
            lambda context: barrier.wait(timeout=2) is not None
            and {"success": True, "value": context["value"]},
            description="Read an independent test value.",
            inputs={"value": "The test value."},
            outputs=["value"],
            verification="Compare the observed value with the requested value.",
            verifier=lambda context, result: {
                "verified": result.get("value") == context.get("value")
            },
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)
        planner_plan = {
            "goal": "Read two independent values.",
            "steps": [
                {
                    "action": "Read first value",
                    "capability": capability,
                    "depends_on": [],
                    "inputs": {"value": "first"},
                    "expected_result": "First value is read.",
                },
                {
                    "action": "Read second value",
                    "capability": capability,
                    "depends_on": [],
                    "inputs": {"value": "second"},
                    "expected_result": "Second value is read.",
                },
            ],
        }
        with patch("app.workflow.create_plan", return_value=planner_plan):
            task_id = self.create_task("Read two independent test values.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertEqual(state["completed_steps"], [1, 2])
        self.assertTrue(all(item["started_at"] and item["finished_at"] for item in state["step_results"]))
        self.assertTrue(all(item["verification_evidence"]["matches"] for item in state["step_results"]))

    def test_execution_observation_replans_without_replaying_completed_steps(self):
        capability = "test_observation_replan"
        calls = []

        def handler(context):
            calls.append(context["phase"])
            result = {"success": True, "phase": context["phase"]}
            if context["phase"] == "observe":
                result["replan_required"] = True
            return result

        tool_registry.register_tool(
            capability,
            handler,
            description="Observe a replan test phase.",
            inputs={"phase": "Execution phase."},
            outputs=["phase", "replan_required"],
            verification="Check the phase returned by the tool.",
            verifier=lambda context, result: {"verified": result.get("phase") == context.get("phase")},
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)
        initial_plan = {"goal": "Observe and follow up.", "steps": [{
            "action": "Observe execution result",
            "capability": capability,
            "depends_on": [],
            "inputs": {"phase": "observe"},
            "expected_result": "The result determines the next action.",
        }]}
        replanned_plan = {"goal": "Observe and follow up.", "steps": [{
            "action": "Complete follow-up",
            "capability": capability,
            "depends_on": [],
            "inputs": {"phase": "follow_up"},
            "expected_result": "The follow-up is completed.",
        }]}
        with patch("app.workflow.create_plan", side_effect=[initial_plan, replanned_plan]) as planner:
            task_id = self.create_task("Observe the result and choose the next action.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(calls, ["observe", "follow_up"])
        task = self.read_task(task_id)
        self.assertEqual(task.status, "completed")
        state = json.loads(task.result_json)
        self.assertEqual(state["completed_steps"], [1, 2])
        events = self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
        self.assertIn("plan_replanned", [event["event_type"] for event in events])

    def test_replanning_stops_at_configured_budget(self):
        capability = "test_bounded_replan"
        tool_registry.register_tool(
            capability,
            lambda context: {"success": True, "phase": context["phase"], "replan_required": True},
            description="Return an observation that requests another plan.",
            inputs={"phase": "Execution phase."},
            outputs=["phase", "replan_required"],
            verification="Check the recorded phase.",
            verifier=lambda context, result: {"verified": result.get("phase") == context.get("phase")},
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)

        def make_plan(_objective, context=None):
            stage = (context or {}).get("execution_observation", {}).get("step", 0) + 1
            return {"goal": "Keep replanning", "steps": [{
                "action": f"Stage {stage}",
                "capability": capability,
                "depends_on": [],
                "inputs": {"phase": str(stage)},
                "expected_result": "Observe the next stage.",
            }]}

        with patch("app.workflow.create_plan", side_effect=make_plan) as planner:
            task_id = self.create_task("Repeatedly observe and replan.")
            response = self.client.post(f"/api/tasks/{task_id}/run", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(planner.call_count, 3)
        task = self.read_task(task_id)
        self.assertEqual(task.status, "partially_completed")
        state = json.loads(task.result_json)
        self.assertEqual(len(state["replans"]), 2)
        self.assertEqual(state["completed_steps"], [1, 2, 3])

    def test_tool_timeout_is_reported_without_hanging_the_runner(self):
        task_id = self.create_task("Run a bounded tool check.")
        release = threading.Event()

        def slow_executor(_capability, _context):
            release.wait(timeout=2)
            return {"success": True}

        try:
            result = workflow._execute_observed_tool(
                task_id,
                "read_company_policy",
                executor=slow_executor,
                timeout_seconds=0.01,
            )
        finally:
            release.set()
        self.assertFalse(result["success"])
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["failure_type"], "timeout")

    def test_interrupted_side_effect_is_never_retried_blindly(self):
        capability = "test_interrupted_write"
        handler = Mock(return_value={"success": True, "written": True})
        tool_registry.register_tool(
            capability,
            handler,
            description="Write one test record.",
            inputs={},
            outputs=["written"],
            side_effect=True,
            permission_gated_side_effect=True,
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
        task = Task(
            objective="Write one test record.",
            status="running",
            plan_json=json.dumps({"goal": "Write one test record.", "steps": [{
                "step": 1,
                "action": "Write one test record",
                "capability": capability,
                "depends_on": [],
                "inputs": {},
                "requires_approval": False,
                "expected_result": "The record is written.",
            }]}),
            result_json=json.dumps({
                **workflow._new_state(),
                "workflow": "registered_tool_plan",
                "step_results": [{
                    "step": 1,
                    "capability": capability,
                    "status": "running",
                    "inputs": {},
                    "success": False,
                }],
            }),
        )
        with Session(self.test_engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        workflow.run_task_workflow(task_id)
        handler.assert_not_called()
        saved = self.read_task(task_id)
        self.assertEqual(saved.status, "failed")
        result = json.loads(saved.result_json)["step_results"][-1]["result"]
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["failure_type"], "interrupted_outcome_unknown")

    def test_cancel_records_event_and_preserves_completed_evidence(self):
        task_id = self.create_task("Review the procurement policy.")
        with Session(self.test_engine) as session:
            task = session.get(Task, task_id)
            state = workflow._new_state()
            state["completed_steps"] = [1]
            workflow._save_state(task, state)
            task.status = "running"
            session.add(task)
            session.commit()

        response = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        saved = self.read_task(task_id)
        self.assertEqual(saved.status, "cancelled")
        self.assertEqual(json.loads(saved.result_json)["completed_steps"], [1])
        events = self.client.get(f"/api/tasks/{task_id}/events").json()["events"]
        self.assertEqual(events[-1]["event_type"], "workflow_cancelled")


if __name__ == "__main__":
    unittest.main()
