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
    # The founder's cancel route cannot reach Stage C unless a test says so (_reach): the worker path withdraws.
    monkeypatch.setattr(h, "http_transport", lambda method, path, body: (0, b"", None))

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



def _depend(w, status):
    """TASK-0001 depends on TASK-0002 in production (Stage C imports no dependency records)."""
    with Session(w.prod) as s:
        main_task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        dep = s.scalar(select(models.Task).where(models.Task.key == "TASK-0002"))
        if dep is None:
            dep = models.Task(key="TASK-0002", title="prerequisite", success_criteria="done", status=status)
            s.add(dep)
            s.flush()
            s.add(models.TaskDependency(task_id=main_task.id, depends_on_id=dep.id))
        dep.status = status
        s.commit()


def test_deps(w):
    """R4a pre-review finding 2: the handoff dropped task dependencies, so an imported task could run while its
    production prerequisites were unmet. Release (and renewal) refuses while any dependency is not DONE, and a
    dependency reopened after the release refuses delivery: nothing reaches Stage C."""
    _depend(w, enums.TaskStatus.READY)
    r = w.release()
    assert r.status_code == 409 and "DEPENDENCIES_OPEN" in r.text
    assert "remote_stage_c" not in w.prod_task().context and w.calls == []
    _depend(w, enums.TaskStatus.DONE)
    assert w.release().status_code == 201
    _depend(w, enums.TaskStatus.IN_PROGRESS)                      # reopened after the founder's release
    out = w.sync()["requests"]
    assert out[0]["outcome"] == "DEPENDENCIES_OPEN" and w.calls == [] and w.sc_tasks() == []
    assert w.prod_task().context["remote_stage_c"]["delivery"] == "REFUSED"


@pytest.mark.parametrize("change", ["expired", "edited"])
def test_renew(w, change):
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


def test_putback(w):
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
        return h.activate_import(s, hid, sha, sent_at=datetime.now(timezone.utc))


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
def test_renew_imp(w, change):
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


def test_renew_act(w):
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


def test_lost_ack(w):
    """Stage C activated the old import but production never saw the ack. The renewal reaches Stage C, which refuses it
    (ACTIVE_IMPORT) rather than retire live work or open a second task; production then restores the old handoff
    record, so polling and cancellation address the live import again. Nothing is renewed automatically."""
    old = w.release().json()
    w.sync()
    assert _sc_activate(w, old["id"], old["payload_sha256"])["activation"] == "ACTIVATED"
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    new = w.release().json()
    assert new["id"] != old["id"]
    out = w.sync()["requests"][0]
    assert out["outcome"] == "RESTORED", out
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["id"] == old["id"] and remote["delivery"] == "DELIVERED" and "superseded" not in remote
    [only] = _open_sc(w)
    assert only.context["remote_stage_c"]["id"] == old["id"] and len(w.sc_tasks()) == 1
    with Session(w.prod) as s:
        row = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede_reverted"))
        assert row.before["id"] == new["id"] and row.after["id"] == old["id"] and row.after["code"] == "ACTIVE_IMPORT"
    assert w.release().json()["id"] == old["id"]                              # no new founder approval is made
    assert w.sync()["requests"][0]["outcome"] == "OBSERVED"                   # the live import is polled again
    assert w.calls[-1] == ("GET", h.API_PREFIX + old["id"])
    with w.on("prod"):
        assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 202
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED"               # ...and cancelled by its own id
    assert w.calls[-1] == ("POST", f"{h.API_PREFIX}{old['id']}/cancel") and _open_sc(w) == []


def test_c_old(w):
    """R4a pre-review finding 1: Stage C activated the old import (its ack lost), the founder renewed, then cancelled
    before the renewed packet was delivered. The cancellation must withdraw the live old import by its own id, not
    only tombstone the new id; the task's cancellation is acknowledged only after both."""
    old = w.release().json()
    w.sync()
    assert _sc_activate(w, old["id"], old["payload_sha256"])["activation"] == "ACTIVATED"
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    new = w.release().json()
    assert new["id"] != old["id"] and len(_open_sc(w)) == 1
    with w.on("prod"):
        assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT,
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 202
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED_SUPERSEDED"
    assert w.calls[-1] == ("POST", f"{h.API_PREFIX}{old['id']}/cancel") and _open_sc(w) == []
    assert w.prod_task().context["remote_stage_c"]["cancellation"] != "ACKNOWLEDGED"
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED"
    assert w.calls[-1] == ("POST", f"{h.API_PREFIX}{new['id']}/cancel")
    assert w.prod_task().context["remote_stage_c"]["cancellation"] == "ACKNOWLEDGED"
    assert w.sync()["requests"] == []


