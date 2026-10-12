import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import httpx
from pypdf import PdfWriter

from app import main
from app.tools import documents as document_tools
from app.tools import registry as tool_registry
from app.tools import web as web_tools
from app.tools.registry import execute_tool, get_registered_tool_contracts
from app.workflows.support.plan import _validated_plan


class ToolFrameworkTests(unittest.TestCase):
    def test_api_bearer_auth_is_opt_in_and_health_remains_public(self):
        with patch.dict(os.environ, {"API_BEARER_TOKEN": "test-server-token"}):
            with TestClient(main.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/tasks/1/events").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/api/tasks/1/events",
                        headers={"Authorization": "Bearer wrong-token"},
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.get(
                        "/api/tasks/1/events",
                        headers={"Authorization": "Bearer test-server-token"},
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/api/tasks/1/execute",
                        headers={"Authorization": "Bearer test-server-token"},
                    ).status_code,
                    410,
                )

    def test_registered_contracts_include_schema_permissions_outputs_and_errors(self):
        contracts = get_registered_tool_contracts()
        self.assertEqual({item["capability"] for item in contracts}, set(tool_registry.TOOLS))
        for contract in contracts:
            self.assertEqual(contract["input_schema"]["type"], "object")
            self.assertIsInstance(contract["permissions"], list)
            self.assertIsInstance(contract["outputs"], list)
            self.assertTrue(contract["output_format"])
            self.assertIsInstance(contract["errors"], list)

    def test_registration_rejects_invalid_schema_and_duplicate_capability(self):
        name = "test_invalid_contract"
        with self.assertRaises(ValueError):
            tool_registry.register_tool(
                name,
                lambda _context: {"success": True},
                description="Invalid test contract.",
                input_schema={"type": "mystery", "properties": {}},
            )
        self.assertNotIn(name, tool_registry.TOOLS)

        tool_registry.register_tool(
            name,
            lambda _context: {"success": True},
            description="Valid test contract.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.addCleanup(tool_registry.TOOLS.pop, name, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, name, None)
        with self.assertRaises(ValueError):
            tool_registry.register_tool(
                name,
                lambda _context: {"success": True},
                description="Duplicate test contract.",
            )

    def test_dispatch_validates_inputs_and_permissions_before_handler(self):
        capability = "test_dispatch_gates"
        handler = Mock(return_value={"success": True, "value": "done"})
        tool_registry.register_tool(
            capability,
            handler,
            description="Exercise dispatcher validation and permissions.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 2}},
                "required": ["name"],
                "additionalProperties": False,
            },
            outputs=["value"],
            permissions=["records:write"],
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)

        invalid_results = [
            execute_tool(capability, {}, permissions={"records:write"}),
            execute_tool(capability, {"name": "x"}, permissions={"records:write"}),
            execute_tool(capability, {"name": "valid", "extra": True}, permissions={"records:write"}),
        ]
        self.assertTrue(all(item["failure_type"] == "validation_error" for item in invalid_results))
        denied = execute_tool(capability, {"name": "valid"}, permissions={"records:read"})
        self.assertEqual(denied["failure_type"], "permission_denied")
        handler.assert_not_called()

        allowed = execute_tool(capability, {"name": "valid"}, permissions={"records:write"})
        self.assertEqual(allowed, {"success": True, "value": "done"})
        handler.assert_called_once_with({"name": "valid"})

    def test_trusted_user_role_grants_are_resolved_server_side(self):
        with patch.dict(os.environ, {
            "TOOL_ROLE_GRANTS": '{"reader":["procurement:read"]}',
            "TRUSTED_USER_ROLES": '{"alice":"reader"}',
        }, clear=False):
            allowed = execute_tool("read_company_policy", owner_id="alice")
            denied = execute_tool(
                "register_vendor",
                {"approval_id": 1},
                owner_id="alice",
            )

        self.assertTrue(allowed["success"])
        self.assertEqual(denied["failure_type"], "permission_denied")

    def test_credential_like_tool_inputs_and_outputs_are_blocked(self):
        input_result = execute_tool(
            "documents.summarize_text",
            {"text": "password=hunter2"},
            permissions={"documents:process"},
        )
        self.assertEqual(input_result["failure_type"], "secret_material_blocked")

        capability = "test_secret_output"
        tool_registry.register_tool(
            capability,
            lambda context: {"success": True, "access_token": "not-for-audit"},
            description="Return a test response.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)

        output_result = execute_tool(capability, {}, permissions={"business:read"})

        self.assertEqual(output_result["failure_type"], "secret_material_blocked")
        self.assertNotIn("not-for-audit", str(output_result))

    def test_dispatch_converts_handler_errors_to_structured_failures(self):
        capability = "test_dispatch_failure"
        tool_registry.register_tool(
            capability,
            Mock(side_effect=RuntimeError("sensitive internal detail")),
            description="Exercise structured handler failure.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.addCleanup(tool_registry.TOOLS.pop, capability, None)
        self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)

        with self.assertLogs("app.tools.registry", level="ERROR"):
            result = execute_tool(capability, {}, permissions={"business:read"})
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_type"], "tool_error")
        self.assertNotIn("sensitive internal detail", result["error"])

        for capability, raised, expected_type in (
            ("test_dispatch_timeout", TimeoutError(), "timeout"),
            ("test_dispatch_rate_limit", RuntimeError("429 rate limit"), "rate_limited"),
        ):
            tool_registry.register_tool(
                capability,
                Mock(side_effect=raised),
                description="Exercise generic transient exception classification.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
            self.addCleanup(tool_registry.TOOLS.pop, capability, None)
            self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
            with self.assertLogs("app.tools.registry", level="ERROR"):
                failure = execute_tool(capability, {}, permissions={"business:read"})
            self.assertEqual(failure["failure_type"], expected_type)

    def test_file_tools_enforce_root_permissions_and_controlled_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_bytes(b"first line\nkeep me\n")
            with patch.dict(os.environ, {"AUTHORIZED_FILE_ROOTS": directory}):
                read_result = execute_tool(
                    "files.read", {"path": "source.txt"}, permissions={"files:read"}
                )
                self.assertTrue(read_result["success"])
                self.assertEqual(read_result["content"], "first line\nkeep me\n")

                denied = execute_tool(
                    "files.create", {"path": "new.txt", "content": "draft"},
                    permissions={"files:read"},
                )
                self.assertEqual(denied["failure_type"], "permission_denied")
                traversal = execute_tool(
                    "files.read", {"path": "../outside.txt"}, permissions={"files:read"}
                )
                self.assertEqual(traversal["failure_type"], "unauthorized_path")

                created = execute_tool(
                    "files.create", {"path": "new.txt", "content": "draft"},
                    permissions={"files:write"},
                )
                self.assertTrue(created["success"])
                duplicate = execute_tool(
                    "files.create", {"path": "new.txt", "content": "overwrite"},
                    permissions={"files:write"},
                )
                self.assertEqual(duplicate["failure_type"], "file_already_exists")

                edited = execute_tool(
                    "files.edit",
                    {"path": "source.txt", "old_text": "keep me", "new_text": "updated"},
                    permissions={"files:write"},
                )
                self.assertTrue(edited["success"])
                self.assertEqual((root / "source.txt").read_text(encoding="utf-8"), "first line\nupdated\n")
                conflict = execute_tool(
                    "files.edit",
                    {"path": "source.txt", "old_text": "first", "new_text": "x"},
                    permissions={"files:write"},
                )
                self.assertEqual(conflict["failure_type"], "edit_conflict")

    def test_document_comparison_and_report_are_deterministic_and_verified(self):
        comparison_inputs = {"left_text": "alpha\nbeta", "right_text": "alpha\ngamma"}
        comparison = execute_tool(
            "documents.compare_text", comparison_inputs, permissions={"documents:process"}
        )
        self.assertTrue(comparison["success"])
        self.assertFalse(comparison["identical"])
        self.assertTrue(tool_registry.verify_registered_tool(
            "documents.compare_text", comparison_inputs, comparison
        )["verified"])

        report_inputs = {
            "title": "Vendor Review",
            "summary": "Review complete.",
            "findings": ["Policy checked."],
            "sources": ["policy.txt"],
        }
        report = execute_tool(
            "documents.generate_report", report_inputs, permissions={"documents:process"}
        )
        self.assertIn("# Vendor Review", report["report_markdown"])
        self.assertTrue(tool_registry.verify_registered_tool(
            "documents.generate_report", report_inputs, report
        )["verified"])

    def test_pdf_extraction_and_model_failures_are_bounded_and_structured(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        pdf = io.BytesIO()
        writer.write(pdf)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "blank.pdf").write_bytes(pdf.getvalue())
            with patch.dict(os.environ, {"AUTHORIZED_FILE_ROOTS": directory}):
                extracted = execute_tool(
                    "documents.extract_pdf_text",
                    {"path": "blank.pdf"},
                    permissions={"documents:process", "files:read"},
                )
        self.assertTrue(extracted["success"])
        self.assertEqual(extracted["page_count"], 1)

        with patch.object(document_tools, "_invoke_document_model", side_effect=TimeoutError()):
            timeout = execute_tool(
                "documents.summarize_text",
                {"text": "A short source document."},
                permissions={"documents:process"},
            )
        self.assertEqual(timeout["failure_type"], "timeout")
        self.assertEqual(
            document_tools._model_failure(RuntimeError("429 RESOURCE_EXHAUSTED"))["failure_type"],
            "rate_limited",
        )

    def test_permission_gated_file_write_does_not_claim_human_approval(self):
        plan, _workflow_name = _validated_plan("Create a report file.", {
            "goal": "Create a report file.",
            "steps": [{
                "step": 1,
                "action": "Create report",
                "capability": "files.create",
                "depends_on": [],
                "inputs": {"path": "report.txt", "content": "Review complete."},
                "requires_approval": False,
                "expected_result": "The authorized report file exists.",
            }],
        })
        self.assertFalse(plan["steps"][0]["requires_approval"])

    def test_web_page_retrieval_requires_allowlist_and_extracts_sources(self):
        capability = "web.fetch_page"
        with patch.dict(os.environ, {"WEB_ALLOWED_HOSTS": "research.example"}):
            web_tools.register_web_tools(tool_registry.register_tool)
            self.addCleanup(tool_registry.TOOLS.pop, capability, None)
            self.addCleanup(tool_registry.TOOL_CONTRACTS.pop, capability, None)
            self.addCleanup(tool_registry.TOOL_VERIFIERS.pop, capability, None)

            denied = execute_tool(
                capability,
                {"url": "https://other.example/page"},
                permissions={"web:read"},
            )
            self.assertEqual(denied["failure_type"], "unauthorized_host")

            transport = httpx.MockTransport(lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><script>ignored()</script><p>Policy details</p><a href='/policy'>Policy</a></html>",
                request=request,
            ))
            client = httpx.Client(transport=transport)
            with patch.object(web_tools.httpx, "Client", return_value=client):
                fetched = execute_tool(
                    capability,
                    {"url": "https://research.example/page"},
                    permissions={"web:read"},
                )

        self.assertTrue(fetched["success"])
        self.assertEqual(fetched["source"], "https://research.example/page")
        self.assertIn("Policy details", fetched["text"])
        self.assertNotIn("ignored", fetched["text"])
        self.assertEqual(fetched["links"], ["https://research.example/policy"])


if __name__ == "__main__":
    unittest.main()