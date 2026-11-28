"""B5: production Desk -> isolated Stage C task handoff (artifacts/handoff-design-2026-11-09/HANDOFF_DESIGN.md).

Production pushes one immutable, founder-released packet to Stage C and polls a bounded, task-specific result
snapshot back. Stage C holds no production credential and never calls production. Off by default on both sides:

- sender (production):  ENABLE_STAGE_C_HANDOFF=true, STAGE_C_HANDOFF_URL=https://<stage-c host>,
                        COMPANY_API_TOKEN_HANDOFF (>= 32 chars). Production ENABLE_AUTONOMOUS_CODE_WRITE stays false:
                        the sender refuses to run in a process that has it on.
- receiver (Stage C):   ENABLE_STAGE_C_HANDOFF_RECEIVER=true, the same COMPANY_API_TOKEN_HANDOFF.
A process with both flags on, a missing/short token or a non-https origin is disabled (fail closed).

What crosses the boundary and what it may do:
- The packet imports one BACKLOG, LOW-risk developer task with write scope re-checked against Stage C's own allowed
  files. It carries no status, grant, run or context: coding still needs the founder's one-task authorization issued
  on Stage C from its own Access identity, bound to this handoff's id and digest (control.write_authorization_denial),
  and an imported task never falls back to a standing grant.
- Production re-verifies the founder release before every delivery (`delivery_denial`): founder audit row for exactly
  this packet, unchanged task text, not cancelled, not expired.
- Snapshots are authenticated with an HMAC (key derived from the shared handoff secret, never the raw bearer) over
  method, path and body, validated against a strict schema, and ordered by `seq` (Stage C's newest audit id touching
  the task, its handoff or the pilot grant): a lower seq is stale and dropped, an equal one is a duplicate. A snapshot
  never changes production's native task status; only the founder closes the task (DONE needs a fresh VERIFIED merge).
Every release, delivery, refusal, observation, import and cancellation is an audit row with ids, digests and codes
only, never packet text.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import approval_identity, audit, control, enums, keys, models, schemas

log = logging.getLogger("company.stage_c_handoff")

VERSION = 1
SERVICE = "service:handoff"          # approval_identity.from_caller("handoff"): a credential, never a person
TOKEN_ENV = "COMPANY_API_TOKEN_HANDOFF"
SENDER_FLAG, RECEIVER_FLAG = "ENABLE_STAGE_C_HANDOFF", "ENABLE_STAGE_C_HANDOFF_RECEIVER"
URL_ENV = "STAGE_C_HANDOFF_URL"
ACCESS_ID_ENV, ACCESS_SECRET_ENV = "STAGE_C_HANDOFF_ACCESS_CLIENT_ID", "STAGE_C_HANDOFF_ACCESS_CLIENT_SECRET"
MAC_HEADER = "x-acthelm-handoff-mac"
_MAC_LABEL = b"acthelm-b5-response-mac-v1"
KEY_PREFIX = "handoff:"
ORIGIN_PREFIX = "handoff-origin:"   # + production task key: serializes its imports (_origin)
API_PREFIX = "/stage-c/handoff-api/"
MAX_TTL = timedelta(hours=24)
MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES = 32 * 1024, 64 * 1024
TIMEOUT_S = 5.0          # per network operation (connect, each read)
DEADLINE_S = 15.0        # the whole request: a reply that drips bytes cannot hold a worker tick open
REQUESTS_PER_TICK = 3
MAX_DELIVERY_FAILURES = 5
POLL_EVERY = timedelta(seconds=60)
STALE_AFTER = timedelta(seconds=180)
# An activated import may be authorized or claimed only while production's confirmation is live on Stage C: the
# activation, then a renewal with every status poll (POLL_EVERY, about one worker tick), each valid for
# ACTIVATION_LEASE from the production time it was decided at (capped at Stage C's clock), never shortened. Production
# never decides a confirmation after it committed a cancel, so an unacknowledged cancel stops new grants and claims
# within ACTIVATION_LEASE (plus clock skew between the hosts), even if Stage C never hears from production again.
# Fail closed: without a live confirmation there is no grant and no claim.
ACTIVATION_LEASE = timedelta(minutes=5)
MAX_BACKOFF = timedelta(minutes=15)
RELEASABLE = (enums.TaskStatus.BACKLOG, enums.TaskStatus.READY, enums.TaskStatus.BLOCKED)
# A delivery refused for these reasons can never succeed with its packet: the founder's release ran out or the task
# changed after it. Only the founder's own new release replaces it (never automatically), built from the task as stored
# now, with a new id, digest and expiry, while production never recorded an activation of the old packet (refused
# before delivery, or after import but before activation) or, once activated, while the packet expired before the
# founder's first coding authorization on Stage C (_expired_unauthorized). Production never sends the old packet again,
# and Stage C retires the old import when the new packet for the same task arrives (import_packet) only if it was never
# activated, or was activated but expired on Stage C's clock without ever being authorized or run; anything else is
# live work and refused (ACTIVE_IMPORT). One open task per production task; the old import can never be activated.
RENEWABLE = ("RELEASE_EXPIRED", "TASK_CHANGED")
# What a renewal keeps of the record it supersedes: what _cancel, _restore_superseded, desk_view and a later renewal
# read. Never the packet (private task text; a restored record cannot be delivered again: delivery_denial refuses it).
SUPERSEDED_FIELDS = ("id", "payload_sha256", "source_sha256", "released_by", "released_at", "expires_at",
                     "remote_task_key", "delivery", "cancel_requested_at", "cancellation", "last_success_at")
HARD_STOP = (401, 403, 404, 409, 410, 422)   # founder-approved watchdog rule: auth/absence/conflict never retry
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SHA40, _SHA64, _KEY = r"^[0-9a-f]{40}$", r"^[0-9a-f]{64}$", r"^[A-Z]+-\d{1,6}$"
_CODE = r"^[A-Z0-9_]{1,40}$"


class HandoffError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


# --------------------------------------------------------------------------- configuration (fail closed)
def _on(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() == "true"


def _token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "")
    return token if len(token) >= 32 else None


def stage_c_origin() -> str | None:
    """The one configured Stage C origin: https, a host name, no credentials, path, query or fragment."""
    raw = os.environ.get(URL_ENV, "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    return f"https://{parts.hostname}" + (f":{port}" if port else "")


def sender_enabled() -> bool:
    return (_on(SENDER_FLAG) and not _on(RECEIVER_FLAG) and _token() is not None and stage_c_origin() is not None
            and os.environ.get("ENABLE_AUTONOMOUS_CODE_WRITE", "false").lower() != "true")


def receiver_enabled() -> bool:
    return _on(RECEIVER_FLAG) and not _on(SENDER_FLAG) and _token() is not None


# --------------------------------------------------------------------------- integrity
def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(packet: dict) -> str:
    """SHA-256 of every packet field but the digest itself. Integrity/idempotency binding, not authentication."""
    return hashlib.sha256(canonical({k: v for k, v in packet.items() if k != "payload_sha256"})).hexdigest()


def source_digest(task: models.Task) -> str:
    """The production task's own text at release: an edit afterwards voids the release (a new one is needed)."""
    ctx = task.context if isinstance(task.context, dict) else {}
    risk = task.risk_level.value if hasattr(task.risk_level, "value") else task.risk_level
    return hashlib.sha256(canonical({"title": task.title, "description": task.description,
                                     "objective": task.objective, "success_criteria": task.success_criteria,
                                     "stage_c": ctx.get("stage_c"), "write": ctx.get("write"), "risk": risk,
                                     "approval_required": task.approval_required,
                                     "agent": task.assigned_agent_id})).hexdigest()