def test_renew2(w):
    """A renewal that itself expired before delivery is renewed again: the record that reached Stage C is carried
    forward, so the live import is the one restored."""
    old = w.release().json()
    w.sync()
    _sc_activate(w, old["id"], old["payload_sha256"])
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    mid = w.release().json()
    with Session(w.prod) as s:   # the second packet expires before it is ever delivered
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        assert task.context["remote_stage_c"]["superseded"]["id"] == old["id"]
    assert w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))["requests"][0]["outcome"] == "RELEASE_EXPIRED"
    last = w.release().json()
    assert last["id"] not in (old["id"], mid["id"])
    assert w.prod_task().context["remote_stage_c"]["superseded"]["id"] == old["id"]
    assert w.sync()["requests"][0]["outcome"] == "RESTORED"
    assert w.prod_task().context["remote_stage_c"]["id"] == old["id"] and len(w.sc_tasks()) == 1


@pytest.mark.parametrize("who", ["cancel", "operator"])
def test_renew_refused(w, who):
    old = _refused_after_import(w, "expired")
    if who == "cancel":
        with Session(w.prod) as s:
            task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
            task.context = {**task.context, "remote_stage_c": {**task.context["remote_stage_c"],
                                                                 "cancel_requested_at": "2026-01-01T00:00:00+00:00"}}
            s.commit()
        assert w.release().status_code == 409                   # a pending cancel is never renewed or retried
    else:
        with w.on("prod"), Session(w.prod) as s, pytest.raises(PermissionError):
            h.release(s, s.scalar(select(models.Task).where(models.Task.key == "TASK-0001")), founder="operator:x")
    with Session(w.prod) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede")) is None
    assert len(_open_sc(w)) == 1


@pytest.mark.parametrize("code", ["RELEASE_NOT_AUTHORIZED", "HTTP_409"], ids=["auth", "409"])
def test_no_renew(w, code):
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
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 202
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED" and w.calls[0][0] == "POST"
    status, _, _ = w.transport("PUT", h.API_PREFIX + hid, h.canonical(packet))
    assert status == 410 and w.sc_tasks() == []
    assert w.sync()["requests"] == []


def test_cancel_run(w):
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


def test_bind(w):
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


def test_fixes(w):
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
                               json={"status": "CANCELLED", "actor": "founder"}).status_code == 202
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
            assert cancel().status_code == 202
        return out
    t0 = datetime.now(timezone.utc)
    assert w.sync(put_then_cancel if when == "in_put" else None, now=t0)["requests"][0]["outcome"] == "IMPORTED"
    if when == "after_put":
        with w.on("prod"):
            assert cancel().status_code == 202
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


def test_act_auth(w):
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


# ------------------------------------------------------------------------------------------------ R4a hardening
def _cancel_prod(w):
    """The founder's cancel: 200 once Stage C acknowledged it, else 202 (pending, the task stays WAITING_EXTERNAL)."""
    with w.on("prod"):
        r = w.client(host=PROD_HOST).post("/tasks/TASK-0001/status", headers=ASSERT,
                                          json={"status": "CANCELLED", "actor": "founder"})
    assert r.status_code in (200, 202)
    return r


def test_no_lock(w):
    """R4a finding 3: production held the task row lock (an open transaction) during the activation HTTP call, so a
    slow Stage C reply blocked the founder's cancel. No request of any kind may run inside a DB transaction."""
    w.release()
    seen = []
    with w.on("prod"), Session(w.prod) as s:
        def probe(method, path, body):
            seen.append((path.rsplit("/", 1)[-1], s.in_transaction()))
            return w.transport(method, path, body)
        assert h.sync(s, probe)["requests"][0]["outcome"] == "IMPORTED"
        assert h.sync(s, probe)["requests"][0]["outcome"] == "ACTIVATED"
        assert h.sync(s, probe)["requests"][0]["outcome"] == "OBSERVED"
    assert [locked for _, locked in seen] == [False] * 4 and [p for p, _ in seen[1:3]] == ["activate"] * 2


