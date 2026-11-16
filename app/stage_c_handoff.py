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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select
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
API_PREFIX = "/stage-c/handoff-api/"
MAX_TTL = timedelta(hours=24)
MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES = 32 * 1024, 64 * 1024
TIMEOUT_S = 5.0
REQUESTS_PER_TICK = 3
MAX_DELIVERY_FAILURES = 5
POLL_EVERY = timedelta(seconds=60)
STALE_AFTER = timedelta(seconds=180)
MAX_BACKOFF = timedelta(minutes=15)
RELEASABLE = (enums.TaskStatus.BACKLOG, enums.TaskStatus.READY, enums.TaskStatus.BLOCKED)
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
    return hashlib.sha256(canonical({"title": task.title, "description": task.description,
                                     "objective": task.objective,
                                     "success_criteria": task.success_criteria})).hexdigest()


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


# =========================================================================== production (sender)
def _remote(task: models.Task) -> dict | None:
    ctx = task.context if isinstance(task.context, dict) else {}
    remote = ctx.get("remote_stage_c")
    return remote if isinstance(remote, dict) and remote.get("origin") is None else None


def _set_remote(task: models.Task, **changes) -> dict:
    remote = {**(_remote(task) or {}), **changes}
    task.context = {**(task.context or {}), "remote_stage_c": remote}
    return remote


def release(db: Session, task: models.Task, body: schemas.StageCTask, *, founder: str,
            now: datetime | None = None) -> dict:
    """The founder releases exactly this transfer text for `task`. Idempotent: the same text returns the same release
    (and restarts a suspended delivery of that exact packet); different text is a conflict. Caller handles commit
    failures; this commits on success."""
    from app import stage_c_worker   # late: stage_c_worker imports control, which imports this lazily
    if not approval_identity.is_founder_id(founder):
        raise PermissionError("only the verified founder may release a task to Stage C")
    if not sender_enabled():
        raise HandoffError(403, "HANDOFF_DISABLED")
    now = now or datetime.now(timezone.utc)
    db.refresh(task, with_for_update=True)
    text = {"title": " ".join(body.title.split()), "objective": body.objective,
            "success_criteria": body.success_criteria, "paths": list(body.paths), "context": list(body.context)}
    remote = _remote(task)
    if remote is not None:
        packet = remote.get("packet") if isinstance(remote.get("packet"), dict) else {}
        if packet.get("task") != text:
            raise HandoffError(409, "RELEASE_CONFLICT")
        if remote.get("delivery") in ("SUSPENDED", "REFUSED"):   # explicit founder retry; delivery re-verifies
            _set_remote(task, delivery="PENDING", attempts=0, next_attempt_at=None, last_error_code=None)
            audit.record(db, actor=founder, action="handoff.retry", entity_type="task", entity_key=task.key,
                         task_id=task.id, after={"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256")})
        db.commit()
        remote = _remote(task)
        return {"id": remote["id"], "payload_sha256": remote["payload_sha256"], "delivery": remote["delivery"]}
    if task.status not in RELEASABLE:
        raise HandoffError(409, "TASK_NOT_RELEASABLE")
    if stage_c_worker.scope_refused({"paths": text["paths"], "context": text["context"]},
                                    set(stage_c_worker.SAFE_FILES)):
        raise HandoffError(422, "SCOPE_REFUSED")
    packet = {"version": VERSION, "id": str(uuid.uuid4()), "origin": "production", "origin_task_key": task.key,
              "repository": control.AUTH_REPOSITORY, "issued_at": now.isoformat(),
              "expires_at": (now + MAX_TTL).isoformat(), "task": text}
    packet["payload_sha256"] = digest(packet)
    previous = task.status
    remote = _set_remote(task, id=packet["id"], payload_sha256=packet["payload_sha256"],
                         source_sha256=source_digest(task), packet=packet, released_by=founder,
                         released_at=now.isoformat(), expires_at=packet["expires_at"], delivery="PENDING",
                         attempts=0, next_attempt_at=None, last_attempt_at=None, last_success_at=None,
                         last_error_code=None, remote_task_key=None, cancel_requested_at=None,
                         cancellation="NONE", poll_failures=0)
    task.status = enums.TaskStatus.WAITING_EXTERNAL
    audit.record(db, actor=founder, action="handoff.release", entity_type="task", entity_key=task.key,
                 task_id=task.id, before={"status": previous.value},
                 after={"status": task.status.value, **_release_fields(remote)})
    db.commit()
    return {"id": remote["id"], "payload_sha256": remote["payload_sha256"], "delivery": "PENDING"}


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
    expires = _utc(packet.get("expires_at"))
    if expires is None or now >= expires:
        return "RELEASE_EXPIRED"
    return None


