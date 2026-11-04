"""Founder Desk data exposure (P0 Desk API audit F1, F2, F4, F5, F6): credential-shaped provider or model text never
leaves the API, the task detail serves projections only, every response carries browser security headers, and a
cross-site write on the Desk host is refused before it reaches a handler.

Every canary is built at runtime, so no credential-shaped literal sits in this public repository."""
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cloudflare_access, desk, enums, main, models, notebook, providers, redact, worker
from app.providers import Health, ProviderResult

DESK_HOST, FOUNDER = "desk.example.invalid", "founder@example.invalid"
ASSERT = {"Cf-Access-Jwt-Assertion": "founder-assertion"}
SHA = "c" * 40


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


GH_BODY = "A1b2C3" * 6
GH = "gh" + "p_" + GH_BODY
SK_BODY = "Q9w8E7r6T5y4U3i2O1p0"
SK = "sk" + "-live-" + SK_BODY
BEARER_BODY = "Zx9" * 8
JWT_CLAIMS, JWT_SIG = _b64({"sub": "canary", "exp": 1}), "S1g" * 8
JWT = _b64({"alg": "HS256", "typ": "JWT"}) + "." + JWT_CLAIMS + "." + JWT_SIG
PASSWORD = "pa55" + "w0rd" + "Canary"
# What must never appear in a response, in whole or as a body left behind by partial masking.
LEAKS = (GH_BODY, SK_BODY, BEARER_BODY, JWT_CLAIMS, JWT_SIG, PASSWORD)
PR = "https://github.com/Seerxo/Acthelm/pull/7"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        ceo = models.Agent(key="ceo", role="Coordination", enabled=True)
        research = models.Agent(key="research", role="Research", enabled=True,
                                last_error=f"provider said: id_token {JWT}")
        s.add_all([ceo, research])
        project = models.Project(key="canary", name="Canary")
        s.add(project)
        s.flush()
        task = models.Task(
            key="TASK-0101", title=f"leaky {GH}", objective=f"investigate {SK}",
            success_criteria="s", status=enums.TaskStatus.BLOCKED, priority=enums.Priority.P1,
            assigned_agent_id=research.id, project_id=project.id,
            error=f"codex: stderr tail ... Authorization: Bearer {BEARER_BODY}",
            context={"write": True, "autonomous": True, "source": "founder-desk-command",
                     "approval": {"fingerprint": f"token={GH}"}, "verify": {"one_of": [SK]},
                     "improvement": {"stage": "CI", "history": [
                         {"stage": "PR_OPEN", "at": "2026-11-04T00:00:00+00:00", "actor": "stage-c-worker",
                          "evidence": {"pr": PR, "pr_number": 7, "commit": "a" * 40, "ci_state": "failed",
                                       "failed_check": f"python ({GH})", "verdict": "APPROVE",
                                       "risk": {"detail": GH}, "expected_effect": [{"detail": SK}],
                                       "previous_diff": f"+KEY = '{GH}'\n+URL = 'https://u:{PASSWORD}@h'",
                                       "failures": [f"E   {SK}"], "findings": [{"detail": [JWT]}]}}]}},
            artifacts={"actions": ["Ranked item one", f"leak {SK}"], "evidence": [GH], "artifacts": {"k": [JWT]},
                       "pr": PR, "commit": "a" * 40, "run_id": 1})
        s.add(task)
        s.flush()
        s.add(models.AgentRun(agent_id=research.id, task_id=task.id, provider="codex", started_at=now,
                              model=f"legacy {SK}", status=enums.AgentRunStatus.FAILED, error=f"stderr: token={GH}",
                              supervisor_verdict=enums.SupervisorVerdict.FAILED,
                              supervisor_notes=f"model wrote {SK}", result=f"summary {GH}"))
        s.add(models.AgentNote(agent_id=research.id, task_id=task.id, note=f"clone https://u:{PASSWORD}@git.example",
                               fingerprint="leaky-note"))
        s.add(models.Learning(title="Legacy learning", lesson=f"learned {GH}", project_id=project.id,
                              status=enums.LearningStatus.APPROVED))
        s.add(models.Incident(key="INC-0101", title="leak", description=f"stderr {GH}", root_cause=f"key {SK}",
                              severity=enums.IncidentSeverity.SEV3, status=enums.IncidentStatus.OPEN,
                              detected_by="watchdog", started_at=now))
        s.add(models.Event(event_type="TASK_FAILED", source="company-worker", severity=enums.Severity.MEDIUM,
                           task_id=task.id, payload={"title": "TASK-0101 failed", "message": f"Bearer {BEARER_BODY}"}))
        s.commit()
    for k, v in {"CF_ACCESS_DESK_HOST": DESK_HOST, "API_AUTH_MODE": "enforce", "COMPANY_OS_GIT_SHA": SHA}.items():
        monkeypatch.setenv(k, v)

    async def identity(token, **kw):
        return FOUNDER if token == "founder-assertion" else None
    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", identity)
    monkeypatch.setattr(main.worker, "worker_state", lambda db: {"runtime": {"git_sha": SHA}})

    def get_db():
        with Session(engine) as s:
            yield s
    main.app.dependency_overrides[main.get_db] = get_db
    yield engine
    main.app.dependency_overrides.clear()