def test_cancel_act(w):
    """R4a finding 3: a founder cancel that commits while the (now unlocked) activation request is on the wire is
    never overwritten: production records no activation, and withdraws the import on Stage C in the same tick."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"

    def cancel_then_send(method, path, body):
        if path.endswith("/activate"):
            _cancel_prod(w)
        return w.transport(method, path, body)
    out = w.sync(cancel_then_send)["requests"][0]
    assert (out["outcome"], out["cancel"]) == ("CANCEL_PENDING", "ACKNOWLEDGED")
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["cancel_requested_at"] and remote["delivery"] == "IMPORTED"
    with Session(w.prod) as s:
        assert s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.activation")) is None
    assert w.sync()["requests"] == []
    [imp] = w.sc_tasks()
    assert imp.status == enums.TaskStatus.CANCELLED
    assert _authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code == 422


def test_no_act(w):
    """The cancel committed before the activation tick: production sees it under the row lock and never sends."""
    w.release()
    w.sync()
    _cancel_prod(w)
    w.calls.clear()
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED"
    assert not [c for c in w.calls if c[1].endswith("/activate")]


def test_dl(monkeypatch):
    """R4a finding 3: the 5 s timeout is per network operation; a reply that drips one chunk at a time kept the
    request open for as long as the peer liked. The whole request now has one deadline."""
    import time

    import httpx
    monkeypatch.setenv(h.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(h.URL_ENV, f"https://{SC_HOST}")
    monkeypatch.setattr(h, "DEADLINE_S", 0.5, raising=False)

    def drip():
        for _ in range(25):
            time.sleep(0.2)
            yield b" "
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=drip())),
        **{k: v for k, v in kw.items() if k != "verify"}))
    t0 = time.monotonic()
    status, body, _ = h.http_transport("GET", h.API_PREFIX + "x", None)
    assert status == 0 and body == b"" and time.monotonic() - t0 < 2.5


def _age(w, hid):
    """The packet's expiry passes on both clocks: Stage C's handoff row and production's record (never the packet,
    whose digest binds its expiry). Stands in for a day of waiting."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with Session(w.sc) as s:
        row = s.get(models.ControlState, h.KEY_PREFIX + hid)
        row.value = {**row.value, "expires_at": past}
        s.commit()
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        task.context = {**task.context, "remote_stage_c": {**task.context["remote_stage_c"], "expires_at": past}}
        s.commit()


def _activated_and_polled(w):
    old = w.release().json()
    assert [w.sync()["requests"][0]["outcome"] for _ in range(3)] == ["IMPORTED", "ACTIVATED", "OBSERVED"]
    [imp] = w.sc_tasks()
    return old, imp