def request_cancel(db: Session, task: models.Task, *, actor: str, now: datetime | None = None) -> None:
    """Called inside the founder's CANCELLED status change (caller commits): remote withdrawal becomes durable."""
    remote = _remote(task)
    if remote is None or remote.get("cancel_requested_at"):
        return
    now = now or datetime.now(timezone.utc)
    _set_remote(task, cancel_requested_at=now.isoformat(), next_attempt_at=None)
    audit.record(db, actor=actor, action="handoff.cancel_requested", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": remote.get("id"), "payload_sha256": remote.get("payload_sha256")})


def status_refusal(task: models.Task, status: enums.TaskStatus, now: datetime | None = None) -> str | None:
    """What the founder's status route may not do to a handed-off task: production never runs it (no READY/QUEUED/
    IN_PROGRESS), and DONE needs a fresh, validated Stage C snapshot that reports a verified merge."""
    remote = _remote(task)
    if remote is None:
        return None
    if status in (enums.TaskStatus.READY, enums.TaskStatus.QUEUED, enums.TaskStatus.IN_PROGRESS):
        return "a task handed to Stage C never runs on production"
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
    environment, bounded time and response size. Transport errors are status 0, never raised."""
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
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, verify=True, timeout=TIMEOUT_S) as client:
            with client.stream(method, origin + path, content=body, headers=headers) as r:
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_RESPONSE_BYTES:
                        return 0, b"", None
                return r.status_code, data, r.headers.get(MAC_HEADER)
    except Exception:  # noqa: BLE001 - any transport failure is a retryable "unreachable", never a crash
        log.warning("stage c handoff transport failed: %s %s", method, path)
        return 0, b"", None


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
    return min(timedelta(seconds=30 * 2 ** max(failures - 1, 0)), MAX_BACKOFF)


def sync(db: Session, transport: Transport | None = None, *, paused: bool = False,
         now: datetime | None = None) -> dict:
    """Deterministic, model-free, bounded: cancellations first, then (unless agents are paused) new deliveries, then
    status polls; at most REQUESTS_PER_TICK requests. No DB lock is held across a network call."""
    if not sender_enabled():
        return {"handoff": "disabled"}
    transport = transport or http_transport
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(models.Task).where(
        models.Task.context["remote_stage_c"]["id"].as_string().is_not(None)).order_by(models.Task.id)).all()
    cancels, deliveries, polls = [], [], []
    for task in rows:
        remote = _remote(task)
        if remote is None:
            continue
        if remote.get("cancel_requested_at") and remote.get("cancellation") != "ACKNOWLEDGED":
            cancels.append(task.id)
        elif task.status != enums.TaskStatus.WAITING_EXTERNAL:
            continue    # closed by the founder: nothing more is read or sent
        elif remote.get("delivery") == "PENDING" and not paused and _due(remote, now):
            deliveries.append(task.id)
        elif remote.get("delivery") == "DELIVERED" and _due(remote, now):
            polls.append((remote.get("last_attempt_at") or "", task.id))
    db.commit()  # end the read transaction before any network call
    done = []
    work = [("cancel", i) for i in cancels] + [("deliver", i) for i in deliveries] + \
           [("poll", i) for _, i in sorted(polls)]
    for kind, task_id in work[:REQUESTS_PER_TICK]:
        try:
            done.append({"cancel": _cancel, "deliver": _deliver, "poll": _poll}[kind](db, task_id, transport, now))
        except Exception:  # noqa: BLE001 - one bad row must not stop the others or the worker tick
            db.rollback()
            log.exception("stage c handoff %s failed for task id %s", kind, task_id)
            done.append({"task_id": task_id, "kind": kind, "outcome": "ERROR"})
    return {"handoff": "enabled", "requests": done}


def _fail(db, task, remote, code: str, *, hard: bool, now: datetime, delivery: bool) -> dict:
    attempts = int(remote.get("attempts") or 0) + 1 if delivery else int(remote.get("attempts") or 0)
    polls = int(remote.get("poll_failures") or 0) + (0 if delivery else 1)
    state = remote.get("delivery")
    if delivery:
        state = "REFUSED" if hard else ("SUSPENDED" if attempts >= MAX_DELIVERY_FAILURES else "PENDING")
    _set_remote(task, delivery=state, attempts=attempts, poll_failures=polls, last_error_code=code,
                last_attempt_at=now.isoformat(),
                next_attempt_at=(now + _backoff(attempts if delivery else polls)).isoformat())
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
    _set_remote(task, delivery="DELIVERED", remote_task_key=receipt.task_key, attempts=0, last_error_code=None,
                last_attempt_at=now.isoformat(), last_success_at=now.isoformat(), next_attempt_at=None)
    audit.record(db, actor="company-worker", action="handoff.delivery", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"id": hid, "payload_sha256": receipt.payload_sha256,
                                         "remote_task_key": receipt.task_key, "http": status})
    db.commit()
    return {"task": task.key, "kind": "deliver", "outcome": "DELIVERED"}


def _poll(db: Session, task_id: int, transport: Transport, now: datetime) -> dict:
    task = db.get(models.Task, task_id)
    remote = _remote(task) if task is not None else None
    if remote is None:
        return {"task_id": task_id, "kind": "poll", "outcome": "SKIPPED"}
    hid = remote["id"]
    db.commit()
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
    """Monotonic per handoff: a lower seq is stale and dropped, an equal one only refreshes freshness. A verified
    merge, once observed, is never replaced by a report without one."""
    stored = (task.artifacts or {}).get("remote_stage_c") if isinstance(task.artifacts, dict) else None
    last_seq = stored.get("seq") if isinstance(stored, dict) and type(stored.get("seq")) is int else -1
    previous = (stored.get("snapshot") or {}) if isinstance(stored, dict) else {}
    merged = (previous.get("merge") or {}).get("state") == "VERIFIED"
    closed = (previous.get("task") or {}).get("status") in ("DONE", "CANCELLED")
    fresh = {"last_attempt_at": now.isoformat(), "poll_failures": 0, "last_error_code": None,
             "next_attempt_at": (now + POLL_EVERY).isoformat()}
    # Latches: a verified merge or a closed remote task is never replaced by a report without it.
    if (snap.seq < last_seq or (merged and snap.merge.state != "VERIFIED")
            or (closed and (snap.task is None or snap.task.status.value != previous["task"]["status"]))):
        _set_remote(task, **{**fresh, "last_error_code": "STALE_REPORT"})
        db.commit()
        return {"task": task.key, "kind": "poll", "outcome": "STALE"}
    _set_remote(task, **fresh, last_success_at=now.isoformat())
    if snap.seq == last_seq:
        db.commit()
        return {"task": task.key, "kind": "poll", "outcome": "UNCHANGED"}
    task.artifacts = {**(task.artifacts or {}), "remote_stage_c": {
        "seq": snap.seq, "observed_at": now.isoformat(), "snapshot": snap.model_dump(mode="json")}}
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
    hid, sha = remote["id"], remote["payload_sha256"]
    db.commit()
    path = f"{API_PREFIX}{hid}/cancel"
    status, body, sig = transport("POST", path, canonical({"payload_sha256": sha, "reason": "PRODUCTION_CANCELLED"}))
    task = _locked(db, task_id)
    remote = _remote(task)
    data = _verified("POST", path, status, body, sig) if status == 200 else None
    try:
        ack = CancelAck.model_validate(data) if data is not None else None
    except ValidationError:
        ack = None
    if ack is None or ack.id != hid:
        code = _transport_code(status) if status != 200 else ("BAD_RESPONSE" if data is not None else "BAD_MAC")
        _set_remote(task, last_error_code=code, last_attempt_at=now.isoformat())
        db.commit()
        return {"task": task.key, "kind": "cancel", "outcome": code}   # "remote stop unconfirmed" until acknowledged
    _set_remote(task, cancellation="ACKNOWLEDGED", last_error_code=None, last_attempt_at=now.isoformat())
    audit.record(db, actor="company-worker", action="handoff.cancel_acknowledged", entity_type="task",
                 entity_key=task.key, task_id=task.id, after={"id": hid, "remote_task_key": ack.task_key})
    db.commit()
    return {"task": task.key, "kind": "cancel", "outcome": "ACKNOWLEDGED"}


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
    last = _utc(remote.get("last_success_at"))
    fresh = stored.get("snapshot") is not None and last is not None and timedelta(0) <= now - last <= STALE_AFTER
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
    """Idempotent import of one immutable packet: 201 created, 200 identical replay. The ControlState primary key
    serializes concurrent deliveries; reservation, task and audit commit together."""
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
    row = _row(db, hid, lock=True)
    if row is not None:
        return _existing(db, hid, row, packet.payload_sha256)
    if now >= expires:
        raise HandoffError(410, "EXPIRED")
    scope = {"paths": list(packet.task.paths), "context": list(packet.task.context)}
    if stage_c_worker.scope_refused(scope, set(stage_c_worker.task_paths())):
        raise HandoffError(422, "SCOPE_REFUSED")
    project = db.scalar(select(models.Project).where(models.Project.key == "company-os"))
    agent = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
    if project is None or agent is None:
        raise HandoffError(409, "RECEIVER_NOT_CONFIGURED")
    reservation = {"origin": "production", "origin_task_key": packet.origin_task_key,
                   "payload_sha256": packet.payload_sha256, "task_key": None,
                   "expires_at": expires.isoformat(), "cancelled_at": None, "first_authorized_at": None}
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
    audit.record(db, actor=SERVICE, action="task.create", entity_type="task", entity_key=task.key,
                 reason="stage c handoff import", task_id=task.id,
                 after={"status": task.status.value, "handoff_id": hid})
    audit.record(db, actor=SERVICE, action="handoff.import", entity_type="control_state",
                 entity_key=KEY_PREFIX + hid, task_id=task.id,
                 after={"id": hid, "payload_sha256": packet.payload_sha256, "task_key": task.key,
                        "origin_task_key": packet.origin_task_key})
    db.commit()
    return 201, _receipt(db, hid, row.value)


def cancel_import(db: Session, hid: str, sha: str, now: datetime | None = None) -> dict:
    """Withdraw only this import. A tombstone is written even before the PUT arrives, so a delayed PUT cannot
    resurrect it. Cancels the local task if it is still open and revokes the pilot grant only if it is bound to this
    handoff. A run already in flight stops at its next owner check (acknowledgement is not proof of termination)."""
    now = now or datetime.now(timezone.utc)
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
    row.value = {**value, "cancelled_at": now.isoformat()}
    task = db.scalar(select(models.Task).where(models.Task.key == value.get("task_key")).with_for_update()
                     .execution_options(populate_existing=True)) if value.get("task_key") else None
    if task is not None and task.status not in (enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED):
        before = task.status.value
        task.status, task.completed_at = enums.TaskStatus.CANCELLED, now
        audit.record(db, actor=SERVICE, action="task.status", entity_type="task", entity_key=task.key,
                     task_id=task.id, reason="PRODUCTION_CANCELLED", before={"status": before},
                     after={"status": "CANCELLED"})
    grant = db.scalar(select(models.ControlState).where(models.ControlState.key == control.PILOT_AUTHORIZATION)
                      .with_for_update().execution_options(populate_existing=True))
    gv = grant.value if grant is not None and isinstance(grant.value, dict) else {}
    if grant is not None and gv.get("handoff_id") == hid and gv.get("task_key") == value.get("task_key"):
        db.delete(grant)
        audit.record(db, actor=SERVICE, action="stage-c.authorization.revoke", entity_type="control_state",
                     entity_key=control.PILOT_AUTHORIZATION, reason="handoff cancelled", before=dict(gv), after=None)
    audit.record(db, actor=SERVICE, action="handoff.cancel", entity_type="control_state", entity_key=KEY_PREFIX + hid,
                 task_id=task.id if task else None, after={"id": hid, "payload_sha256": sha,
                                                           "task_key": value.get("task_key")})
    db.commit()
    return {"id": hid, "cancellation": "ACKNOWLEDGED", "task_key": value.get("task_key")}


def _seq(db: Session, hid: str, task: models.Task | None) -> int:
    """Stage C's newest audit row for this task, its handoff row or the pilot grant: strictly increasing with
    every audited change, unchanged by reads. ponytail: the pilot grant is a singleton, so another task's grant
    also bumps it (harmless: more reports, never fewer)."""
    cond = [models.AuditLog.entity_key.in_((KEY_PREFIX + hid, control.PILOT_AUTHORIZATION))]
    if task is not None:
        cond.append(models.AuditLog.task_id == task.id)
    return db.scalar(select(func.max(models.AuditLog.id)).where(or_(*cond))) or 0


def _grant_view(db: Session, task: models.Task | None, hid: str, now: datetime) -> dict:
    row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    if task is None or value.get("task_key") != task.key or value.get("handoff_id") != hid:
        return {"status": "NONE", "source_sha": None, "expires_at": None}
    denial = control._authorization_denial(db, row, task.key, value.get("source_sha") or "", now) \
        or grant_denial(db, task, value)
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
    row = _row(db, hid)
    if row is None:
        raise HandoffError(404, "NOT_FOUND")
    value = row.value if isinstance(row.value, dict) else {}
    task = db.scalar(select(models.Task).where(models.Task.key == value.get("task_key"))) \
        if value.get("task_key") else None
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
            "task_key": value.get("task_key"), "seq": _seq(db, hid, task), "generated_at": now.isoformat(),
            "runtime": {"api_sha": stage_c_state._sha(os.environ.get("COMPANY_OS_GIT_SHA")),
                        "worker_sha": stage_c_state._sha(runtime.get("git_sha")),
                        "write_enabled": flag if isinstance(flag, bool) else None},
            "authorization": _grant_view(db, task, hid, now), "task": projection, "merge": merge,
            "cancellation": "ACKNOWLEDGED" if value.get("cancelled_at") else "NONE"}


# ---------------------------------------------------------------------------- Stage C grant binding
_GRANT_FIELDS = ("task_key", "source_sha", "issued_at", "expires_at", "issued_by", "handoff_id", "payload_sha256")


def imported(task) -> dict | None:
    """The import binding of a Stage C task created by a handoff, or None for a native task."""
    ctx = task.context if isinstance(task.context, dict) else {}
    ref = ctx.get("remote_stage_c")
    return ref if isinstance(ref, dict) and ref.get("origin") == "production" else None


def grant_denial(db: Session, task, value: dict) -> str | None:
    """An imported task's pilot grant must be issued for exactly this handoff and digest (and its issue audit must
    say so), and the import must not be withdrawn. Anything else denies; there is no standing-grant fallback."""
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
    expires = _utc(hv.get("expires_at"))
    if not hv.get("first_authorized_at") and (expires is None or now >= expires):
        raise ValueError("the handoff expired before its first authorization; production must release it again")
    if not hv.get("first_authorized_at"):
        row.value = {**hv, "first_authorized_at": now.isoformat()}
    return {"handoff_id": handoff_id, "payload_sha256": payload_sha256}