def mac(method: str, path: str, body: bytes, token: str | None = None) -> str:
    token = token or _token()
    if token is None:
        raise HandoffError(403, "HANDOFF_DISABLED")
    key = hmac.new(token.encode(), _MAC_LABEL, hashlib.sha256).digest()
    return hmac.new(key, method.upper().encode() + b" " + path.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def _utc(value) -> datetime | None:
    at = control._utc(value) if isinstance(value, str) else value if isinstance(value, datetime) else None
    return at.astimezone(timezone.utc) if at is not None and at.tzinfo is not None else None


# --------------------------------------------------------------------------- wire schemas (unknown fields refused)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PacketTask(schemas.StageCTask):
    model_config = ConfigDict(extra="forbid")


class Packet(_Strict):
    version: Literal[1]
    id: str = Field(pattern=UUID_RE)
    origin: Literal["production"]
    origin_task_key: str = Field(pattern=_KEY, max_length=32)
    repository: Literal["seerxo/acthelm"]
    issued_at: datetime
    expires_at: datetime
    # Declared by production; Stage C imports only "LOW" / "code_change" and refuses anything else (import_packet).
    risk: str = Field(max_length=16)
    kind: str = Field(max_length=32)
    task: PacketTask
    payload_sha256: str = Field(pattern=_SHA64)


class Receipt(_Strict):
    version: Literal[1]
    id: str = Field(pattern=UUID_RE)
    payload_sha256: str = Field(pattern=_SHA64)
    task_key: str = Field(pattern=_KEY, max_length=32)
    status: enums.TaskStatus


class CancelAck(_Strict):
    id: str = Field(pattern=UUID_RE)
    cancellation: Literal["ACKNOWLEDGED"]
    task_key: str | None = Field(default=None, pattern=_KEY, max_length=32)


class ActivationAck(_Strict):
    id: str = Field(pattern=UUID_RE)
    activation: Literal["ACTIVATED"]
    task_key: str = Field(pattern=_KEY, max_length=32)


class _Runtime(_Strict):
    api_sha: str | None = Field(pattern=_SHA40)
    worker_sha: str | None = Field(pattern=_SHA40)
    write_enabled: bool | None


class _Grant(_Strict):
    status: Literal["NONE", "ACTIVE", "CONSUMED", "EXPIRED", "INVALID"]
    source_sha: str | None = Field(pattern=_SHA40)
    expires_at: datetime | None


class _PR(_Strict):
    number: int | None = Field(ge=1, le=9_999_999_999)
    head_sha: str | None = Field(pattern=_SHA40)


class _CI(_Strict):
    state: Literal["SUCCESS", "FAILURE", "PENDING", "MERGED_OBSERVED", "STALE"] | None
    sha: str | None = Field(pattern=_SHA40)


class _Step(_Strict):
    stage: str = Field(pattern=_CODE)
    at: datetime | None


class _RemoteTask(_Strict):
    status: enums.TaskStatus
    stage: str | None = Field(pattern=_CODE)
    test_verdict: Literal["PASSED", "FAILED"] | None
    reviewer_verdict: Literal["APPROVED", "UNVERIFIED", "REJECTED"] | None
    tested_fingerprint: str | None = Field(pattern=_SHA64)
    reviewed_fingerprint: str | None = Field(pattern=_SHA64)
    fingerprint_match: bool | None
    pr: _PR | None
    ci: _CI | None
    failure_category: str | None = Field(pattern=_CODE)
    needs_founder: bool | None
    history: list[_Step] = Field(max_length=12)


class _Merge(_Strict):
    state: Literal["NOT_OBSERVED", "VERIFIED"]
    commit: str | None = Field(pattern=_SHA40)
    at: datetime | None


class Snapshot(_Strict):
    version: Literal[1]
    id: str = Field(pattern=UUID_RE)
    payload_sha256: str = Field(pattern=_SHA64)
    task_key: str | None = Field(pattern=_KEY, max_length=32)
    seq: int = Field(ge=0)
    generated_at: datetime
    runtime: _Runtime
    authorization: _Grant
    task: _RemoteTask | None
    merge: _Merge
    cancellation: Literal["NONE", "ACKNOWLEDGED"]
    activation: Literal["IMPORTED", "ACTIVATED"] | None


# =========================================================================== production (sender)
def _remote(task: models.Task) -> dict | None:
    ctx = task.context if isinstance(task.context, dict) else {}
    remote = ctx.get("remote_stage_c")
    return remote if isinstance(remote, dict) and remote.get("origin") is None else None


def released(task: models.Task) -> bool:
    """A production task released to Stage C: production never runs, claims or auto-releases it."""
    return _remote(task) is not None


def _set_remote(task: models.Task, **changes) -> dict:
    remote = {**(_remote(task) or {}), **changes}
    task.context = {**(task.context or {}), "remote_stage_c": remote}
    return remote


def _eligible_text(db: Session, task: models.Task) -> dict:
    """The transfer text, built only from what is STORED on the task, after the server-side eligibility checks: a
    LOW-risk developer code task with no approval gate, a valid Stage C write scope and complete text."""
    from app import stage_c_worker   # late: stage_c_worker imports control, which imports this lazily
    agent = db.get(models.Agent, task.assigned_agent_id) if task.assigned_agent_id else None
    ctx = task.context if isinstance(task.context, dict) else {}
    scope = ctx.get("stage_c") if isinstance(ctx.get("stage_c"), dict) else None
    if (agent is None or agent.key != "developer" or ctx.get("write") is not True or scope is None
            or task.risk_level != enums.RiskLevel.LOW or task.approval_required):
        raise HandoffError(409, "NOT_ELIGIBLE")
    if stage_c_worker.scope_refused(scope, set(stage_c_worker.SAFE_FILES)):
        raise HandoffError(422, "SCOPE_REFUSED")
    if _open_dependencies(db, task):
        raise HandoffError(409, "DEPENDENCIES_OPEN")
    try:
        text = PacketTask(title=" ".join((task.title or "").split()), objective=task.objective or "",
                          success_criteria=task.success_criteria or "", paths=list(scope["paths"]),
                          context=list(scope.get("context", [])))
    except ValidationError:
        raise HandoffError(422, "TASK_INCOMPLETE")
    return text.model_dump()


def _open_dependencies(db: Session, task: models.Task) -> bool:
    """Stage C imports the task without its dependency records, so production holds it back until every task it
    depends on is DONE: at release (and renewal), and again at delivery and activation (delivery_denial)."""
    return db.scalar(select(func.count()).select_from(models.TaskDependency)
                     .join(models.Task, models.Task.id == models.TaskDependency.depends_on_id)
                     .where(models.TaskDependency.task_id == task.id,
                            models.Task.status != enums.TaskStatus.DONE)) > 0


def release(db: Session, task: models.Task, *, founder: str, now: datetime | None = None) -> dict:
    """The founder releases `task` as it is stored; the request names the task only, never its content. Idempotent:
    a released task returns its one release (and an explicit repeat restarts a stopped delivery of that exact packet).
    Caller handles commit failures; this commits on success."""
    if not approval_identity.is_founder_id(founder):
        raise PermissionError("only the verified founder may release a task to Stage C")
    if not sender_enabled():
        raise HandoffError(403, "HANDOFF_DISABLED")
    now = now or datetime.now(timezone.utc)
    db.refresh(task, with_for_update=True)
    remote = _remote(task)
    if remote is not None and remote.get("cancel_requested_at"):
        raise HandoffError(409, "CANCELLED")
    renew = (remote is not None and not remote.get("cancel_requested_at")
             and task.status == enums.TaskStatus.WAITING_EXTERNAL
             and ((remote.get("delivery") == "REFUSED" and remote.get("last_error_code") in RENEWABLE
                   and not _activation_recorded(db, task, remote.get("id")))
                  or _expired_unauthorized(task, remote, now)))
    if remote is not None and not renew:
        if remote.get("delivery") in ("SUSPENDED", "REFUSED"):   # explicit founder retry; delivery re-verifies
            _set_remote(task, delivery="PENDING", attempts=0, next_attempt_at=None, last_error_code=None)
            audit.record(db, actor=founder, action="handoff.retry", entity_type="task", entity_key=task.key,
                         task_id=task.id, after={"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256")})
        db.commit()
        remote = _remote(task)
        return {"id": remote["id"], "payload_sha256": remote["payload_sha256"], "delivery": remote["delivery"]}
    if not renew and task.status not in RELEASABLE:
        raise HandoffError(409, "TASK_NOT_RELEASABLE")
    text = _eligible_text(db, task)            # a renewal passes the same eligibility checks as a first release
    superseded = None
    if renew:
        # The old packet and its release are superseded, never reused: its record leaves the task (the audit keeps
        # it), so delivery re-verifies only the new release below against its own new audit row. The newest record
        # that reached Stage C is kept aside (never delivered or activated from there): if Stage C answers that it is
        # live (an activation whose reply was lost), _deliver restores it, so the live import stays addressable.
        code = remote.get("last_error_code") if remote.get("delivery") == "REFUSED" else "RELEASE_EXPIRED"
        audit.record(db, actor=founder, action="handoff.supersede", entity_type="task", entity_key=task.key,
                     task_id=task.id, before={**_release_fields(remote), "code": code})
        superseded = ({k: remote[k] for k in SUPERSEDED_FIELDS if k in remote} if remote.get("remote_task_key")
                      else remote.get("superseded"))
        task.context = {k: v for k, v in task.context.items() if k != "remote_stage_c"}
        # The report projection belongs to the old import (its seq is that Stage C task's revision): the new import
        # starts its own, or a restored record re-reads its own on the next poll.
        if isinstance(task.artifacts, dict) and "remote_stage_c" in task.artifacts:
            task.artifacts = {k: v for k, v in task.artifacts.items() if k != "remote_stage_c"}
    packet = {"version": VERSION, "id": str(uuid.uuid4()), "origin": "production", "origin_task_key": task.key,
              "repository": control.AUTH_REPOSITORY, "issued_at": now.isoformat(),
              "expires_at": (now + MAX_TTL).isoformat(), "risk": "LOW", "kind": "code_change", "task": text}
    packet["payload_sha256"] = digest(packet)
    previous = task.status
    remote = _set_remote(task, id=packet["id"], payload_sha256=packet["payload_sha256"],
                         source_sha256=source_digest(task), packet=packet, released_by=founder,
                         released_at=now.isoformat(), expires_at=packet["expires_at"], delivery="PENDING",
                         attempts=0, next_attempt_at=None, last_attempt_at=None, last_success_at=None,
                         last_error_code=None, remote_task_key=None, cancel_requested_at=None,
                         cancellation="NONE", poll_failures=0)
    if superseded:
        remote = _set_remote(task, superseded=superseded)
    task.status = enums.TaskStatus.WAITING_EXTERNAL
    audit.record(db, actor=founder, action="handoff.release", entity_type="task", entity_key=task.key,
                 task_id=task.id, before={"status": previous.value},
                 after={"status": task.status.value, **_release_fields(remote)})
    db.commit()
    return {"id": remote["id"], "payload_sha256": remote["payload_sha256"], "delivery": "PENDING"}


def _expired_unauthorized(task: models.Task, remote: dict, now: datetime) -> bool:
    """An activated handoff whose release expired before the founder's first coding authorization on Stage C: the
    newest accepted Stage C report for this id shows it activated, with no grant, no work started, no merge and no
    cancellation. Only the founder's renewal acts on it (never sync), and Stage C decides: it retires only an import
    that expired on its own clock and was never authorized or run, and refuses anything else (ACTIVE_IMPORT, then
    _restore_superseded), so a stale report here can never supersede live work."""
    expires = _utc(remote.get("expires_at"))
    stored = (task.artifacts or {}).get("remote_stage_c") if isinstance(task.artifacts, dict) else None
    snap = stored.get("snapshot") if isinstance(stored, dict) else None
    if remote.get("delivery") != "DELIVERED" or expires is None or now < expires or not isinstance(snap, dict):
        return False
    rt = snap.get("task") if isinstance(snap.get("task"), dict) else {}
    return (snap.get("id") == remote.get("id") and snap.get("activation") == "ACTIVATED"
            and (snap.get("authorization") or {}).get("status") == "NONE" and snap.get("cancellation") == "NONE"
            and rt.get("status") == "BACKLOG" and (snap.get("merge") or {}).get("state") == "NOT_OBSERVED")