def test_rn(w):
    """R4a finding 1: production activated the import, but the packet expired before the founder's first coding
    authorization on Stage C. Stage C refused the late authorization ("release again"), production refused to renew a
    recorded activation and Stage C refused to retire an activated import: only cancel remained. An import that was
    activated but never authorized or run is not live work: the founder's renewal (never sync) replaces it, Stage C
    retires it (tombstone + cancelled task, audited) and the new packet goes through the normal two phases."""
    old, imp = _activated_and_polled(w)
    _age(w, old["id"])
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 422
    assert w.sync(now=datetime.now(timezone.utc) + timedelta(minutes=2))["requests"][0]["outcome"] == "UNCHANGED"
    assert w.prod_task().context["remote_stage_c"]["id"] == old["id"]            # sync alone never renews
    r = w.release()
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != old["id"] and new["payload_sha256"] != old["payload_sha256"]
    assert w.prod_task().context["remote_stage_c"]["superseded"]["id"] == old["id"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    [live] = _open_sc(w)
    assert live.context["remote_stage_c"]["id"] == new["id"] and len(w.sc_tasks()) == 2
    assert next(t for t in w.sc_tasks() if t.key == imp.key).status == enums.TaskStatus.CANCELLED
    with Session(w.sc) as s:
        sup = s.scalar(select(models.AuditLog).where(models.AuditLog.action == "handoff.supersede"))
        assert sup.after["id"] == old["id"] and sup.after["superseded_by"] == new["id"]
    with pytest.raises(h.HandoffError) as e:
        _sc_activate(w, old["id"], old["payload_sha256"])
    assert e.value.code == "CANCELLED"
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 422
    assert w.sync()["requests"][0]["outcome"] == "ACTIVATED"
    assert w.sync()["requests"][0]["outcome"] == "OBSERVED"                      # the old projection does not latch
    view = w.prod_task().artifacts["remote_stage_c"]["snapshot"]
    assert view["id"] == new["id"] and view["task_key"] == live.key
    assert _authorize(w, live.key, handoff_id=new["id"], payload_sha256=new["payload_sha256"]).status_code == 201


@pytest.mark.parametrize("live", ["authorized", "ran"])
def test_live_kept(w, live):
    """An import that was authorized (or ran) is live work: production does not renew it from its report, and even a
    renewal sent anyway is refused by Stage C (ACTIVE_IMPORT) and the live record restored; nothing is retired."""
    old, imp = _activated_and_polled(w)
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 201
    if live == "ran":
        assert _claim(w, imp.key) is None
        with Session(w.sc) as s:   # a run, even with the authorization mark gone, is live work
            row = s.get(models.ControlState, h.KEY_PREFIX + old["id"])
            row.value = {**row.value, "first_authorized_at": None}
            s.commit()
    assert w.sync(now=datetime.now(timezone.utc) + timedelta(minutes=2))["requests"][0]["outcome"] == "OBSERVED"
    _age(w, old["id"])
    assert w.release().json()["id"] == old["id"]                                # the report shows a grant: no renewal
    with Session(w.prod) as s:                                                  # a stale report saying "no grant"
        task = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
        stored = task.artifacts["remote_stage_c"]
        snap = {**stored["snapshot"], "authorization": {"status": "NONE", "source_sha": None, "expires_at": None},
                "task": {**stored["snapshot"]["task"], "status": "BACKLOG"}}
        task.artifacts = {**task.artifacts, "remote_stage_c": {**stored, "snapshot": snap}}
        s.commit()
    new = w.release().json()
    assert new["id"] != old["id"]
    assert w.sync()["requests"][0]["outcome"] == "RESTORED"
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["id"] == old["id"] and remote["delivery"] == "DELIVERED"
    [only] = _open_sc(w)
    assert only.key == imp.key and len(w.sc_tasks()) == 1


def test_no_dup(w):
    """State audit: Stage C finished the import (DONE) while production missed its activation ack. A renewal must not
    import a second task for the same production task: Stage C refuses it and production restores the finished
    import's record, whose report carries the verified merge."""
    old = w.release().json()
    w.sync()
    _sc_activate(w, old["id"], old["payload_sha256"])
    [imp] = w.sc_tasks()
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 201
    assert _claim(w, imp.key) is None
    _pipeline(w, imp.key)
    with Session(w.sc) as s:
        s.scalar(select(models.Task).where(models.Task.key == imp.key)).status = enums.TaskStatus.DONE
        s.commit()
    assert w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))["requests"][0]["outcome"] == "RELEASE_EXPIRED"
    assert w.release().json()["id"] != old["id"]
    assert w.sync()["requests"][0]["outcome"] == "RESTORED"
    assert len(w.sc_tasks()) == 1 and w.prod_task().context["remote_stage_c"]["id"] == old["id"]


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_no_packet(w):
    """R4a finding 2: a renewal kept a copy of the old record, packet included, under remote_stage_c.superseded, and
    audit snapshots stripped only the top-level packet: the private task text reached the audit log. The superseded
    record keeps only the fields its restore and cancel paths read, and audit snapshots strip a packet at any depth."""
    old = _refused_after_import(w, "expired")
    w.release()
    sup = w.prod_task().context["remote_stage_c"]["superseded"]
    assert sup["id"] == old["id"] and sup["remote_task_key"] and "packet" not in set(_keys(sup))
    _cancel_prod(w)   # the founder's status change writes before/after snapshots of the task
    with Session(w.prod) as s:
        rows = s.scalars(select(models.AuditLog.after)).all() + s.scalars(select(models.AuditLog.before)).all()
        assert rows and "packet" not in set(_keys(rows))
    assert w.sync()["requests"][0]["outcome"] == "ACKNOWLEDGED_SUPERSEDED"   # the cancel path still has what it needs