def _desk():
    return TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.9", 5000))


def _no_leak(text: str):
    __tracebackhide__ = True   # a failure names canary indexes, never the response text
    found = [i for i, leak in enumerate(LEAKS) if leak in text]
    assert not found, f"canary {found} served"   # indexes only: never print a canary


def test_no_credential_shaped_text_reaches_any_desk_read(env):
    client = _desk()
    for path in ("/desk/state", "/tasks/TASK-0101", "/agents/research", "/desk/memory"):
        r = client.get(path, headers=ASSERT)
        assert r.status_code == 200, path
        _no_leak(r.text)


def test_task_detail_serves_only_the_fields_the_desk_reads(env):
    t = _desk().get("/tasks/TASK-0101", headers=ASSERT).json()
    ctx = t["context"]
    assert set(ctx) == {"write", "autonomous", "source", "improvement"}
    assert ctx["write"] is True and ctx["autonomous"] is True and ctx["improvement"]["stage"] == "CI"
    step = ctx["improvement"]["history"][0]
    assert set(step) == {"stage", "at", "evidence"} and step["stage"] == "PR_OPEN"
    assert set(step["evidence"]) == {"pr", "pr_number", "commit", "ci_state", "failed_check", "verdict"}
    assert (step["evidence"]["pr"], step["evidence"]["pr_number"], step["evidence"]["ci_state"]) == (PR, 7, "failed")
    assert set(t["artifacts"]) == {"actions", "pr", "commit", "run_id"}
    assert t["artifacts"]["actions"][0] == "Ranked item one" and "***" in t["artifacts"]["actions"][1]
    assert "result" not in t["runs"][0] and t["runs"][0]["error"] and t["supervisor"]["notes"]
    a = _desk().get("/agents/research", headers=ASSERT).json()
    assert set(a["identity"]) == {"description", "capabilities", "skills", "allowed_tools"}
    assert "context_policy" not in a and a["last_error"] and a["errors"]


def test_provider_error_is_redacted_before_it_is_bounded():
    # The URL's `@` falls past the 500-character cut: truncating first would keep the password unmatched.
    stderr = "x" * 475 + f" https://u:{PASSWORD}@git.example " + "y" * 40
    assert stderr.index("@") >= providers.MAX_ERROR_CHARS
    out = json.dumps({"type": "result", "is_error": True, "result": stderr})
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin"}, runner=lambda *a, **k: (1, out, ""))
    p.health_check = lambda: (Health.AVAILABLE, None)
    result = p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=2)
    assert result.error and len(result.error) <= providers.MAX_ERROR_CHARS
    assert PASSWORD[:8] not in result.error
    _no_leak(result.error)
    codex = providers.provider_error("z" * 9000 + f" Bearer {BEARER_BODY}", tail=True)
    assert len(codex) <= providers.MAX_ERROR_CHARS
    _no_leak(codex)


