import threading
import os
from functools import wraps

_task_lock = threading.Lock()
_scheduled_tasks: set[int] = set()
_state_locks: dict[int, threading.RLock] = {}


def serialized_task_state(function):
    @wraps(function)
    def wrapped(task_id, *args, **kwargs):
        with _task_lock:
            lock = _state_locks.setdefault(task_id, threading.RLock())
        with lock:
            return function(task_id, *args, **kwargs)
    return wrapped

MAX_SAFE_RETRIES = 2
MAX_WORKFLOW_STEPS = max(1, int(os.getenv("MAX_WORKFLOW_STEPS", "50")))
MAX_WORKFLOW_REPLANS = max(0, int(os.getenv("MAX_WORKFLOW_REPLANS", "2")))
MAX_PARALLEL_STEPS = max(1, int(os.getenv("MAX_PARALLEL_STEPS", "4")))
WORKFLOW_TIMEOUT_SECONDS = max(1, int(os.getenv("WORKFLOW_TIMEOUT_SECONDS", "300")))
DEFAULT_TOOL_TIMEOUT_SECONDS = max(1, int(os.getenv("TOOL_TIMEOUT_SECONDS", "60")))
SAFE_RETRY_CAPABILITIES = frozenset({
    "read_company_policy",
    "check_policy",
    "check_vendor_duplicate",
    "documents.summarize_text",
    "documents.extract_structured_data",
    "web.fetch_page",
})
TRANSIENT_FAILURE_TYPES = frozenset({
    "transient_io_error",
    "transient_database_error",
    "timeout",
    "rate_limited",
    "service_unavailable",
})
