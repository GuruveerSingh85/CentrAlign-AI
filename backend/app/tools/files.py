from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
MAX_FILE_BYTES = 1_000_000
MAX_RESULTS = 500


def _authorized_roots() -> list[Path]:
    configured = os.getenv("AUTHORIZED_FILE_ROOTS")
    roots = [
        Path(item.strip()).expanduser().resolve()
        for item in configured.split(os.pathsep)
        if item.strip()
    ] if configured else [DATA_ROOT.resolve()]
    return [root for root in roots if root.is_dir()]


def _resolve_path(relative_path: str, *, allow_root: bool = False) -> tuple[Path | None, dict | None]:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None, _failure("The path must stay within an authorized file root.", "unauthorized_path")

    for root in _authorized_roots():
        resolved = (root / candidate).resolve()
        if resolved == root and allow_root:
            return resolved, None
        if resolved != root and resolved.is_relative_to(root):
            return resolved, None
    return None, _failure("The path is outside configured authorized file roots.", "unauthorized_path")


def _relative_to_root(path: Path) -> str | None:
    resolved = path.resolve()
    for root in _authorized_roots():
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    return None


def _failure(message: str, failure_type: str) -> dict:
    return {"success": False, "error": message, "failure_type": failure_type}


def _classify_os_error(exc: OSError) -> dict:
    if exc.errno in {errno.EAGAIN, errno.EINTR, errno.EBUSY}:
        return _failure("The file operation was temporarily unavailable.", "transient_io_error")
    return _failure("The authorized file operation failed.", "file_error")


def _read_authorized_bytes(relative_path: str) -> tuple[bytes | None, dict | None]:
    path, error = _resolve_path(relative_path)
    if error:
        return None, error
    try:
        if not path.is_file():
            return None, _failure("The authorized file does not exist.", "file_not_found")
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, _failure("The file exceeds the 1 MB processing limit.", "file_too_large")
        return path.read_bytes(), None
    except OSError as exc:
        return None, _classify_os_error(exc)


def _read_text(relative_path: str) -> tuple[str | None, dict | None]:
    data, error = _read_authorized_bytes(relative_path)
    if error:
        return None, error
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, _failure("The file is not valid UTF-8 text.", "unsupported_file_format")


def list_files(context: dict) -> dict:
    relative_path = context.get("path", "")
    directory, error = _resolve_path(relative_path, allow_root=True)
    if error:
        return error
    if not directory.is_dir():
        return _failure("The authorized path is not a directory.", "not_a_directory")

    recursive = context.get("recursive", False)
    listed = []
    try:
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        for candidate in iterator:
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved.is_relative_to(directory.resolve()):
                    listed.append(resolved.relative_to(directory.resolve()).as_posix())
                if len(listed) >= MAX_RESULTS:
                    break
    except OSError as exc:
        return _classify_os_error(exc)
    return {"success": True, "path": relative_path, "files": sorted(listed)}


def read_file(context: dict) -> dict:
    content, error = _read_text(context["path"])
    if error:
        return error
    return {
        "success": True,
        "path": context["path"],
        "content": content,
        "size_bytes": len(content.encode("utf-8")),
    }


def search_files(context: dict) -> dict:
    query = context["query"].casefold()
    root_name = context.get("path", "")
    directory, error = _resolve_path(root_name, allow_root=True)
    if error:
        return error
    if not directory.is_dir():
        return _failure("The authorized path is not a directory.", "not_a_directory")

    results = []
    try:
        for candidate in directory.rglob("*"):
            if not candidate.is_file() or not candidate.resolve().is_relative_to(directory.resolve()):
                continue
            relative_path = _relative_to_root(candidate)
            if relative_path is None:
                continue
            text, read_error = _read_text(relative_path)
            if read_error:
                if read_error["failure_type"] in {"file_too_large", "unsupported_file_format"}:
                    continue
                return read_error
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line.casefold():
                    results.append({
                        "path": relative_path,
                        "line": line_number,
                        "snippet": line[:500],
                    })
                    if len(results) >= 100:
                        return {"success": True, "query": context["query"], "matches": results}
    except OSError as exc:
        return _classify_os_error(exc)
    return {"success": True, "query": context["query"], "matches": results}


