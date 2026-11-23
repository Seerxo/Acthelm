"""B5 Desk -> Stage C handoff (app/stage_c_handoff.py) across two isolated databases: a production app and a Stage C
app (the same FastAPI code, each call routed to its own SQLite engine and environment). Breaks caught: the handoff
works while its flag is off, a non-service or non-founder caller reaches it, a replayed delivery creates a second task,
grant or run, the founder's release is not re-verified at delivery, an unauthenticated, tampered or stale Stage C
report is stored or moves state backwards, an imported task falls back to a standing grant, and the result never
reaches the production Desk. The coder, launcher and reviewer are not exercised: stage_c.advance drives the pipeline.
Short test ids on purpose (root conftest byte budget)."""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cloudflare_access, control, enums, main, models, stage_c, stage_c_handoff as h

PROD_HOST, SC_HOST = "desk.example.invalid", "stagec.example.invalid"
FOUNDER, FID = "founder@example.invalid", "founder:access:founder@example.invalid"
ASSERT = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
TOKEN, ADMIN, DESK = "h" * 40, "a" * 40, "d" * 40
SHA, HEAD, MERGE = "c" * 40, "e" * 40, "f" * 40
BODY = {"title": "Fix goal rounding", "objective": "Round progress once", "success_criteria": "Tests pass",
        "paths": ["app/goals.py", "tests/test_goals.py"], "context": ["app/models.py"]}
SIDES = {
    "prod": {"CF_ACCESS_DESK_HOST": PROD_HOST, h.SENDER_FLAG: "true", h.URL_ENV: f"https://{SC_HOST}",
             h.RECEIVER_FLAG: None},
    "sc": {"CF_ACCESS_DESK_HOST": SC_HOST, h.RECEIVER_FLAG: "true", h.SENDER_FLAG: None, h.URL_ENV: None},
}


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    return engine


class World:
    def __init__(self, monkeypatch):
        self.mp, self.prod, self.sc, self.calls = monkeypatch, _engine(), _engine(), []
        with Session(self.sc) as s:
            s.add_all([models.Project(key="company-os", name="Company OS", status=enums.ProjectStatus.ACTIVE),
                       models.Agent(key="developer", role="dev")])
            s.commit()
        with Session(self.prod) as s:   # a production developer code task, as the materializer or Desk stores it
            dev = models.Agent(key="developer", role="dev")
            s.add(dev)
            s.flush()
            s.add(models.Task(key="TASK-0001", title=BODY["title"], objective=BODY["objective"],
                              success_criteria=BODY["success_criteria"], status=enums.TaskStatus.BACKLOG,
                              assigned_agent_id=dev.id, context={"write": True, "stage_c": {
                                  "paths": BODY["paths"], "context": BODY["context"]}}))
            s.commit()
        self.side = None

    @contextmanager
    def on(self, side, **extra):
        previous = self.side
        self.use(side, **extra)
        try:
            yield
        finally:
            if previous:
                self.use(previous)

    def use(self, side, **extra):
        self.side = side
        for k, v in {**SIDES[side], **extra}.items():
            self.mp.delenv(k, raising=False) if v is None else self.mp.setenv(k, v)
        engine = self.prod if side == "prod" else self.sc

        def get_db():
            with Session(engine) as s:
                yield s
        main.app.dependency_overrides[main.get_db] = get_db

    def client(self, host=None, peer="172.18.0.5"):
        host = host or (PROD_HOST if self.side == "prod" else SC_HOST)
        return TestClient(main.app, base_url=f"https://{host}", client=(peer, 50000))

    def transport(self, method, path, body):
        """Production's HTTP call, served by the Stage C app with the bearer and a Cloudflare service assertion."""
        self.calls.append((method, path))
        with self.on("sc"):
            r = self.client().request(method, path, content=body, headers={
                "authorization": f"Bearer {TOKEN}", "cf-access-jwt-assertion": "service-assertion",
                "content-type": "application/json"})
        self.last = (r.status_code, r.content, r.headers.get(h.MAC_HEADER))
        return self.last

    def sync(self, transport=None, **kw):
        with self.on("prod"), Session(self.prod) as s:
            return h.sync(s, transport or self.transport, **kw)

    def prod_task(self):
        with Session(self.prod) as s:
            return s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))

    def sc_tasks(self):
        with Session(self.sc) as s:
            return list(s.scalars(select(models.Task)))

    def release(self, body=None, key="TASK-0001"):   # the request names the task only
        with self.on("prod"):
            return self.client().post(f"/tasks/{key}/handoff", json=body, headers=ASSERT)


@pytest.fixture
def w(monkeypatch):
    for k, v in {"COMPANY_API_TOKEN_ADMIN": ADMIN, "COMPANY_API_TOKEN_DESK": DESK, h.TOKEN_ENV: TOKEN,
                 "API_AUTH_MODE": "enforce", "COMPANY_OS_GIT_SHA": SHA, "API_HOST_IPS": "10.0.6.1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ENABLE_AUTONOMOUS_CODE_WRITE", raising=False)

    async def identity(token, **kw):   # a Cloudflare service-token assertion is never a founder
        return {"founder-assertion": FOUNDER}.get(token)
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)
    monkeypatch.setattr(main.worker, "worker_state",
                        lambda db: {"runtime": {"git_sha": SHA}, "autonomy": {"code_write_flag": False}})
    world = World(monkeypatch)
    world.use("prod")
    yield world
    main.app.dependency_overrides.clear()


def _authorize(w, task_key, **binding):
    with w.on("sc"):
        return w.client().post("/stage-c/authorization", json={"task_key": task_key, **binding}, headers=ASSERT)