def _activation_recorded(db: Session, task: models.Task, hid) -> bool:
    """Did production ever record Stage C's activation of this handoff? Read from the audit log, not the mutable
    record. An activation Stage C committed but whose reply was lost is not here; Stage C then refuses the renewed
    packet itself when that import is live (import_packet), so live work is never superseded."""
    rows = db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "handoff.activation", models.AuditLog.entity_key == task.key,
        models.AuditLog.task_id == task.id))
    return any(isinstance(r.after, dict) and r.after.get("id") == hid for r in rows)


def _release_fields(remote: dict) -> dict:
    return {k: remote.get(k) for k in ("id", "payload_sha256", "source_sha256", "expires_at", "released_by")}


def delivery_denial(db: Session, task: models.Task, now: datetime) -> str | None:
    """Re-verify the founder's release at handoff time, under the caller's task row lock. Any doubt refuses."""
    remote = _remote(task)
    packet = remote.get("packet") if remote else None
    if not isinstance(packet, dict) or remote.get("payload_sha256") != packet.get("payload_sha256") \
            or digest(packet) != packet.get("payload_sha256") or packet.get("id") != remote.get("id") \
            or packet.get("origin_task_key") != task.key:
        return "PACKET_INVALID"
    released_by = remote.get("released_by")
    entry = db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "handoff.release", models.AuditLog.entity_key == task.key,
        models.AuditLog.task_id == task.id).order_by(models.AuditLog.id.desc()).limit(1)).first()
    after = entry.after if entry is not None and isinstance(entry.after, dict) else {}
    if (entry is None or not approval_identity.is_founder_id(released_by) or entry.actor != released_by
            or any(after.get(k) != v for k, v in _release_fields(remote).items())):
        return "RELEASE_NOT_AUTHORIZED"
    if task.status != enums.TaskStatus.WAITING_EXTERNAL or remote.get("cancel_requested_at"):
        return "RELEASE_WITHDRAWN"
    if source_digest(task) != remote.get("source_sha256"):
        return "TASK_CHANGED"
    if _open_dependencies(db, task):
        return "DEPENDENCIES_OPEN"
    expires = _utc(packet.get("expires_at"))
    if expires is None or now >= expires:
        return "RELEASE_EXPIRED"
    return None


def withdraw(db: Session, task: models.Task, transport: "Transport | None" = None) -> list[str]:
    """Called by the founder's CANCELLED status change after it validated the change and committed the cancel request
    (pending) under the task lock: Stage C withdraws the import (the superseded live one first, then the current one)
    before production closes the task, so a task production shows CANCELLED never leaves a grant consumable on Stage C,
    even with an activation already in flight (Stage C serializes cancel against activation on the handoff row and
    against a claim on the task -> grant locks; a cancelled import is never activated, authorized or claimed). Ends
    the read transaction first: no transaction is held across the request. Returns the ids Stage C acknowledged, in order; request_cancel records
    only those that still match the record under the lock. Unreachable: nothing is acknowledged, the task stays
    WAITING_EXTERNAL (cancel PENDING) and the worker's sync retries the cancel (the remaining window: see _activate)."""
    remote = _remote(task)
    if remote is None or remote.get("cancellation") == "ACKNOWLEDGED" or not sender_enabled():
        return []
    targets = _cancel_targets(remote)
    db.commit()
    acked = []
    for hid, sha in targets:
        if _send_cancel(hid, sha, transport or http_transport)[0] is None:
            break
        acked.append(hid)
    return acked


def cancel_unconfirmed(task: models.Task) -> bool:
    """A cancel production requested that Stage C has not acknowledged yet: the task is never shown CANCELLED. It stays
    WAITING_EXTERNAL with its handoff cancellation PENDING (desk_view) while sync retries the cancel, idempotently and
    with back-off, until Stage C acknowledges it; only then is the task closed (_close_cancelled)."""
    remote = _remote(task)
    return bool(remote and remote.get("cancel_requested_at") and remote.get("cancellation") != "ACKNOWLEDGED")


def renewal_denial(db: Session, task: models.Task) -> str | None:
    """Why production must not renew an activated import's confirmation lease (ACTIVATION_LEASE) now: the founder's
    cancel, a closed task, the task's text changed since its release, or a dependency reopened. The founder's release
    itself was verified at activation and its audit is immutable; its expiry is enforced by Stage C (first
    authorization only). A renewal is not sent while agents are paused either (sync)."""
    remote = _remote(task)
    if (remote is None or remote.get("cancel_requested_at") or task.status != enums.TaskStatus.WAITING_EXTERNAL
            or remote.get("delivery") != "DELIVERED"):
        return "RELEASE_WITHDRAWN"
    if source_digest(task) != remote.get("source_sha256"):
        return "TASK_CHANGED"
    if _open_dependencies(db, task):
        return "DEPENDENCIES_OPEN"
    return None


def request_cancel(db: Session, task: models.Task, *, actor: str, now: datetime | None = None,
                   withdrawn=()) -> None:
    """Called inside the founder's CANCELLED status change (caller commits): remote withdrawal becomes durable, and
    what `withdraw` already confirmed on Stage C is recorded as acknowledged (matched by id, in cancel order). The
    caller closes the task only if nothing is left unconfirmed (cancel_unconfirmed)."""
    remote = _remote(task)
    if remote is None:
        return
    now = now or datetime.now(timezone.utc)
    if not remote.get("cancel_requested_at"):
        _set_remote(task, cancel_requested_at=now.isoformat(), next_attempt_at=None)
        audit.record(db, actor=actor, action="handoff.cancel_requested", entity_type="task", entity_key=task.key,
                     task_id=task.id, after={"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256")})
    remote = _remote(task)
    for hid, _ in _cancel_targets(remote) if remote.get("cancellation") != "ACKNOWLEDGED" else []:
        if hid not in withdrawn:
            break
        _acknowledged(db, task, hid, actor, now)


def status_refusal(task: models.Task, status: enums.TaskStatus, now: datetime | None = None) -> str | None:
    """What the founder's status route may not do to a handed-off task: production never runs it (no READY/QUEUED/
    IN_PROGRESS), and DONE needs a fresh, validated Stage C snapshot that reports a verified merge."""
    remote = _remote(task)
    if remote is None:
        return None
    if remote.get("cancel_requested_at") and status != enums.TaskStatus.CANCELLED:
        return "the cancellation of this handoff is pending until Stage C acknowledges it"
    if status not in (enums.TaskStatus.CANCELLED, enums.TaskStatus.DONE, enums.TaskStatus.WAITING_EXTERNAL):
        # Leaving WAITING_EXTERNAL any other way would stop sync while Stage C may still be running it: only
        # cancellation (which withdraws it remotely) or an evidenced DONE closes a handed-off task.
        return "a task handed to Stage C stays WAITING_EXTERNAL until it is cancelled or accepted as DONE"
    if status == enums.TaskStatus.DONE:
        view = desk_view(task, now)
        snap = (view or {}).get("snapshot") or {}
        if view["freshness"] != "FRESH" or (snap.get("merge") or {}).get("state") != "VERIFIED":
            return "DONE needs a fresh Stage C report of a verified founder merge for this handoff"
    return None


# ---------------------------------------------------------------------------- transport
Transport = Callable[[str, str, bytes | None], tuple[int, bytes, str | None]]


def http_transport(method: str, path: str, body: bytes | None) -> tuple[int, bytes, str | None]:
    """One HTTPS request to the configured Stage C origin: certificate verification on, no redirects, no proxy
    environment, bounded response size, TIMEOUT_S per operation and DEADLINE_S for the whole request (the request runs
    in a helper thread; at the deadline its client is closed). Transport errors are status 0, never raised."""
    import httpx
    origin, token = stage_c_origin(), _token()
    if origin is None or token is None:
        return 0, b"", None
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    if os.environ.get(ACCESS_ID_ENV) and os.environ.get(ACCESS_SECRET_ENV):
        headers["cf-access-client-id"] = os.environ[ACCESS_ID_ENV]
        headers["cf-access-client-secret"] = os.environ[ACCESS_SECRET_ENV]
    deadline, client = time.monotonic() + DEADLINE_S, None

    def call() -> tuple[int, bytes, str | None]:
        with client.stream(method, origin + path, content=body, headers=headers) as r:
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    return 0, b"", None
            return r.status_code, data, r.headers.get(MAC_HEADER)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        client = httpx.Client(trust_env=False, follow_redirects=False, verify=True, timeout=TIMEOUT_S)
        return pool.submit(call).result(timeout=DEADLINE_S)
    except Exception:  # noqa: BLE001 - any transport failure or the deadline is a retryable "unreachable"
        log.warning("stage c handoff transport failed: %s %s", method, path)
        return 0, b"", None
    finally:
        if client is not None:
            client.close()          # also aborts a request still running past the deadline
        pool.shutdown(wait=False)


def _verified(method: str, path: str, status: int, body: bytes, sig: str | None) -> dict | None:
    """The response JSON when its MAC proves it came from the holder of the handoff secret for this exact request."""
    if not sig or len(body) > MAX_RESPONSE_BYTES or not hmac.compare_digest(sig, mac(method, path, body)):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------- sync (production worker tick)
