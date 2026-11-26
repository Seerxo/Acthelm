"""Phase C joined path, offline and in-process: production Desk create -> founder release -> B5 delivery/activation ->
Stage C founder Start Pilot + READY -> armed worker (real admission, authorization consume, runner, checks, independent
review, fork publish) -> CI ingest -> founder merge on GitHub -> post-merge verify -> signed snapshot back to production
-> production verifies with its own runtime -> founder DONE. Only the external edges are faked: the HTTP hop between the two apps (World.transport), the coder
container (pipeline's launcher_coder), git fetch/push remotes (local bare repos), and GitHub's API answers (gh_facts).
Short test ids on purpose (root conftest byte budget)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import control, enums, models, stage_c_ci, stage_c_merge_gate, stage_c_verify, stage_c_worker, watchdog, worker
from app import stage_c_handoff as h
from tests.test_stage_c_handoff import ADMIN, ASSERT, SHA, World, _authorize, w  # noqa: F401  (w is the two-app fixture)
from tests.test_stage_c_worker import Reviewer, accept_host, pipeline, runtime_report
from tests.test_worker import SPECS

FORK = "seerxo-agent/acthelm"
PATH = "tests/test_metrics.py"
MERGE = "9" * 40


def _count(engine, model, **where):
    with Session(engine) as s:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return s.scalar(q)


def _audits(engine, action):
    return _count(engine, models.AuditLog, action=action)


def _task(engine, key):
    with Session(engine) as s:
        return s.scalar(select(models.Task).where(models.Task.key == key))


def _stage(engine, key):
    return ((_task(engine, key).context or {}).get("improvement") or {}).get("stage")


def _poll(w, key):
    """One status poll as if POLL_EVERY had elapsed (the next poll is due), keeping the report clock real so the
    snapshot's freshness, which DONE needs, is judged honestly."""
    with Session(w.prod) as s:
        task = s.scalar(select(models.Task).where(models.Task.key == key))
        h._set_remote(task, next_attempt_at=None)
        s.commit()
    return w.sync()["requests"]


def _checker():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "phasec_e2e_check", Path(__file__).resolve().parents[1] / "scripts/phasec-e2e-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _evidence(w, pkey, key, pr=None):
    """What the founder captures for scripts/phasec-e2e-check.py: GET reads of both apps only."""
    def get(path):   # /audit is not a Desk read: the runbook captures it on each host (admin/loopback GET)
        r = (w.client(host="api.example.invalid").get(path, headers={"authorization": f"Bearer {ADMIN}"})
             if path.startswith("/audit") else w.client().get(path, headers=ASSERT))
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        return r.json()
    with w.on("prod"):
        prod_task, prod_audit = get(f"/tasks/{pkey}"), get("/audit?limit=500")
    with w.on("sc"):
        sc_task, sc_audit = get(f"/tasks/{key}") if key else None, get("/audit?limit=500")
    return {"prod-task.json": prod_task, "prod-audit.json": prod_audit, "sc-task.json": sc_task,
            "sc-audit.json": sc_audit, "pr.json": pr, "arm.txt": None}


def _steps(w, pkey, key, pr=None):
    out = _checker().check(_evidence(w, pkey, key, pr), founder_login=stage_c_merge_gate.FOUNDER, path=PATH,
                           fork_owner=FORK.split("/")[0])
    assert not [r for r in out if r["status"] == "STOP"], out
    return {r["step"]: r["status"] for r in out}


