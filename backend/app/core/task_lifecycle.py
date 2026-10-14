TASK_STATUSES = frozenset({
    "pending",
    "planned",
    "waiting_for_input",
    "needs_input",
    "running",
    "processing",
    "executed",
    "waiting_for_approval",
    "awaiting_approval",
    "partially_completed",
    "completed",
    "failed",
    "cancelled",
    "verified",
    "rejected",
    "unsupported",
    "approval_expired",
})

_ACTIVE = {
    "running",
    "processing",
    "waiting_for_input",
    "needs_input",
    "waiting_for_approval",
    "awaiting_approval",
    "partially_completed",
}
TASK_TRANSITIONS = {
    "pending": {"planned", "waiting_for_input", "needs_input", "running", "failed", "cancelled", "unsupported"},
    "planned": {"planned", "waiting_for_input", "needs_input", "running", "failed", "cancelled", "unsupported"},
    "waiting_for_input": {"planned", "waiting_for_input", "needs_input", "running", "failed", "partially_completed", "cancelled", "unsupported"},
    "needs_input": {"planned", "waiting_for_input", "needs_input", "running", "failed", "partially_completed", "cancelled", "unsupported"},
    "running": _ACTIVE | {"completed", "executed", "failed", "cancelled", "unsupported", "approval_expired"},
    "processing": _ACTIVE | {"completed", "executed", "failed", "cancelled", "unsupported", "approval_expired"},
    "waiting_for_input": _ACTIVE | {"completed", "executed", "failed", "partially_completed", "cancelled", "unsupported"},
    "needs_input": _ACTIVE | {"completed", "executed", "failed", "partially_completed", "cancelled", "unsupported"},
    "waiting_for_approval": _ACTIVE | {"completed", "executed", "failed", "cancelled", "rejected", "approval_expired"},
    "awaiting_approval": _ACTIVE | {"completed", "executed", "failed", "cancelled", "rejected", "approval_expired"},
    "partially_completed": _ACTIVE | {"completed", "executed", "failed", "cancelled", "unsupported"},
    "failed": {"running", "processing", "partially_completed"},
    "approval_expired": set(),
    "completed": set(),
    "cancelled": set(),
    "verified": set(),
    "rejected": set(),
    "unsupported": set(),
}


def validate_task_transition(previous: str, current: str) -> None:
    if current not in TASK_STATUSES:
        raise ValueError(f"Unknown task status: {current!r}.")
    if previous == current:
        return
    if previous not in TASK_TRANSITIONS or current not in TASK_TRANSITIONS[previous]:
        raise ValueError(f"Invalid task status transition: {previous!r} -> {current!r}.")