def _boundary_url(boundary):
    url = f" https://u:{PASSWORD}@git.example "
    return "x" * (boundary - url.index("@")) + url


def test_the_scan_boundary_withholds_whole_text_and_never_exposes_a_jwt_tail(monkeypatch):
    url = _boundary_url(8000)
    jwt = _b64({"alg": "HS256"}) + "." + "A" * 8500 + "." + JWT_SIG
    assert url.index("@") == redact.MAX_SCAN_CHARS
    scanned = []
    monkeypatch.setattr(redact, "_redact_secrets", lambda text: scanned.append(text) or text)
    for text in (url, jwt):
        assert desk._clean(text) == redact.UNAVAILABLE
        assert providers.provider_error(text) == redact.UNAVAILABLE
        assert providers.provider_error(text, tail=True) == redact.UNAVAILABLE
    assert not scanned   # no head or tail was passed to the scanner


def test_a_jwt_is_redacted_whole_before_taking_a_short_tail():
    jwt = _b64({"alg": "HS256"}) + "." + "A" * 1000 + "." + JWT_SIG
    _no_leak(providers.provider_error(jwt, tail=True))


def test_quota_reset_text_is_redacted_before_extraction_and_truncation():
    message = "quota: try again at " + _boundary_url(providers.MAX_ERROR_CHARS)
    _no_leak(providers.quota_message(message))
    jwt = "quota: try again at " + JWT
    _no_leak(providers.quota_message(jwt))


def test_improvement_evidence_drops_containers_but_keeps_scalars(env):
    improvement = _desk().get("/desk/state", headers=ASSERT).json()["improvements"][0]
    assert improvement["risk"] is None and improvement["expected_effect"] is None
    assert improvement["pr_number"] == 7 and improvement["ci_state"] == "failed"


def test_legacy_learning_lessons_are_redacted_on_both_desk_reads(env):
    client = _desk()
    recent = client.get("/agents/research", headers=ASSERT).json()["recent_learnings"]
    memory = client.get("/desk/memory", headers=ASSERT).json()["learnings"]
    assert recent and memory
    assert recent[0]["lesson"] == memory[0]["lesson"] == "learned ***"


def test_failure_and_notes_are_redacted_when_written(env):
    with Session(env) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == "TASK-0101"))
        worker._set_failure(db, task, f"stage c outcome {SK}")
        note = notebook.record(db, note=f"lesson: token={GH}", agent_key="research")
        _no_leak(task.error + note.note)


# --------------------------------------------------------------------------- F4 headers

class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts, self.handlers, self._current = [], [], None

    def handle_starttag(self, tag, attrs):
        self.handlers += [name for name, _ in attrs if name.lower().startswith("on")]
        if tag == "script":
            self._current = "" if not dict(attrs).get("src") else None

    def handle_data(self, data):
        if self._current is not None:
            self._current += data

    def handle_endtag(self, tag):
        if tag == "script" and self._current is not None:
            self.scripts.append(self._current)
            self._current = None


def test_desk_csp_allows_exactly_its_inline_scripts_and_no_inline_handlers():
    page = _Scripts()
    page.feed(main._DESK.read_text())
    assert page.scripts and not page.handlers
    csp = TestClient(main.app, client=("172.18.0.5", 5000)).get("/").headers["content-security-policy"]
    script_src = re.search(r"script-src ([^;]+)", csp).group(1).split()
    for js in page.scripts:
        assert f"'sha256-{base64.b64encode(hashlib.sha256(js.encode()).digest()).decode()}'" in script_src
    assert len(script_src) == len(page.scripts) and "'unsafe-inline'" not in script_src
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp


def test_every_response_carries_security_headers_even_an_auth_refusal(env):
    client = _desk()
    for r in (client.get("/desk/state"), client.get("/desk/state", headers=ASSERT), client.get("/", headers=ASSERT),
              client.get("/sw.js", headers=ASSERT)):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
    assert "content-security-policy" not in client.get("/desk/state", headers=ASSERT).headers
    assert client.get("/sw.js", headers=ASSERT).headers["service-worker-allowed"] == "/"