def _claim(w, key):
    """The worker's claim step: a STARTED run and the real consume (stage_c_worker.execute)."""
    with Session(w.sc) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == key))
        run = models.AgentRun(task_id=task.id, status=enums.AgentRunStatus.STARTED, tool_calls={"stage_c": True})
        s.add(run)
        s.flush()
        denial = control.consume_write_authorization(s, task, SHA, run.id)
        s.commit() if denial is None else s.rollback()
        return denial


def _pipeline(w, key, upto="MERGED"):
    """Fake coder/reviewer/CI/founder merge: the recorded lifecycle the real pipeline writes."""
    evidence = {"TESTING": {"checks_passed": True}, "PR_OPEN": {"pr": "https://github.com/seerxo/acthelm/pull/7",
                                                                "commit": HEAD},
                "CI": {"ci_state": "passed", "commit": HEAD},
                "MERGED": {"merged_by": "founder-login", "merge_commit": MERGE}}
    with Session(w.sc) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == key))
        for stage in stage_c.STAGES[:stage_c.STAGES.index(upto) + 1]:
            actor = "founder (github:founder-login)" if stage == "MERGED" else "company-worker"
            stage_c.advance(s, task, stage, actor, evidence.get(stage))
        s.commit()


# ------------------------------------------------------------------------------------------------ off by default
def test_off(w, monkeypatch):
    for k in (h.SENDER_FLAG, h.RECEIVER_FLAG):
        monkeypatch.delenv(k, raising=False)
    w.side = "prod"
    with w.on("prod", **{h.SENDER_FLAG: None}):
        assert w.client().post("/tasks/TASK-0001/handoff", headers=ASSERT).status_code == 403
        assert w.client().get("/desk/handoffs", headers=ASSERT).json()["enabled"] is False
    with w.on("prod", **{h.SENDER_FLAG: None}), Session(w.prod) as s:
        assert h.sync(s, w.transport) == {"handoff": "disabled"} and w.calls == []
    with w.on("sc", **{h.RECEIVER_FLAG: None}):
        r = w.client().put(f"{h.API_PREFIX}{'0' * 8}-0000-4000-8000-{'0' * 12}", content=b"{}",
                           headers={"authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 403 and w.sc_tasks() == []
    # Fail closed: both roles in one process, a short token, a non-https origin, or code write on.
    for extra in ({h.RECEIVER_FLAG: "true"}, {h.TOKEN_ENV: "short"}, {h.URL_ENV: f"http://{SC_HOST}"},
                  {"ENABLE_AUTONOMOUS_CODE_WRITE": "true"}):
        with w.on("prod", **extra):
            assert not h.sender_enabled(), extra
    with w.on("sc", **{h.SENDER_FLAG: "true"}):
        assert not h.receiver_enabled()
    assert w.prod_task().status == enums.TaskStatus.BACKLOG


# ------------------------------------------------------------------------------------------------ authentication
def test_auth(w):
    assert w.release().status_code == 201
    w.sync()
    path = w.calls[0][1]
    with w.on("sc"):
        callers = [({}, "172.18.0.5", SC_HOST, 401), ({"authorization": f"Bearer {ADMIN}"}, "172.18.0.5", "api", 403),
                   ({}, "127.0.0.1", "api", 403), ({"authorization": f"Bearer {DESK}"}, "172.18.0.5", "api", 403),
                   (ASSERT, "172.18.0.5", SC_HOST, 403), ({"authorization": f"Bearer {DESK}"}, "172.18.0.5", SC_HOST, 401)]
        for headers, peer, host, code in callers:
            for method in ("GET", "PUT"):
                r = w.client(host=None if host == SC_HOST else "api.example.invalid", peer=peer).request(
                    method, path, content=b"{}", headers=headers)
                assert r.status_code == code, (headers, peer, host, method, r.text)
        # The handoff bearer outside its namespace stays founder-only on the Desk host, scoped elsewhere.
        bearer = {"authorization": f"Bearer {TOKEN}"}
        assert w.client().get("/stage-c/state", headers=bearer).status_code == 401
        assert w.client(host="api.example.invalid").get("/stage-c/state", headers=bearer).status_code == 403
        assert w.client(host="api.example.invalid").post("/stage-c/tasks", json=BODY,
                                                        headers=bearer).status_code == 403
        w.mp.setenv("API_AUTH_MODE", "log")    # rollout mode never opens the machine routes
        assert w.client(host="api.example.invalid").get(path).status_code == 401
    # Release and the Desk read are the verified founder's only.
    with w.on("prod"):
        for headers in ({}, {"authorization": f"Bearer {ADMIN}"}, {"authorization": f"Bearer {TOKEN}"}):
            assert w.client(host="api.example.invalid").post("/tasks/TASK-0001/handoff",
                                                              headers=headers).status_code in (401, 403)
            assert w.client(host="api.example.invalid").get("/desk/handoffs", headers=headers).status_code in (401, 403)
    assert len(w.sc_tasks()) == 1


# ------------------------------------------------------------------------------------------------ end to end
def test_e2e(w):
    r = w.release()
    assert r.status_code == 201 and r.json()["delivery"] == "PENDING", r.text
    hid, sha = r.json()["id"], r.json()["payload_sha256"]
    assert w.release().json()["id"] == hid                                  # repeat click: same release
    assert w.release({"objective": "Something else"}).status_code == 422    # content is never client-supplied
    assert w.prod_task().context["remote_stage_c"]["packet"]["task"] == BODY   # built from the stored task
    assert w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL

    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    [imp] = w.sc_tasks()
    # Phase 1 only: the import cannot be authorized or run before production's authenticated activation.
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code == 422
    assert _claim(w, imp.key) is not None
    assert w.sync()["requests"][0]["outcome"] == "ACTIVATED"
    assert imp.status == enums.TaskStatus.BACKLOG and imp.created_by == h.SERVICE
    from app import worker
    with Session(w.sc) as s:   # the production-side "never run it here" guard does not touch Stage C imports
        assert worker._admission_gate(s, s.get(models.Task, imp.id), {})[0] != "handed_off_to_stage_c"
    assert imp.context["remote_stage_c"] == {"id": hid, "origin": "production", "origin_task_key": "TASK-0001",
                                             "payload_sha256": sha}
    packet = w.prod_task().context["remote_stage_c"]["packet"]

    # Founder authorization on Stage C must name this handoff; then one claim spends it.
    assert _authorize(w, imp.key).status_code == 422
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256="0" * 64).status_code == 422
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha, expected_source_sha="b" * 40).status_code == 409
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha, expected_source_sha=SHA).status_code == 201
    assert _claim(w, imp.key) is None
    assert _claim(w, imp.key) == "stage_c_authorization_consumed"

    # A resubmitted packet (lost reply, retry, replay) after the run started: same receipt, nothing new.
    status, body, _ = w.transport("PUT", h.API_PREFIX + hid, h.canonical(packet))
    assert status == 200 and json.loads(body)["task_key"] == imp.key
    # ...and a repeated activation is idempotent: no new task, grant, run or activation.
    for _ in range(2):
        assert w.transport("POST", f"{h.API_PREFIX}{hid}/activate", h.canonical({"payload_sha256": sha}))[0] == 200
    with Session(w.sc) as s:
        assert s.scalar(select(func.count()).select_from(models.Task)) == 1
        assert s.scalar(select(func.count()).select_from(models.AgentRun)) == 1
        assert s.get(models.ControlState, control.PILOT_AUTHORIZATION).value["consumed_at"] is not None
        for action in ("handoff.import", "handoff.activate"):
            assert s.scalar(select(func.count()).select_from(models.AuditLog)
                            .where(models.AuditLog.action == action)) == 1

    _pipeline(w, imp.key)
    out = w.sync()["requests"][0]
    assert out["outcome"] == "OBSERVED", out
    with w.on("prod"):
        [row] = w.client().get("/desk/handoffs", headers=ASSERT).json()["handoffs"]
        detail = w.client().get("/tasks/TASK-0001", headers=ASSERT).json()["remote_stage_c"]
    snap = row["handoff"]["snapshot"]
    assert row["handoff"]["freshness"] == "FRESH" and detail["snapshot"] == snap
    assert snap["merge"] == {"state": "VERIFIED", "commit": MERGE, "at": snap["merge"]["at"]}
    assert snap["task"]["pr"] == {"number": 7, "head_sha": HEAD} and snap["task"]["ci"]["state"] == "SUCCESS"
    assert snap["authorization"]["status"] == "CONSUMED" and snap["task_key"] == imp.key
    assert w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL       # a report never closes production work
    with Session(w.prod) as s:
        actions = [a for (a,) in s.execute(select(models.AuditLog.action).order_by(models.AuditLog.id))]
        assert actions == ["handoff.release", "handoff.delivery", "handoff.activation", "handoff.observed"]

    with w.on("prod"):
        assert w.client().post("/tasks/TASK-0001/status", json={"status": "READY", "actor": "x"},
                               headers=ASSERT).status_code == 409
        r = w.client().post("/tasks/TASK-0001/status", headers=ASSERT, json={
            "status": "DONE", "actor": "founder", "evidence": f"merged {MERGE}"})
        assert r.status_code == 200, r.text
    before = len(w.calls)
    assert w.sync()["requests"] == [] and len(w.calls) == before            # closed: nothing more is read or sent


