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

from app import cloudflare_access, enums, main, models, notebook, providers, worker
from app.providers import Health

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
        s.flush()
        task = models.Task(
            key="TASK-0101", title="leaky", success_criteria="s", status=enums.TaskStatus.BLOCKED,
            assigned_agent_id=research.id,
            error=f"codex: stderr tail ... Authorization: Bearer {BEARER_BODY}",
            context={"write": True, "autonomous": True, "source": "founder-desk-command",
                     "approval": {"fingerprint": f"token={GH}"}, "verify": {"one_of": [SK]},
                     "improvement": {"stage": "CI", "history": [
                         {"stage": "PR_OPEN", "at": "2026-11-04T00:00:00+00:00", "actor": "stage-c-worker",
                          "evidence": {"pr": PR, "pr_number": 7, "commit": "a" * 40, "ci_state": "failed",
                                       "failed_check": f"python ({GH})", "verdict": "APPROVE",
                                       "previous_diff": f"+KEY = '{GH}'\n+URL = 'https://u:{PASSWORD}@h'",
                                       "failures": [f"E   {SK}"], "findings": [{"detail": [JWT]}]}}]}},
            artifacts={"actions": ["Ranked item one", f"leak {SK}"], "evidence": [GH], "artifacts": {"k": [JWT]},
                       "pr": PR, "commit": "a" * 40, "run_id": 1})
        s.add(task)
        s.flush()
        s.add(models.AgentRun(agent_id=research.id, task_id=task.id, provider="codex", started_at=now,
                              status=enums.AgentRunStatus.FAILED, error=f"stderr: token={GH}",
                              supervisor_verdict=enums.SupervisorVerdict.FAILED,
                              supervisor_notes=f"model wrote {SK}", result=f"summary {GH}"))
        s.add(models.AgentNote(agent_id=research.id, task_id=task.id, note=f"clone https://u:{PASSWORD}@git.example",
                               fingerprint="leaky-note"))
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