def test_strip():
    from app import audit
    task = models.Task(key="TASK-0009", title="t", success_criteria="s", context={"remote_stage_c": {
        "id": "x", "packet": {"task": "secret"}, "superseded": {"id": "y", "packet": {"task": "secret"},
                                                                 "superseded": [{"packet": "secret"}]}}})
    out = audit.snapshot(task)
    assert "packet" not in set(_keys(out)) and "secret" not in json.dumps(out, default=str)
    assert out["context"]["remote_stage_c"]["superseded"]["id"] == "y"
    assert task.context["remote_stage_c"]["packet"] == {"task": "secret"}   # the stored row is untouched


def test_lost(w):
    """State audit: Stage C applies an import, an activation and a cancellation but each reply is lost (production
    sees UNREACHABLE). Each retry is answered from Stage C's stored state: one task, one activation, one tombstone."""
    hid = w.release().json()["id"]
    lost = lambda m, p, b: (w.transport(m, p, b), (0, b"", None))[1]   # noqa: E731
    t0 = datetime.now(timezone.utc)
    assert w.sync(lost, now=t0)["requests"][0]["outcome"] == "UNREACHABLE"
    assert w.sync(now=t0 + timedelta(hours=1))["requests"][0]["outcome"] == "IMPORTED"
    assert w.sync(lost, now=t0 + timedelta(hours=1))["requests"][0]["outcome"] == "UNREACHABLE"
    assert w.sync(now=t0 + timedelta(hours=2))["requests"][0]["outcome"] == "ACTIVATED"
    assert _activated(w, hid) == 1 and len(w.sc_tasks()) == 1
    _cancel_prod(w)
    assert w.sync(lost, now=t0 + timedelta(hours=2))["requests"][0]["outcome"] == "UNREACHABLE"
    assert w.prod_task().context["remote_stage_c"]["cancellation"] == "NONE"   # unconfirmed until acknowledged
    assert w.sync(now=t0 + timedelta(hours=3))["requests"][0]["outcome"] == "ACKNOWLEDGED"
    [imp] = w.sc_tasks()
    assert imp.status == enums.TaskStatus.CANCELLED
    with Session(w.sc) as s:
        assert s.scalar(select(func.count()).select_from(models.AuditLog)
                        .where(models.AuditLog.action == "handoff.cancel")) == 1


# ------------------------------------------------------------------------------------------------ R4a-2 review
def _reach(w):
    w.mp.setattr(h, "http_transport", w.transport)


def _consumed(w):
    with Session(w.sc) as s:
        return s.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.action == "stage-c.authorization.consume"))


def _packet(issued, origin="TASK-0001"):
    p = {"version": 1, "id": str(uuid.uuid4()), "origin": "production", "origin_task_key": origin,
         "repository": "seerxo/acthelm", "issued_at": issued.isoformat(),
         "expires_at": (issued + timedelta(hours=1)).isoformat(), "risk": "LOW", "kind": "code_change",
         "task": {k: v for k, v in BODY.items()}}
    p["payload_sha256"] = h.digest(p)
    return p


def test_f1(w):
    """R4a-2 finding 1: the founder cancels (Stage C unreachable from the route) while the renewal PUT is on the wire
    and Stage C answers ACTIVE_IMPORT. The restore dropped the cancel: the old live import stayed open for good."""
    old = w.release().json()
    w.sync()
    _sc_activate(w, old["id"], old["payload_sha256"])
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    new = w.release().json()

    def put_then_cancel(method, path, body):
        out = w.transport(method, path, body)
        _cancel_prod(w)
        return out
    assert w.sync(put_then_cancel)["requests"][0]["outcome"] == "HTTP_409"
    assert [w.sync()["requests"][0]["outcome"] for _ in range(2)] == ["ACKNOWLEDGED_SUPERSEDED", "ACKNOWLEDGED"]
    assert w.calls[-2:] == [("POST", f"{h.API_PREFIX}{old['id']}/cancel"), ("POST", f"{h.API_PREFIX}{new['id']}/cancel")]
    assert _open_sc(w) == [] and w.prod_task().context["remote_stage_c"]["cancellation"] == "ACKNOWLEDGED"


