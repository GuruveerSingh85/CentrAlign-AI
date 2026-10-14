import json
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pypdf import PdfWriter
from sqlalchemy import create_engine

from app import main
from app.tools import api as api_tools
from app.tools import browser as browser_tools
from app.tools import documents as document_tools
from app.tools import registry as tool_registry
from app.tools import web as web_tools
from app.tools.registry import execute_tool, get_registered_tool_contracts
from app.workflows.support.plan import _validated_plan
from app.api.middleware import install_api_middleware


class ToolFrameworkTests(unittest.TestCase):
    def test_browser_navigation_uses_default_site_when_url_is_omitted(self):
        with patch.object(browser_tools, "default_browser_url", return_value="http://localhost:4173/dashboard"):
            resolved = browser_tools._apply_default_navigation({
                "steps": [{"action": "navigate"}, {"action": "inspect"}],
            })
        with patch.object(browser_tools, "default_browser_url", return_value="http://localhost:4173/dashboard"), \
             patch.dict(os.environ, {"BROWSER_AUTO_APPROVE": "true"}):
            auto_approved = browser_tools.browser_auto_approval_allowed({
                "steps": [{"action": "navigate"}],
            }, task_id=None)

        self.assertEqual(resolved["steps"][0]["url"], "http://localhost:4173/dashboard")
        self.assertEqual(resolved["steps"][1], {"action": "inspect"})
        self.assertTrue(auto_approved)

    def test_document_summary_applies_task_specific_output_requirements(self):
        prompts = []

        def capture_prompt(prompt):
            prompts.append(prompt)
            return "Ranked records from the supplied evidence."

        with patch.object(document_tools, "_invoke_document_model", side_effect=capture_prompt):
            result = document_tools.summarize_text({
                "text": "Invoice table with 20 records.",
                "instructions": "Return the five largest invoices with due date and payment status.",
            })

        self.assertTrue(result["success"])
        self.assertIn("five largest invoices with due date and payment status", prompts[0])
        self.assertIn("compare all records", prompts[0])

    def test_document_summary_includes_all_matching_records_not_first_match(self):
        prompts = []

        def capture_prompt(prompt):
            prompts.append(prompt)
            return "All matching records."

        with patch.object(document_tools, "_invoke_document_model", side_effect=capture_prompt):
            result = document_tools.summarize_text({
                "text": "Three matching invoices: INV-1, INV-2, and INV-3.",
                "instructions": "Review all invoices from Metro Facilities Group.",
            })

        self.assertTrue(result["success"])
        self.assertIn("include every match", prompts[0])
        self.assertIn("never return only the first", prompts[0])

    def test_legacy_frontend_origin_environment_setting_is_respected(self):
        with patch.dict(os.environ, {
            "FRONTEND_ORIGINS": "",
            "FRONTEND_ORIGIN": "https://frontend.example",
        }):
            app = FastAPI()
            install_api_middleware(app)
            app.get("/")(lambda: {"status": "ok"})
            response = TestClient(app).options(
                "/",
                headers={
                    "Origin": "https://frontend.example",
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.headers["access-control-allow-origin"], "https://frontend.example")

    def test_api_bearer_auth_is_opt_in_and_health_remains_public(self):
        with tempfile.TemporaryDirectory() as directory:
            test_engine = create_engine(
                f"sqlite:///{(Path(directory) / 'auth-test.db').as_posix()}",
                connect_args={"check_same_thread": False},
            )
            try:
                with patch.object(main, "engine", test_engine), patch.dict(
                    os.environ, {"API_BEARER_TOKEN": "test-server-token"}
                ), TestClient(main.app) as client:
                    self.assertEqual(client.get("/api/health").status_code, 200)
                    self.assertEqual(client.get("/api/tasks/1/events").status_code, 401)
                    self.assertEqual(
                        client.get(
                            "/api/tasks/1/events",
                            headers={"Authorization": "Bearer wrong-token"},
                        ).status_code,
                        401,
                    )
                    created_task = client.post(
                        "/api/tasks",
                        headers={"Authorization": "Bearer test-server-token"},
                        json={"objective": "Verify bearer authentication."},
                    )
                    self.assertEqual(created_task.status_code, 200, created_task.text)
                    task_id = created_task.json()["id"]
                    self.assertEqual(
                        client.get(
                            f"/api/tasks/{task_id}/events",
                            headers={"Authorization": "Bearer test-server-token"},
                        ).status_code,
                        200,
                    )
                    self.assertEqual(
                        client.post(
                            f"/api/tasks/{task_id}/execute",
                            headers={"Authorization": "Bearer test-server-token"},
                        ).status_code,
                        410,
                    )
            finally:
                test_engine.dispose()

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

    def test_web_search_returns_bounded_verifiable_results(self):
        seen = {}
        payload = {"results": [
            {"title": "First source", "url": "https://source.example/one", "content": "First snippet."},
            {"title": "Second source", "url": "https://source.example/two", "content": "Second snippet."},
            {"title": "Ignored extra", "url": "https://source.example/three", "content": "Not requested."},
        ]}

        def respond(request):
            seen["request"] = request
            return httpx.Response(200, json=payload, request=request)

        client_factory = httpx.Client

        def create_client(**kwargs):
            return client_factory(transport=httpx.MockTransport(respond), **kwargs)

        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-search-secret"}, clear=False), patch.object(
            web_tools, "load_dotenv"
        ) as load_dotenv, patch.object(web_tools.httpx, "Client", side_effect=create_client):
            result = execute_tool(
                "web.search",
                {"query": "  latest research  ", "max_results": 2},
                permissions={"web:read"},
            )
            verification = tool_registry.verify_registered_tool(
                "web.search",
                {"query": "latest research", "max_results": 2},
                result,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["query"], "latest research")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0], {
            "title": "First source",
            "url": "https://source.example/one",
            "snippet": "First snippet.",
        })
        self.assertIn("URL: https://source.example/two", result["evidence_text"])
        self.assertTrue(verification["verified"])
        self.assertEqual(seen["request"].headers["authorization"], "Bearer test-search-secret")
        self.assertEqual(json.loads(seen["request"].content)["max_results"], 2)
        self.assertNotIn("test-search-secret", json.dumps(result))
        load_dotenv.assert_called_once()
        self.assertEqual(
            load_dotenv.call_args.kwargs["dotenv_path"],
            Path(web_tools.__file__).resolve().parents[2] / ".env",
        )

    def test_web_search_returns_structured_configuration_and_api_failures(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False), patch.object(
            web_tools, "load_dotenv"
        ):
            missing_key = web_tools.web_search({"query": "current information"})
        self.assertEqual(missing_key["failure_type"], "missing_configuration")
        self.assertEqual(web_tools.web_search({"query": "   "})["failure_type"], "validation_error")
        self.assertEqual(
            web_tools.web_search({"query": "valid query", "max_results": 11})["failure_type"],
            "validation_error",
        )

        api_cases = [
            (lambda request: httpx.Response(429, request=request), "rate_limited"),
            (lambda request: httpx.Response(200, content=b"not-json", request=request), "invalid_response"),
            (lambda request: httpx.Response(200, json={"results": [{"title": "Missing fields"}]}, request=request), "invalid_response"),
            (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)), "timeout"),
            (lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)), "service_unavailable"),
        ]
        client_factory = httpx.Client
        for respond, expected_failure in api_cases:
            with self.subTest(failure=expected_failure):
                def create_client(**kwargs):
                    return client_factory(transport=httpx.MockTransport(respond), **kwargs)

                with patch.dict(os.environ, {"TAVILY_API_KEY": "test-search-secret"}, clear=False), patch.object(
                    web_tools, "load_dotenv"
                ), patch.object(web_tools.httpx, "Client", side_effect=create_client):
                    result = web_tools.web_search({"query": "current information"})
                self.assertFalse(result["success"])
                self.assertEqual(result["failure_type"], expected_failure)

    def test_configured_api_read_validates_inputs_authenticates_and_returns_json(self):
        connector_config = {
            "crm": {
                "base_url": "https://crm.example/api/v1",
                "auth_env": "TEST_CRM_TOKEN",
                "endpoints": {
                    "contact_lookup": {
                        "method": "GET",
                        "path": "/contacts",
                        "input_schema": {
                            "type": "object",
                            "properties": {"contact_id": {"type": "integer", "minimum": 1}},
                            "required": ["contact_id"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        }
        seen = {}

        def respond(request):
            seen["request"] = request
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"id": 17, "name": "Jordan"},
                request=request,
            )

        client_factory = httpx.Client

        def create_client(**kwargs):
            return client_factory(transport=httpx.MockTransport(respond), **kwargs)

        with patch.dict(os.environ, {
            "BUSINESS_API_CONNECTORS": json.dumps(connector_config),
            "BUSINESS_API_ALLOWED_HOSTS": "crm.example",
            "TEST_CRM_TOKEN": "server-only-token",
        }), patch.object(api_tools, "_wait_for_host"), patch.object(
            api_tools.httpx, "Client", side_effect=create_client
        ):
            result = execute_tool(
                "api.read",
                {"connector": "crm", "operation": "contact_lookup", "inputs": {"contact_id": 17}},
                permissions={"api:read"},
            )
            verified = tool_registry.verify_registered_tool(
                "api.read",
                {"connector": "crm", "operation": "contact_lookup", "inputs": {"contact_id": 17}},
                result,
            )
            invalid = execute_tool(
                "api.read",
                {"connector": "crm", "operation": "contact_lookup", "inputs": {"contact_id": "17"}},
                permissions={"api:read"},
            )
            read_contract = next(
                contract for contract in get_registered_tool_contracts()
                if contract["capability"] == "api.read"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], {"id": 17, "name": "Jordan"})
        self.assertEqual(seen["request"].headers["authorization"], "Bearer server-only-token")
        self.assertEqual(seen["request"].url.params["contact_id"], "17")
        self.assertTrue(verified["verified"])
        self.assertNotIn("data", verified["actual"])
        self.assertEqual(invalid["failure_type"], "validation_error")

        self.assertIn("contact_lookup", read_contract["description"])

    def test_api_connectors_reject_unauthorized_hosts_and_missing_configuration(self):
        connector_config = {
            "tickets": {
                "base_url": "https://tickets.example/api",
                "endpoints": {"list": {"method": "GET", "path": "/tickets"}},
            },
        }
        with patch.dict(os.environ, {
            "BUSINESS_API_CONNECTORS": json.dumps(connector_config),
            "BUSINESS_API_ALLOWED_HOSTS": "other.example",
        }):
            denied = execute_tool(
                "api.read", {"connector": "tickets", "operation": "list"},
                permissions={"api:read"},
            )
        self.assertEqual(denied["failure_type"], "unauthorized_host")

        with patch.dict(os.environ, {"BUSINESS_API_CONNECTORS": ""}):
            unavailable = execute_tool(
                "api.read", {"connector": "tickets", "operation": "list"},
                permissions={"api:read"},
            )
        self.assertEqual(unavailable["failure_type"], "configuration_missing")

    def test_api_rate_limit_and_unavailable_service_are_structured(self):
        connector_config = {
            "hr": {
                "base_url": "https://hr.example/api",
                "endpoints": {"employees": {"method": "GET", "path": "/employees"}},
            },
        }
        request_handler = Mock(side_effect=lambda request: httpx.Response(
            429,
            headers={"retry-after": "12"},
            request=request,
        ))
        client = httpx.Client(transport=httpx.MockTransport(request_handler))
        with patch.dict(os.environ, {
            "BUSINESS_API_CONNECTORS": json.dumps(connector_config),
            "BUSINESS_API_ALLOWED_HOSTS": "hr.example",
        }), patch.object(api_tools, "_wait_for_host"), patch.object(
            api_tools.httpx, "Client", return_value=client
        ):
            limited = execute_tool(
                "api.read", {"connector": "hr", "operation": "employees"},
                permissions={"api:read"},
            )
        self.assertEqual(limited["failure_type"], "rate_limited")
        self.assertEqual(limited["retry_after_seconds"], 12)

        failure_client = httpx.Client(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request))
        ))
        with patch.dict(os.environ, {
            "BUSINESS_API_CONNECTORS": json.dumps(connector_config),
            "BUSINESS_API_ALLOWED_HOSTS": "hr.example",
        }), patch.object(api_tools, "_wait_for_host"), patch.object(
            api_tools.httpx, "Client", return_value=failure_client
        ):
            unavailable = execute_tool(
                "api.read", {"connector": "hr", "operation": "employees"},
                permissions={"api:read"},
            )
        self.assertEqual(unavailable["failure_type"], "service_unavailable")

    def test_api_writes_and_browser_interactions_require_human_approval(self):
        api_write = execute_tool(
            "api.write",
            {"connector": "crm", "operation": "update_contact", "inputs": {}},
            permissions={"api:write"},
        )
        browser_interaction = execute_tool(
            "browser.interact",
            {"steps": [{"action": "click", "selector": "#submit"}]},
            permissions={"web:interact"},
        )
        self.assertEqual(api_write["failure_type"], "approval_required")
        self.assertEqual(browser_interaction["failure_type"], "approval_required")

    def test_browser_inspection_uses_allowlist_and_registered_runtime(self):
        with patch.dict(os.environ, {"WEB_ALLOWED_HOSTS": "research.example"}):
            denied = execute_tool(
                "browser.inspect",
                {"steps": [{"action": "navigate", "url": "https://outside.example"}]},
                permissions={"web:read"},
            )
            disallowed_action = execute_tool(
                "browser.inspect",
                {"steps": [{"action": "click", "selector": "#submit"}]},
                permissions={"web:read"},
            )

        self.assertEqual(denied["failure_type"], "unauthorized_host")
        self.assertEqual(disallowed_action["failure_type"], "validation_error")

        fake_locator = Mock()
        fake_locator.inner_text.return_value = "Rendered policy page"
        fake_locator.evaluate_all.return_value = [{"text": "Policy", "href": "https://research.example/policy"}]
        fake_page = Mock()
        fake_page.url = "https://research.example/policy"
        fake_page.title.return_value = "Policy"
        fake_page.locator.return_value = fake_locator
        fake_browser_context = Mock()
        fake_browser_context.new_page.return_value = fake_page
        fake_browser = Mock()
        fake_browser.new_context.return_value = fake_browser_context
        automation = Mock()
        automation.chromium.launch.return_value = fake_browser
        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = automation
        playwright_manager.start.return_value = automation
        playwright_module = types.ModuleType("playwright.sync_api")
        playwright_module.sync_playwright = Mock(return_value=playwright_manager)
        playwright_package = types.ModuleType("playwright")
        playwright_package.__path__ = []

        with patch.dict(sys.modules, {
            "playwright": playwright_package,
            "playwright.sync_api": playwright_module,
        }), patch.dict(os.environ, {"WEB_ALLOWED_HOSTS": "research.example"}):
            inspected = execute_tool(
                "browser.inspect",
                {"steps": [
                    {"action": "navigate", "url": "https://research.example/policy"},
                    {"action": "inspect"},
                ]},
                permissions={"web:read"},
            )
            verified = tool_registry.verify_registered_tool(
                "browser.inspect",
                {"steps": [
                    {"action": "navigate", "url": "https://research.example/policy"},
                    {"action": "inspect"},
                ]},
                inspected,
            )

        self.assertTrue(inspected["success"])
        self.assertEqual(inspected["observations"][1]["text"], "Rendered policy page")
        self.assertTrue(verified["verified"])
        self.assertNotIn("observations", verified["actual"])
        self.assertEqual(fake_browser.close.call_count, 1)


if __name__ == "__main__":
    unittest.main()