def _locked(db: Session, task_id: int) -> models.Task | None:
    return db.scalar(select(models.Task).where(models.Task.id == task_id).with_for_update()
                     .execution_options(populate_existing=True))


def _due(remote: dict, now: datetime) -> bool:
    at = _utc(remote.get("next_attempt_at"))
    return at is None or at <= now


def _backoff(failures: int) -> timedelta:
    return min(timedelta(seconds=30 * 2 ** min(max(failures - 1, 0), 10)), MAX_BACKOFF)   # exponent capped


def sync(db: Session, transport: Transport | None = None, *, paused: bool = False,
         now: datetime | None = None) -> dict:
    """Deterministic, model-free, bounded: cancellations first, then (unless agents are paused) new deliveries, then
    status polls; at most REQUESTS_PER_TICK work items. A poll is two requests (the confirmation renewal, then the
    GET), and an activation that finds a cancel committed while it was on the wire also sends that cancel (at most two
    more). Confirmation leases ride on polls: with more delivered handoffs than the per-tick budget renews within
    ACTIVATION_LEASE, or polls backing off after failures, leases lapse and Stage C refuses grants (fail closed) until
    the next renewal. No DB lock or transaction is held across a network call."""
    if not sender_enabled():
        return {"handoff": "disabled"}
    transport = transport or http_transport
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(models.Task).where(
        models.Task.context["remote_stage_c"]["id"].as_string().is_not(None)).order_by(models.Task.id)).all()
    cancels, activations, deliveries, polls = [], [], [], []
    for task in rows:
        remote = _remote(task)
        if remote is None:
            continue
        if remote.get("cancel_requested_at") and remote.get("cancellation") != "ACKNOWLEDGED":
            if _due(remote, now):   # failed cancellations back off, so they cannot starve the others
                cancels.append((remote.get("last_attempt_at") or "", task.id))
        elif task.status != enums.TaskStatus.WAITING_EXTERNAL:
            continue    # closed by the founder: nothing more is read or sent
        elif remote.get("delivery") == "IMPORTED" and not paused and _due(remote, now):
            activations.append(task.id)
        elif remote.get("delivery") == "PENDING" and not paused and _due(remote, now):
            deliveries.append(task.id)
        elif remote.get("delivery") == "DELIVERED" and _due(remote, now):
            polls.append((remote.get("last_attempt_at") or "", task.id))
    db.commit()  # end the read transaction before any network call
    done = []
    work = [("cancel", i) for _, i in sorted(cancels)] + [("activate", i) for i in activations] + \
           [("deliver", i) for i in deliveries] + [("poll", i) for _, i in sorted(polls)]
    for kind, task_id in work[:REQUESTS_PER_TICK]:
        try:
            if kind == "poll":   # a paused production confirms nothing: Stage C stops new claims when the lease ends
                done.append(_poll(db, task_id, transport, now, renew=not paused))
            else:
                done.append({"cancel": _cancel, "activate": _activate, "deliver": _deliver}[kind](
                    db, task_id, transport, now))
        except Exception:  # noqa: BLE001 - one bad row must not stop the others or the worker tick
            db.rollback()
            log.exception("stage c handoff %s failed for task id %s", kind, task_id)
            done.append({"task_id": task_id, "kind": kind, "outcome": "ERROR"})
    return {"handoff": "enabled", "requests": done}


def _fail(db, task, remote, code: str, *, hard: bool, now: datetime, delivery: bool, retry: str = "PENDING") -> dict:
    attempts = int(remote.get("attempts") or 0) + 1 if delivery else int(remote.get("attempts") or 0)
    polls = int(remote.get("poll_failures") or 0) + (0 if delivery else 1)
    state = remote.get("delivery")
    if delivery:
        state = "REFUSED" if hard else ("SUSPENDED" if attempts >= MAX_DELIVERY_FAILURES else retry)
    # A cancel requested meanwhile owns the record now: it goes out next tick, never behind the delivery back-off.
    retry_at = None if delivery and remote.get("cancel_requested_at") else \
        (now + _backoff(attempts if delivery else polls)).isoformat()
    _set_remote(task, delivery=state, attempts=attempts, poll_failures=polls, last_error_code=code,
                last_attempt_at=now.isoformat(), next_attempt_at=retry_at)
    if delivery:
        audit.record(db, actor="company-worker", action="handoff.delivery_failed", entity_type="task",
                     entity_key=task.key, task_id=task.id,
                     after={"id": remote.get("id"), "code": code, "delivery": state, "attempts": attempts})
    db.commit()
    return {"task": task.key, "kind": "deliver" if delivery else "poll", "outcome": code}


def _transport_code(status: int) -> str:
    return "UNREACHABLE" if status == 0 else f"HTTP_{status}"


def _deliver(db: Session, task_id: int, transport: Transport, now: datetime) -> dict:
    task = _locked(db, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None or remote.get("delivery") != "PENDING":
        db.commit()
        return {"task_id": task_id, "kind": "deliver", "outcome": "SKIPPED"}
    denial = delivery_denial(db, task, now)
    if denial:
        return _fail(db, task, remote, denial, hard=True, now=now, delivery=True)
    packet, hid = remote["packet"], remote["id"]
    db.commit()   # release the row lock before the network call
    path = API_PREFIX + hid
    status, body, sig = transport("PUT", path, canonical(packet))
    task = _locked(db, task_id)
    remote = _remote(task)
    if remote is None or remote.get("id") != hid:
        db.commit()
        return {"task_id": task_id, "kind": "deliver", "outcome": "SKIPPED"}
    # A cancel that committed while the PUT was on the wire wins: the record (with its superseded live import) stays
    # as it is, the refusal is recorded and _cancel withdraws the old id, then the new one.
    if status == 409 and isinstance(remote.get("superseded"), dict) and not remote.get("cancel_requested_at") \
            and (_verified("PUT", path, status, body, sig) or {}).get("error") == "ACTIVE_IMPORT":
        return _restore_superseded(db, task, remote, now)
    if status not in (200, 201):
        return _fail(db, task, remote, _transport_code(status), hard=status in HARD_STOP, now=now, delivery=True)
    data = _verified("PUT", path, status, body, sig)
    try:
        receipt = Receipt.model_validate(data) if data is not None else None
    except ValidationError:
        receipt = None
    if receipt is None or receipt.id != hid or receipt.payload_sha256 != remote.get("payload_sha256"):
        return _fail(db, task, remote, "BAD_RESPONSE" if data is not None else "BAD_MAC", hard=False, now=now,
                     delivery=True)
    # Phase 1 only: Stage C holds the import as IMPORTED, which can never be authorized or run. A cancellation that
    # committed while this PUT was in flight is sent first next tick, and activation (below) re-checks it.
    _set_remote(task, delivery="IMPORTED", remote_task_key=receipt.task_key, attempts=0, last_error_code=None,
                last_attempt_at=now.isoformat(), last_success_at=now.isoformat(), next_attempt_at=None)
    audit.record(db, actor="company-worker", action="handoff.delivery", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": hid, "payload_sha256": receipt.payload_sha256,
                                         "remote_task_key": receipt.task_key, "http": status})
    db.commit()
    return {"task": task.key, "kind": "deliver", "outcome": "IMPORTED"}


def _restore_superseded(db: Session, task: models.Task, remote: dict, now: datetime) -> dict:
    """Stage C refused a renewed packet because the import it replaces is live (activated or authorized there; the
    activation reply never reached production). Put that import's record back as delivered, so polling, the Desk and
    a cancellation address the live work by its own id again. The renewed packet is dropped (never delivered); this
    renews nothing and records no activation: the next poll reports Stage C's own state."""
    old = remote["superseded"]
    task.context = {**task.context, "remote_stage_c": {
        **old, "delivery": "DELIVERED", "attempts": 0, "poll_failures": 0, "last_error_code": "ACTIVE_IMPORT",
        "last_attempt_at": now.isoformat(), "next_attempt_at": None}}
    audit.record(db, actor="company-worker", action="handoff.supersede_reverted", entity_type="task",
                 entity_key=task.key, task_id=task.id,
                 before={"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256")},
                 after={"id": old.get("id"), "payload_sha256": old.get("payload_sha256"),
                        "remote_task_key": old.get("remote_task_key"), "code": "ACTIVE_IMPORT"})
    db.commit()
    return {"task": task.key, "kind": "deliver", "outcome": "RESTORED"}