def _in_window(w, reach):
    """A cancel commits after the activation's lock release, before its POST; the founder authorizes on Stage C right
    after the activation request was served. With `reach`, the route's own cancel request is recorded too, with
    whether the route's session held a transaction across it."""
    sessions, seen = [], []
    if reach:
        real = getattr(h, "withdraw", None)
        w.mp.setattr(h, "withdraw", lambda db, task, t=None: (sessions.append(db), real(db, task, t))[1], raising=False)
        w.mp.setattr(h, "http_transport", lambda m, p, b: (
            seen.append(("route", any(s.in_transaction() for s in sessions))), w.transport(m, p, b))[1])
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"

    def cancel_then_send(method, path, body):
        if not path.endswith("/activate"):
            return w.transport(method, path, body)
        _cancel_prod(w)
        out = w.transport(method, path, body)
        [imp] = w.sc_tasks()
        seen.append(_authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code)
        return out
    return hid, w.sync(cancel_then_send)["requests"][0], seen


def test_f2(w):
    """R4a-2 finding 2, founder rule: the cancel route records the cancel (pending) and withdraws the import on Stage C
    in the same request, before the task is shown CANCELLED: the activation sent after it is refused, no grant exists."""
    hid, out, seen = _in_window(w, reach=True)
    [imp] = w.sc_tasks()
    assert seen == [("route", False), 422] and _activated(w, hid) == 0   # no transaction across the request
    assert _claim(w, imp.key) is not None and _consumed(w) == 0
    assert w.prod_task().context["remote_stage_c"]["cancellation"] == "ACKNOWLEDGED"


def test_f2b(w):
    """The residual window (the route cannot reach Stage C): the activation lands and a grant can be issued, but
    production re-checks under its lock right after the reply and withdraws the import in the same tick."""
    hid, out, seen = _in_window(w, reach=False)
    [imp] = w.sc_tasks()
    assert seen == [201]
    assert _claim(w, imp.key) is not None and _consumed(w) == 0
    with Session(w.sc) as s:
        assert s.get(models.ControlState, control.PILOT_AUTHORIZATION) is None
    assert out["outcome"] == "CANCEL_PENDING" and out["cancel"] == "ACKNOWLEDGED"


def test_f3(w):
    """R4a-2 finding 3: a native authorization replaced a live handoff grant, and replacing a finished one left the
    handoff task's revision (its report seq) unchanged."""
    old, imp = _activated_and_polled(w)
    with Session(w.sc) as s:   # production's confirmation lease ran out: no grant until a poll renews it
        row = s.get(models.ControlState, h.KEY_PREFIX + old["id"])
        row.value = {**row.value, "confirmed_until": datetime.now(timezone.utc).isoformat()}
        s.commit()
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 422
    w.sync(now=datetime.now(timezone.utc) + timedelta(seconds=61))
    assert w.calls[-2:] == [("POST", f"{h.API_PREFIX}{old['id']}/activate"), ("GET", h.API_PREFIX + old["id"])]
    assert _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"]).status_code == 201
    with Session(w.sc) as s:
        s.add(models.Task(key="TASK-0900", title="native", objective="o", success_criteria="s"))
        s.commit()
    assert _authorize(w, "TASK-0900").status_code == 422                     # live
    assert _claim(w, imp.key) is None
    assert _authorize(w, "TASK-0900").status_code == 422                     # in flight
    with Session(w.sc) as s:
        for run in s.scalars(select(models.AgentRun)):
            run.status = enums.AgentRunStatus.SUCCEEDED
        s.commit()
        rev = s.scalar(select(models.Task.revision).where(models.Task.key == imp.key))
    assert _authorize(w, "TASK-0900").status_code == 201
    with Session(w.sc) as s:
        assert s.scalar(select(models.Task.revision).where(models.Task.key == imp.key)) == rev + 1


