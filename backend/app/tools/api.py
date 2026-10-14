from __future__ import annotations

import json
import hashlib
import os
import threading
import time
from urllib.parse import unquote, urlparse, urlunparse

import httpx

from app.tools.files import _failure

MAX_RESPONSE_BYTES = 1_000_000
MAX_TIMEOUT_SECONDS = 60
_rate_limit_lock = threading.Lock()
_last_request_by_host: dict[str, float] = {}


def _allowed_hosts() -> set[str]:
    return {
        host.strip().lower().rstrip(".")
        for host in os.getenv("BUSINESS_API_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }


def _configuration() -> tuple[dict | None, dict | None]:
    raw = os.getenv("BUSINESS_API_CONNECTORS", "")
    if not raw.strip():
        return None, _failure("No business API connectors are configured.", "configuration_missing")
    try:
        connectors = json.loads(raw)
    except json.JSONDecodeError:
        return None, _failure("Business API connector configuration is invalid.", "configuration_error")
    if not isinstance(connectors, dict):
        return None, _failure("Business API connector configuration must be an object.", "configuration_error")
    return connectors, None


def _validated_endpoint(connector_name: str, operation_name: str, *, mutation: bool) -> tuple[dict | None, dict | None]:
    from app.tools.registry import _schema_definition_error

    connectors, error = _configuration()
    if error:
        return None, error
    connector = connectors.get(connector_name)
    if not isinstance(connector, dict):
        return None, _failure("The named business API connector is not configured.", "configuration_missing")
    endpoints = connector.get("endpoints")
    endpoint = endpoints.get(operation_name) if isinstance(endpoints, dict) else None
    if not isinstance(endpoint, dict):
        return None, _failure("The named business API operation is not configured.", "configuration_missing")

    method = endpoint.get("method")
    allowed_methods = {"POST", "PUT", "PATCH", "DELETE"} if mutation else {"GET", "HEAD"}
    if not isinstance(method, str) or method.upper() not in allowed_methods:
        return None, _failure("The configured operation is not permitted by this tool.", "configuration_error")

    base_url = connector.get("base_url")
    endpoint_path = endpoint.get("path")
    try:
        parsed_base = urlparse(base_url)
        host = (parsed_base.hostname or "").lower().rstrip(".")
        port = parsed_base.port
    except (TypeError, ValueError):
        return None, _failure("The connector base URL is invalid.", "configuration_error")
    if (
        parsed_base.scheme != "https"
        or not host
        or parsed_base.username
        or parsed_base.password
        or port not in (None, 443)
        or parsed_base.query
        or parsed_base.fragment
    ):
        return None, _failure("Business API base URLs must be credential-free HTTPS URLs on port 443.", "configuration_error")
    if host not in _allowed_hosts():
        return None, _failure("The connector host is not in BUSINESS_API_ALLOWED_HOSTS.", "unauthorized_host")
    if not isinstance(endpoint_path, str) or not endpoint_path.startswith("/"):
        return None, _failure("Configured operation paths must begin with '/'.", "configuration_error")
    decoded_segments = unquote(endpoint_path).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments) or "?" in endpoint_path or "#" in endpoint_path:
        return None, _failure("Configured operation paths cannot change the connector origin or path scope.", "configuration_error")

    schema = endpoint.get("input_schema", {
        "type": "object", "properties": {}, "additionalProperties": False,
    })
    schema_error = _schema_definition_error(schema)
    if schema_error or schema.get("type") != "object":
        return None, _failure("The configured operation input schema is invalid.", "configuration_error")
    timeout = endpoint.get("timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        return None, _failure("Configured operation timeouts must be between 0 and 60 seconds.", "configuration_error")

    origin = urlunparse((parsed_base.scheme, parsed_base.netloc, "", "", "", ""))
    base_path = parsed_base.path.rstrip("/")
    operation_url = origin + base_path + endpoint_path
    return {
        "method": method.upper(),
        "url": operation_url,
        "schema": schema,
        "timeout": float(timeout),
        "auth_env": connector.get("auth_env"),
        "auth_header": connector.get("auth_header", "Authorization"),
        "auth_scheme": connector.get("auth_scheme", "Bearer"),
    }, None


def _wait_for_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    with _rate_limit_lock:
        now = time.monotonic()
        delay = max(0.0, 0.25 - (now - _last_request_by_host.get(host, 0.0)))
        if delay:
            time.sleep(delay)
        _last_request_by_host[host] = time.monotonic()


def _call(context: dict, *, mutation: bool) -> dict:
    from app.tools.registry import _schema_error

    endpoint, error = _validated_endpoint(
        context["connector"], context["operation"], mutation=mutation,
    )
    if error:
        return error
    parameters = context.get("inputs", {})
    validation_error = _schema_error(parameters, endpoint["schema"], "inputs")
    if validation_error:
        return _failure(validation_error, "validation_error")

    headers = {"Accept": "application/json, text/plain"}
    auth_env = endpoint["auth_env"]
    if auth_env:
        if not isinstance(auth_env, str) or not auth_env:
            return _failure("The connector authentication configuration is invalid.", "configuration_error")
        token = os.getenv(auth_env)
        if not token:
            return _failure("The configured API credential is unavailable.", "configuration_missing")
        header_name = endpoint["auth_header"]
        scheme = endpoint["auth_scheme"]
        if not isinstance(header_name, str) or not header_name or "\r" in header_name or "\n" in header_name:
            return _failure("The connector authentication header is invalid.", "configuration_error")
        if not isinstance(scheme, str) or "\r" in scheme or "\n" in scheme:
            return _failure("The connector authentication scheme is invalid.", "configuration_error")
        headers[header_name] = f"{scheme} {token}".strip()

    _wait_for_host(endpoint["url"])
    try:
        with httpx.Client(
            timeout=httpx.Timeout(endpoint["timeout"]),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            request_arguments = {"params": parameters} if not mutation else {"json": parameters}
            with client.stream(endpoint["method"], endpoint["url"], **request_arguments) as response:
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        retry_seconds = max(0, min(int(retry_after), 3600))
                    except ValueError:
                        retry_seconds = None
                    return {
                        **_failure("The business API rate limit was reached.", "rate_limited"),
                        "retry_after_seconds": retry_seconds,
                    }
                if response.status_code >= 500:
                    return _failure("The business API is temporarily unavailable.", "service_unavailable")
                if not 200 <= response.status_code < 300:
                    return _failure(f"The business API returned HTTP {response.status_code}.", "http_error")
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
                    return _failure("The API response exceeds the 1 MB processing limit.", "response_too_large")
                chunks = []
                total_bytes = 0
                for chunk in response.iter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > MAX_RESPONSE_BYTES:
                        return _failure("The API response exceeds the 1 MB processing limit.", "response_too_large")
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = response.headers.get("content-type", "").lower()
                if not body:
                    data = None
                elif "json" in content_type:
                    try:
                        data = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return _failure("The business API returned invalid JSON.", "invalid_response")
                else:
                    data = body.decode("utf-8", errors="replace")
                return {
                    "success": True,
                    "connector": context["connector"],
                    "operation": context["operation"],
                    "status_code": response.status_code,
                    "data": data,
                }
    except httpx.TimeoutException:
        return _failure("The business API request timed out.", "timeout")
    except httpx.NetworkError:
        return _failure("The business API could not be reached.", "service_unavailable")
    except httpx.HTTPError:
        return _failure("The business API request failed.", "service_unavailable")


def call_read_api(context: dict) -> dict:
    return _call(context, mutation=False)


def call_write_api(context: dict) -> dict:
    return _call(context, mutation=True)


def _value_digest(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _verify_api_read(context: dict, observed: dict) -> dict:
    actual = call_read_api(context)
    if not actual.get("success"):
        return {
            "verified": False,
            "actual": {"success": False, "failure_type": actual.get("failure_type")},
        }
    try:
        observed_digest = _value_digest(observed.get("data"))
        actual_digest = _value_digest(actual.get("data"))
    except (TypeError, ValueError):
        return {"verified": False, "reason": "The API response could not be safely fingerprinted."}
    return {
        "verified": actual.get("status_code") == observed.get("status_code") and actual_digest == observed_digest,
        "actual": {"success": True, "status_code": actual.get("status_code"), "data_sha256": actual_digest},
    }


def configured_operations(*, mutation: bool) -> list[dict]:
    connectors, error = _configuration()
    if error:
        return []
    method_set = {"POST", "PUT", "PATCH", "DELETE"} if mutation else {"GET", "HEAD"}
    operations = []
    for connector_name, connector in connectors.items():
        endpoints = connector.get("endpoints") if isinstance(connector, dict) else None
        if not isinstance(endpoints, dict):
            continue
        for operation_name, endpoint in endpoints.items():
            if not isinstance(endpoint, dict):
                continue
            method = endpoint.get("method")
            if not isinstance(method, str) or method.upper() not in method_set:
                continue
            validated, validation_error = _validated_endpoint(
                connector_name, operation_name, mutation=mutation,
            )
            if validation_error:
                continue
            operations.append({
                "connector": connector_name,
                "operation": operation_name,
                "method": validated["method"],
                "input_schema": validated["schema"],
            })
    return operations


def register_api_tools(register_tool) -> None:
    common_schema = {
        "type": "object",
        "properties": {
            "connector": {"type": "string", "minLength": 1, "maxLength": 80},
            "operation": {"type": "string", "minLength": 1, "maxLength": 80},
            "inputs": {"type": "object"},
        },
        "required": ["connector", "operation"],
        "additionalProperties": False,
    }
    register_tool(
        "api.read", call_read_api,
        description="Call a configured, allowlisted company API read operation using its validated input schema.",
        inputs={"connector": "Configured connector name.", "operation": "Configured read operation name.", "inputs": "Operation-specific validated parameters."},
        input_schema=common_schema,
        outputs=["connector", "operation", "status_code", "data"],
        permissions=["api:read"],
        errors=["configuration_missing", "configuration_error", "unauthorized_host", "validation_error", "timeout", "rate_limited", "service_unavailable", "http_error", "response_too_large", "invalid_response"],
        timeout_seconds=MAX_TIMEOUT_SECONDS,
        verification="Repeat the safe API read and compare a SHA-256 fingerprint of its structured response.",
        verifier=_verify_api_read,
    )
    register_tool(
        "api.write", call_write_api,
        description="Call a configured, allowlisted company API mutation after explicit human approval.",
        inputs={"connector": "Configured connector name.", "operation": "Configured mutation operation name.", "inputs": "Operation-specific validated parameters."},
        input_schema=common_schema,
        outputs=["connector", "operation", "status_code", "data"],
        permissions=["api:write"],
        errors=["configuration_missing", "configuration_error", "unauthorized_host", "validation_error", "approval_required", "timeout", "rate_limited", "service_unavailable", "http_error", "response_too_large", "invalid_response"],
        timeout_seconds=MAX_TIMEOUT_SECONDS,
        requires_approval=True,
        side_effect=True,
        risk={
            "impact": "high",
            "reversibility": "irreversible",
            "data_sensitivity": "business",
            "external_consequences": "material",
            "autonomy_limits": {"configured_operations_only": True, "authorized_hosts_only": True},
        },
        expected_consequences="Sends a configured mutation request to an external company system.",
        verification="A successful response confirms API acceptance only; verify persisted effects through the system's read API or human review.",
    )