def _activate(db: Session, task_id: int, transport: Transport, now: datetime) -> dict:
    """Phase 2: re-verify the founder release under the task row lock, then send the activation WITHOUT the lock (a
    slow Stage C reply never blocks the founder's cancel). The founder's cancellation (POST /tasks/{key}/status) takes
    the same lock, so production never sends an activation after it observed a committed cancel. A cancel that commits
    after the lock is released is recorded as PENDING and then withdrawn on Stage C by the cancel route itself
    (withdraw) before the task is shown CANCELLED: activation and cancellation serialize on Stage C's handoff row, and
    a cancelled or expired import is never activated, authorized or claimed afterwards.
    Guarantee and its limit: production shows a handed-off task CANCELLED only after Stage C acknowledged the
    withdrawal, and from that acknowledgement no grant for the import can be issued or consumed. Between the pending
    cancel's commit and the acknowledgement, an activation or renewal decided before the commit can still land and a
    grant can be issued and consumed: for the length of the route's one withdrawal request (DEADLINE_S) when Stage C is
    reachable, otherwise until the confirmation lease ends, at most ACTIVATION_LEASE after the cancel committed
    (production never decides a confirmation after it; plus clock skew between the hosts), while the task stays
    WAITING_EXTERNAL with its cancellation PENDING and sync retries the cancel. Production re-checks under the lock as soon
    as this reply returns and sends the cancel in the same tick (cancel_import revokes the bound grant; a run already
    started stops at its next owner check). Closing the lease window entirely would need the row lock across the
    request (removed on purpose: R4a finding 3) or Stage C calling production, which it never does.
    The result is recorded only if the record is unchanged and not cancel-requested. The record's next_attempt_at is a
    lease over the request, so a concurrent tick does not send it twice."""
    task = _locked(db, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None or remote.get("delivery") != "IMPORTED" or not _due(remote, now):
        db.commit()
        return {"task_id": task_id, "kind": "activate", "outcome": "SKIPPED"}
    denial = delivery_denial(db, task, now)
    if denial:
        return _fail(db, task, remote, denial, hard=True, now=now, delivery=True)
    hid, sha = remote["id"], remote["payload_sha256"]
    _set_remote(task, next_attempt_at=(now + timedelta(seconds=DEADLINE_S + 30)).isoformat())
    db.commit()   # release the row lock before the network call
    path = f"{API_PREFIX}{hid}/activate"
    # `now` is no later than the lock check above that saw no cancel: the confirmation's lease starts there.
    status, body, sig = transport("POST", path, canonical({"payload_sha256": sha, "sent_at": now.isoformat()}))
    task = _locked(db, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None or remote.get("id") != hid or remote.get("delivery") != "IMPORTED":
        db.commit()
        return {"task_id": task_id, "kind": "activate", "outcome": "SKIPPED"}
    if remote.get("cancel_requested_at"):
        # Nothing about this activation is recorded. Stage C may have just activated the import (the route could not
        # reach it): the cancel goes out now, in this tick, not after the other work (superseded import first).
        pending = remote.get("cancellation") != "ACKNOWLEDGED"
        out = {"task": task.key, "kind": "activate", "outcome": "CANCEL_PENDING"}
        db.commit()
        for _ in range(2 if pending else 0):
            out["cancel"] = _cancel(db, task_id, transport, now)["outcome"]
            if out["cancel"] != "ACKNOWLEDGED_SUPERSEDED":
                break
        return out
    if status != 200:
        return _fail(db, task, remote, _transport_code(status), hard=status in HARD_STOP, now=now, delivery=True,
                     retry="IMPORTED")
    data = _verified("POST", path, status, body, sig)
    try:
        ack = ActivationAck.model_validate(data) if data is not None else None
    except ValidationError:
        ack = None
    if ack is None or ack.id != hid or ack.task_key != remote.get("remote_task_key"):
        return _fail(db, task, remote, "BAD_RESPONSE" if data is not None else "BAD_MAC", hard=False, now=now,
                     delivery=True, retry="IMPORTED")
    _set_remote(task, delivery="DELIVERED", attempts=0, last_error_code=None, last_attempt_at=now.isoformat(),
                last_success_at=now.isoformat(), next_attempt_at=None)
    audit.record(db, actor="company-worker", action="handoff.activation", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": hid, "payload_sha256": sha, "remote_task_key": ack.task_key})
    db.commit()
    return {"task": task.key, "kind": "activate", "outcome": "ACTIVATED"}


def _poll(db: Session, task_id: int, transport: Transport, now: datetime, renew: bool = True) -> dict:
    task = db.get(models.Task, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None:
        return {"task_id": task_id, "kind": "poll", "outcome": "SKIPPED"}
    hid = remote["id"]
    confirm = renew and renewal_denial(db, task) is None
    db.commit()
    if confirm:
        # Renew the activation's lease (ACTIVATION_LEASE). `now` is no later than the read above that found nothing
        # stopping it, so a cancel, edit or reopened dependency committed after that read ends new claims within one
        # lease. The reply is not needed: the snapshot reports the state, and a failed renewal only lets the lease run
        # out (fail closed).
        transport("POST", f"{API_PREFIX}{hid}/activate",
                  canonical({"payload_sha256": remote["payload_sha256"], "sent_at": now.isoformat()}))
    path = API_PREFIX + hid
    status, body, sig = transport("GET", path, None)
    task = _locked(db, task_id)
    remote = _remote(task)
    if remote is None or remote.get("id") != hid:
        db.commit()
        return {"task_id": task_id, "kind": "poll", "outcome": "SKIPPED"}
    if status != 200:
        return _fail(db, task, remote, _transport_code(status), hard=False, now=now, delivery=False)
    data = _verified("GET", path, status, body, sig)
    if data is None:
        return _fail(db, task, remote, "BAD_MAC", hard=False, now=now, delivery=False)
    try:
        snap = Snapshot.model_validate(data)
    except ValidationError:
        return _fail(db, task, remote, "BAD_RESPONSE", hard=False, now=now, delivery=False)
    if (snap.id != hid or snap.payload_sha256 != remote.get("payload_sha256")
            or snap.task_key != remote.get("remote_task_key")
            or (snap.task is not None and snap.task.pr is not None and snap.task.ci is not None
                and snap.task.ci.state == "SUCCESS" and snap.task.ci.sha != snap.task.pr.head_sha)):
        return _fail(db, task, remote, "BINDING_MISMATCH", hard=False, now=now, delivery=False)
    return _observe(db, task, remote, snap, data, now)


def _observe(db, task, remote, snap: Snapshot, data: dict, now: datetime) -> dict:
    """Monotonic per handoff: a lower seq is stale and dropped; an equal one refreshes freshness and, if its content
    changed, the projection. A verified merge, once observed, is never replaced by a report without one."""
    stored = (task.artifacts or {}).get("remote_stage_c") if isinstance(task.artifacts, dict) else None
    last_seq = stored.get("seq") if isinstance(stored, dict) and type(stored.get("seq")) is int else -1
    previous = (stored.get("snapshot") or {}) if isinstance(stored, dict) else {}
    merged = (previous.get("merge") or {}).get("state") == "VERIFIED"
    closed = (previous.get("task") or {}).get("status") in ("DONE", "CANCELLED")
    fresh = {"last_attempt_at": now.isoformat(), "poll_failures": 0, "last_error_code": None,
             "next_attempt_at": (now + POLL_EVERY).isoformat()}
    # A replayed or delayed authenticated report is not fresh evidence: its generation time must be recent and
    # newer than the last one accepted (freshness, and so DONE, derive from it, never from our receive time).
    generated, last_generated = _utc(snap.generated_at), _utc(stored.get("generated_at") if stored else None)
    replayed = (generated is None or generated > now + timedelta(seconds=60) or now - generated > STALE_AFTER
                or (last_generated is not None and generated <= last_generated))
    # Latches: a verified merge or a closed remote task is never replaced by a report without it.
    if (replayed or snap.seq < last_seq or (merged and snap.merge.state != "VERIFIED")
            or (closed and (snap.task is None or snap.task.status.value != previous["task"]["status"]))):
        _set_remote(task, **{**fresh, "last_error_code": "STALE_REPORT"})
        db.commit()
        return {"task": task.key, "kind": "poll", "outcome": "STALE"}
    _set_remote(task, **fresh, last_success_at=now.isoformat())
    dumped = snap.model_dump(mode="json")
    same = {k: v for k, v in dumped.items() if k != "generated_at"} == {
        k: v for k, v in previous.items() if k != "generated_at"}
    if snap.seq == last_seq and same:
        task.artifacts = {**task.artifacts, "remote_stage_c": {**stored, "generated_at": generated.isoformat(),
                                                                "snapshot": dumped}}   # one consistent report time
        db.commit()
        return {"task": task.key, "kind": "poll", "outcome": "UNCHANGED"}
    # An equal seq with different content is a change no audit row labels (a grant expiring by time, a runtime
    # flag): the fresh, authenticated report passed the latches above, so it replaces the stored projection.
    task.artifacts = {**(task.artifacts or {}), "remote_stage_c": {
        "seq": snap.seq, "observed_at": now.isoformat(), "generated_at": generated.isoformat(),
        "snapshot": dumped}}
    audit.record(db, actor="company-worker", action="handoff.observed", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": snap.id, "seq": snap.seq, "remote_task_key": snap.task_key,
                                         "remote_status": snap.task.status.value if snap.task else None,
                                         "stage": snap.task.stage if snap.task else None,
                                         "merge": snap.merge.state, "merge_commit": snap.merge.commit})
    db.commit()
    return {"task": task.key, "kind": "poll", "outcome": "OBSERVED", "seq": snap.seq}


def _cancel(db: Session, task_id: int, transport: Transport, now: datetime) -> dict:
    task = db.get(models.Task, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None:
        return {"task_id": task_id, "kind": "cancel", "outcome": "SKIPPED"}
    hid, sha = _cancel_targets(remote)[0]
    db.commit()
    ack, code = _send_cancel(hid, sha, transport)
    task = _locked(db, task_id)
    remote = _remote(task)
    if ack is None:
        failures = int(remote.get("cancel_failures") or 0) + 1
        _set_remote(task, last_error_code=code, last_attempt_at=now.isoformat(), cancel_failures=failures,
                    next_attempt_at=(now + _backoff(failures)).isoformat())
        db.commit()
        return {"task": task.key, "kind": "cancel", "outcome": code}   # "remote stop unconfirmed" until acknowledged
    outcome = _acknowledged(db, task, hid, "company-worker", now, ack.task_key) or "SKIPPED"
    if outcome == "ACKNOWLEDGED":
        _close_cancelled(db, task, now)
    db.commit()
    return {"task": task.key, "kind": "cancel", "outcome": outcome}


def _close_cancelled(db: Session, task: models.Task, now: datetime) -> None:
    """Stage C acknowledged the founder's cancel: only now does the production task become CANCELLED."""
    if task.status == enums.TaskStatus.CANCELLED:
        return
    before = task.status.value
    task.status, task.completed_at = enums.TaskStatus.CANCELLED, now
    audit.record(db, actor="company-worker", action="task.status", entity_type="task", entity_key=task.key,
                 task_id=task.id, reason="HANDOFF_CANCEL_ACKNOWLEDGED", before={"status": before},
                 after={"status": "CANCELLED"})


def _cancel_targets(remote: dict) -> list[tuple[str, str]]:
    """What a cancellation withdraws, in order. A renewal keeps aside the record that reached Stage C (release); that
    import may be live there (its activation reply was lost). It is withdrawn first, by its own id, and the task's
    cancellation is ACKNOWLEDGED only after both: cancelling only the new id would leave the old task and its grant
    active on Stage C."""
    old = remote.get("superseded")
    old = old if isinstance(old, dict) and old.get("remote_task_key") and old.get("cancellation") != "ACKNOWLEDGED" \
        else None
    return ([(old["id"], old["payload_sha256"])] if old else []) + [(remote["id"], remote["payload_sha256"])]


def _send_cancel(hid: str, sha: str, transport: Transport) -> tuple[CancelAck | None, str | None]:
    """One authenticated cancel request: Stage C's verified acknowledgement for exactly `hid`, or the failure code."""
    path = f"{API_PREFIX}{hid}/cancel"
    status, body, sig = transport("POST", path, canonical({"payload_sha256": sha, "reason": "PRODUCTION_CANCELLED"}))
    data = _verified("POST", path, status, body, sig) if status == 200 else None
    try:
        ack = CancelAck.model_validate(data) if data is not None else None
    except ValidationError:
        ack = None
    if ack is None or ack.id != hid:
        return None, _transport_code(status) if status != 200 else ("BAD_RESPONSE" if data is not None else "BAD_MAC")
    return ack, None


def _acknowledged(db, task, hid: str, actor: str, now: datetime, remote_task_key: str | None = None) -> str | None:
    """Record Stage C's acknowledged cancel of `hid` on whichever record it names (caller holds the lock, commits);
    None if the record no longer names it."""
    remote = _remote(task)
    sup = remote.get("superseded")
    if isinstance(sup, dict) and sup.get("id") == hid and hid != remote.get("id"):
        _set_remote(task, superseded={**sup, "cancellation": "ACKNOWLEDGED"}, last_error_code=None,
                    last_attempt_at=now.isoformat(), next_attempt_at=None)
        audit.record(db, actor=actor, action="handoff.cancel_acknowledged", entity_type="task", entity_key=task.key,
                     task_id=task.id, after={"id": hid, "remote_task_key": remote_task_key, "superseded": True})
        return "ACKNOWLEDGED_SUPERSEDED"
    if hid != remote.get("id"):
        return None
    _set_remote(task, cancellation="ACKNOWLEDGED", last_error_code=None, last_attempt_at=now.isoformat())
    audit.record(db, actor=actor, action="handoff.cancel_acknowledged", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": hid, "remote_task_key": remote_task_key})
    return "ACKNOWLEDGED"


# ---------------------------------------------------------------------------- Desk read model (production)
def desk_view(task: models.Task, now: datetime | None = None) -> dict | None:
    """Allowlisted handoff state for the Desk. The snapshot is what Stage C reported (validated, authenticated), not a
    production fact; without a successful read in STALE_AFTER the state is UNKNOWN, never success."""
    remote = _remote(task)
    if remote is None:
        return None
    now = now or datetime.now(timezone.utc)
    stored = (task.artifacts or {}).get("remote_stage_c") if isinstance(task.artifacts, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    generated = _utc(stored.get("generated_at"))   # when Stage C produced the newest accepted report
    fresh = (stored.get("snapshot") is not None and generated is not None
             and timedelta(seconds=-60) <= now - generated <= STALE_AFTER)
    cancel = ("ACKNOWLEDGED" if remote.get("cancellation") == "ACKNOWLEDGED"
              else "PENDING" if remote.get("cancel_requested_at") else "NONE")
    return {"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256"),
            "delivery": remote.get("delivery"), "remote_task_key": remote.get("remote_task_key"),
            "released_at": remote.get("released_at"), "expires_at": remote.get("expires_at"),
            "last_error_code": remote.get("last_error_code"), "cancellation": cancel,
            "last_success_at": remote.get("last_success_at"), "freshness": "FRESH" if fresh else "UNKNOWN",
            "source": "Stage C reported", "seq": stored.get("seq"), "observed_at": stored.get("observed_at"),
            "snapshot": stored.get("snapshot")}


def desk_list(db: Session, now: datetime | None = None) -> list[dict]:
    tasks = db.scalars(select(models.Task).where(
        models.Task.context["remote_stage_c"]["id"].as_string().is_not(None))
        .order_by(models.Task.updated_at.desc(), models.Task.id.desc()).limit(50)).all()
    return [{"key": t.key, "title": t.title, "status": t.status.value, "handoff": view}
            for t in tasks if (view := desk_view(t, now)) is not None]


# =========================================================================== Stage C (receiver)
def _row(db: Session, hid: str, lock: bool = False) -> models.ControlState | None:
    stmt = select(models.ControlState).where(models.ControlState.key == KEY_PREFIX + hid)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt.execution_options(populate_existing=True))


def _receipt(db: Session, hid: str, value: dict) -> dict:
    task = db.scalar(select(models.Task).where(models.Task.key == value.get("task_key")))
    return {"version": VERSION, "id": hid, "payload_sha256": value.get("payload_sha256"),
            "task_key": value.get("task_key"), "status": task.status.value if task else None}


def _existing(db: Session, hid: str, row: models.ControlState, sha: str) -> tuple[int, dict]:
    value = row.value if isinstance(row.value, dict) else {}
    if value.get("cancelled_at"):
        raise HandoffError(410, "CANCELLED")
    if value.get("payload_sha256") != sha or not value.get("task_key"):
        raise HandoffError(409, "PAYLOAD_CONFLICT")
    return 200, _receipt(db, hid, value)   # a replay reports, never requeues or touches the task


def import_packet(db: Session, hid: str, raw, now: datetime | None = None) -> tuple[int, dict]:
    """Idempotent import of one immutable packet: 201 created, 200 identical replay. The per-origin row (_origin)
    serializes imports for one production task and the ControlState primary key concurrent deliveries of one id; a
    packet issued before the newest import for its task is refused (SUPERSEDED). Reservation, task and audit commit
    together."""
    from app import stage_c_worker
    now = now or datetime.now(timezone.utc)
    try:
        packet = Packet.model_validate(raw)
    except ValidationError:
        raise HandoffError(422, "MALFORMED")
    issued, expires = _utc(packet.issued_at), _utc(packet.expires_at)
    if (packet.id != hid or digest(raw) != packet.payload_sha256 or issued is None or expires is None
            or issued > now + timedelta(seconds=60) or not timedelta(0) < expires - issued <= MAX_TTL):
        raise HandoffError(422, "MALFORMED")
    # The imported task is created LOW-risk developer code work only because the packet itself declares exactly that
    # (and the digest binds the declaration); anything else is refused, never down-graded.
    if packet.risk != "LOW" or packet.kind != "code_change":
        raise HandoffError(422, "NOT_ELIGIBLE")
    origin = _origin(db, packet.origin_task_key)   # first: one import per production task at a time
    row = _row(db, hid, lock=True)
    if row is not None:
        return _existing(db, hid, row, packet.payload_sha256)
    newest = _utc((origin.value or {}).get("issued_at")) if isinstance(origin.value, dict) else None
    if newest is not None and issued < newest:
        raise HandoffError(410, "SUPERSEDED")   # a late PUT of an older release never retires (or follows) a newer one
    if now >= expires:
        raise HandoffError(410, "EXPIRED")
    scope = {"paths": list(packet.task.paths), "context": list(packet.task.context)}
    if stage_c_worker.scope_refused(scope, set(stage_c_worker.task_paths())):
        raise HandoffError(422, "SCOPE_REFUSED")
    project = db.scalar(select(models.Project).where(models.Project.key == "company-os"))
    agent = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
    if project is None or agent is None:
        raise HandoffError(409, "RECEIVER_NOT_CONFIGURED")
    _retire_superseded(db, packet.origin_task_key, hid, now)
    reservation = {"origin": "production", "origin_task_key": packet.origin_task_key,
                   "payload_sha256": packet.payload_sha256, "task_key": None,
                   "expires_at": expires.isoformat(), "cancelled_at": None, "first_authorized_at": None,
                   "state": "IMPORTED"}   # never runnable until production's authenticated activation
    try:
        with db.begin_nested():
            db.add(models.ControlState(key=KEY_PREFIX + hid, value=reservation, updated_by=SERVICE))
    except IntegrityError:   # a concurrent delivery of the same id won: compare against it
        db.rollback()
        row = _row(db, hid, lock=True)
        if row is None:
            raise
        return _existing(db, hid, row, packet.payload_sha256)
    title = " ".join(packet.task.title.split())
    task = models.Task(
        title=title, description=title,   # the private title also lives in a privacy-scanned field
        objective=packet.task.objective, success_criteria=packet.task.success_criteria,
        status=enums.TaskStatus.BACKLOG, risk_level=enums.RiskLevel.LOW, approval_required=False,
        assigned_agent_id=agent.id, project_id=project.id, created_by=SERVICE,
        dedupe_key=f"handoff:production:{hid}",
        context={"autonomous": True, "write": True, "stage_c": scope,
                 "remote_stage_c": {"id": hid, "origin": "production", "origin_task_key": packet.origin_task_key,
                                    "payload_sha256": packet.payload_sha256}})
    try:
        keys.add_with_key(db, task, "TASK")
    except keys.DuplicateOpenWork:
        db.rollback()
        raise HandoffError(409, "PAYLOAD_CONFLICT")
    row = _row(db, hid)
    row.value = {**reservation, "task_key": task.key}
    origin.value = {"issued_at": issued.isoformat(), "id": hid}
    audit.record(db, actor=SERVICE, action="task.create", entity_type="task", entity_key=task.key,
                 reason="stage c handoff import", task_id=task.id,
                 after={"status": task.status.value, "handoff_id": hid})
    audit.record(db, actor=SERVICE, action="handoff.import", entity_type="control_state",
                 entity_key=KEY_PREFIX + hid, task_id=task.id,
                 after={"id": hid, "payload_sha256": packet.payload_sha256, "task_key": task.key,
                        "origin_task_key": packet.origin_task_key})
    db.commit()
    return 201, _receipt(db, hid, row.value)


def _origin(db: Session, origin_task_key: str) -> models.ControlState:
    """The per-production-task row every import locks first. It serializes first imports with different ids, which
    otherwise find no committed task to lock (_retire_superseded) and both commit; it also keeps the issue time of the
    newest import, so an older packet arriving late is refused even after that newer import was cancelled."""
    stmt = select(models.ControlState).where(models.ControlState.key == ORIGIN_PREFIX + origin_task_key) \
        .with_for_update().execution_options(populate_existing=True)
    row = db.scalar(stmt)
    if row is None:
        try:
            with db.begin_nested():
                db.add(models.ControlState(key=ORIGIN_PREFIX + origin_task_key, value={}, updated_by=SERVICE))
        except IntegrityError:   # a concurrent first import created it: this waits for that one to finish
            db.rollback()        # nothing else is in this transaction yet
        row = db.scalar(stmt)
    if row is None:
        raise HandoffError(409, "PAYLOAD_CONFLICT")
    return row


def _retire_superseded(db: Session, origin_task_key: str, hid: str, now: datetime) -> None:
    """A new packet for a production task replaces an earlier import of it (the founder renewed an expired or changed
    release). An earlier import that was never activated, or was activated but expired on this clock before any
    founder authorization or run (it can never be authorized now), is withdrawn in this same transaction (tombstoned
    and its task cancelled), so Stage C never holds two tasks for one production task and the old packet can never be
    activated. Anything else is live or finished work (authorized, run, DONE, not yet expired): the new packet is
    refused (409) and nothing changes. Lock order task -> handoff row, as cancel_import, activate_import and
    authorize_pilot (which locks the task first), so a concurrent first authorization either commits first and is seen
    here, or finds the tombstone. An import never authorized has no grant (authorization_binding marks the first)."""
    olds = db.scalars(select(models.Task).where(
        models.Task.context["remote_stage_c"]["origin_task_key"].as_string() == origin_task_key,
        models.Task.status != enums.TaskStatus.CANCELLED)
        .order_by(models.Task.id).with_for_update().execution_options(populate_existing=True)).all()
    for task in olds:
        ref = imported(task)
        old_hid = str(ref.get("id")) if ref else None
        if old_hid is None or old_hid == hid:
            continue
        row = _row(db, old_hid, lock=True)
        value = dict(row.value) if row is not None and isinstance(row.value, dict) else None
        if value is None or value.get("task_key") != task.key:
            raise HandoffError(409, "PAYLOAD_CONFLICT")
        if value.get("first_authorized_at") or task.status == enums.TaskStatus.DONE:
            raise HandoffError(409, "ACTIVE_IMPORT")
        if value.get("state") == "ACTIVATED":
            expires = _utc(value.get("expires_at"))
            ran = db.scalar(select(func.count()).select_from(models.AgentRun)
                            .where(models.AgentRun.task_id == task.id))
            if expires is None or now < expires or ran or task.status != enums.TaskStatus.BACKLOG:
                raise HandoffError(409, "ACTIVE_IMPORT")
        if not value.get("cancelled_at"):
            row.value = {**value, "cancelled_at": now.isoformat(), "superseded_by": hid}
        before = task.status.value
        task.status, task.completed_at = enums.TaskStatus.CANCELLED, now
        audit.record(db, actor=SERVICE, action="task.status", entity_type="task", entity_key=task.key,
                     task_id=task.id, reason="HANDOFF_SUPERSEDED", before={"status": before},
                     after={"status": "CANCELLED"})
        audit.record(db, actor=SERVICE, action="handoff.supersede", entity_type="control_state",
                     entity_key=KEY_PREFIX + old_hid, task_id=task.id,
                     after={"id": old_hid, "payload_sha256": value.get("payload_sha256"), "task_key": task.key,
                            "superseded_by": hid, "activated": value.get("state") == "ACTIVATED"})
        models.bump_revision(task)


def cancel_import(db: Session, hid: str, sha: str, now: datetime | None = None) -> dict:
    """Withdraw only this import. A tombstone is written even before the PUT arrives, so a delayed PUT cannot
    resurrect it. Cancels the local task if it is still open and revokes the pilot grant only if it is bound to this
    handoff. A run already in flight stops at its next owner check (acknowledgement is not proof of termination)."""
    now = now or datetime.now(timezone.utc)
    # Lock order task -> pilot grant -> handoff row, the same order as a claim (task, then grant in
    # control.consume_write_authorization) and authorization (grant, then handoff in authorize_pilot), so no two of
    # them can deadlock. The task key is read unlocked first: it is written once, at import, and never changes.
    peek = _row(db, hid)
    task_key = peek.value.get("task_key") if peek is not None and isinstance(peek.value, dict) else None
    task = db.scalar(select(models.Task).where(models.Task.key == task_key).with_for_update()
                     .execution_options(populate_existing=True)) if task_key else None
    grant = db.scalar(select(models.ControlState).where(models.ControlState.key == control.PILOT_AUTHORIZATION)
                      .with_for_update().execution_options(populate_existing=True)) if peek is not None else None
    row = _row(db, hid, lock=True)
    if row is None:
        try:
            with db.begin_nested():
                db.add(models.ControlState(key=KEY_PREFIX + hid, updated_by=SERVICE, value={
                    "origin": "production", "origin_task_key": None, "payload_sha256": sha, "task_key": None,
                    "expires_at": None, "cancelled_at": now.isoformat(), "first_authorized_at": None}))
        except IntegrityError:
            db.rollback()
            return cancel_import(db, hid, sha, now)
        audit.record(db, actor=SERVICE, action="handoff.cancel", entity_type="control_state",
                     entity_key=KEY_PREFIX + hid, after={"id": hid, "payload_sha256": sha, "tombstone": True})
        db.commit()
        return {"id": hid, "cancellation": "ACKNOWLEDGED", "task_key": None}
    value = dict(row.value) if isinstance(row.value, dict) else {}
    if value.get("payload_sha256") != sha:
        raise HandoffError(409, "PAYLOAD_CONFLICT")
    if value.get("cancelled_at"):
        db.commit()
        return {"id": hid, "cancellation": "ACKNOWLEDGED", "task_key": value.get("task_key")}
    if value.get("task_key") != task_key:   # the import committed between the peek and the locks: start over
        db.rollback()
        return cancel_import(db, hid, sha, now)
    row.value = {**value, "cancelled_at": now.isoformat()}
    if task is not None and task.status not in (enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED):
        before = task.status.value
        task.status, task.completed_at = enums.TaskStatus.CANCELLED, now
        audit.record(db, actor=SERVICE, action="task.status", entity_type="task", entity_key=task.key,
                     task_id=task.id, reason="PRODUCTION_CANCELLED", before={"status": before},
                     after={"status": "CANCELLED"})
    gv = grant.value if grant is not None and isinstance(grant.value, dict) else {}
    if grant is not None and gv.get("handoff_id") == hid and gv.get("task_key") == value.get("task_key"):
        db.delete(grant)
        audit.record(db, actor=SERVICE, action="stage-c.authorization.revoke", entity_type="control_state",
                     entity_key=control.PILOT_AUTHORIZATION, reason="handoff cancelled", before=dict(gv), after=None)
    audit.record(db, actor=SERVICE, action="handoff.cancel", entity_type="control_state", entity_key=KEY_PREFIX + hid,
                 task_id=task.id if task else None, after={"id": hid, "payload_sha256": sha,
                                                           "task_key": value.get("task_key")})
    if task is not None:
        models.bump_revision(task)
    db.commit()
    return {"id": hid, "cancellation": "ACKNOWLEDGED", "task_key": value.get("task_key")}


def _lease(value: dict, sent_at, now: datetime) -> str | None:
    """The confirmation lease after a (re)activation decided at production time `sent_at`: ACTIVATION_LEASE from it,
    capped at this clock, never shorter than a lease already granted. No `sent_at`, no new lease."""
    at = _utc(sent_at)
    leases = [t for t in (min(at, now) + ACTIVATION_LEASE if at else None, _utc(value.get("confirmed_until"))) if t]
    return max(leases).isoformat() if leases else None


def confirmed(hv: dict, now: datetime) -> bool:
    """Production's activation confirmation of this import is live (ACTIVATION_LEASE)."""
    until = _utc(hv.get("confirmed_until"))
    return until is not None and now < until


def activate_import(db: Session, hid: str, sha: str, now: datetime | None = None, sent_at=None) -> dict:
    """Phase 2 of the two-phase handoff: production re-verified the founder release and asks Stage C to accept the
    import as work. Locks task -> handoff row (cancel_import's order), so activation and cancellation serialize:
    a cancelled (or tombstoned) or expired import is never activated, and a repeat is idempotent (no new task, grant
    or run). Every (re)activation also renews production's confirmation lease (_lease): a repeat writes only that, and
    bumps no revision (the lease is not part of the report's content)."""
    now = now or datetime.now(timezone.utc)
    peek = _row(db, hid)
    task_key = peek.value.get("task_key") if peek is not None and isinstance(peek.value, dict) else None
    task = db.scalar(select(models.Task).where(models.Task.key == task_key).with_for_update()
                     .execution_options(populate_existing=True)) if task_key else None
    row = _row(db, hid, lock=True)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else None
    if value is None:
        raise HandoffError(404, "NOT_FOUND")
    if value.get("payload_sha256") != sha:
        raise HandoffError(409, "PAYLOAD_CONFLICT")
    if value.get("cancelled_at"):
        raise HandoffError(410, "CANCELLED")
    if task is None or value.get("task_key") != task_key:
        raise HandoffError(409, "NOT_IMPORTED")
    lease = _lease(value, sent_at, now)
    if value.get("state") == "ACTIVATED":
        if lease != value.get("confirmed_until"):
            row.value = {**value, "confirmed_until": lease}
        db.commit()
        return {"id": hid, "activation": "ACTIVATED", "task_key": task.key}
    expires = _utc(value.get("expires_at"))
    if expires is None or now >= expires:
        raise HandoffError(410, "EXPIRED")
    row.value = {**value, "state": "ACTIVATED", "activated_at": now.isoformat(), "confirmed_until": lease}
    audit.record(db, actor=SERVICE, action="handoff.activate", entity_type="control_state",
                 entity_key=KEY_PREFIX + hid, task_id=task.id, after={"id": hid, "payload_sha256": sha,
                                                                      "task_key": task.key})
    models.bump_revision(task)
    db.commit()
    return {"id": hid, "activation": "ACTIVATED", "task_key": task.key}


def _grant_view(db: Session, task: models.Task | None, hid: str, now: datetime) -> dict:
    row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    if task is None or value.get("task_key") != task.key or value.get("handoff_id") != hid:
        return {"status": "NONE", "source_sha": None, "expires_at": None}
    denial = control._authorization_denial(db, row, task.key, value.get("source_sha") or "", now) \
        or grant_denial(db, task, value, now)
    status = {None: "ACTIVE", "stage_c_authorization_consumed": "CONSUMED",
              "stage_c_authorization_expired": "EXPIRED"}.get(denial, "INVALID")
    sha = value.get("source_sha")
    return {"status": status, "source_sha": sha if isinstance(sha, str) and re.fullmatch(_SHA40[1:-1], sha) else None,
            "expires_at": _utc(value.get("expires_at")).isoformat() if _utc(value.get("expires_at")) else None}


def snapshot(db: Session, hid: str, now: datetime | None = None) -> dict:
    """The bounded, task-specific read for production. Read-only: no row is written, so a poll can never look like
    an owner change to a running attempt."""
    from app import stage_c_state, worker
    now = now or datetime.now(timezone.utc)
    # One snapshot of committed state for the whole report: on PostgreSQL a REPEATABLE READ transaction (READ
    # COMMITTED would let a commit land between the task read and the handoff, grant and run reads). SQLite reads in
    # one transaction are already consistent. The connection's isolation level is reset when it returns to the pool.
    db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    row = _row(db, hid)
    if row is None:
        raise HandoffError(404, "NOT_FOUND")
    value = row.value if isinstance(row.value, dict) else {}
    task_key = value.get("task_key")   # written once, at import
    # seq is the task row's revision, read in the same snapshot as everything else reported. Every change to the task,
    # its handoff row (activation, cancel) or its grant bumps that revision in the same transaction
    # (models.Task.revision), so the report is exactly the committed state that revision labels. (Time alone can
    # change the grant view, an expiry or the confirmation lease, under the same seq; _observe accepts that.)
    db.expire_all()
    task = db.scalar(select(models.Task).where(models.Task.key == task_key)) if task_key else None
    seq = task.revision if task is not None else 0
    row = _row(db, hid)
    value = row.value if isinstance(row.value, dict) else {}
    projection = None
    if task is not None:
        runs = list(db.scalars(select(models.AgentRun).where(models.AgentRun.task_id == task.id)
                               .order_by(models.AgentRun.id.desc()).limit(50)))
        p = stage_c_state._task(db, task, runs)
        projection = {k: p[k] for k in ("status", "stage", "test_verdict", "reviewer_verdict", "tested_fingerprint",
                                        "reviewed_fingerprint", "fingerprint_match", "pr", "ci", "failure_category",
                                        "needs_founder", "history")}
    merge = {"state": "NOT_OBSERVED", "commit": None, "at": None}
    if task is not None:
        _, hist, _ = stage_c_state._history(task)
        merged = next((h for h in reversed(hist) if h["stage"] == "MERGED"), None)
        commit = stage_c_state._sha(merged["evidence"].get("merge_commit")) if merged else None
        if merged and commit and str(merged.get("actor", "")).startswith("founder") \
                and (task.context.get("improvement") or {}).get("stage") in ("MERGED", "VERIFYING", "MEASURED"):
            merge = {"state": "VERIFIED", "commit": commit, "at": stage_c_state._ts(merged.get("at"))}
    try:
        state = worker.worker_state(db)
    except Exception:  # noqa: BLE001 - unknown runtime is null, never a guess
        state = {}
    runtime = state.get("runtime") or {}
    flag = (state.get("autonomy") or {}).get("code_write_flag")
    return {"version": VERSION, "id": hid, "payload_sha256": value.get("payload_sha256"),
            "task_key": value.get("task_key"), "seq": seq, "generated_at": now.isoformat(),
            "runtime": {"api_sha": stage_c_state._sha(os.environ.get("COMPANY_OS_GIT_SHA")),
                        "worker_sha": stage_c_state._sha(runtime.get("git_sha")),
                        "write_enabled": flag if isinstance(flag, bool) else None},
            "authorization": _grant_view(db, task, hid, now), "task": projection, "merge": merge,
            "cancellation": "ACKNOWLEDGED" if value.get("cancelled_at") else "NONE",
            "activation": value.get("state") if value.get("state") in ("IMPORTED", "ACTIVATED") else None}


# ---------------------------------------------------------------------------- Stage C grant binding
_GRANT_FIELDS = ("task_key", "source_sha", "issued_at", "expires_at", "issued_by", "handoff_id", "payload_sha256")


def imported(task) -> dict | None:
    """The import binding of a Stage C task created by a handoff, or None for a native task."""
    ctx = task.context if isinstance(task.context, dict) else {}
    ref = ctx.get("remote_stage_c")
    return ref if isinstance(ref, dict) and ref.get("origin") == "production" else None


def import_view(db: Session, task) -> dict | None:
    """Stage C's own Desk: the binding the founder's coding approval must name for an imported task (id, digest) and
    whether production activated it. None for a native task."""
    ref = imported(task)
    if ref is None:
        return None
    row = _row(db, str(ref.get("id")))
    hv = row.value if row is not None and isinstance(row.value, dict) else {}
    return {"id": ref.get("id"), "payload_sha256": ref.get("payload_sha256"),
            "origin_task_key": ref.get("origin_task_key"),
            "activation": hv.get("state") if hv.get("state") in ("IMPORTED", "ACTIVATED") else None,
            "cancelled": bool(hv.get("cancelled_at"))}


def grant_denial(db: Session, task, value: dict, now: datetime | None = None) -> str | None:
    """An imported task's pilot grant must be issued for exactly this handoff and digest (and its issue audit must
    say so), the import must not be withdrawn, and production's activation confirmation must be live (ACTIVATION_LEASE).
    Anything else denies; there is no standing-grant fallback."""
    ref = imported(task)
    if ref is None:
        return None
    row = _row(db, str(ref.get("id")))
    hv = row.value if row is not None and isinstance(row.value, dict) else {}
    if (not isinstance(value, dict) or value.get("handoff_id") != ref.get("id")
            or value.get("payload_sha256") != ref.get("payload_sha256")
            or hv.get("payload_sha256") != ref.get("payload_sha256") or hv.get("task_key") != task.key
            or not control._issue_audited(db, value, fields=_GRANT_FIELDS)):
        return "stage_c_handoff_authorization_mismatch"
    if hv.get("cancelled_at"):
        return "stage_c_handoff_cancelled"
    if hv.get("state") != "ACTIVATED":
        return "stage_c_handoff_not_activated"
    if not confirmed(hv, now or datetime.now(timezone.utc)):
        return "stage_c_handoff_unconfirmed"
    return None


def authorization_binding(db: Session, task, handoff_id: str | None, payload_sha256: str | None,
                          now: datetime) -> dict | None:
    """Checked by control.authorize_pilot under its row lock: the founder must name this import's id and digest,
    it must not be cancelled, and its first authorization must come before the packet expires."""
    ref = imported(task)
    if ref is None:
        if handoff_id or payload_sha256:
            raise ValueError("this task was not handed off; a handoff binding does not apply")
        return None
    if handoff_id != ref.get("id") or payload_sha256 != ref.get("payload_sha256"):
        raise ValueError("an imported task's authorization must name its handoff id and payload digest")
    row = _row(db, handoff_id, lock=True)
    hv = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    if hv.get("payload_sha256") != payload_sha256 or hv.get("task_key") != task.key:
        raise ValueError("the handoff record does not match this task")
    if hv.get("cancelled_at"):
        raise ValueError("the handoff was cancelled")
    if hv.get("state") != "ACTIVATED":
        raise ValueError("the handoff is imported but not activated by production; it cannot be authorized yet")
    if not confirmed(hv, now):
        raise ValueError("production has not confirmed this handoff recently (cancelled there, or unreachable)")
    expires = _utc(hv.get("expires_at"))
    if not hv.get("first_authorized_at") and (expires is None or now >= expires):
        raise ValueError("the handoff expired before its first authorization; the founder renews the release on "
                         "production, which replaces this import")
    if not hv.get("first_authorized_at"):
        row.value = {**hv, "first_authorized_at": now.isoformat()}
    models.bump_revision(task)   # the grant is part of the reported state
    return {"handoff_id": handoff_id, "payload_sha256": payload_sha256}
