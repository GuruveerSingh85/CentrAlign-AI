from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from app.tools.files import _failure
from app.tools.web import _validate_url

MAX_BROWSER_STEPS = 20
MAX_PAGE_TEXT_CHARS = 20_000
MAX_EXTRACTED_CHARS = 10_000
MAX_BROWSER_TIMEOUT_MS = 15_000
MAX_BROWSER_EVIDENCE_CHARS = 25_000
DEFAULT_BROWSER_URL = "http://localhost:5173/dashboard"

_READ_ACTIONS = {"navigate", "inspect", "extract"}
_INTERACTION_ACTIONS = _READ_ACTIONS | {"fill", "click"}
_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[int, "_BrowserSession"] = {}
try:
    _SESSION_IDLE_TIMEOUT = max(60, int(os.getenv("BROWSER_SESSION_IDLE_TIMEOUT_SECONDS", "3600")))
except ValueError:
    _SESSION_IDLE_TIMEOUT = 3600


def _validate_browser_url(url: str) -> tuple[str | None, dict | None]:
    """Allow the configured public HTTPS hosts and local development servers."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        parsed.port  # Validate malformed port numbers before applying the host policy.
    except ValueError:
        return None, _failure("The URL is invalid.", "invalid_url")

    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if (
        is_loopback
        and parsed.scheme == "http"
        and host
        and not parsed.username
        and not parsed.password
    ):
        return host, None
    return _validate_url(url)


def _headless_mode() -> bool:
    value = os.getenv("BROWSER_HEADLESS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def default_browser_url() -> str:
    """Return the configured default site, preferring a running local demo server."""
    configured = os.getenv("BROWSER_DEFAULT_URL", "").strip()
    if configured:
        return configured
    for port in (4173, 5173):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://localhost:{port}/dashboard"
        except OSError:
            continue
    return DEFAULT_BROWSER_URL


def _apply_default_navigation(context: dict) -> dict:
    """Fill in the default Ledgerly URL when a browser plan omits its starting site."""
    steps = context.get("steps")
    if not isinstance(steps, list):
        return context
    default_url = default_browser_url()
    resolved = []
    for step in steps:
        if isinstance(step, dict) and step.get("action") == "navigate" and not step.get("url"):
            step = {**step, "url": default_url}
        resolved.append(step)
    return {**context, "steps": resolved}


def _validate_steps(context: dict, *, allow_interaction: bool) -> dict | None:
    steps = context["steps"]
    if not 1 <= len(steps) <= MAX_BROWSER_STEPS:
        return _failure("Browser requests must contain between 1 and 20 steps.", "validation_error")
    allowed_actions = _INTERACTION_ACTIONS if allow_interaction else _READ_ACTIONS
    for index, step in enumerate(steps):
        action = step.get("action")
        if action not in allowed_actions:
            return _failure(f"Browser step {index + 1} uses an unsupported action.", "validation_error")
        if action == "navigate":
            if not isinstance(step.get("url"), str):
                return _failure(f"Browser step {index + 1} requires a URL.", "validation_error")
            _, error = _validate_browser_url(step["url"])
            if error:
                return error
        if action in {"fill", "click", "extract"} and not isinstance(step.get("selector"), str):
            return _failure(f"Browser step {index + 1} requires a selector.", "validation_error")
        if action == "fill" and not isinstance(step.get("value"), str):
            return _failure(f"Browser step {index + 1} requires a text value.", "validation_error")
    return None


class _BrowserSession:
    """A task's Playwright objects stay on one worker thread across tool calls."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="task-browser")
        self.last_used = time.monotonic()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_page(self, headless: bool):
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(5_000)

        def guard_request(route) -> None:
            _, target_error = _validate_browser_url(route.request.url)
            if target_error:
                route.abort()
            else:
                route.continue_()

        self._page.route("**/*", guard_request)
        return self._page

    def run(self, steps: list[dict]) -> dict:
        self.last_used = time.monotonic()
        return self._executor.submit(self._run_steps, steps, _headless_mode()).result(timeout=25)

    def current_url(self) -> str | None:
        try:
            return self._executor.submit(
                lambda: self._page.url if self._page is not None and not self._page.is_closed() else None
            ).result(timeout=5)
        except Exception:
            return None

    def _run_steps(self, steps: list[dict], headless: bool) -> dict:
        page = self._ensure_page(headless)
        observations = []
        for step in steps:
            action = step["action"]
            if action == "navigate":
                page.goto(step["url"], wait_until="domcontentloaded", timeout=MAX_BROWSER_TIMEOUT_MS)
                observations.append({"action": action, "url": page.url})
            elif action == "fill":
                page.locator(step["selector"]).fill(step["value"])
                observations.append({"action": action, "selector": step["selector"], "completed": True})
            elif action == "click":
                page.locator(step["selector"]).click()
                observations.append({"action": action, "selector": step["selector"], "completed": True})
            elif action == "extract":
                text = page.locator(step["selector"]).inner_text()
                observations.append({
                    "action": action,
                    "selector": step["selector"],
                    "text": text[:MAX_EXTRACTED_CHARS],
                    "truncated": len(text) > MAX_EXTRACTED_CHARS,
                })
            elif action == "inspect":
                text = page.locator("body").inner_text()
                links = page.locator("a[href]").evaluate_all(
                    "elements => elements.slice(0, 100).map(element => ({text: element.innerText, href: element.href}))"
                )
                observations.append({
                    "action": action,
                    "url": page.url,
                    "title": page.title(),
                    "text": text[:MAX_PAGE_TEXT_CHARS],
                    "text_truncated": len(text) > MAX_PAGE_TEXT_CHARS,
                    "links": links,
                })
        evidence_text = json.dumps(observations, ensure_ascii=True, separators=(",", ":"))
        return {
            "success": True,
            "observations": observations,
            "evidence_text": evidence_text[:MAX_BROWSER_EVIDENCE_CHARS],
            "evidence_text_truncated": len(evidence_text) > MAX_BROWSER_EVIDENCE_CHARS,
            "replan_required": True,
        }

    def close(self) -> None:
        try:
            self._executor.submit(self._close_on_worker).result(timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _close_on_worker(self) -> None:
        for resource, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass
        self._page = self._context = self._browser = self._playwright = None


def close_browser_session(task_id: int) -> None:
    with _SESSION_LOCK:
        session = _SESSIONS.pop(task_id, None)
    if session is not None:
        session.close()


def close_all_browser_sessions() -> None:
    with _SESSION_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.close()


def browser_auto_approval_allowed(context: dict, task_id: int | None) -> bool:
    """Return true only for opted-in browser actions whose target is allowlisted."""
    if os.getenv("BROWSER_AUTO_APPROVE", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    context = _apply_default_navigation(context)
    steps = context.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    navigations = [step.get("url") for step in steps if isinstance(step, dict) and step.get("action") == "navigate"]
    if navigations:
        return all(isinstance(url, str) and _validate_browser_url(url)[1] is None for url in navigations)
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return False
    with _SESSION_LOCK:
        session = _SESSIONS.get(task_id)
    current_url = session.current_url() if session is not None else None
    return isinstance(current_url, str) and _validate_browser_url(current_url)[1] is None


def _reap_idle_browser_sessions() -> None:
    while True:
        time.sleep(60)
        cutoff = time.monotonic() - _SESSION_IDLE_TIMEOUT
        with _SESSION_LOCK:
            expired = [
                (task_id, session)
                for task_id, session in _SESSIONS.items()
                if session.last_used < cutoff
            ]
            for task_id, _ in expired:
                _SESSIONS.pop(task_id, None)
        for _, session in expired:
            session.close()


threading.Thread(target=_reap_idle_browser_sessions, name="browser-session-reaper", daemon=True).start()


def _run_browser(context: dict, *, allow_interaction: bool) -> dict:
    context = _apply_default_navigation(context)
    error = _validate_steps(context, allow_interaction=allow_interaction)
    if error:
        return error
    task_id = context.get("_task_id")
    try:
        if isinstance(task_id, int) and not isinstance(task_id, bool):
            with _SESSION_LOCK:
                session = _SESSIONS.get(task_id)
                if session is None:
                    session = _BrowserSession()
                    _SESSIONS[task_id] = session
                session.last_used = time.monotonic()
        else:
            session = _BrowserSession()
        return session.run(context["steps"])
    except ImportError:
        return _failure(
            "Playwright is not installed. Install backend requirements and the Chromium browser runtime.",
            "browser_unavailable",
        )
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            return _failure("The browser operation timed out.", "timeout")
        if "targetclosed" in type(exc).__name__.lower() or "browser" in str(exc).lower():
            if isinstance(task_id, int) and not isinstance(task_id, bool):
                close_browser_session(task_id)
            return _failure("The browser runtime is unavailable or closed unexpectedly.", "browser_unavailable")
        return _failure("The browser operation failed safely.", "browser_error")
    finally:
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            session.close()


def inspect_browser(context: dict) -> dict:
    return _run_browser(context, allow_interaction=False)


def interact_browser(context: dict) -> dict:
    return _run_browser(context, allow_interaction=True)


def _verify_browser_inspection(context: dict, observed: dict) -> dict:
    steps = context.get("steps", [])
    observations = observed.get("observations")
    valid = bool(
        observed.get("success") is True
        and isinstance(observed.get("evidence_text"), str)
        and isinstance(steps, list)
        and isinstance(observations, list)
        and len(steps) == len(observations)
        and all(
            isinstance(step, dict)
            and isinstance(item, dict)
            and item.get("action") == step.get("action")
            for step, item in zip(steps, observations)
        )
    )
    return {
        "verified": valid,
        "actual": {"success": valid, "observation_count": len(observations) if isinstance(observations, list) else 0},
        "reason": (
            "Browser output matches the requested action sequence; page content remains external evidence."
            if valid else "Browser output did not match the requested action sequence."
        ),
    }


def _register_browser_tools(register_tool) -> None:
    step_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_INTERACTION_ACTIONS)},
            "url": {"type": "string", "maxLength": 2048},
            "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
            "value": {"type": "string", "maxLength": 10_000},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": step_schema,
                "minItems": 1,
                "maxItems": MAX_BROWSER_STEPS,
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }
    register_tool(
        "browser.inspect", inspect_browser,
        description="Navigate and inspect the current task's browser page on configured HTTPS hosts or local development servers. A navigate step may omit its URL to start at the configured default site. Successful observations trigger replanning.",
        inputs={"steps": "Bounded sequence of navigate, inspect, and extract actions."},
        input_schema=schema,
        outputs=["observations", "evidence_text", "evidence_text_truncated"],
        permissions=["web:read"],
        errors=["unauthorized_host", "invalid_url", "validation_error", "browser_unavailable", "timeout", "browser_error"],
        timeout_seconds=30,
        verification="Validate that browser output matches the requested actions; page content remains external evidence.",
        verifier=_verify_browser_inspection,
    )
    register_tool(
        "browser.interact", interact_browser,
        description="Fill form fields and click controls in the current task's browser session on configured HTTPS hosts or local development servers after human approval. Successful actions trigger replanning.",
        inputs={"steps": "Bounded sequence of navigation, inspection, extraction, field-fill, and click actions."},
        input_schema=schema,
        outputs=["observations", "evidence_text", "evidence_text_truncated"],
        permissions=["web:interact"],
        errors=["unauthorized_host", "invalid_url", "validation_error", "approval_required", "browser_unavailable", "timeout", "browser_error"],
        timeout_seconds=30,
        requires_approval=True,
        side_effect=True,
        risk={
            "impact": "high",
            "reversibility": "irreversible",
            "data_sensitivity": "business",
            "external_consequences": "material",
            "autonomy_limits": {"authorized_hosts_only": True, "downloads_disabled": True},
        },
        expected_consequences="Interacts with controls on an external business website.",
        verification="Validate the interaction result shape; inspect the page afterward to verify the resulting website state.",
        verifier=_verify_browser_inspection,
    )


register_browser_tools = _register_browser_tools
