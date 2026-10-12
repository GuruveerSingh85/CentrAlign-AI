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
from app.core.models import Memory, Task
from app.workflows import engine as workflow
from app.workflows.support import execution


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
                verification_status="verified",
                updated_at=datetime.now(timezone.utc) - timedelta(days=200),
            ))
            session.commit()
            context = retrieve_context(session, "alice", "procurement office")
        self.assertEqual(context["memories"][0]["verification_status"], "stale")
        self.assertTrue(context["memories"][0]["stale"])

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
            "os.environ", {"TRUSTED_USER_HEADER": "X-Trusted-User"}
        ), TestClient(main.app) as client:
            alice_task = client.post(
                "/api/tasks",
                headers={"X-Trusted-User": "alice"},
                json={"objective": "Review the procurement policy."},
            )
            bob_tasks = client.get("/api/tasks", headers={"X-Trusted-User": "bob"})
            self.assertEqual(alice_task.status_code, 200, alice_task.text)
            self.assertEqual(alice_task.json()["owner_id"], "alice")
            self.assertEqual(bob_tasks.json(), [])
            self.assertEqual(
                client.get(
                    f"/api/tasks/{alice_task.json()['id']}/events",
                    headers={"X-Trusted-User": "bob"},
                ).status_code,
                404,
            )


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
                create_db_and_tables(engine)
                with Session(engine) as session:
                    task = session.get(Task, 1)
                    self.assertEqual(task.owner_id, "default")
                with engine.connect() as connection:
                    approval_columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(approval)")
                    }
                self.assertTrue({
                    "execution_inputs_json",
                    "capability",
                    "action_hash",
                    "risk_json",
                    "decided_by",
                    "expires_at",
                }.issubset(approval_columns))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()