# --------------------------------------------------------------------------- F5 CSRF

CROSS_SITE = [{"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
              {"Origin": "null"}, {"Origin": f"http://{DESK_HOST}"}]


@pytest.mark.parametrize("path", ["/dispatch/run", "/stage-c/acceptance", "/desk/command"])
@pytest.mark.parametrize("headers", CROSS_SITE, ids=["cross-site", "same-site", "null", "http"])
def test_a_cross_site_write_on_the_desk_host_is_refused(env, path, headers):
    r = _desk().post(path, headers={**ASSERT, **headers}, json={"text": "do the thing"})
    assert r.status_code == 403 and r.json()["detail"] == "cross-site request refused"
    with Session(env) as db:
        assert db.scalar(select(func.count()).select_from(models.Task)) == 1   # no task created


@pytest.mark.parametrize("headers", [{"Origin": f"https://{DESK_HOST}", "Sec-Fetch-Site": "same-origin"}, {}],
                         ids=["same-origin", "no-browser-headers"])
def test_a_same_origin_or_non_browser_write_still_passes(env, headers):
    client = _desk()
    r = client.post("/stage-c/standing/revoke", headers={**ASSERT, **headers}, json={"reason": "test"})
    assert r.status_code == 200, r.text
    r = client.post("/desk/command", headers={**ASSERT, **headers}, json={"text": "summarise the week"})
    assert r.status_code == 201, r.text
    # A GET is never refused for its origin headers.
    assert client.get("/desk/state", headers={**ASSERT, "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_origin_is_compared_with_the_configured_desk_host_not_the_host_port(env):
    client, host = _desk(), {"Host": f"{DESK_HOST}:443"}
    same = {**ASSERT, **host, "Origin": f"https://{DESK_HOST}", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/desk/command", headers=same, json={"text": "summarise the week"}).status_code == 201
    evil = {**ASSERT, **host, "Origin": "https://evil.example"}
    r = client.post("/desk/command", headers=evil, json={"text": "summarise the month"})
    assert r.status_code == 403 and r.json()["detail"] == "cross-site request refused"


# --------------------------------------------------------------------------- write side (F1): stored rows, notifier

from tests.test_worker import SPECS, FakeProvider, _db, _good, _task   # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    payloads = []
    monkeypatch.setattr(worker, "notifier", type("N", (), {"dispatch": lambda self, db, ev: payloads.append(
        json.dumps(ev.payload))})())
    return payloads


def test_a_model_reported_failure_is_redacted_in_every_stored_row_and_the_notification(sent):
    db = _db()
    task = _task(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, status="failed",
                                                                  error=f"stderr Bearer {BEARER_BODY} {GH}")])}, SPECS)
    run = db.scalar(select(models.AgentRun))
    agent = db.scalar(select(models.Agent).where(models.Agent.key == "research"))
    db.refresh(task)
    events = [json.dumps(e.payload) for e in db.scalars(select(models.Event))]
    assert run.error and task.error and sent
    _no_leak("\n".join([run.error, run.supervisor_notes, agent.last_error, task.error, *events]))
    _no_leak("\n".join(sent))   # Telegram is its own egress channel


def test_an_approved_summary_is_redacted_in_the_result_and_the_notification_title(sent):
    db = _db()
    task = _task(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, summary=f"done {GH}")])}, SPECS)
    db.refresh(task)
    run = db.scalar(select(models.AgentRun))
    assert task.status == enums.TaskStatus.REVIEW and any(task.key in p for p in sent)
    _no_leak("\n".join([task.result, run.result, *sent]))


@pytest.mark.parametrize("boundary", [worker.MAX_ACTION_CHARS, redact.MAX_SCAN_CHARS], ids=["storage", "scan-cap"])
def test_approved_actions_are_redacted_before_storage_truncation(sent, boundary):
    db = _db()
    task = _task(db)
    action = _boundary_url(boundary)
    assert action.index("@") == boundary
    result = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, actions=[action])])}, SPECS)
    db.refresh(task)
    assert result["verdict"] == "APPROVED"
    stored = task.artifacts["actions"][0]
    assert len(stored) <= worker.MAX_ACTION_CHARS and PASSWORD[:8] not in stored
    assert ("***" in stored) if boundary == worker.MAX_ACTION_CHARS else (stored == redact.UNAVAILABLE)