def create_file(context: dict) -> dict:
    path, error = _resolve_path(context["path"])
    if error:
        return error
    content = context["content"]
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _failure("Content exceeds the 1 MB file limit.", "file_too_large")
    if not path.parent.is_dir():
        return _failure("The parent directory must already exist inside an authorized root.", "directory_not_found")
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except FileExistsError:
        return _failure("The file already exists; use the edit operation instead.", "file_already_exists")
    except OSError as exc:
        return _classify_os_error(exc)
    return {
        "success": True,
        "path": context["path"],
        "size_bytes": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def edit_file(context: dict) -> dict:
    original, error = _read_text(context["path"])
    if error:
        return error
    old_text = context["old_text"]
    if not old_text or original.count(old_text) != 1:
        return _failure("The expected text must occur exactly once; no edit was made.", "edit_conflict")
    updated = original.replace(old_text, context["new_text"], 1)
    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        return _failure("Edited content exceeds the 1 MB file limit.", "file_too_large")
    path, error = _resolve_path(context["path"])
    if error:
        return error
    temporary_path = path.with_name(f".{path.name}.centr-align.tmp")
    try:
        if path.read_text(encoding="utf-8") != original:
            return _failure("The file changed during editing; no edit was made.", "edit_conflict")
        temporary_path.write_text(updated, encoding="utf-8")
        temporary_path.replace(path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return _classify_os_error(exc)
    return {
        "success": True,
        "path": context["path"],
        "size_bytes": len(updated.encode("utf-8")),
        "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
    }


def _verify_read(context: dict, observed: dict) -> dict:
    actual = read_file(context)
    return {"verified": bool(actual.get("success") and actual.get("content") == observed.get("content")), "actual": actual}


def _verify_listing(context: dict, observed: dict) -> dict:
    actual = list_files(context)
    return {"verified": bool(actual.get("success") and actual.get("files") == observed.get("files")), "actual": actual}


def _verify_search(context: dict, observed: dict) -> dict:
    actual = search_files(context)
    return {"verified": bool(actual.get("success") and actual.get("matches") == observed.get("matches")), "actual": actual}


def _verify_written_file(context: dict, observed: dict) -> dict:
    content, error = _read_text(context["path"])
    actual = error or {
        "success": True,
        "path": context["path"],
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    return {"verified": bool(actual.get("success") and actual.get("sha256") == observed.get("sha256")), "actual": actual}


def register_file_tools(register_tool) -> None:
    register_tool(
        "files.list", list_files,
        description="List text files in an authorized file root.",
        inputs={"path": "Optional authorized directory path relative to a configured root.", "recursive": "Whether to include nested files."},
        input_schema={"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}}, "additionalProperties": False},
        outputs=["path", "files"], permissions=["files:read"], errors=["unauthorized_path", "not_a_directory", "transient_io_error", "file_error"],
        timeout_seconds=5, verifier=_verify_listing, verification="Repeat the bounded authorized directory listing.",
    )
    register_tool(
        "files.read", read_file,
        description="Read a UTF-8 text file from an authorized file root.",
        inputs={"path": "Required authorized file path relative to a configured root."},
        input_schema={"type": "object", "properties": {"path": {"type": "string", "minLength": 1}}, "required": ["path"], "additionalProperties": False},
        outputs=["path", "content", "size_bytes"], permissions=["files:read"], errors=["unauthorized_path", "file_not_found", "file_too_large", "unsupported_file_format", "transient_io_error", "file_error"],
        timeout_seconds=5, verifier=_verify_read, verification="Read the authorized file again and compare its content.",
    )
    register_tool(
        "files.search", search_files,
        description="Search line-by-line across authorized UTF-8 text files.",
        inputs={"query": "Required text to find.", "path": "Optional authorized directory path relative to a configured root."},
        input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
        outputs=["query", "matches"], permissions=["files:read"], errors=["unauthorized_path", "file_too_large", "unsupported_file_format", "transient_io_error", "file_error"],
        timeout_seconds=10, verifier=_verify_search, verification="Repeat the same bounded authorized text search.",
    )
    register_tool(
        "files.create", create_file,
        description="Create a new UTF-8 text file within an authorized root without overwriting existing files.",
        inputs={"path": "Required new file path relative to a configured root.", "content": "Required UTF-8 text to write."},
        input_schema={"type": "object", "properties": {"path": {"type": "string", "minLength": 1}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False},
        outputs=["path", "size_bytes", "sha256"], permissions=["files:write"], errors=["unauthorized_path", "file_already_exists", "directory_not_found", "file_too_large", "transient_io_error", "file_error"],
        side_effect=True, permission_gated_side_effect=True, timeout_seconds=5,
        risk={"impact": "low", "reversibility": "reversible", "data_sensitivity": "internal", "external_consequences": "none", "autonomy_limits": {"max_bytes": MAX_FILE_BYTES, "authorized_roots_only": True}},
        verifier=_verify_written_file, verification="Read the created file and compare its SHA-256 digest.",
    )
    register_tool(
        "files.edit", edit_file,
        description="Replace one exact, unique text occurrence in an authorized UTF-8 file.",
        inputs={"path": "Required authorized file path.", "old_text": "Exact text that must occur once.", "new_text": "Replacement text."},
        input_schema={"type": "object", "properties": {"path": {"type": "string", "minLength": 1}, "old_text": {"type": "string", "minLength": 1}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False},
        outputs=["path", "size_bytes", "sha256"], permissions=["files:write"], errors=["unauthorized_path", "file_not_found", "edit_conflict", "file_too_large", "transient_io_error", "file_error"],
        side_effect=True, permission_gated_side_effect=True, timeout_seconds=5,
        risk={"impact": "low", "reversibility": "reversible", "data_sensitivity": "internal", "external_consequences": "none", "autonomy_limits": {"max_bytes": MAX_FILE_BYTES, "authorized_roots_only": True, "exact_unique_match": True}},
        verifier=_verify_written_file, verification="Read the edited file and compare its SHA-256 digest.",
    )