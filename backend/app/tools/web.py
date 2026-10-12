from __future__ import annotations

import hashlib
import ipaddress
import os
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.tools.files import _failure

MAX_RESPONSE_BYTES = 1_000_000
MAX_EXTRACTED_CHARS = 50_000
MAX_SOURCE_LINKS = 100
_rate_lock = threading.Lock()
_last_request_by_host: dict[str, float] = {}


def _allowed_hosts() -> set[str]:
    return {
        host.strip().lower().rstrip(".")
        for host in os.getenv("WEB_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }


def _validate_url(url: str) -> tuple[str | None, dict | None]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None, _failure("The URL is invalid.", "invalid_url")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in (None, 443):
        return None, _failure("Only credential-free HTTPS URLs on port 443 are supported.", "invalid_url")
    if host not in _allowed_hosts():
        return None, _failure("The website host is not in WEB_ALLOWED_HOSTS.", "unauthorized_host")
    try:
        if not ipaddress.ip_address(host).is_global:
            return None, _failure("Non-public IP addresses are not allowed.", "unauthorized_host")
    except ValueError:
        pass
    return host, None


def _wait_for_host_rate_limit(host: str) -> None:
    with _rate_lock:
        now = time.monotonic()
        delay = max(0.0, 0.5 - (now - _last_request_by_host.get(host, 0.0)))
        if delay:
            time.sleep(delay)
        _last_request_by_host[host] = time.monotonic()


class _PageExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "a" and len(self.links) < MAX_SOURCE_LINKS:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.text_parts.append(data.strip())


def _extract_page(body: bytes, content_type: str, final_url: str) -> tuple[str | None, list[str], dict | None]:
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None, [], _failure("The page encoding could not be decoded.", "unsupported_content_type")
    if "text/html" not in content_type.lower():
        if "text/plain" not in content_type.lower():
            return None, [], _failure("Only HTML and plain-text pages are supported.", "unsupported_content_type")
        return text[:MAX_EXTRACTED_CHARS], [], None

    parser = _PageExtractor()
    try:
        parser.feed(text)
    except Exception:
        return None, [], _failure("The HTML page could not be parsed.", "invalid_page")
    page_text = " ".join(parser.text_parts)
    links = []
    for href in parser.links:
        absolute = urljoin(final_url, href)
        if urlparse(absolute).scheme in {"http", "https"} and absolute not in links:
            links.append(absolute)
    return page_text[:MAX_EXTRACTED_CHARS], links, None


def fetch_page(context: dict) -> dict:
    target = context["url"]
    redirect_count = 0
    while True:
        host, error = _validate_url(target)
        if error:
            return error
        _wait_for_host_rate_limit(host)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "CentrAlign-Research/1.0", "Accept": "text/html,text/plain"},
            ) as client:
                with client.stream("GET", target) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= 3:
                            return _failure("The page redirect limit was reached.", "redirect_limit")
                        target = urljoin(target, location)
                        redirect_count += 1
                        continue
                    if response.status_code == 429:
                        return _failure("The website rate limit was reached.", "rate_limited")
                    if response.status_code >= 500:
                        return _failure("The website is temporarily unavailable.", "service_unavailable")
                    if response.status_code >= 400:
                        return _failure(f"The website returned HTTP {response.status_code}.", "http_error")

                    content_type = response.headers.get("content-type", "")
                    declared_size = response.headers.get("content-length")
                    if declared_size and declared_size.isdigit() and int(declared_size) > MAX_RESPONSE_BYTES:
                        return _failure("The page exceeds the 1 MB download limit.", "response_too_large")
                    chunks = []
                    total_bytes = 0
                    for chunk in response.iter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_RESPONSE_BYTES:
                            return _failure("The page exceeds the 1 MB download limit.", "response_too_large")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    final_url = str(response.url)
                    break
        except httpx.TimeoutException:
            return _failure("The website request timed out.", "timeout")
        except httpx.NetworkError:
            return _failure("The website could not be reached.", "service_unavailable")
        except httpx.HTTPError:
            return _failure("The website request failed.", "service_unavailable")

    page_text, links, error = _extract_page(body, content_type, final_url)
    if error:
        return error
    return {
        "success": True,
        "url": final_url,
        "source": final_url,
        "text": page_text,
        "links": links,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _verify_page(context: dict, observed: dict) -> dict:
    actual = fetch_page(context)
    return {
        "verified": bool(actual.get("success") and actual.get("sha256") == observed.get("sha256")),
        "actual": actual,
    }


def register_web_tools(register_tool) -> None:
    if not _allowed_hosts():
        return
    register_tool(
        "web.fetch_page", fetch_page,
        description="Retrieve and extract text and source links from an HTTPS page on an explicitly allowed website.",
        inputs={"url": "Required HTTPS URL on an administrator-allowlisted host."},
        input_schema={"type": "object", "properties": {"url": {"type": "string", "minLength": 1}}, "required": ["url"], "additionalProperties": False},
        outputs=["url", "source", "text", "links", "sha256"], permissions=["web:read"],
        errors=["invalid_url", "unauthorized_host", "timeout", "rate_limited", "service_unavailable", "http_error", "response_too_large", "unsupported_content_type"],
        timeout_seconds=12, verifier=_verify_page,
        verification="Fetch the same allowlisted page again and compare its content digest.",
    )