def test_approved_output_is_redacted_in_artifacts_child_tasks_checkpoints_and_nested_audits(sent, monkeypatch):
    db = _db()
    task = _task(db, artifacts={"legacy": {"nested": [{"detail": GH, GH: "keep this text"}]}})
    original_materialize = worker.task_materializer.materialize
    original_resume = worker._resume_result
    observed = []

    def resume(db, run_id, specs):
        run = db.get(models.AgentRun, run_id)
        assert run.tool_calls["result_checkpoint"]["output"]["actions"]
        _no_leak(json.dumps(run.tool_calls))
        observed.append("checkpoint")
        return original_resume(db, run_id, specs)

    def materialize(db, parent, run, candidates):
        observed.append("materialize")
        _no_leak(json.dumps([c.model_dump() for c in candidates]))
        _no_leak(json.dumps(run.tool_calls))
        return original_materialize(db, parent, run, candidates)

    monkeypatch.setattr(worker.task_materializer, "materialize", materialize)
    monkeypatch.setattr(worker, "_resume_result", resume)
    result = worker.run_once(db, {"claude": FakeProvider("claude", [_good(
        task.key, actions=[f"action {GH}"], evidence=[f"selftest-x {task.key} {SK}"],
        artifacts=[json.dumps({"nested": [{"detail": GH}, {"detail": JWT}]})],
        new_task_candidates=[{"title": f"Research {GH}", "reason": f"Investigate {SK}", "kind": "research"}])])}, SPECS)
    db.refresh(task)
    assert result["verdict"] == "APPROVED" and observed == ["checkpoint", "materialize"]
    _no_leak(json.dumps(task.artifacts))
    child = db.scalar(select(models.Task).where(models.Task.created_by == "task-materializer"))
    assert child is not None and child.title and child.objective and child.context["reason"]
    _no_leak(json.dumps({"title": child.title, "objective": child.objective, "context": child.context}))
    audit = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.autonomous-run"))
    assert audit.before["artifacts"]["legacy"]["nested"] and audit.after["artifacts"]["artifacts"]
    assert audit.before["artifacts"]["legacy"]["nested"][0]["***"] == "keep this text"
    _no_leak(json.dumps({"before": audit.before, "after": audit.after, "reason": audit.reason}))


def test_legacy_notes_and_run_text_are_redacted_in_the_provider_prompt(sent):
    db = _db()
    task = _task(db)
    db.add(models.AgentNote(agent_id=task.assigned_agent_id, task_id=task.id,
                            note=f"legacy note {GH}", fingerprint="legacy"))
    db.add(models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id,
                           status=enums.AgentRunStatus.FAILED, ended_at=datetime.now(timezone.utc),
                           result=f"legacy result {SK}", error=f"legacy error {JWT}"))
    db.commit()
    provider = FakeProvider("claude", [_good(task.key)])
    prompts = []
    original = provider._run

    def capture(prompt, schema, **kwargs):
        prompts.append(prompt)
        return original(prompt, schema, **kwargs)

    provider._run = capture
    result = worker.run_once(db, {"claude": provider}, SPECS)
    assert result["verdict"] == "APPROVED" and len(prompts) == 1
    assert "legacy note" in prompts[0] and "legacy result" in prompts[0] and "recent_runs" in prompts[0]
    _no_leak(prompts[0])


def test_provider_run_redacts_any_providers_error():
    raw = ProviderResult("claude", Health.AVAILABLE, error=f"reasoner said token={GH}")
    assert "***" in FakeProvider("claude", [raw]).run("p", {}, cwd="/tmp", timeout_s=5, max_turns=1).error
    _no_leak(raw.error)