def test_e2e(w, monkeypatch, tmp_path):  # noqa: F811
    # ---- fixtures: real receiver config, armed-worker control plane; external edges only are fakes
    with Session(w.sc) as s:
        s.scalar(select(models.Project)).repository = "seerxo/acthelm"
        s.add_all([models.Agent(key=k, role=k) for k in ("ceo", "supervisor", "research")])
        for agent in s.scalars(select(models.Agent)):
            agent.enabled = True
        s.commit()
    with Session(w.prod) as s:
        s.add(models.Project(key="company-os", name="Company OS", status=enums.ProjectStatus.ACTIVE))
        s.commit()
    real_ready, real_runtime_ready = stage_c_worker.ready, stage_c_worker.runtime_ready
    remote = pipeline(monkeypatch, tmp_path, mock_authorization=False, publish_repository=FORK)
    monkeypatch.setattr(stage_c_worker, "ready", real_ready)               # real acceptance + preflight gate
    monkeypatch.setattr(stage_c_worker, "runtime_ready", real_runtime_ready)
    monkeypatch.setenv("GH_TOKEN", "fixture-credential")
    monkeypatch.setattr(stage_c_worker.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(worker, "worker_state",   # each side's worker heartbeat: code write on Stage C only
                        lambda db: {"runtime": {"git_sha": SHA}, "autonomy": {"code_write_flag": w.side == "sc"}})
    gh = {"pr": None, "main": {"main_ci": None, "live_contains_merge": None}}
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *a, **k: dict(gh["pr"] or {}))
    monkeypatch.setattr(stage_c_verify, "gh_facts", lambda *a, **k: dict(gh["main"]))
    ps = {n: Reviewer(n) for n in ("claude", "codex")}

    def tick():
        with w.on("sc"), Session(w.sc) as s:
            return worker.run_once(s, ps, SPECS)

    def done(key):
        with w.on("prod"):
            return w.client().post(f"/tasks/{key}/status", headers=ASSERT, json={
                "status": "DONE", "actor": "founder", "evidence": f"merged {MERGE}"})

    # ---- T1 founder creates the code task in the production Desk (verified founder only)
    with w.on("prod"):
        body = {"title": "Pilot metric boundary", "objective": "Make the metric test pass",
                "success_criteria": "Tests pass", "paths": [PATH]}
        assert w.client(host="api.example.invalid").post("/stage-c/tasks", json=body).status_code in (401, 403)
        r = w.client().post("/stage-c/tasks", json=body, headers=ASSERT)
    assert r.status_code == 201, r.text
    pkey = r.json()["key"]
    assert _task(w.prod, pkey).status == enums.TaskStatus.BACKLOG

    # ---- T2 founder release: bound to the stored task; production never runs it
    r = w.release(key=pkey)
    assert r.status_code == 201, r.text
    hid, digest = r.json()["id"], r.json()["payload_sha256"]
    assert _task(w.prod, pkey).status == enums.TaskStatus.WAITING_EXTERNAL
    assert done(pkey).status_code == 409                                   # DONE refused: nothing reported yet

    # ---- T3 delivery (import) then T4 activation; repeated syncs never duplicate
    assert w.sync()["requests"][0]["outcome"] == "IMPORTED"
    assert w.sync()["requests"][0]["outcome"] == "ACTIVATED"
    [imp] = w.sc_tasks()
    key = imp.key
    assert imp.status == enums.TaskStatus.BACKLOG and imp.context["remote_stage_c"]["id"] == hid
    assert imp.context["stage_c"]["paths"] == [PATH] and imp.created_by == h.SERVICE
    assert _poll(w, pkey)[0]["outcome"] == "OBSERVED"                       # third sync: a status poll, no import
    assert len(w.sc_tasks()) == 1 and _audits(w.sc, "handoff.import") == 1 and _audits(w.sc, "handoff.activate") == 1
    steps = _steps(w, pkey, key)
    assert [steps[k] for k in ("CREATE", "RELEASE", "IMPORT", "ACTIVATION", "AUTHORIZATION")] == [
        "PASS", "PASS", "PASS", "PASS", "WAIT"], steps

    # ---- T5 Stage C host acceptance + fresh preflight (host wizard / watchdog), then founder Start Pilot
    with Session(w.sc) as s:
        accept_host(s, SHA)
        runtime_report(s, SHA)
        other = models.Task(key="TASK-0900", title="native", objective="o", success_criteria="s",
                            status=enums.TaskStatus.READY, risk_level=enums.RiskLevel.LOW,
                            assigned_agent_id=imp.assigned_agent_id, project_id=imp.project_id,
                            context={"autonomous": True, "write": True, "stage_c": {"paths": [PATH]}})
        s.add(other)
        s.commit()
        other_id = other.id
    assert _authorize(w, key).status_code == 422                           # must name the handoff binding
    assert _authorize(w, "TASK-0900", handoff_id=hid, payload_sha256=digest).status_code == 422
    r = _authorize(w, key, handoff_id=hid, payload_sha256=digest, expected_source_sha=SHA)
    assert r.status_code == 201, r.text
    assert _audits(w.sc, "stage-c.authorization.issue") == 1
    with Session(w.sc) as s:                                               # the grant is this task's alone
        task_other = s.get(models.Task, other_id)
        assert control.write_authorization_denial(s, task_other, SHA) is not None

    # ---- T6 founder marks the imported task READY on Stage C (the API re-checks every gate)
    with w.on("sc"):
        r = w.client().post(f"/tasks/{key}/status", headers=ASSERT, json={"status": "READY", "actor": "founder"})
    assert r.status_code == 200, r.text

    # ---- T7 armed worker (scripts/stage-c-arm sets STAGE_C_ARMED_TASK): codes, checks, reviews, publishes once
    monkeypatch.setenv(control.ARMED_TASK_ENV, key)
    with Session(w.sc) as s:
        assert control.write_authorization_denial(s, s.get(models.Task, other_id), SHA) \
            == "stage_c_armed_for_other_task"
    out = tick()
    assert out.get("task") == key and out["model_calls"] == 2, out          # coder + independent reviewer
    assert _stage(w.sc, key) == "PR_OPEN"
    t = _task(w.sc, key)
    approval = t.context["improvement"]["approval"]
    assert t.status == enums.TaskStatus.REVIEW and approval["coder"] != approval["reviewer"]
    commit = t.artifacts["commit"]
    with Session(w.sc) as s:
        grant = s.get(models.ControlState, control.PILOT_AUTHORIZATION).value
        assert grant["consumed_at"] and grant["handoff_id"] == hid and grant["task_key"] == key
    assert _task(w.sc, "TASK-0900").status == enums.TaskStatus.READY      # the other task was never claimed
    assert _count(w.sc, models.AgentRun, task_id=other_id) == 0
    runs = _count(w.sc, models.AgentRun)
    # second tick: no new run, no new model call, the grant cannot be spent again
    out = tick()
    assert out["model_calls"] == 0 and _count(w.sc, models.AgentRun) == runs and _stage(w.sc, key) == "PR_OPEN"
    with Session(w.sc) as s:
        assert control.consume_write_authorization(s, s.get(models.Task, imp.id), SHA, 999) is not None
        s.rollback()
    assert _audits(w.sc, "improvement.review_approved") == 1

    # snapshot back: PR open, no merge -> DONE refused
    assert _poll(w, pkey)[0]["outcome"] == "OBSERVED"
    snap = _task(w.prod, pkey).artifacts["remote_stage_c"]["snapshot"]
    assert snap["task_key"] == key and snap["task"]["stage"] == "PR_OPEN" and snap["merge"]["state"] == "NOT_OBSERVED"
    assert snap["authorization"]["status"] == "CONSUMED"
    assert done(pkey).status_code == 409
    pr_json = {"number": snap["task"]["pr"]["number"], "state": "OPEN", "headRefName": f"task/{key}",
               "headRepositoryOwner": {"login": FORK.split("/")[0]}, "files": [{"path": PATH}],
               "mergedBy": None, "mergeCommit": None}
    steps = _steps(w, pkey, key, pr_json)
    assert steps["CODER"] == "PASS" and steps["PR"] == "PASS" and steps["CI"] == "WAIT", steps
    bad = _checker().check({**_evidence(w, pkey, key, pr_json), "pr.json": {**pr_json, "files": [{"path": PATH},
                            {"path": "app/main.py"}]}}, founder_login=stage_c_merge_gate.FOUNDER, path=PATH,
                           fork_owner=FORK.split("/")[0])
    assert [r["step"] for r in bad if r["status"] == "STOP"] == ["PR"]        # a stop condition, not a wait

    # ---- T8 GitHub CI green on the fork head -> READY_TO_MERGE (no auto-merge)
    pr = {"state": "open", "head_sha": commit, "head_ref": f"task/{key}", "head_repo": FORK, "base_ref": "main",
          "draft": False, "checks_complete": True, "statuses": {}, "statuses_complete": True,
          "status_problems": [], "checks": [{"name": "python-tests", "status": "completed", "conclusion": "success"}]}
    # (A head outside the fork, or a non-founder merge, BLOCKS the task fail-closed: tests/test_stage_c_ci.py covers it;
    # here it would end the joined run.)
    gh["pr"] = pr
    tick()
    assert _stage(w.sc, key) == "READY_TO_MERGE", _task(w.sc, key).error
    tick()
    assert _stage(w.sc, key) == "READY_TO_MERGE" and _count(w.sc, models.AgentRun) == runs
    assert _poll(w, pkey)[0]["outcome"] == "OBSERVED"
    assert _task(w.prod, pkey).artifacts["remote_stage_c"]["snapshot"]["task"]["ci"]["state"] == "SUCCESS"
    assert done(pkey).status_code == 409

    # ---- T9 founder merges on GitHub -> MERGED recorded once from GitHub's facts
    gh["pr"] = {**pr, "state": "closed", "merged": True, "merged_by": stage_c_merge_gate.FOUNDER,
                "merge_commit": MERGE}
    tick()
    assert _stage(w.sc, key) == "MERGED"
    tick()
    with Session(w.sc) as s:
        merged = [a for a in s.scalars(select(models.AuditLog).where(models.AuditLog.action == "improvement.advance",
                                                                     models.AuditLog.entity_key == key))
                  if (a.after or {}).get("stage") == "MERGED"]
    assert len(merged) == 1 and merged[0].after["evidence"]["merge_commit"] == MERGE
    assert merged[0].actor.startswith("founder (github:")

    # ---- T10 Stage C reads main CI on the merge commit. An imported task's deploy is production's: Stage C stays MERGED
    # even when its own host runs the merge (G4), and a repeated identical read does not move the report's seq.
    gh["main"] = {"main_ci": "success", "live_contains_merge": True}
    with Session(w.sc) as s:
        watchdog.record_host_metrics(s, {"cpu_used_pct": 1, "runtime": {"api_sha": SHA, "worker_sha": SHA}})
    tick()
    assert _stage(w.sc, key) == "MERGED"
    assert _task(w.sc, key).artifacts[h.MAIN_CI_KEY] == {"sha": MERGE, "state": "SUCCESS"}
    revision = _task(w.sc, key).revision
    tick()
    assert _stage(w.sc, key) == "MERGED" and _task(w.sc, key).revision == revision

    # ---- T11 signed snapshot back to production: MERGED + main CI green, never VERIFIED from Stage C (G1)
    assert _poll(w, pkey)[0]["outcome"] == "OBSERVED"
    snap = _task(w.prod, pkey).artifacts["remote_stage_c"]["snapshot"]
    assert snap["merge"] == {"state": "MERGED", "commit": MERGE, "at": snap["merge"]["at"],
                             "main_ci": {"state": "SUCCESS", "sha": MERGE}}
    assert snap["id"] == hid and snap["payload_sha256"] == digest and snap["task_key"] == key
    assert _task(w.prod, pkey).status == enums.TaskStatus.WAITING_EXTERNAL
    assert w.sync()["requests"] == []                                      # not due yet: nothing is sent
    assert _poll(w, pkey)[0]["outcome"] == "UNCHANGED"                      # same seq: freshness only, no audit
    r = done(pkey)
    assert r.status_code == 409 and "production" in r.text, r.text        # merged, but production does not run it
    pr_json = {**pr_json, "state": "MERGED", "mergedBy": {"login": stage_c_merge_gate.FOUNDER},
               "mergeCommit": {"oid": MERGE}}
    steps = _steps(w, pkey, key, pr_json)
    assert steps["MERGED"] == "PASS" and steps["VERIFIED"] == "WAIT" and steps["DONE"] == "WAIT", steps

    # ---- T11b production is released to the merge commit (its own watchdog + process SHA), then VERIFIED
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", MERGE)
    with Session(w.prod) as s:
        watchdog.record_host_metrics(s, {"cpu_used_pct": 1, "runtime": {"api_sha": MERGE, "worker_sha": MERGE}})
    # (one process plays both apps here, so Stage C's reported runtime SHA moves too: OBSERVED, not UNCHANGED)
    assert _poll(w, pkey)[0]["outcome"] in ("OBSERVED", "UNCHANGED")
    with w.on("prod"):
        [row] = w.client().get("/desk/handoffs", headers=ASSERT).json()["handoffs"]
    assert row["handoff"]["verification"]["state"] == "VERIFIED" and row["status"] == "WAITING_EXTERNAL"
    steps = _steps(w, pkey, key, pr_json)
    assert steps["VERIFIED"] == "PASS" and steps["DONE"] == "WAIT", steps

    # ---- T12 founder DONE: only the verified founder, only now
    with w.on("prod"):
        assert w.client(host="api.example.invalid").post(f"/tasks/{pkey}/status", json={
            "status": "DONE", "actor": "founder", "evidence": "x"}).status_code in (401, 403)
    r = done(pkey)
    assert r.status_code == 200, r.text
    assert _task(w.prod, pkey).status == enums.TaskStatus.DONE
    calls = len(w.calls)
    assert w.sync()["requests"] == [] and len(w.calls) == calls             # closed: nothing more is sent
    with Session(w.prod) as s:
        actions = [a for (a,) in s.execute(select(models.AuditLog.action).where(
            models.AuditLog.entity_key == pkey).order_by(models.AuditLog.id))]
    assert actions[0] == "task.create" and actions[-1] == "task.status"
    assert actions.count("handoff.release") == 1 and actions.count("handoff.delivery") == 1
    assert actions.count("handoff.activation") == 1 and "handoff.observed" in actions
    assert actions.count("handoff.production_verification") == 1
    assert set(_steps(w, pkey, key, pr_json).values()) == {"PASS"}          # every transition evidenced
    # Stage C: one task, one coding run's grant, the coded file reached the fork branch only
    assert len(w.sc_tasks()) == 2 and _audits(w.sc, "stage-c.authorization.issue") == 1
    import subprocess
    head = subprocess.run(["git", "--git-dir", str(remote), "show", f"task/{key}:{PATH}"],
                          capture_output=True, check=True).stdout
    assert b"assert 1 == 1" in head
