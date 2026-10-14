import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select

from app import main
from app.api import memory as memory_api
from app.core.database import create_db_and_tables
from app.services.memory import record_verified_procedure, retrieve_context, retrieve_context_safely
from app.core.models import Approval, ExecutionEvent, Memory, Task
from app.services.agent import _ledgerly_invoice_fallback, understand_and_plan
from app.services.procedures import record_verified_procedure
from app.workflows import engine as workflow
from app.workflows.support import execution
from app.workflows.support.plan import _validated_plan


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory-test.db"
        self.test_engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.test_engine)

    def tearDown(self):
        self.test_engine.dispose()
        self.temp_dir.cleanup()

    def test_persistence_relevance_and_owner_isolation(self):
        with Session(self.test_engine) as session:
            session.add_all([
                Memory(
                    owner_id="alice",
                    category="preference",
                    kind="user_instruction",
                    content="Use Markdown for procurement reports.",
                    verification_status="verified",
                ),
                Memory(
                    owner_id="alice",
                    category="business_context",
                    kind="fact",
                    content="Project Northstar uses the approved procurement policy.",
                    source="approved_document",
                    source_ref="policy-2026",
                    verification_status="unverified",
                ),
                Memory(
                    owner_id="bob",
                    category="business_context",
                    kind="fact",
                    content="Project Northstar is Bob's confidential initiative.",
                    verification_status="verified",
                ),
            ])
            session.add(Task(
                owner_id="alice",
                objective="Review procurement vendor registration.",
                status="failed",
                result_json=json.dumps({"error": "Prior attempt failed safely."}),
            ))
            session.commit()

        with Session(self.test_engine) as session:
            context = retrieve_context(session, "alice", "procurement report vendor registration")
            self.assertEqual(len(context["memories"]), 2)
            self.assertEqual(context["memories"][0]["category"], "preference")
            self.assertEqual(context["memories"][0]["verification_status"], "verified")
            self.assertEqual(len(context["task_history"]), 1)
            self.assertEqual(context["task_history"][0]["status"], "failed")
            self.assertNotIn("confidential", json.dumps(context))

        with Session(self.test_engine) as session:
            persisted = session.exec(select(Memory).where(Memory.owner_id == "alice")).all()
            self.assertEqual(len(persisted), 2)
            self.assertTrue(all(item.created_at for item in persisted))

    def test_verified_procedure_is_only_recorded_after_matching_verification(self):
        task = Task(
            id=72,
            owner_id="alice",
            objective="Review the approved procurement policy.",
        )
        state = {"step_results": [{
            "capability": "read_company_policy",
            "action": "Read current procurement policy",
            "success": True,
        }]}
        with Session(self.test_engine) as session:
            self.assertIsNone(record_verified_procedure(
                session, task, state, {"matches": False}
            ))
            procedure = record_verified_procedure(
                session, task, state, {"matches": True}
            )
            session.commit()
            self.assertEqual(procedure.verification_status, "verified")
            self.assertEqual(procedure.source_ref, "72")

    def test_procedure_versions_history_confidence_and_retrieval(self):
        task = Task(
            id=73,
            owner_id="alice",
            objective="Search invoice reports in authorized files",
        )
        state = {
            "workflow": "registered_tool_plan",
            "inputs": {"query": "invoice"},
            "step_results": [{"capability": "files.search", "success": True}],
        }
        with Session(self.test_engine) as session:
            first = record_verified_procedure(
                session, task, state, {"matches": True, "type": "independent"}
            )
            session.commit()
            self.assertEqual(first.procedure_successes, 1)
            self.assertEqual(first.procedure_confidence, 1.0)

            task.id = 74
            state["step_results"] = [{"capability": "files.list", "success": True}]
            revised = record_verified_procedure(
                session, task, state, {"matches": True, "type": "independent"}
            )
            session.commit()
            self.assertEqual(revised.procedure_version, 2)
            self.assertEqual(json.loads(revised.evidence_json)["versions"][0]["version"], 1)

            task.id = 75
            failed = record_verified_procedure(
                session, task, state, {"matches": False, "type": "independent"}
            )
            session.commit()
            self.assertEqual(failed.procedure_successes, 2)
            self.assertEqual(failed.procedure_failures, 1)
            self.assertAlmostEqual(failed.procedure_confidence, 2 / 3)

            task.id = 76
            state["step_results"] = [{"capability": "files.search", "success": False}]
            failed_before_first_step = record_verified_procedure(
                session, task, state, {"matches": False, "type": "independent"}
            )
            session.commit()
            self.assertEqual(failed_before_first_step.procedure_version, 2)
            self.assertEqual(failed_before_first_step.procedure_failures, 2)
            self.assertAlmostEqual(failed_before_first_step.procedure_confidence, 0.5)

            context = retrieve_context(session, "alice", task.objective)
        self.assertEqual(context["verified_procedures"][0]["procedure_version"], 2)
        self.assertEqual(context["verified_procedures"][0]["failures"], 2)
        self.assertEqual(context["verified_procedures"][0]["evidence"]["outcomes"][-1]["outcome"], "verification_failure")

    def test_user_feedback_is_linked_but_never_promotes_procedures(self):
        with Session(self.test_engine) as session:
            task = Task(
                owner_id="alice",
                objective="Search invoice reports in authorized files",
                status="completed",
                plan_json=json.dumps({"steps": [{"capability": "files.search"}]}),
                result_json=json.dumps({
                    "verification": {"type": "file_digest", "matches": True},
                    "step_results": [{"capability": "files.search", "success": True}],
                }),
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            from app.services.feedback import record_task_feedback

            event = record_task_feedback(
                session,
                task,
                "alice",
                "correction",
                "The report should include invoice dates.",
            )
            details = json.loads(event.details_json)
            self.assertEqual(details["capabilities"], ["files.search"])
            self.assertEqual(details["verification_evidence"]["matches"], True)
            self.assertEqual(details["authority"], "user_feedback_unverified")
            self.assertEqual(session.exec(select(Memory)).all(), [])

            context = retrieve_context(session, "alice", task.objective)
        self.assertEqual(context["task_history"][0]["feedback"][0]["type"], "correction")
        self.assertEqual(context["task_history"][0]["feedback"][0]["authority"], "unverified_user_feedback")

    def test_memory_records_are_isolated_between_user_and_organization_scopes(self):
        with patch.object(main, "engine", self.test_engine), patch.object(
            memory_api, "engine", self.test_engine
        ), patch.dict("os.environ", {
            "TRUSTED_USER_HEADER": "X-Trusted-User",
            "TRUSTED_ORG_HEADER": "X-Trusted-Org",
        }), TestClient(main.app) as client:
            alice = {"X-Trusted-User": "alice", "X-Trusted-Org": "north"}
            bob = {"X-Trusted-User": "bob", "X-Trusted-Org": "north"}
            another_org = {"X-Trusted-User": "carol", "X-Trusted-Org": "south"}
            private = client.post("/api/memories", headers=alice, json={
                "category": "preference", "kind": "user_instruction",
                "content": "Use my private report template.",
            })
            shared = client.post("/api/memories", headers=alice, json={
                "category": "business_context", "kind": "fact",
                "content": "The approved invoice archive is under finance records.",
                "memory_key": "invoice-archive-location", "scope": "organization",
            })
            conflicting_shared = client.post("/api/memories", headers=alice, json={
                "category": "business_context", "kind": "fact",
                "content": "The approved invoice archive is in legal records.",
                "memory_key": "invoice-archive-location", "scope": "organization",
            })
            self.assertEqual(private.status_code, 201, private.text)
            self.assertEqual(shared.status_code, 201, shared.text)
            self.assertEqual(conflicting_shared.json()["verification_status"], "conflicted")
            bob_records = client.get("/api/memories", headers=bob).json()
            carol_records = client.get("/api/memories", headers=another_org).json()
            self.assertEqual(len(bob_records), 2)
            self.assertTrue(all(item["scope"] == "organization" for item in bob_records))
            self.assertTrue(all(item["verification_status"] == "conflicted" for item in bob_records))
            self.assertEqual(carol_records, [])
            self.assertEqual(
                client.get(f"/api/memories/{private.json()['id']}", headers=alice).json()["verification_status"],
                "unverified",
            )
            self.assertEqual(
                client.get(f"/api/memories/{private.json()['id']}", headers=bob).status_code,
                404,
            )
            self.assertEqual(
                client.get(f"/api/memories/{shared.json()['id']}", headers=bob).status_code,
                200,
            )

    def test_task_feedback_api_requires_owner_and_completed_task(self):
        with Session(self.test_engine) as session:
            task = Task(owner_id="alice", objective="Review invoice report", status="completed")
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id
        with patch.object(main, "engine", self.test_engine), patch.dict(
            "os.environ", {"TRUSTED_USER_HEADER": "X-Trusted-User"}
        ), TestClient(main.app) as client:
            payload = {"feedback_type": "usefulness", "content": "The source links were helpful."}
            self.assertEqual(
                client.post(f"/api/tasks/{task_id}/feedback", headers={"X-Trusted-User": "bob"}, json=payload).status_code,
                404,
            )
            saved = client.post(
                f"/api/tasks/{task_id}/feedback",
                headers={"X-Trusted-User": "alice"},
                json=payload,
            )
            self.assertEqual(saved.status_code, 201, saved.text)

    def test_verified_procedure_guidance_reaches_planner_prompt(self):
        class FakeModel:
            def invoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": json.dumps({"steps": []})})()

        fake_model = FakeModel()
        with patch("app.services.agent.get_model", return_value=fake_model):
            understand_and_plan({
                "objective": "Search invoice reports in authorized files",
                "provided_information": {},
                "memory_context": {
                    "verified_procedures": [{
                        "content": "Verified procedure: files.search",
                        "conditions": {"objective_terms": ["invoice", "reports"]},
                        "successes": 3,
                        "failures": 0,
                        "confidence": 1.0,
                        "procedure_version": 2,
                    }],
                    "task_history": [{"feedback": [{"type": "correction", "authority": "unverified_user_feedback"}]}],
                },
                "plan": {},
            })
        self.assertIn("Verified procedure: files.search", fake_model.prompt)
        self.assertIn("preferred starting sequence", fake_model.prompt)
        self.assertIn("unverified evidence", fake_model.prompt)
        self.assertIn("approval requirements", fake_model.prompt)

    def test_planner_requires_complete_record_coverage_for_ranked_website_requests(self):
        class FakeModel:
            def invoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": json.dumps({"steps": []})})()

        fake_model = FakeModel()
        with patch("app.services.agent.get_model", return_value=fake_model):
            understand_and_plan({
                "objective": "Find the five invoices with the largest amounts.",
                "provided_information": {},
                "memory_context": {},
                "plan": {},
            })

        self.assertIn("do not infer the answer from a dashboard", fake_model.prompt)
        self.assertIn("records/list page", fake_model.prompt)
        self.assertIn("Compare the requested numeric/date field", fake_model.prompt)
        self.assertIn("every in-scope record", fake_model.prompt)
        self.assertIn("number of records", fake_model.prompt)
        self.assertIn("compared", fake_model.prompt)

    def test_planner_requires_every_matching_record_for_all_vendor_requests(self):
        class FakeModel:
            def invoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": json.dumps({"steps": []})})()

        fake_model = FakeModel()
        with patch("app.services.agent.get_model", return_value=fake_model):
            understand_and_plan({
                "objective": "Review all invoices from Metro Facilities Group.",
                "provided_information": {},
                "memory_context": {},
                "plan": {},
            })

        self.assertIn("list every matching record", fake_model.prompt)
        self.assertIn("displayed result count", fake_model.prompt)
        self.assertIn("historical or paid matches", fake_model.prompt)

    def test_unsupported_all_vendor_invoice_request_falls_back_to_filtered_read_only_browser(self):
        objective = "review all invoices from Metro Facilities Group"
        unsupported = {
            "goal": objective,
            "supported": False,
            "limitations": ["No registered tool can do this."],
            "unavailable_capabilities": ["invoice_retrieval"],
            "missing_information": [],
            "risks": [],
            "success_criteria": [],
            "steps": [],
        }

        with patch("app.services.agent.default_browser_url", return_value="http://localhost:4173/dashboard"):
            initial = _ledgerly_invoice_fallback(unsupported, objective, {})

        self.assertTrue(initial["supported"])
        self.assertEqual(initial["steps"][0]["capability"], "browser.inspect")
        self.assertIn("search=Metro+Facilities+Group", initial["steps"][0]["inputs"]["steps"][0]["url"])
        validated_initial, initial_status = _validated_plan(objective, initial)
        self.assertEqual(initial_status, "registered_tool_plan")
        self.assertEqual(validated_initial["steps"][0]["capability"], "browser.inspect")

        completed = [{
            "step": 1,
            "action": "Inspect filtered invoice list",
            "capability": "browser.inspect",
            "depends_on": [],
            "inputs": {"steps": [{"action": "navigate", "url": "http://localhost:4173/invoices?search=Metro+Facilities+Group"}]},
            "requires_approval": False,
            "expected_result": "Matching invoice rows",
            "success_criteria": ["Rows captured"],
        }]
        continued = _ledgerly_invoice_fallback(unsupported, objective, {
            "completed_plan_steps": completed,
            "completed_evidence": [{
                "capability": "browser.inspect",
                "success": True,
                "outputs": {"evidence_text": "3 matching records"},
            }],
        })

        summary = continued["steps"][-1]
        self.assertEqual(summary["capability"], "documents.summarize_text")
        self.assertEqual(summary["depends_on"], [1])
        self.assertEqual(summary["inputs"]["text"]["$ref"], {"step": 1, "field": "evidence_text"})
        self.assertIn("Do not omit any matching row", summary["inputs"]["instructions"])
        validated_continuation, continuation_status = _validated_plan(objective, continued)
        self.assertEqual(continuation_status, "registered_tool_plan")
        self.assertEqual(validated_continuation["steps"][-1]["capability"], "documents.summarize_text")

    def test_retrieved_verified_procedure_changes_later_tool_selection(self):
        with Session(self.test_engine) as session:
            session.add(Memory(
                owner_id="alice",
                category="procedure",
                kind="inference",
                content="Verified procedure for Search invoice reports in authorized files: files.search",
                memory_key="verified-procedure:invoice-search",
                procedure_key="procedure:invoice-search",
                procedure_successes=3,
                procedure_confidence=1.0,
                conditions_json=json.dumps({"objective_terms": ["invoice", "reports"]}),
                evidence_json=json.dumps({
                    "capabilities": ["files.search"],
                    "outcomes": [{"outcome": "verified_success"}],
                }),
                source="verified_task",
                verification_status="verified",
            ))
            task = Task(
                owner_id="alice",
                objective="Search invoice reports in authorized files",
                status="pending",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        def select_from_verified_context(objective, inputs, memory_context):
            candidates = memory_context["verified_procedures"]
            capability = (
                candidates[0]["evidence"]["capabilities"][0]
                if candidates else "files.list"
            )
            return {"steps": [{
                "action": "Search invoice reports",
                "capability": capability,
                "inputs": {"query": "invoice reports"} if capability == "files.search" else {},
                "expected_result": "The relevant authorized files are identified.",
                "success_criteria": ["The tool returns the relevant files."],
            }]}

        with patch.object(main, "engine", self.test_engine), patch.object(
            memory_api, "engine", self.test_engine
        ), patch.object(workflow, "_create_plan_with_memory", side_effect=select_from_verified_context), patch.dict(
            "os.environ", {"TRUSTED_USER_HEADER": "X-Trusted-User"}
        ), TestClient(main.app) as client:
            planned = client.post(
                f"/api/tasks/{task_id}/plan",
                headers={"X-Trusted-User": "alice"},
                json={},
            )
        self.assertEqual(planned.status_code, 200, planned.text)
        self.assertEqual(planned.json()["plan"]["steps"][0]["capability"], "files.search")

    def test_policy_workflow_persists_procedure_after_independent_verification(self):
        task = Task(
            owner_id="alice",
            objective="Review the approved procurement policy.",
            status="running",
            result_json=json.dumps({
                "workflow": "procurement_policy_review",
                "step_results": [{
                    "capability": "read_company_policy",
                    "success": True,
                    "result": {"success": True, "policy_text": "Approved policy text."},
                }],
            }),
        )
        with Session(self.test_engine) as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        with patch.object(workflow, "engine", self.test_engine), patch.object(
            execution,
            "_active_policy_reader",
            return_value=lambda: {
                "success": True,
                "policy_text": "Approved policy text.",
                "source": "policy.txt",
            },
        ):
            execution._finish_policy_verification(task_id)

        with Session(self.test_engine) as session:
            saved_task = session.get(Task, task_id)
            procedure = session.exec(
                select(Memory).where(Memory.source_ref == str(task_id))
            ).first()
        self.assertEqual(saved_task.status, "completed")
        self.assertIsNotNone(procedure)
        self.assertEqual(procedure.verification_status, "verified")

    def test_retrieval_failure_is_reported_without_raising(self):
        with Session(self.test_engine) as session:
            with patch("app.services.memory.retrieve_context", side_effect=RuntimeError("database unavailable")):
                context = retrieve_context_safely(session, "alice", "policy review")
        self.assertEqual(context["retrieval_status"], "unavailable")
        self.assertEqual(context["memories"], [])
        self.assertEqual(context["task_history"], [])

    def test_aging_business_memory_is_returned_as_stale(self):
        with Session(self.test_engine) as session:
            session.add(Memory(
                owner_id="alice",
                category="business_context",
                kind="fact",
                content="The procurement office is in the North building.",
                scope="organization",
                organization_id="north",
                verification_status="verified",
                updated_at=datetime.now(timezone.utc) - timedelta(days=200),
            ))
            session.commit()
            context = retrieve_context(
                session,
                "alice",
                "procurement office",
                organization_id="north",
            )
            isolated_context = retrieve_context(session, "alice", "procurement office")
        self.assertEqual(context["memories"][0]["verification_status"], "stale")
        self.assertTrue(context["memories"][0]["stale"])
        self.assertEqual(isolated_context["memories"], [])

    def test_legacy_task_results_are_compacted_with_verification_status(self):
        with Session(self.test_engine) as session:
            session.add(Task(
                owner_id="alice",
                objective="Review the procurement policy document.",
                status="verified",
                result_json=json.dumps([{
                    "capability": "read_company_policy",
                    "result": {"success": True, "policy_text": "Do not include full policy text."},
                }]),
            ))
            session.commit()
            context = retrieve_context(session, "alice", "procurement policy document")
        self.assertEqual(context["task_history"][0]["verification_status"], "verified")
        self.assertEqual(context["task_history"][0]["summary"], "read_company_policy succeeded")

    def test_memory_api_conflicts_correction_deletion_and_user_isolation(self):
        with patch.object(main, "engine", self.test_engine), patch.object(
            memory_api, "engine", self.test_engine
        ), patch.dict(
            "os.environ", {"TRUSTED_USER_HEADER": "X-Trusted-User"}
        ), TestClient(main.app) as client:
            alice = {"X-Trusted-User": "alice"}
            bob = {"X-Trusted-User": "bob"}
            first = client.post("/api/memories", headers=alice, json={
                "category": "preference",
                "kind": "user_instruction",
                "content": "Write reports in Markdown.",
                "memory_key": "report-format",
            })
            self.assertEqual(first.status_code, 201, first.text)
            first_id = first.json()["id"]
            rejected = client.post("/api/memories", headers=alice, json={
                "category": "business_context",
                "kind": "fact",
                "content": "api_key=do-not-store-this",
            })
            self.assertEqual(rejected.status_code, 422)

            claimed_procedure = client.post("/api/memories", headers=alice, json={
                "category": "procedure",
                "kind": "inference",
                "content": "Search these files first.",
                "verification_status": "verified",
            })
            self.assertEqual(claimed_procedure.status_code, 422)
            editable_procedure = client.post("/api/memories", headers=alice, json={
                "category": "procedure",
                "kind": "inference",
                "content": "Unverified search sequence.",
            })
            corrected_procedure = client.patch(
                f"/api/memories/{editable_procedure.json()['id']}",
                headers=alice,
                json={"content": "Corrected unverified search sequence."},
            )
            self.assertEqual(corrected_procedure.status_code, 200, corrected_procedure.text)
            self.assertEqual(corrected_procedure.json()["verification_status"], "unverified")

            second = client.post("/api/memories", headers=alice, json={
                "category": "preference",
                "kind": "user_instruction",
                "content": "Write reports as plain text.",
                "memory_key": "report-format",
            })
            self.assertEqual(second.status_code, 201, second.text)
            self.assertEqual(second.json()["verification_status"], "conflicted")
            self.assertEqual(
                client.get("/api/memories", headers=bob).json(),
                [],
            )
            self.assertEqual(
                client.get(f"/api/memories/{first_id}", headers=bob).status_code,
                404,
            )
            self.assertEqual(
                client.get("/api/memories/context", headers=alice, params={"query": "reports"})
                .json()["memories"][0]["verification_status"],
                "conflicted",
            )

            corrected = client.patch(
                f"/api/memories/{first_id}",
                headers=alice,
                json={
                    "content": "Write reports in Markdown with a short summary.",
                    "verification_status": "verified",
                },
            )
            self.assertEqual(corrected.status_code, 200, corrected.text)
            self.assertEqual(corrected.json()["verification_status"], "verified")
            self.assertEqual(client.delete(f"/api/memories/{first_id}", headers=alice).status_code, 204)
            self.assertEqual(client.get(f"/api/memories/{first_id}", headers=alice).status_code, 404)

    def test_task_api_uses_trusted_owner_identity(self):
        with patch.object(main, "engine", self.test_engine), patch.object(
            memory_api, "engine", self.test_engine
        ), patch.dict(
            "os.environ", {
                "TRUSTED_USER_HEADER": "X-Trusted-User",
                "TRUSTED_ORG_HEADER": "X-Trusted-Org",
            }
        ), TestClient(main.app) as client:
            alice_task = client.post(
                "/api/tasks",
                headers={"X-Trusted-User": "alice", "X-Trusted-Org": "north"},
                json={"objective": "Review the procurement policy."},
            )
            bob_tasks = client.get("/api/tasks", headers={"X-Trusted-User": "bob"})
            self.assertEqual(alice_task.status_code, 200, alice_task.text)
            self.assertEqual(alice_task.json()["owner_id"], "alice")
            self.assertEqual(alice_task.json()["organization_id"], "north")
            self.assertEqual(bob_tasks.json(), [])
            wrong_org_plan = client.post(
                f"/api/tasks/{alice_task.json()['id']}/plan",
                headers={"X-Trusted-User": "alice", "X-Trusted-Org": "south"},
                json={},
            )
            self.assertEqual(wrong_org_plan.status_code, 403)
            self.assertEqual(
                client.get(
                    f"/api/tasks/{alice_task.json()['id']}/events",
                    headers={"X-Trusted-User": "bob"},
                ).status_code,
                404,
            )
            task_id = alice_task.json()["id"]
            with Session(self.test_engine) as session:
                session.add_all([
                    ExecutionEvent(
                        task_id=task_id,
                        event_type="test_event",
                        message="Persisted task event.",
                    ),
                    Approval(task_id=task_id, action_json="{}"),
                ])
                session.commit()

            self.assertEqual(
                client.delete(f"/api/tasks/{task_id}", headers={"X-Trusted-User": "bob"}).status_code,
                404,
            )
            cleared = client.delete(
                f"/api/tasks/{task_id}",
                headers={"X-Trusted-User": "alice"},
            )
            self.assertEqual(cleared.status_code, 204, cleared.text)
            self.assertEqual(client.get("/api/tasks", headers={"X-Trusted-User": "alice"}).json(), [])
            with Session(self.test_engine) as session:
                self.assertEqual(session.exec(select(ExecutionEvent)).all(), [])
                self.assertEqual(session.exec(select(Approval)).all(), [])


class DatabaseCompatibilityTests(unittest.TestCase):
    def test_existing_task_table_gets_default_owner_column(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            engine = create_engine(
                f"sqlite:///{database_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE task (id INTEGER PRIMARY KEY, objective VARCHAR NOT NULL, "
                        "status VARCHAR NOT NULL, plan_json VARCHAR, result_json VARCHAR, "
                        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO task (id, objective, status, created_at, updated_at) "
                        "VALUES (1, 'legacy task', 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                    connection.exec_driver_sql(
                        "CREATE TABLE approval (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, "
                        "action_json TEXT NOT NULL, status VARCHAR NOT NULL, decided_at DATETIME)"
                    )
                    connection.exec_driver_sql(
                        "CREATE TABLE memory (id INTEGER PRIMARY KEY, owner_id VARCHAR NOT NULL, "
                        "category VARCHAR NOT NULL, kind VARCHAR NOT NULL, content VARCHAR NOT NULL, "
                        "memory_key VARCHAR, source VARCHAR NOT NULL, source_ref VARCHAR, "
                        "verification_status VARCHAR NOT NULL, verified_at DATETIME, expires_at DATETIME, "
                        "active BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                    )
                create_db_and_tables(engine)
                with Session(engine) as session:
                    task = session.get(Task, 1)
                    self.assertEqual(task.owner_id, "default")
                    self.assertIsNone(task.organization_id)
                with engine.connect() as connection:
                    approval_columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(approval)")
                    }
                    memory_columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(memory)")
                    }
                self.assertTrue({
                    "execution_inputs_json",
                    "capability",
                    "action_hash",
                    "risk_json",
                    "decided_by",
                    "expires_at",
                }.issubset(approval_columns))
                self.assertTrue({
                    "scope",
                    "organization_id",
                    "procedure_key",
                    "procedure_version",
                    "procedure_successes",
                    "procedure_failures",
                    "procedure_confidence",
                    "conditions_json",
                    "evidence_json",
                }.issubset(memory_columns))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
