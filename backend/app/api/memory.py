from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import Session, SQLModel, select

from app.core.database import engine
from app.services.memory import (
    _ALLOWED_CATEGORIES,
    _ALLOWED_KINDS,
    _ALLOWED_VERIFICATION,
    _memory_is_stale,
    retrieve_context_safely,
    validate_memory_content,
)
from app.core.models import Memory

router = APIRouter(prefix="/api")


class MemoryCreate(SQLModel):
    category: str
    kind: str
    content: str
    memory_key: str | None = None
    source: str = "user"
    source_ref: str | None = None
    verification_status: str = "unverified"
    expires_at: datetime | None = None


class MemoryUpdate(SQLModel):
    category: str | None = None
    kind: str | None = None
    content: str | None = None
    memory_key: str | None = None
    source: str | None = None
    source_ref: str | None = None
    verification_status: str | None = None
    expires_at: datetime | None = None


def _memory_response(memory: Memory) -> dict:
    stale = _memory_is_stale(memory, datetime.now(timezone.utc))
    return {
        "id": memory.id,
        "category": memory.category,
        "kind": memory.kind,
        "content": memory.content,
        "memory_key": memory.memory_key,
        "source": memory.source,
        "source_ref": memory.source_ref,
        "verification_status": "stale" if stale else memory.verification_status,
        "stale": stale,
        "verified_at": memory.verified_at,
        "expires_at": memory.expires_at,
        "active": memory.active,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


def _validate_memory_fields(category: str, kind: str, status: str, content: str) -> None:
    if category not in _ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unsupported memory category.")
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(status_code=422, detail="Unsupported memory kind.")
    if status not in _ALLOWED_VERIFICATION:
        raise HTTPException(status_code=422, detail="Unsupported verification status.")
    try:
        validate_memory_content(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is None:
        return
    try:
        validate_memory_content(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: {exc}") from exc


@router.get("/memories")
def list_memories(request: Request):
    with Session(engine) as session:
        memories = session.exec(
            select(Memory)
            .where(Memory.owner_id == request.state.owner_id)
            .order_by(Memory.updated_at.desc())
        ).all()
        return [_memory_response(memory) for memory in memories]


@router.post("/memories", status_code=201)
def create_memory(payload: MemoryCreate, request: Request):
    _validate_memory_fields(
        payload.category,
        payload.kind,
        payload.verification_status,
        payload.content,
    )
    if payload.memory_key and len(payload.memory_key) > 128:
        raise HTTPException(status_code=422, detail="Memory key cannot exceed 128 characters.")
    if not payload.source.strip() or len(payload.source) > 100:
        raise HTTPException(status_code=422, detail="Memory source must be 1 to 100 characters.")
    _validate_optional_text(payload.source, "source")
    _validate_optional_text(payload.source_ref, "source reference")
    _validate_optional_text(payload.memory_key, "memory key")

    with Session(engine) as session:
        conflicts = []
        if payload.memory_key:
            conflicts = session.exec(
                select(Memory).where(
                    Memory.owner_id == request.state.owner_id,
                    Memory.category == payload.category,
                    Memory.memory_key == payload.memory_key,
                    Memory.active == True,  # noqa: E712
                )
            ).all()
        has_conflict = any(item.content != payload.content for item in conflicts)
        status = "conflicted" if has_conflict else payload.verification_status
        if has_conflict:
            for item in conflicts:
                item.verification_status = "conflicted"
                item.updated_at = datetime.now(timezone.utc)
                session.add(item)
        memory = Memory(
            owner_id=request.state.owner_id,
            category=payload.category,
            kind=payload.kind,
            content=payload.content.strip(),
            memory_key=payload.memory_key.strip() or None if payload.memory_key else None,
            source=payload.source.strip(),
            source_ref=payload.source_ref,
            verification_status=status,
            verified_at=(datetime.now(timezone.utc) if status == "verified" else None),
            expires_at=payload.expires_at,
        )
        session.add(memory)
        session.commit()
        session.refresh(memory)
        return _memory_response(memory)


@router.get("/memories/context")
def inspect_memory_context(request: Request, query: str):
    with Session(engine) as session:
        return retrieve_context_safely(session, request.state.owner_id, query)


@router.get("/memories/{memory_id}")
def get_memory(memory_id: int, request: Request):
    with Session(engine) as session:
        memory = session.get(Memory, memory_id)
        if memory is None or memory.owner_id != request.state.owner_id:
            raise HTTPException(status_code=404, detail="Memory not found.")
        return _memory_response(memory)


@router.patch("/memories/{memory_id}")
def update_memory(memory_id: int, payload: MemoryUpdate, request: Request):
    updates = payload.model_dump(exclude_unset=True)
    with Session(engine) as session:
        memory = session.get(Memory, memory_id)
        if memory is None or memory.owner_id != request.state.owner_id:
            raise HTTPException(status_code=404, detail="Memory not found.")
        values = {
            "category": updates.get("category", memory.category),
            "kind": updates.get("kind", memory.kind),
            "verification_status": updates.get("verification_status", memory.verification_status),
            "content": updates.get("content", memory.content),
        }
        _validate_memory_fields(
            values["category"],
            values["kind"],
            values["verification_status"],
            values["content"],
        )
        if "memory_key" in updates and updates["memory_key"] and len(updates["memory_key"]) > 128:
            raise HTTPException(status_code=422, detail="Memory key cannot exceed 128 characters.")
        if "source" in updates and (
            not isinstance(updates["source"], str)
            or not updates["source"].strip()
            or len(updates["source"]) > 100
        ):
            raise HTTPException(status_code=422, detail="Memory source must be 1 to 100 characters.")
        for field_name in ("memory_key", "source", "source_ref"):
            if field_name in updates:
                _validate_optional_text(updates[field_name], field_name.replace("_", " "))
        if updates.get("memory_key") is not None:
            updates["memory_key"] = updates["memory_key"].strip() or None
        for key, value in updates.items():
            if key == "content":
                value = value.strip()
            setattr(memory, key, value)
        if memory.memory_key and any(
            field in updates for field in ("content", "memory_key", "category")
        ):
            peers = session.exec(
                select(Memory).where(
                    Memory.owner_id == memory.owner_id,
                    Memory.category == memory.category,
                    Memory.memory_key == memory.memory_key,
                    Memory.id != memory.id,
                    Memory.active == True,  # noqa: E712
                )
            ).all()
            differing = [peer for peer in peers if peer.content != memory.content]
            if differing and memory.verification_status != "verified":
                memory.verification_status = "conflicted"
                for peer in differing:
                    peer.verification_status = "conflicted"
                    peer.updated_at = datetime.now(timezone.utc)
                    session.add(peer)
        if memory.verification_status == "verified":
            memory.verified_at = datetime.now(timezone.utc)
            if memory.memory_key:
                superseded = session.exec(
                    select(Memory).where(
                        Memory.owner_id == memory.owner_id,
                        Memory.category == memory.category,
                        Memory.memory_key == memory.memory_key,
                        Memory.id != memory.id,
                        Memory.active == True,  # noqa: E712
                    )
                ).all()
                for older in superseded:
                    older.active = False
                    older.updated_at = datetime.now(timezone.utc)
                    session.add(older)
                else:
                    memory.verified_at = None
        memory.updated_at = datetime.now(timezone.utc)
        session.add(memory)
        session.commit()
        session.refresh(memory)
        return _memory_response(memory)


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: int, request: Request):
    with Session(engine) as session:
        memory = session.get(Memory, memory_id)
        if memory is None or memory.owner_id != request.state.owner_id:
            raise HTTPException(status_code=404, detail="Memory not found.")
        session.delete(memory)
        session.commit()