def test_done_guard(w):
    w.release()
    w.sync()
    with w.on("prod"):
        r = w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                            json={"status": "DONE", "actor": "founder", "evidence": "trust me"})
    assert r.status_code == 409 and w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL


# ------------------------------------------------------------------------------------------------ re-verification
@pytest.mark.parametrize("change", ["edited", "forged", "expired", "retitled_audit"])
def test_reverify(w, change):
    """Founder authorization is re-checked at handoff time: nothing reaches Stage C after any of these."""
    if change == "forged":   # a hand-written release with no founder audit row
        with Session(w.prod) as s:
            task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
            pkt = {"version": 1, "id": "11111111-1111-4111-8111-111111111111", "origin": "production",
                   "origin_task_key": "TASK-0001", "repository": "seerxo/acthelm",
                   "issued_at": datetime.now(timezone.utc).isoformat(),
                   "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                   "task": {k: BODY[k] for k in ("title", "objective", "success_criteria", "paths", "context")}}
            pkt["payload_sha256"] = h.digest(pkt)
            task.context = {"remote_stage_c": {"id": pkt["id"], "payload_sha256": pkt["payload_sha256"], "packet": pkt,
                                               "released_by": FID, "delivery": "PENDING", "source_sha256": h.source_digest(task),
                                               "expires_at": pkt["expires_at"]}}
            task.status = enums.TaskStatus.WAITING_EXTERNAL
            s.commit()
        code = "RELEASE_NOT_AUTHORIZED"
    else:
        assert w.release().status_code == 201
        with Session(w.prod) as s:
            task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
            if change == "edited":
                task.objective = "something the founder never saw"
            if change == "retitled_audit":
                row = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.release"))
                row.actor = "operator:host"
            s.commit()
        code = {"edited": "TASK_CHANGED", "expired": "RELEASE_EXPIRED", "retitled_audit": "RELEASE_NOT_AUTHORIZED"}[change]
    now = datetime.now(timezone.utc) + (timedelta(hours=25) if change == "expired" else timedelta(0))
    out = w.sync(now=now)["requests"]
    assert out[0]["outcome"] == code and w.calls == [] and w.sc_tasks() == []
    assert w.prod_task().context["remote_stage_c"]["delivery"] == "REFUSED"
    assert w.sync(now=now)["requests"] == []                                 # a refusal is not retried



@pytest.mark.parametrize("change", ["expired", "edited"])
def test_the_founder_renews_an_expired_or_edited_release(w, change):
    """R3 finding 4: a release refused for RELEASE_EXPIRED / TASK_CHANGED was stuck for good. The founder's own new
    release (never sync) supersedes it with a new packet from the task as stored now; the old one is never sent."""
    old = w.release().json()
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        if change == "edited":
            task.objective = "the corrected objective"
        s.commit()
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    out = w.sync(now=later if change == "expired" else None)["requests"]
    assert out[0]["outcome"] == {"expired": "RELEASE_EXPIRED", "edited": "TASK_CHANGED"}[change] and w.calls == []
    assert w.sync()["requests"] == []                                         # sync alone never renews it
    assert w.prod_task().context["remote_stage_c"]["id"] == old["id"]
    r = w.release()
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != old["id"] and new["payload_sha256"] != old["payload_sha256"] and new["delivery"] == "PENDING"
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["packet"]["task"]["objective"] == w.prod_task().objective
    assert w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL
    with Session(w.prod) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0001")
                         .order_by(models.AuditLog.id)).all()
        actions = [r.action for r in rows]
        assert actions.count("handoff.release") == 2 and "handoff.supersede" in actions   # history kept
        sup = next(r for r in rows if r.action == "handoff.supersede")
        assert sup.actor == FID and sup.before["id"] == old["id"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    assert [c for c in w.calls if old["id"] in c[1]] == []                    # the old packet never reached Stage C
    assert [t.context["remote_stage_c"]["id"] for t in w.sc_tasks()] == [new["id"]]


def test_a_superseded_release_cannot_be_put_back(w):
    """The old packet's founder release no longer authorizes anything once a new release superseded it."""
    w.release()
    old_remote = dict(w.prod_task().context["remote_stage_c"])
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    w.release()
    with Session(w.prod) as s:                                                # replay the old record by hand
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        task.context = {**task.context, "remote_stage_c": {**old_remote, "delivery": "PENDING"}}
        s.commit()
    out = w.sync()["requests"]
    assert out[0]["outcome"] == "RELEASE_NOT_AUTHORIZED" and w.calls == []
    assert w.sc_tasks() == []


def _open_sc(w):
    return [t for t in w.sc_tasks() if t.status not in (enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED)]


def _sc_activate(w, hid, sha):
    with w.on("sc"), Session(w.sc) as s:
        return h.activate_import(s, hid, sha)


def _refused_after_import(w, change):
    """Release, import on Stage C, then let the activation be refused (expired or edited) before it is ever sent."""
    old = w.release().json()
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    if change == "edited":
        with Session(w.prod) as s:
            s.scalar(select(models.Task).where(models.Task.key == "TASK-0001")).objective = "the corrected objective"
            s.commit()
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    out = w.sync(now=later if change == "expired" else None)["requests"]
    assert out[0]["outcome"] == {"expired": "RELEASE_EXPIRED", "edited": "TASK_CHANGED"}[change]
    assert not [c for c in w.calls if c[1].endswith("/activate")]           # never activated
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["delivery"] == "REFUSED" and remote["remote_task_key"]
    return old


@pytest.mark.parametrize("change", ["expired", "edited"])
def test_renew_after_import(w, change):
    """R3 review finding 4: a packet refused after IMPORT but before ACTIVATION was stranded (renewal needed
    remote_task_key None). Only the founder's new release renews it; Stage C retires the old import when the new one
    arrives, so there is never a second open task and the old packet can never be activated."""
    old = _refused_after_import(w, change)
    assert w.sync()["requests"] == [] and w.prod_task().context["remote_stage_c"]["id"] == old["id"]   # never auto
    r = w.release()
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != old["id"] and new["payload_sha256"] != old["payload_sha256"]
    with Session(w.prod) as s:
        sup = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede"))
        assert sup.actor == FID and sup.before["id"] == old["id"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    [live] = _open_sc(w)
    assert live.context["remote_stage_c"]["id"] == new["id"] and len(w.sc_tasks()) == 2   # no duplicate open task
    [gone] = [t for t in w.sc_tasks() if t.context["remote_stage_c"]["id"] == old["id"]]
    assert gone.status == enums.TaskStatus.CANCELLED
    with pytest.raises(h.HandoffError) as e:                                  # the old import never activates
        _sc_activate(w, old["id"], old["payload_sha256"])
    assert e.value.code == "CANCELLED"
    assert w.sync()["requests"][0]["outcome"] == "ACTIVATED"
    with Session(w.sc) as s:
        assert s.scalar(select(func.count()).select_from(models.AuditLog)
                        .where(models.AuditLog.action == "handoff.supersede")) == 1


def test_renew_refused_if_activated(w):
    """Once production recorded an activation, the old packet is Stage C's live work: never superseded."""
    old = w.release().json()
    w.sync()
    assert w.sync()["requests"][0]["outcome"] == "ACTIVATED"
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        task.context = {**task.context, "remote_stage_c": {**task.context["remote_stage_c"], "delivery": "REFUSED",
                                                             "last_error_code": "RELEASE_EXPIRED"}}
        s.commit()
    assert w.release().json()["id"] == old["id"]
    with Session(w.prod) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede")) is None


def test_renew_lost_activation_ack(w):
    """Stage C activated the old import but production never saw the ack: the renewal reaches Stage C, which refuses
    it (409, a hard stop) rather than retire live work or open a second task."""
    old = w.release().json()
    w.sync()
    assert _sc_activate(w, old["id"], old["payload_sha256"])["activation"] == "ACTIVATED"
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    new = w.release().json()
    assert new["id"] != old["id"]
    out = w.sync()["requests"][0]
    assert out["outcome"] == "HTTP_409" and w.prod_task().context["remote_stage_c"]["delivery"] == "REFUSED"
    [only] = _open_sc(w)
    assert only.context["remote_stage_c"]["id"] == old["id"] and len(w.sc_tasks()) == 1
    assert w.release().json()["id"] == new["id"]                              # HTTP_409 is never renewable


@pytest.mark.parametrize("who", ["cancel", "operator"])
def test_renew_refused(w, who):
    old = _refused_after_import(w, "expired")
    if who == "cancel":
        with Session(w.prod) as s:
            task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
            task.context = {**task.context, "remote_stage_c": {**task.context["remote_stage_c"],
                                                                 "cancel_requested_at": "2026-01-01T00:00:00+00:00"}}
            s.commit()
        assert w.release().json()["id"] == old["id"]
    else:
        with w.on("prod"), Session(w.prod) as s, pytest.raises(PermissionError):
            h.release(s, s.scalar(select(models.Task).where(models.Task.key == "TASK-0001")), founder="operator:x")
    with Session(w.prod) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede")) is None
    assert len(_open_sc(w)) == 1


@pytest.mark.parametrize("code", ["RELEASE_NOT_AUTHORIZED", "HTTP_409"])
def test_other_refusals_are_never_renewed(w, code):
    """Only an expired or changed release is renewable; any other refusal keeps its packet (a plain retry)."""
    old = w.release().json()
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        task.context = {**task.context, "remote_stage_c": {**task.context["remote_stage_c"], "delivery": "REFUSED",
                                                             "last_error_code": code}}
        s.commit()
    assert w.release().json()["id"] == old["id"]
    with Session(w.prod) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede")) is None

def test_cancel_first(w):
    """Founder cancels before delivery: Stage C gets a tombstone, and a delayed PUT can never import it."""
    hid = w.release().json()["id"]
    packet = w.prod_task().context["remote_stage_c"]["packet"]
    with w.on("prod"):
        assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 200
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED" and w.calls[0][0] == "POST"
    status, _, _ = w.transport("PUT", h.API_PREFIX + hid, h.canonical(packet))
    assert status == 410 and w.sc_tasks() == []
    assert w.sync()["requests"] == []


def test_cancel_running(w):
    hid = w.release().json()["id"]
    w.sync()
    w.sync()   # import, then activation
    [imp] = w.sc_tasks()
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code == 201
    with w.on("prod"):
        w.client().post("/tasks/TASK-0001/status", headers=ASSERT, json={"status": "CANCELLED", "actor": "founder"})
    w.sync()
    [imp] = w.sc_tasks()
    assert imp.status == enums.TaskStatus.CANCELLED
    with Session(w.sc) as s:
        assert s.get(models.ControlState, control.PILOT_AUTHORIZATION) is None   # only the bound grant is revoked
    assert _claim(w, imp.key) is not None
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code == 422


# ------------------------------------------------------------------------------------------------ receiver contract
def test_receiver(w):
    hid = w.release().json()["id"]
    packet = w.prod_task().context["remote_stage_c"]["packet"]
    path = h.API_PREFIX + hid
    for bad in ({**packet, "extra": 1}, {**packet, "id": "22222222-2222-4222-8222-222222222222"},
                {**packet, "task": {**packet["task"], "paths": ["app/main.py"]}}):
        assert w.transport("PUT", path, h.canonical(bad))[0] == 422
    assert w.transport("PUT", path, h.canonical({**packet, "task": {**packet["task"], "paths": ["app/main.py"]},
                                                  "payload_sha256": h.digest({**packet, "task": {
                                                      **packet["task"], "paths": ["app/main.py"]}})}))[0] == 422
    assert w.transport("PUT", path, h.canonical(packet))[0] == 201
    changed = {**packet, "task": {**packet["task"], "objective": "changed"}}
    changed["payload_sha256"] = h.digest(changed)
    assert w.transport("PUT", path, h.canonical(changed))[0] == 409          # same id, other text
    old = {**packet, "id": "33333333-3333-4333-8333-333333333333",
           "issued_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
           "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    old["payload_sha256"] = h.digest(old)
    assert w.transport("PUT", h.API_PREFIX + old["id"], h.canonical(old))[0] == 410
    # A correctly digested packet that declares anything but LOW-risk code work is refused, never down-graded.
    for change in ({"risk": "HIGH"}, {"kind": "operations"}):
        bad = {**packet, "id": str(uuid.uuid4()), **change}
        bad["payload_sha256"] = h.digest(bad)
        assert w.transport("PUT", h.API_PREFIX + bad["id"], h.canonical(bad))[0] == 422, change
    assert len(w.sc_tasks()) == 1


@pytest.mark.parametrize("change", ["high", "gated", "agent", "noscope", "closed"])
def test_eligible(w, change):
    """Production releases only a LOW-risk, ungated developer code task with a valid scope in an open status."""
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        if change == "high":
            task.risk_level = enums.RiskLevel.HIGH
        elif change == "gated":
            task.approval_required = True
        elif change == "agent":
            other = models.Agent(key="research", role="r")
            s.add(other)
            s.flush()
            task.assigned_agent_id = other.id
        elif change == "noscope":
            task.context = {"write": True}
        else:
            task.status = enums.TaskStatus.CANCELLED
        s.commit()
    assert w.release().status_code == 409
    assert "remote_stage_c" not in (w.prod_task().context or {}) and w.sync()["requests"] == []


def test_approval_binding(w):
    """Stage C's own Desk shows an imported task's binding; only the founder may approve, naming it exactly."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    w.sync()
    [imp] = w.sc_tasks()
    with w.on("sc"):
        view = w.client().get(f"/tasks/{imp.key}", headers=ASSERT).json()["stage_c_handoff"]
        assert view == {"id": hid, "payload_sha256": sha, "origin_task_key": "TASK-0001",
                        "activation": "IMPORTED", "cancelled": False}
    w.sync()   # activation
    body = {"task_key": imp.key, "handoff_id": hid, "payload_sha256": sha}
    with w.on("sc"):
        for headers, host in (({}, SC_HOST), ({"authorization": f"Bearer {ADMIN}"}, "api.example.invalid"),
                              ({"authorization": f"Bearer {TOKEN}"}, "api.example.invalid")):
            assert w.client(host=host).post("/stage-c/authorization", json=body,
                                            headers=headers).status_code in (401, 403), headers
        assert w.client().get(f"/tasks/{imp.key}", headers=ASSERT).json()["stage_c_handoff"]["activation"] == "ACTIVATED"
    assert _authorize(w, imp.key, handoff_id=hid).status_code == 422
    assert _authorize(w, imp.key, handoff_id=str(uuid.uuid4()), payload_sha256=sha).status_code == 422
    assert _authorize(w, imp.key, **{k: v for k, v in body.items() if k != "task_key"}).status_code == 201


def test_no_standing(w):
    """An imported task never rides a standing grant, even one that covers its files."""
    hid = w.release().json()["id"]
    w.sync()
    [imp] = w.sc_tasks()
    with Session(w.sc) as s:
        control.issue_standing(s, BODY["paths"], SHA, founder=FID)
        task = s.get(models.Task, imp.id)
        assert control.write_authorization_denial(s, task, SHA) == "stage_c_authorization_missing"
    assert _claim(w, imp.key) == "stage_c_authorization_missing"
    with Session(w.sc) as s:   # a plain (unbound) grant for it does not count either
        task = s.get(models.Task, imp.id)
        s.add(models.Task(key="TASK-0099", title="other", success_criteria="s"))
        s.commit()
        control.authorize_pilot(s, "TASK-0099", SHA, founder=FID)
        row = s.get(models.ControlState, control.PILOT_AUTHORIZATION)
        row.value = {**row.value, "task_key": imp.key}
        s.commit()
        assert control.write_authorization_denial(s, task, SHA) == "stage_c_authorization_invalid"
    assert hid


# ------------------------------------------------------------------------------------------------ reports back
def test_reports(w):
    """Stage C reports are authenticated and monotonic: tampered, unsigned, stale or replayed ones change nothing."""
    hid = w.release().json()["id"]
    t0 = datetime.now(timezone.utc)
    w.sync(now=t0)
    w.sync(now=t0)
    [imp] = w.sc_tasks()
    assert w.sync(now=t0)["requests"][0]["outcome"] == "OBSERVED"      # first poll: BACKLOG snapshot
    old = w.last
    _pipeline(w, imp.key)
    assert w.sync(now=t0 + timedelta(minutes=1))["requests"][0]["outcome"] == "OBSERVED"
    stored = w.prod_task().artifacts["remote_stage_c"]
    assert stored["snapshot"]["merge"]["state"] == "VERIFIED"

    def fixed(resp):
        return lambda method, path, body: resp
    later = datetime.now(timezone.utc) + timedelta(minutes=2)
    status, body, sig = w.last
    tampered = body.replace(b'"VERIFIED"', b'"NOT_OBSERVED"')
    for resp, code in (((status, tampered, sig), "BAD_MAC"), ((status, body, None), "BAD_MAC"),
                       ((status, body, h.mac("GET", h.API_PREFIX + "x", body)), "BAD_MAC"),
                       (old, "STALE"), ((500, b"", None), "HTTP_500"), ((0, b"", None), "UNREACHABLE")):
        out = w.sync(fixed(resp), now=later)["requests"][0]
        later += timedelta(hours=1)
        assert out["outcome"] == code, (code, out)
        assert w.prod_task().artifacts["remote_stage_c"] == stored
    # A correctly signed report that is not about this handoff is refused too.
    other = json.loads(body)
    other["payload_sha256"] = "0" * 64
    raw = h.canonical(other)
    out = w.sync(fixed((200, raw, h.mac("GET", h.API_PREFIX + hid, raw))), now=later)["requests"][0]
    assert out["outcome"] == "BINDING_MISMATCH" and w.prod_task().artifacts["remote_stage_c"] == stored
    # A newer report at the same seq only refreshes freshness (no audit row); an exact replay is stale, so an old
    # verified-merge report can never renew the freshness that DONE needs.
    later += timedelta(hours=1)
    same = {**json.loads(body), "generated_at": later.isoformat()}
    raw = h.canonical(same)
    out = w.sync(fixed((200, raw, h.mac("GET", h.API_PREFIX + hid, raw))), now=later)["requests"][0]
    assert out["outcome"] == "UNCHANGED"
    assert w.prod_task().artifacts["remote_stage_c"]["generated_at"] == later.isoformat()
    # One consistent report time: the nested snapshot (what the Desk derives freshness from) moves too.
    nested = w.prod_task().artifacts["remote_stage_c"]["snapshot"]["generated_at"]
    assert datetime.fromisoformat(nested.replace("Z", "+00:00")) == later
    out = w.sync(fixed((200, raw, h.mac("GET", h.API_PREFIX + hid, raw))), now=later + timedelta(hours=1))
    assert out["requests"][0]["outcome"] == "STALE"
    # Same seq, different content (a change no audit row labels, e.g. a grant expiring by time): the fresh report
    # replaces the stored projection instead of being kept as UNCHANGED forever.
    later += timedelta(hours=2)
    moved = {**same, "generated_at": later.isoformat(),
             "authorization": {**same["authorization"], "status": "EXPIRED"}}
    raw = h.canonical(moved)
    out = w.sync(fixed((200, raw, h.mac("GET", h.API_PREFIX + hid, raw))), now=later)["requests"][0]
    assert out["outcome"] == "OBSERVED", out
    assert w.prod_task().artifacts["remote_stage_c"]["snapshot"]["authorization"]["status"] == "EXPIRED"
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        assert h.desk_view(task, later + timedelta(hours=1))["freshness"] == "UNKNOWN"
        assert h.status_refusal(task, enums.TaskStatus.DONE, later + timedelta(hours=1))
    with Session(w.prod) as s:
        assert s.scalar(select(func.count()).select_from(models.AuditLog)
                        .where(models.AuditLog.action == "handoff.observed")) == 3


def test_backoff(w):
    """Transient failures back off and suspend after MAX_DELIVERY_FAILURES; a founder re-release resumes the same
    packet; an auth failure is a hard stop."""
    w.release()
    t0 = datetime.now(timezone.utc)
    for i in range(h.MAX_DELIVERY_FAILURES):
        out = w.sync(lambda m, p, b: (0, b"", None), now=t0 + timedelta(hours=i))["requests"]
        assert out[0]["outcome"] == "UNREACHABLE"
    assert w.prod_task().context["remote_stage_c"]["delivery"] == "SUSPENDED"
    assert w.sync(now=t0 + timedelta(hours=9))["requests"] == []
    assert w.release().json()["delivery"] == "PENDING"
    assert w.sync(lambda m, p, b: (401, b"", None))["requests"][0]["outcome"] == "HTTP_401"
    assert w.prod_task().context["remote_stage_c"]["delivery"] == "REFUSED"
    assert w.release().json()["delivery"] == "PENDING"                      # founder retry after a hard stop


def test_review_fixes(w):
    """Codex review of 081632a: other local writers cannot run a released task, failed cancellations cannot starve
    the rest, the backoff never overflows, and ordinary audit rows never copy the packet text."""
    from app import worker
    w.release()
    with Session(w.prod) as s:
        dep = models.Task(key="TASK-0002", title="dep", success_criteria="s", status=enums.TaskStatus.DONE)
        s.add(dep)
        s.flush()
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        task.context = {**task.context, "release_on_dependencies": True, "autonomous": True}
        s.add(models.TaskDependency(task_id=task.id, depends_on_id=dep.id))
        s.commit()
        assert worker.release_dependency_waits(s) == []
        assert worker._admission_gate(s, task, {})[0] == "handed_off_to_stage_c"
    with w.on("prod"):
        r = w.client(host="api.example.invalid", peer="127.0.0.1").post("/tasks/TASK-0001/run-report", json={
            "herdr_state": {"created_at": "2026-11-10T10:00:00+00:00", "status": "running"}})
        assert r.status_code == 409, r.text
        assert w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL
        for status in ("BLOCKED", "BACKLOG", "REVIEW", "FAILED"):   # would silently stop sync: refused
            assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                                   json={"status": status, "actor": "founder"}).status_code == 409, status
        assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 200
    assert h._backoff(10_000) == h.MAX_BACKOFF
    with Session(w.prod) as s:
        rows = s.scalars(select(models.AuditLog.after)).all() + s.scalars(select(models.AuditLog.before)).all()
        assert '"packet"' not in json.dumps(rows)
    # Four cancellations, a Stage C that refuses the first three: the fourth is still sent within a few ticks.
    with Session(w.prod) as s:
        for n in range(2, 6):
            s.add(models.Task(key=f"TASK-01{n:02d}", title="t", success_criteria="s",
                              status=enums.TaskStatus.CANCELLED, context={"remote_stage_c": {
                                  "id": f"{n}" * 8 + "-0000-4000-8000-" + "0" * 12, "payload_sha256": "0" * 64,
                                  "cancel_requested_at": "2026-11-10T10:00:00+00:00", "cancellation": "NONE"}}))
        s.commit()
    seen, t0 = set(), datetime.now(timezone.utc)
    for tick in range(4):
        w.sync(lambda method, path, body: seen.add(path) or (0, b"", None), now=t0 + timedelta(seconds=tick))
    assert any(p.startswith(h.API_PREFIX + "5") for p in seen), seen


def test_latch(w):
    """A finished remote task is never reopened by a later report, even a correctly signed one with a higher seq."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    t0 = datetime.now(timezone.utc)
    w.sync(now=t0)
    w.sync(now=t0)
    w.transport("POST", f"{h.API_PREFIX}{hid}/cancel", h.canonical({"payload_sha256": sha,
                                                                     "reason": "PRODUCTION_CANCELLED"}))
    assert w.sync(now=t0)["requests"][0]["outcome"] == "OBSERVED"
    stored = w.prod_task().artifacts["remote_stage_c"]
    assert stored["snapshot"]["task"]["status"] == "CANCELLED"
    reopened = {**stored["snapshot"], "seq": stored["seq"] + 10, "generated_at": (t0 + timedelta(hours=1)).isoformat(),
                "task": {**stored["snapshot"]["task"], "status": "READY"}}
    raw = h.canonical(reopened)
    out = w.sync(lambda m, p, b: (200, raw, h.mac("GET", h.API_PREFIX + hid, raw)), now=t0 + timedelta(hours=1))
    assert out["requests"][0]["outcome"] == "STALE" and w.prod_task().artifacts["remote_stage_c"] == stored


# ------------------------------------------------------------------------------------------------ two-phase acceptance
def _activated(w, hid):
    with Session(w.sc) as s:
        return s.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.action == "handoff.activate", models.AuditLog.entity_key == h.KEY_PREFIX + hid))


@pytest.mark.parametrize("when", ["in_put", "after_put", "sc_first", "expired"])
def test_race(w, when):
    """Cancel vs delivery: a founder cancel at any point, or expiry, means the import is never activated, so it can
    never be authorized or run; the cancel reaches Stage C and wins."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    cancel = lambda: w.client(host=PROD_HOST).post("/tasks/TASK-0001/status", headers=ASSERT,  # noqa: E731
                                                   json={"status": "CANCELLED", "actor": "founder"})

    def put_then_cancel(method, path, body):   # the founder cancels while the PUT is on the wire
        out = w.transport(method, path, body)
        with w.on("prod"):
            assert cancel().status_code == 200
        return out
    t0 = datetime.now(timezone.utc)
    assert w.sync(put_then_cancel if when == "in_put" else None, now=t0)["requests"][0]["outcome"] == "IMPORTED"
    if when == "after_put":
        with w.on("prod"):
            assert cancel().status_code == 200
    if when == "sc_first":   # Stage C records the cancel before production tries to activate
        w.transport("POST", f"{h.API_PREFIX}{hid}/cancel", h.canonical({"payload_sha256": sha,
                                                                         "reason": "PRODUCTION_CANCELLED"}))
    later = t0 + (timedelta(hours=25) if when == "expired" else timedelta(minutes=1))
    for _ in range(2):
        w.sync(now=later)
    [imp] = w.sc_tasks()
    assert _activated(w, hid) == 0
    if when != "expired":
        assert imp.status == enums.TaskStatus.CANCELLED
    if when == "expired":   # Stage C refuses a late activation on its own clock too
        with Session(w.sc) as s, pytest.raises(h.HandoffError):
            h.activate_import(s, hid, sha, now=later)
    else:
        assert w.transport("POST", f"{h.API_PREFIX}{hid}/activate", h.canonical({"payload_sha256": sha}))[0] == 410
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code == 422
    assert _claim(w, imp.key) is not None and _activated(w, hid) == 0


def test_activate_auth(w):
    """Only the handoff service may activate; wrong digests and unknown ids are refused."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    w.sync()
    path, body = f"{h.API_PREFIX}{hid}/activate", h.canonical({"payload_sha256": sha})
    with w.on("sc"):
        for headers, peer, host, code in (({}, "172.18.0.5", SC_HOST, 401), (ASSERT, "172.18.0.5", SC_HOST, 403),
                                          ({"authorization": f"Bearer {ADMIN}"}, "172.18.0.5", "api.example.invalid", 403),
                                          ({}, "127.0.0.1", "api.example.invalid", 403)):
            r = w.client(host=host, peer=peer).post(path, content=body, headers=headers)
            assert r.status_code == code, (headers, peer, r.text)
    assert w.transport("POST", path, h.canonical({"payload_sha256": "0" * 64}))[0] == 409
    assert w.transport("POST", f"{h.API_PREFIX}{'9' * 8}-0000-4000-8000-{'0' * 12}/activate", body)[0] == 404
    assert _activated(w, hid) == 0


def test_scope_view():
    """GET /tasks/{key} shows a task's Stage C scope only when it passes the release's own server-side check;
    free text, credentials, absolute or unknown paths in a stored context never reach the response."""
    from app.desk import _context_view
    ok = {"write": True, "stage_c": {"paths": ["app/packaging.py"], "context": ["app/models.py"]}}
    assert _context_view(ok)["stage_c"] == {"paths": ["app/packaging.py"]}   # read-only context is never returned
    secret = "sk-live-" + "x" * 32
    # A credential disguised as a repository path passes the shape check, but context is never part of the view.
    disguised = {"write": True, "stage_c": {"paths": ["app/packaging.py"], "context": [f"app/password={secret}.py"]}}
    assert secret not in repr(_context_view(disguised))
    for bad in ({"paths": ["app/packaging.py"], "context": [f"note: password={secret}"]},
                {"paths": ["/etc/passwd"]}, {"paths": ["app/packaging.py"], "context": ["/root/.env"]},
                {"paths": ["app/unknown.py"]}, {"paths": ["app/packaging.py"], "notes": secret}, {"paths": secret}):
        view = _context_view({"write": True, "stage_c": bad}) or {}
        assert "stage_c" not in view and secret not in repr(view), bad