def test_a_herdr_run_report_stores_redacted_checks_and_notes(env):
    with Session(env) as db:
        db.add(models.Task(key="TASK-0102", title="herdr", success_criteria="s", result=f"legacy {GH}",
                           status=enums.TaskStatus.IN_PROGRESS))
        db.commit()
    state = {"state": "running", "created_at": "2026-11-04T10:00:00Z", "updated_at": "2026-11-04T10:05:00Z",
             "workflow": {"name": f"wf {SK}"}, "goal": f"goal {GH}",
             "checks": {"status": "failed", "summary": f"E {GH}"}}
    client = TestClient(main.app, client=("127.0.0.1", 5000))
    r = client.post("/tasks/TASK-0102/run-report", json={"herdr_state": state})
    assert r.status_code == 201, r.status_code
    _no_leak(r.text)
    duplicate = client.post("/tasks/TASK-0102/run-report", json={"herdr_state": state})
    assert duplicate.status_code == 201 and duplicate.json()["ignored"] == "duplicate"
    _no_leak(duplicate.text)
    with Session(env) as db:
        run = db.scalar(select(models.AgentRun).where(models.AgentRun.provider == main.HERDR_PROVIDER))
        task = db.get(models.Task, run.task_id)
        audit = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "task.run-report"))
        event = db.scalar(select(models.Event).where(models.Event.task_id == task.id))
        assert run.error and run.supervisor_notes and run.model and run.result and task.result
        assert audit.reason and event.payload["title"] and r.json()["outcome"]["summary"]
        _no_leak(json.dumps({"model": run.model, "error": run.error, "notes": run.supervisor_notes,
                             "run_result": run.result, "task_result": task.result, "audit_reason": audit.reason,
                             "before": audit.before, "after": audit.after, "event": event.payload}))


# --------------------------------------------------------------------------- fail closed, and no false positives

def test_a_broken_redactor_fails_closed_on_the_desk_and_in_notifications(env, sent, monkeypatch, caplog):
    def broken(text):
        raise RuntimeError("scanner unavailable")
    caplog.set_level("ERROR", logger="company.redact")
    monkeypatch.setattr(redact, "_redact_secrets", broken)
    client = _desk()
    for path in ("/desk/state", "/tasks/TASK-0101", "/agents/research", "/desk/memory"):
        r = client.get(path, headers=ASSERT)
        assert r.status_code == 200, path
        _no_leak(r.text)
    t = client.get("/tasks/TASK-0101", headers=ASSERT)
    assert t.json()["error"] == redact.UNAVAILABLE == "[unavailable]"   # a fixed constant: no length or fragment
    assert "scanner unavailable" not in t.text and "RuntimeError" not in t.text
    db = _db()
    task = _task(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, status="failed", error=f"Bearer {BEARER_BODY}")])},
                    SPECS)
    assert sent and redact.UNAVAILABLE in sent[-1]
    _no_leak("\n".join(sent))
    assert not any("scanner unavailable" in p or "RuntimeError" in p for p in sent)
    logged = [r for r in caplog.records if r.name == "company.redact"]
    assert logged and {r.getMessage() for r in logged} == {"redaction_failed"} and not any(r.exc_info for r in logged)
    assert providers.provider_error(f"token={GH}") == redact.UNAVAILABLE


ORDINARY = ["codex run refused: its login could not be snapshotted or its output could not be checked",
            "subscription usage limit reached; waiting for reset (provider says: try again at 5:00 PM)",
            "timeout after 600s", "exit 1", "FileNotFoundError: /srv/worktree/app/main.py line 42",
            "commit a1b2c3d failed CI on main", "https://github.com/Seerxo/Acthelm/pull/61",
            "TASK-8101 blocked: CHECKS_REPEATED: the same tests failed in consecutive attempts",
            "FAILED tests/test_calc.py::test_add - assert 3 == 4"]


@pytest.mark.parametrize("text", ORDINARY, ids=range(len(ORDINARY)))
def test_ordinary_text_passes_through_unchanged(text):
    from app import desk
    assert desk._clean(text) == text
    assert providers.provider_error(text) == text and providers.provider_error(text, tail=True) == text
