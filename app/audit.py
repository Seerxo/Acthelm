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
    remote = ctx.get("remote_stage_c") if isinstance(ctx, dict) else None
    if isinstance(remote, dict) and "packet" in remote:
        # B5: the released transfer text is bound by its digest; audit rows never copy it (app/stage_c_handoff.py).
        out["context"] = {**ctx, "remote_stage_c": {k: v for k, v in remote.items() if k != "packet"}}
    return out


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
