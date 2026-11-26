"""Audit trail helper (§86, §115). Every mutating operation records who, what, why,
which task, when, and the previous state."""
from typing import Any

from sqlalchemy.orm import Session

from app import models

_SKIP = {"_sa_instance_state"}


def snapshot(obj: Any) -> dict:
    """Serialisable view of a model row, for the before/after columns."""
    if obj is None:
        return {}
    out = {}
    for k, v in vars(obj).items():
        if k in _SKIP:
            continue
        out[k] = v.isoformat() if hasattr(v, "isoformat") else (v.value if hasattr(v, "value") else v)
    ctx = out.get("context")
    if isinstance(ctx, dict) and "remote_stage_c" in ctx:
        # B5: the released transfer text is bound by its digest; audit rows never copy it, at any depth (a renewal
        # keeps the superseded record nested under it) (app/stage_c_handoff.py).
        out["context"] = {**ctx, "remote_stage_c": _without_packets(ctx["remote_stage_c"])}
    return out


def _without_packets(value):
    if isinstance(value, dict):
        return {k: _without_packets(v) for k, v in value.items() if k != "packet"}
    if isinstance(value, list):
        return [_without_packets(v) for v in value]
    return value


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_key: str | None = None,
    reason: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    task_id: int | None = None,
) -> models.AuditLog:
    entry = models.AuditLog(
        actor=actor, action=action, entity_type=entity_type, entity_key=entity_key,
        reason=reason, before=before, after=after, task_id=task_id,
    )
    db.add(entry)
    return entry