def test_f4(w):
    """R4a-2 finding 4: a late PUT of an older packet retired the newer import, or, once that was cancelled, opened an
    orphan. Stage C keeps the newest issue time per production task and refuses anything older."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    p1, p2 = _packet(t0), _packet(t0 + timedelta(minutes=1))
    with Session(w.sc) as s:
        assert h.import_packet(s, p2["id"], p2)[0] == 201
        for cancel in (False, True):
            if cancel:
                h.cancel_import(s, p2["id"], p2["payload_sha256"])
            with pytest.raises(h.HandoffError) as e:
                h.import_packet(s, p1["id"], p1)
            s.rollback()
            assert e.value.code == "SUPERSEDED"
            if not cancel:
                assert h.import_packet(s, p2["id"], p2)[0] == 200
    assert _open_sc(w) == [] and len(w.sc_tasks()) == 1


def test_f1r(w):
    """The route reaches Stage C: the founder's cancel withdraws the superseded live import, then the renewal, before
    production commits; both are recorded acknowledged and the worker has nothing left to send."""
    _reach(w)
    old = w.release().json()
    w.sync()
    _sc_activate(w, old["id"], old["payload_sha256"])
    w.sync(now=datetime.now(timezone.utc) + timedelta(hours=25))
    new = w.release().json()
    _cancel_prod(w)
    assert w.calls[-2:] == [("POST", f"{h.API_PREFIX}{old['id']}/cancel"), ("POST", f"{h.API_PREFIX}{new['id']}/cancel")]
    remote = w.prod_task().context["remote_stage_c"]
    assert remote["cancellation"] == remote["superseded"]["cancellation"] == "ACKNOWLEDGED"
    assert _open_sc(w) == [] and w.sync()["requests"] == []


def _try_claim(w, key, at):
    """A claim at Stage C time `at`, rolled back: its denial, or None where it would be allowed."""
    with Session(w.sc) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == key))
        run = models.AgentRun(task_id=task.id, status=enums.AgentRunStatus.STARTED, tool_calls={"stage_c": True})
        s.add(run)
        s.flush()
        denial = control.consume_write_authorization(s, task, SHA, run.id, now=at)
        s.rollback()
        return denial


def test_c(w):
    """Founder rule: Stage C is unreachable for every cancel while the activation already on the wire lands. The cancel
    is never shown done (202, WAITING_EXTERNAL, PENDING) and is retried until acknowledged; with no renewal from
    production, the grant issued in that window can be consumed only until the confirmation lease ends."""
    hid = w.release().json()["id"]
    sha = w.prod_task().context["remote_stage_c"]["payload_sha256"]
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    seen = []

    def lost(method, path, body):
        if path.endswith("/cancel"):
            return 0, b"", None
        if not path.endswith("/activate"):
            return w.transport(method, path, body)
        seen.append(_cancel_prod(w).status_code)
        out = w.transport(method, path, body)
        [imp] = w.sc_tasks()
        seen.append(_authorize(w, imp.key, handoff_id=hid, payload_sha256=sha).status_code)
        return out
    t0 = datetime.now(timezone.utc)
    out = w.sync(lost, now=t0)["requests"][0]
    assert seen == [202, 201] and (out["outcome"], out["cancel"]) == ("CANCEL_PENDING", "UNREACHABLE")
    task = w.prod_task()
    assert task.status == enums.TaskStatus.WAITING_EXTERNAL and h.desk_view(task)["cancellation"] == "PENDING"
    with w.on("prod"):
        assert w.client().post("/tasks/TASK-0001/status", headers=ASSERT, json={
            "status": "DONE", "actor": "founder", "evidence": "x"}).status_code == 409
    [imp] = w.sc_tasks()
    end = t0 + h.ACTIVATION_LEASE
    assert _try_claim(w, imp.key, end - timedelta(seconds=1)) is None      # the documented bound
    assert _try_claim(w, imp.key, end) == "stage_c_handoff_unconfirmed" and _consumed(w) == 0
    assert w.sync(lost, now=t0 + timedelta(minutes=1))["requests"][0]["outcome"] == "UNREACHABLE"
    assert w.prod_task().status == enums.TaskStatus.WAITING_EXTERNAL
    assert w.sync(now=t0 + timedelta(hours=1))["requests"][0]["outcome"] == "ACKNOWLEDGED"
    assert w.prod_task().status == enums.TaskStatus.CANCELLED and _open_sc(w) == []
    assert w.sync(now=t0 + timedelta(hours=2))["requests"] == []


def test_r1(w):
    """R4a-2 pre-review 1: the poll renewed the confirmation lease without re-checking the release, and while agents
    were paused. A paused production, a changed task or a reopened dependency stops renewals (fail closed)."""
    old, _ = _activated_and_polled(w)
    act, get = ("POST", f"{h.API_PREFIX}{old['id']}/activate"), ("GET", h.API_PREFIX + old["id"])
    t = datetime.now(timezone.utc)

    def tick(n, **kw):
        w.calls.clear()
        w.sync(now=t + timedelta(seconds=61 * n), **kw)
        return w.calls
    assert tick(1) == [act, get]                                # live: renewed, then polled
    assert tick(2, paused=True) == [get]
    _depend(w, enums.TaskStatus.BACKLOG)
    assert tick(3) == [get]
    _depend(w, enums.TaskStatus.DONE)
    with Session(w.prod) as s:
        s.scalar(select(models.Task).where(models.Task.key == "TASK-0001")).objective = "edited"
        s.commit()
    assert tick(4) == [get]


def test_r3(w):
    """R4a-2 pre-review 3: the cancel route withdrew on Stage C before it locked and validated the task, so a DONE
    committed meanwhile left production DONE with Stage C cancelled. The cancel is now validated and recorded (pending)
    under the lock first: while it is withdrawn on Stage C, a concurrent DONE is refused."""
    _reach(w)
    _, imp = _activated_and_polled(w)
    _pipeline(w, imp.key)                         # a fresh verified merge report: DONE alone would be accepted
    assert w.sync(now=datetime.now(timezone.utc) + timedelta(seconds=61))["requests"][0]["outcome"] == "OBSERVED"
    seen = []
    real = w.transport

    def route(method, path, body):
        if path.endswith("/cancel"):
            with Session(w.prod) as s:
                t = s.scalar(select(models.Task).where(models.Task.key == "TASK-0001"))
                seen.append(bool(t.context["remote_stage_c"].get("cancel_requested_at")))
            with w.on("prod"):
                seen.append(w.client().post("/tasks/TASK-0001/status", headers=ASSERT, json={
                    "status": "DONE", "actor": "founder", "evidence": "x"}).status_code)
        return real(method, path, body)
    w.mp.setattr(h, "http_transport", route)
    assert _cancel_prod(w).status_code == 200
    assert seen == [True, 409] and w.prod_task().status == enums.TaskStatus.CANCELLED


def test_g2(w):
    """R4a-2 pre-review 2: re-authorizing the same task replaced its consumed grant while the run that consumed it was
    still STARTED. Refused until that run ends (or is lost: past the worker's stale cutoff); then allowed."""
    old, imp = _activated_and_polled(w)
    auth = lambda: _authorize(w, imp.key, handoff_id=old["id"], payload_sha256=old["payload_sha256"])  # noqa: E731
    assert auth().status_code == 201 and _claim(w, imp.key) is None
    assert auth().status_code == 422
    with Session(w.sc) as s:   # a STARTED run past the worker's stale cutoff is lost, not going: recovery may proceed
        for run in s.scalars(select(models.AgentRun)):
            run.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    assert auth().status_code == 201 and _claim(w, imp.key) is None
    assert auth().status_code == 422
    with Session(w.sc) as s:
        for run in s.scalars(select(models.AgentRun)):
            run.status = enums.AgentRunStatus.SUCCEEDED
        s.commit()
    assert auth().status_code == 201


def test_lease(w):
    """Founder conditions on Stage C: the confirmation lease runs from production's sent_at, capped at Stage C's clock.
    A future-dated, replayed (older) or delayed (re)activation never extends it past sent_at + ACTIVATION_LEASE, a
    missing or invalid sent_at grants none (the import activates unconfirmed: no grant), and an acknowledged cancel
    refuses every later activation."""
    t0, lease = datetime.now(timezone.utc), h.ACTIVATION_LEASE
    p = _packet(t0)
    with Session(w.sc) as s:
        assert h.import_packet(s, p["id"], p)[0] == 201

        def act(at, sent_at):
            h.activate_import(s, p["id"], p["payload_sha256"], now=at, sent_at=sent_at)
            value = h._row(s, p["id"]).value
            return h._utc(value.get("confirmed_until")), value
        for bad in (None, "not-a-time"):
            until, value = act(t0, bad)
            assert until is None and value["state"] == "ACTIVATED" and not h.confirmed(value, t0)
        assert act(t0, t0 + timedelta(days=1))[0] == t0 + lease                                   # future: capped
        assert act(t0 + timedelta(minutes=2), t0 - timedelta(hours=1))[0] == t0 + lease          # replayed: no gain
        until, value = act(t0 + timedelta(minutes=20), t0 + timedelta(minutes=1))                # delayed
        assert until == t0 + timedelta(minutes=1) + lease and not h.confirmed(value, t0 + timedelta(minutes=20))
        h.cancel_import(s, p["id"], p["payload_sha256"])
        with pytest.raises(h.HandoffError) as e:
            act(t0 + timedelta(minutes=21), t0 + timedelta(minutes=21))
        assert e.value.code == "CANCELLED"
