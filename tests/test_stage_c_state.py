"""GET /stage-c/state: the Founder Desk's one Stage C read model. Break caught: a non-founder reads it, the frontend
gets free text (context, errors, findings, emails, tokens), missing evidence reads as success, CI on other bytes
reads as green, or the lifecycle state is wrong for running / blocked / failed / ready-to-merge / expired / revoked."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app import audit, control, enums, main, models, stage_c_worker
from tests.test_founder_authorization import (ADMIN, ASSERT, DESK, FID, FOUNDER, SHA, YT, _bearer, _c, _founder,  # noqa: F401
                                              env)

PATH = "tests/test_metrics.py"
HEAD, OLD = "1" * 40, "2" * 40
FP, FP2 = "a" * 64, "b" * 64
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_now():
    """Stamp each test at its own run time: an import-time NOW goes stale in a long serial run (the gateway's
    single-CPU checks), and a stale preflight/grant then reads NOT_VERIFIED instead of the state under test."""
    global NOW
    NOW = datetime.now(timezone.utc)


def heartbeat(monkeypatch, *, sha=SHA, status="HEALTHY", flag=True, stage="C"):
    view = {"status": status, "runtime": {"git_sha": sha}, "autonomy": {"code_write_flag": flag, "stage": stage}}
    monkeypatch.setattr(main.worker, "worker_state", lambda db: view)


def green(s):
    """Accepted host and a fresh watchdog preflight for SHA, through the real functions."""
    control.record_acceptance_candidate(s, SHA, {k: True for k in stage_c_worker.ACCEPTANCE_CHECKS})
    control.accept_host(s, SHA, founder=FID)
    s.add(models.ControlState(key=stage_c_worker.RUNTIME_READINESS_KEY, updated_by="watchdog",
                              value={"ready": True, "source_sha": SHA, "checked_at": NOW.isoformat()}))
    s.commit()


def task(s, key="TASK-0101", status=enums.TaskStatus.REVIEW, history=None, approval=None, error=None, **run):
    t = models.Task(key=key, title="t", success_criteria="s", status=status, error=error,
                    context={"improvement": {"stage": history[-1]["stage"] if history else None,
                                             "history": history or []}} if history is not None else None)
    s.add(t)
    s.flush()
    if approval:
        ev = audit.record(s, actor="codex", action="improvement.review_approved", entity_type="task",
                          entity_key=key, after=approval, task_id=t.id)
        s.flush()
        t.context = {**t.context, "improvement": {**t.context["improvement"], "approval": {**approval, "audit_id": ev.id}}}
    fields = {"status": run.pop("run_status", enums.AgentRunStatus.SUCCEEDED), "started_at": NOW - timedelta(minutes=10),
              "ended_at": NOW, "tool_calls": {"stage_c": True, "model_calls": 2, "outcome": "PR_OPEN"}}
    s.add(models.AgentRun(task_id=t.id, provider="claude", **{**fields, **run}))
    s.commit()
    return t


def h(stage, **evidence):
    return {"stage": stage, "actor": "stage-c-runner", "at": NOW.isoformat(), "evidence": evidence}


def published(ci_commit=HEAD, ci_state="passed", stage="READY_TO_MERGE", review_fp=FP):
    hist = [h("DETECTED"), h("PLANNED"), h("IMPLEMENTING", base=OLD),
            h("TESTING", fingerprint=FP, coder="claude"),
            h("INDEPENDENT_REVIEW", fingerprint=FP, checks_passed=True, checks="ok"),
            h("PR_OPEN", commit=HEAD, fingerprint=FP, pr="https://github.com/seerxo/acthelm/pull/61"),
            h("CI", commit=HEAD, pr_number=61, ci_state="running")]
    if stage == "READY_TO_MERGE":
        hist.append(h("READY_TO_MERGE", commit=ci_commit, pr_number=61, ci_state=ci_state))
    approval = {"fingerprint": review_fp, "contract_hash": "x", "review_stage_at": NOW.isoformat(),
                "reviewer": "codex", "coder": "claude", "findings": [], "at": NOW.isoformat()}
    return hist, approval


def get(env):
    r = _founder().get("/stage-c/state", headers=ASSERT)
    assert r.status_code == 200, r.text
    return r.json()


def db(env):
    return Session(env[0])


def test_pf_names(env):
    names = [f"acceptance:{name}" for name in sorted(stage_c_worker.ACCEPTANCE_CHECKS)]
    cases = [(["acceptance:claude_edit"], ["acceptance:claude_edit"]),
             (["not_a_check", "acceptance:unknown", {}, 7], ["other"] * 4),
             (["source_sha"] * 11, ["source_sha"] * 10)]
    cases += [(names[i:i + 10], names[i:i + 10]) for i in range(0, len(names), 10)]
    for failed, expected in cases:
        with db(env) as s:
            s.merge(models.ControlState(key=stage_c_worker.RUNTIME_READINESS_KEY, updated_by="watchdog",
                                        value={"ready": False, "source_sha": SHA, "checked_at": NOW.isoformat(),
                                               "failed": failed}))
            s.commit()
        pre = get(env)["runtime"]["preflight"]
        assert pre["status"] == "FAIL" and pre["failed"] == expected


# ---------------------------------------------------------------------------------------------- authorization
@pytest.mark.parametrize("mode", ["enforce", "log"])
@pytest.mark.parametrize("label,headers,host,code", [
    ("anonymous", {}, "172.18.0.5", 401), ("youtube", _bearer(YT), "172.18.0.5", 403),
    ("desk token", _bearer(DESK), "172.18.0.5", 403), ("admin", _bearer(ADMIN), "172.18.0.5", 403),
    ("local", {}, "127.0.0.1", 403), ("host", {}, "10.0.6.1", 403)])
def test_founder_only_read(env, monkeypatch, mode, label, headers, host, code):
    monkeypatch.setenv("API_AUTH_MODE", mode)
    r = _c(host).get("/stage-c/state", headers=headers)
    assert r.status_code == code, (label, mode, r.text)
    assert "runtime" not in r.text


def test_the_founder_reads_it_and_a_wrong_access_assertion_does_not(env):
    assert _founder().get("/stage-c/state", headers=ASSERT).status_code == 200
    assert _founder().get("/stage-c/state", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 401


# ---------------------------------------------------------------------------------------------- contract
def test_the_contract_has_exactly_these_keys(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.authorize_pilot(s, "TASK-0045", SHA, founder=FID)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        hist, approval = published()
        task(s, history=hist, approval=approval)
    out = get(env)
    assert set(out) == {"version", "generated_at", "state", "blockers", "next_action", "last_ready_pr", "runtime",
                        "authorizations", "tasks", "metrics", "code_task_paths"}
    assert out["version"] == 1
    assert set(out["runtime"]) == {"api_sha", "worker_sha", "sha_agree", "worker_heartbeat", "write_enabled",
                                   "worker_ready", "agents_paused", "acceptance", "preflight", "reasoner_sha",
                                   "host_facts"}
    assert out["runtime"]["reasoner_sha"] is None
    assert out["runtime"]["host_facts"] == {"host_checks": "NOT_VERIFIED", "reasoner_sha": "NOT_VERIFIED",
                                              "api_sha": "RECORDED", "worker_sha": "RECORDED"}
    assert set(out["last_ready_pr"]) == {"task_key", "number", "head_sha", "stage"}
    assert set(out["runtime"]["acceptance"]) == {"source_sha", "accepted_at", "valid"}
    assert set(out["runtime"]["preflight"]) == {"status", "checked_at", "source_sha", "failed"}
    assert set(out["authorizations"]) == {"one_task", "standing"}
    assert set(out["authorizations"]["one_task"]) == {"task_key", "source_sha", "expires_at", "revoked", "issuer", "status"}
    assert set(out["authorizations"]["standing"]) == {"source_sha", "paths", "expires_at", "max_per_day", "used_today",
                                                      "remaining_today", "revoked", "issuer", "status"}
    (t,) = out["tasks"]
    assert set(t) == {"key", "status", "stage", "attempts", "retry_count", "repairs", "started_at", "updated_at",
                      "test_verdict", "reviewer_verdict", "tested_fingerprint", "reviewed_fingerprint",
                      "fingerprint_match", "pr", "ci", "last_outcome", "failure_category", "needs_founder", "history"}
    assert all(set(e) == {"stage", "at"} for e in t["history"])
    assert set(t["pr"]) == {"number", "head_sha"} and set(t["ci"]) == {"state", "sha"}
    assert set(out["metrics"]) == {"tasks", "attempts", "model_calls", "minutes", "tokens", "usd_recorded", "usd_note",
                                   "quota_stops", "truncated"}
    assert set(out["metrics"]["tasks"]) == {"started", "reached_ready_to_merge", "stopped", "in_progress", "abandoned"}
    assert set(out["metrics"]["model_calls"]) == {"total"}
    assert out["authorizations"]["one_task"] == {"task_key": "TASK-0045", "source_sha": SHA, "issuer": "founder",
                                                 "expires_at": out["authorizations"]["one_task"]["expires_at"],
                                                 "revoked": False, "status": "ACTIVE"}


def test_tasks_are_bounded_to_twenty(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        for i in range(25):
            task(s, key=f"TASK-{1000 + i}", status=enums.TaskStatus.READY)
    out = get(env)
    assert len(out["tasks"]) == 20 and out["metrics"]["tasks"]["started"] == 25


def test_metrics_do_not_load_full_tasks_outside_the_list(env, monkeypatch):
    from sqlalchemy import event

    heartbeat(monkeypatch)
    with db(env) as s:
        t = task(s, history=[h("READY_TO_MERGE"), h("IMPLEMENTING")])
        t.context = {**t.context, "notes": "unused context " * 10000}
        s.commit()
        for i in range(24):
            task(s, key=f"TASK-{1000 + i}", status=enums.TaskStatus.READY)

    loaded = []

    def loaded_task(instance, context):
        loaded.append(instance.key)

    event.listen(models.Task, "load", loaded_task)
    try:
        out = get(env)
    finally:
        event.remove(models.Task, "load", loaded_task)
    assert len(loaded) == len(out["tasks"]) == 20
    assert "TASK-0101" not in loaded
    assert out["metrics"]["tasks"] == {"started": 25, "reached_ready_to_merge": 1, "stopped": 0,
                                        "in_progress": 24, "abandoned": 0}


# ---------------------------------------------------------------------------------------------- states
def test_ready_to_merge_with_an_active_grant_is_ready_and_names_the_founder(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID, max_per_day=2)
        hist, approval = published()
        task(s, history=hist, approval=approval)
    out = get(env)
    assert out["state"] == "READY" and out["blockers"] == ["task_needs_founder"] and out["next_action"] == "review_task"
    assert out["last_ready_pr"] == {"task_key": "TASK-0101", "number": 61, "head_sha": HEAD, "stage": "READY_TO_MERGE"}
    rt = out["runtime"]
    assert (rt["api_sha"], rt["worker_sha"], rt["sha_agree"], rt["write_enabled"]) == (SHA, SHA, None, True)
    assert rt["acceptance"]["valid"] is True and rt["acceptance"]["source_sha"] == SHA
    assert rt["preflight"]["status"] == "PASS"
    st = out["authorizations"]["standing"]
    assert st == {**st, "paths": [PATH], "max_per_day": 2, "used_today": 0, "remaining_today": 2, "revoked": False,
                  "issuer": "founder", "status": "ACTIVE", "source_sha": SHA}
    (t,) = out["tasks"]
    assert t["stage"] == "READY_TO_MERGE" and t["needs_founder"] is True
    assert t["test_verdict"] == "PASSED" and t["reviewer_verdict"] == "APPROVED" and t["fingerprint_match"] is True
    assert t["pr"] == {"number": 61, "head_sha": HEAD}
    assert t["ci"] == {"state": "SUCCESS", "sha": HEAD}
    assert out["metrics"]["tasks"]["reached_ready_to_merge"] == 1


def test_a_running_task_is_active(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        task(s, status=enums.TaskStatus.IN_PROGRESS, history=[h("DETECTED"), h("PLANNED"), h("IMPLEMENTING")],
             run_status=enums.AgentRunStatus.STARTED, ended_at=None)
    out = get(env)
    assert out["state"] == "ACTIVE"
    (t,) = out["tasks"]
    assert t["needs_founder"] is False and t["stage"] == "IMPLEMENTING" and t["pr"] is None and t["ci"] is None
    assert out["metrics"]["tasks"]["in_progress"] == 1


def test_a_blocked_task_blocks_with_a_fixed_failure_code(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        task(s, status=enums.TaskStatus.BLOCKED, history=[h("DETECTED")],
             error=stage_c_worker.REPAIR_INTERRUPTED + " worker lost a CI repair of https://x; inspect")
    out = get(env)
    assert out["state"] == "BLOCKED" and "task_needs_founder" in out["blockers"]
    assert out["tasks"][0]["failure_category"] == "REPAIR_INTERRUPTED"
    assert out["metrics"]["tasks"]["stopped"] == 1


@pytest.mark.parametrize("code", ["PRIVACY_GATE_BLOCKED", "PRIVACY_GATE_ERROR", "UNRECOGNIZED_OUTCOME"])
def test_privacy_gate_stops_keep_their_fixed_codes(env, monkeypatch, code):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        task(s, status=enums.TaskStatus.BLOCKED, history=[h("INDEPENDENT_REVIEW")], error=code,
             run_status=enums.AgentRunStatus.FAILED,
             tool_calls={"stage_c": True, "outcome": code})
    out = get(env)
    assert out["state"] == "BLOCKED"
    expected = "OTHER" if code == "UNRECOGNIZED_OUTCOME" else code
    assert out["tasks"][0]["failure_category"] == expected
    assert out["tasks"][0]["last_outcome"] == expected


def test_a_failed_task_is_failed(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        task(s, status=enums.TaskStatus.FAILED, history=[h("DETECTED")], error="CHECKS_FAILED",
             run_status=enums.AgentRunStatus.FAILED)
    out = get(env)
    assert out["state"] == "FAILED" and out["tasks"][0]["failure_category"] == "CHECKS_FAILED"
    assert out["metrics"]["attempts"] == {"total": 1, "failed": 1}


def test_an_expired_standing_grant_is_expired(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID, days=1, now=NOW - timedelta(days=3))
    out = get(env)
    assert out["state"] == "EXPIRED" and "standing_expired" in out["blockers"]
    assert out["authorizations"]["standing"]["status"] == "EXPIRED"


def test_a_revoked_standing_grant_reads_as_revoked_not_absent(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        assert control.revoke_standing(s, actor=FID)
    out = get(env)
    st = out["authorizations"]["standing"]
    assert out["state"] == "BLOCKED" and "standing_revoked" in out["blockers"]
    assert st["revoked"] is True and st["status"] == "REVOKED" and st["issuer"] == "founder"
    assert st["used_today"] == 0 and st["remaining_today"] == 0


def test_a_spent_daily_cap_names_the_standing_limit_not_the_consumed_pilot(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID, max_per_day=2)
        control.authorize_pilot(s, "TASK-0045", SHA, founder=FID)
        pilot = s.get(models.ControlState, control.PILOT_AUTHORIZATION)
        pilot.value = {**pilot.value, "consumed_at": NOW.isoformat(), "consumed_by_run_id": 1}
        row = s.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "used": {"day": NOW.date().isoformat(), "count": 2}}
        s.commit()
    out = get(env)
    st = out["authorizations"]["standing"]
    assert out["authorizations"]["one_task"]["status"] == "CONSUMED"
    assert st["status"] == "DAILY_LIMIT" and st["used_today"] == 2 and st["remaining_today"] == 0
    assert "standing_daily_limit" in out["blockers"] and "one_task_consumed" in out["blockers"]
    assert out["next_action"] == "wait_daily_reset"
    assert not any("invalid" in b for b in out["blockers"]) and out["state"] == "BLOCKED"


def test_yesterdays_count_is_not_today(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID, max_per_day=2)
        row = s.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "used": {"day": (NOW - timedelta(days=1)).date().isoformat(), "count": 2}}
        s.commit()
    st = get(env)["authorizations"]["standing"]
    assert (st["status"], st["used_today"], st["remaining_today"]) == ("ACTIVE", 0, 2)


def test_no_grant_is_blocked_on_no_authorization(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
    out = get(env)
    assert out["state"] == "BLOCKED" and out["blockers"] == ["no_authorization"]
    assert out["next_action"] == "issue_authorization" and out["last_ready_pr"] is None
    assert out["authorizations"] == {"one_task": None, "standing": None}


def test_a_standing_grant_for_another_deploy_is_a_sha_mismatch(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], "e" * 40, founder=FID)
    out = get(env)
    assert out["authorizations"]["standing"]["status"] == "SHA_MISMATCH" and out["state"] == "BLOCKED"


@pytest.mark.parametrize("status", ["EXPIRED", "INVALID", "SHA_MISMATCH"])
def test_non_active_standing_grants_have_no_remaining_capacity(env, monkeypatch, status):
    heartbeat(monkeypatch)
    with db(env) as s:
        control.issue_standing(s, [PATH], OLD if status == "SHA_MISMATCH" else SHA, founder=FID,
                               max_per_day=3, days=1,
                               now=NOW - timedelta(days=3) if status == "EXPIRED" else NOW)
        if status == "INVALID":
            s.get(models.ControlState, control.STANDING_AUTHORIZATION).updated_by = "forged"
            s.commit()
    st = get(env)["authorizations"]["standing"]
    assert st["status"] == status and st["used_today"] == 0 and st["remaining_today"] == 0


def test_a_forged_standing_grant_for_another_sha_stays_invalid(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        control.issue_standing(s, [PATH], OLD, founder=FID)
        s.get(models.ControlState, control.STANDING_AUTHORIZATION).updated_by = "forged"
        s.commit()
    assert get(env)["authorizations"]["standing"]["status"] == "INVALID"


def test_a_standing_grant_without_an_api_sha_stays_invalid(env, monkeypatch):
    heartbeat(monkeypatch)
    monkeypatch.delenv("COMPANY_OS_GIT_SHA")
    with db(env) as s:
        control.issue_standing(s, [PATH], SHA, founder=FID)
    st = get(env)["authorizations"]["standing"]
    assert st["status"] == "INVALID" and st["remaining_today"] == 0


@pytest.mark.parametrize("label,kw,state,blocker", [
    ("no heartbeat", {"status": "NO_DATA", "sha": None, "flag": None, "stage": None}, "UNKNOWN", "worker_sha_unknown"),
    ("stale", {"status": "STALE"}, "UNKNOWN", "worker_not_healthy"),
    ("other sha", {"sha": "e" * 40}, "BLOCKED", "sha_mismatch"),
    ("writes off", {"flag": False, "stage": "B"}, "BLOCKED", "write_disabled"),
    ("worker not ready", {"stage": "B"}, "NOT_VERIFIED", "worker_not_ready"),
])
def test_runtime_gates(env, monkeypatch, label, kw, state, blocker):
    heartbeat(monkeypatch, **kw)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
    out = get(env)
    assert out["state"] == state and blocker in out["blockers"], (label, out["state"], out["blockers"])
    assert out["runtime"]["write_enabled"] in (True, False, None)


def test_no_watchdog_preflight_is_not_verified_and_no_acceptance_is_null_not_false(env, monkeypatch):
    heartbeat(monkeypatch)
    out = get(env)
    assert out["runtime"]["preflight"] == {"status": "NOT_VERIFIED", "checked_at": None, "source_sha": None,
                                         "failed": None}
    assert out["runtime"]["acceptance"] == {"source_sha": None, "accepted_at": None, "valid": None}
    assert out["state"] == "BLOCKED" and "host_not_accepted" in out["blockers"]


def test_an_extreme_readiness_timestamp_is_stale(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        row = s.get(models.ControlState, stage_c_worker.RUNTIME_READINESS_KEY)
        row.value = {**row.value, "checked_at": "0001-01-01T00:00:00+23:00"}
        s.commit()
    assert get(env)["runtime"]["preflight"] == {"status": "STALE", "checked_at": None, "source_sha": SHA,
                                                       "failed": None}


@pytest.mark.parametrize("source", ["readiness", "heartbeat"])
def test_runtime_reader_exceptions_are_unverified(env, monkeypatch, source):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)

    def unreadable(*args):
        raise OverflowError("unreadable timestamp")

    if source == "readiness":
        monkeypatch.setattr(stage_c_worker, "runtime_ready", unreadable)
    else:
        monkeypatch.setattr(main.worker, "worker_state", unreadable)
    out = get(env)
    if source == "readiness":
        assert out["runtime"]["preflight"]["status"] == "STALE" and "preflight_stale" in out["blockers"]
    else:
        assert out["state"] == "UNKNOWN" and out["runtime"]["worker_heartbeat"] is None


@pytest.mark.parametrize("key,expected", [
    ("A" * 25 + "-123456", "A" * 25 + "-123456"), ("A" * 26 + "-123456", None), ("TASK-１２３", None)])
def test_task_keys_are_bounded_ascii_identifiers_in_every_view(env, monkeypatch, key, expected):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, _ = published()
        task(s, key=key, history=hist)
        control.authorize_pilot(s, key, SHA, founder=FID)
    out = get(env)
    assert out["tasks"][0]["key"] == out["last_ready_pr"]["task_key"] == expected
    assert out["authorizations"]["one_task"]["task_key"] == expected


# ---------------------------------------------------------------------------------------------- evidence
def test_ci_on_an_outdated_sha_is_never_success(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published(ci_commit=OLD)
        task(s, history=hist, approval=approval)
    (t,) = get(env)["tasks"]
    assert t["ci"] == {"state": "STALE", "sha": OLD} and t["pr"]["head_sha"] == HEAD


def test_ci_from_before_the_latest_publication_does_not_count(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published()
        hist.append(h("IMPLEMENTING", commit=HEAD, ci_state="failed"))       # a repair ...
        hist.append(h("PR_OPEN", commit=OLD, fingerprint=FP, pr="https://github.com/seerxo/acthelm/pull/61"))
        task(s, history=hist)                                                 # ... republished, no CI yet
    (t,) = get(env)["tasks"]
    assert t["ci"] == {"state": None, "sha": None} and t["pr"]["head_sha"] == OLD
    assert t["repairs"] == 1


def test_a_mismatched_review_fingerprint_is_unverified(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published(review_fp=FP2)
        task(s, history=hist, approval=approval)
    (t,) = get(env)["tasks"]
    assert t["reviewer_verdict"] == "UNVERIFIED" and t["fingerprint_match"] is False
    assert (t["tested_fingerprint"], t["reviewed_fingerprint"]) == (FP, FP2)


def test_an_approval_without_its_audit_record_is_unverified(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published()
        t = task(s, history=hist)
        t.context = {**t.context, "improvement": {**t.context["improvement"], "approval": {**approval, "audit_id": 999}}}
        s.commit()
    assert get(env)["tasks"][0]["reviewer_verdict"] == "UNVERIFIED"


def test_a_foreign_pr_url_yields_no_pr_number(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, _ = published(stage="CI")
        hist[5] = h("PR_OPEN", commit=HEAD, fingerprint=FP, pr="https://github.com/attacker/acthelm/pull/61")
        task(s, history=hist)
    pr = get(env)["tasks"][0]["pr"]
    assert pr == {"number": None, "head_sha": HEAD}


def test_missing_evidence_is_null_never_false_or_passed(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        task(s, status=enums.TaskStatus.READY, history=None)                  # a run, no lifecycle at all
    (t,) = get(env)["tasks"]
    for k in ("stage", "repairs", "test_verdict", "reviewer_verdict", "tested_fingerprint", "reviewed_fingerprint",
              "fingerprint_match", "pr", "ci", "failure_category"):
        assert t[k] is None, k


def test_tokens_are_null_when_unrecorded_and_summed_when_recorded(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        task(s, key="TASK-0201", estimated_cost_usd=0)
    m = get(env)["metrics"]
    assert m["tokens"] is None and m["usd_recorded"] == 0 and m["usd_note"] == "excludes_subscription_cost"
    assert m["model_calls"] == {"total": 2} and m["minutes"] == 10.0
    with db(env) as s:
        task(s, key="TASK-0202", input_tokens=100, output_tokens=20)
    assert get(env)["metrics"]["tokens"] == 120


def test_quota_stops_count_only_quota_exhaustion(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        t = task(s)
        for health in ("QUOTA_EXHAUSTED", "DISABLED", "RATE_LIMITED"):
            s.add(models.AgentRun(task_id=t.id, provider="claude", status=enums.AgentRunStatus.FAILED,
                                  tool_calls={"stage_c": True, "outcome": "CODER_FAILED", "capacity_failure": True,
                                              "attempts": [{"provider": "claude", "health": health}]}))
        s.add(models.AgentRun(task_id=t.id, provider="claude", tool_calls={"attempts": [{"health": "QUOTA_EXHAUSTED"}]}))
        s.commit()
    m = get(env)["metrics"]
    assert m["quota_stops"] == 1 and m["attempts"]["total"] == 4      # the non-Stage-C run is not counted


# ---------------------------------------------------------------------------------------------- canary
def test_no_free_text_secret_or_email_reaches_the_response(env, monkeypatch):
    canaries = ["gh" + "p_" + "Q" * 36, "sk-" + "ant-" + "api03-" + "Z" * 40, "AK" + "IA" + "Y" * 16,
                "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----", "eyJ" + "hbGciOi" + "J" * 20, "canary" + "@example.org"]
    blob = " ".join(canaries)
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        row = s.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "paths": [PATH, "app/" + blob]}               # hand-edited: never echoed
        hist, approval = published()
        hist[4]["evidence"].update(checks=blob, findings=[blob], previous_diff=blob, failures=[blob])
        hist.append(h("IMPLEMENTING", review="REJECTED", findings=[blob], fingerprint=blob))
        t = task(s, history=hist, approval={**approval, "findings": [blob]}, status=enums.TaskStatus.BLOCKED,
                 error="RUNTIME_ERROR: " + blob, result=blob, supervisor_notes=blob)
        t.description, t.result, t.objective = blob, blob, blob
        t.context = {**t.context, "stage_c": {"paths": [blob]}, "notes": blob}
        t.artifacts = {"pr": blob}
        r = s.query(models.AgentRun).first()
        r.error, r.tool_calls = blob, {**r.tool_calls, "outcome": blob, "detail": blob}
        task(s, key="x" + canaries[0][:20], status=enums.TaskStatus.READY)  # malformed key
        s.commit()
    body = _founder().get("/stage-c/state", headers=ASSERT).text
    out = json.loads(body)
    for c in canaries + [FOUNDER, "example.invalid", "@"]:
        assert c not in body, c
    assert out["authorizations"]["standing"]["paths"] == [PATH]
    keys = [t["key"] for t in out["tasks"]]
    assert None in keys and "TASK-0101" in keys
    t = next(t for t in out["tasks"] if t["key"] == "TASK-0101")
    assert t["failure_category"] == "OTHER" and t["last_outcome"] == "OTHER"
    from app import stage_c, stage_c_state
    codes = (set(stage_c.STAGES) | set(stage_c_state.STATES) | stage_c_state.WORKER_STATUSES
             | stage_c_state.OUTCOMES | set(stage_c_state.CI_STATES.values())
             | {action for _, action in stage_c_state.NEXT_ACTIONS} | {status.value for status in enums.TaskStatus}
             | {code for _, code in stage_c_state.ERROR_PREFIXES}
             | {"PASS", "FAIL", "RECORDED", "STALE", "PASSED", "FAILED", "UNVERIFIED", "APPROVED", "REJECTED",
                "CONSUMED", "DAILY_LIMIT", "INVALID", "SHA_MISMATCH", "REVOKED", "founder", "operator", "unknown",
                "REVIEW", "sha_mismatch", "worker_not_healthy", "task_needs_founder", "standing_invalid",
                "OTHER", "excludes_subscription_cost", "check_worker"})

    def allowed_strings(value):
        if isinstance(value, dict):
            for child in value.values():
                allowed_strings(child)
        elif isinstance(value, list):
            for child in value:
                allowed_strings(child)
        elif isinstance(value, str):
            if value in codes or value in stage_c_worker.SAFE_FILES or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}|[A-Z]+-\d{1,6}", value):
                return
            assert datetime.fromisoformat(value).astimezone(timezone.utc).isoformat() == value
    allowed_strings(out)


# ---------------------------------------------------------------------------------------------- Desk additions
def test_history_is_stage_and_time_only_and_bounded(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published()
        hist[3]["stage"] = "NOT_A_STAGE"
        hist += [h("IMPLEMENTING", ci_state="failed"), h("TESTING"), h("INDEPENDENT_REVIEW"), h("PR_OPEN"), h("CI")] * 2
        task(s, history=hist, approval=approval)
    t = get(env)["tasks"][0]
    assert len(t["history"]) == 12 and t["history"][-1]["stage"] == "CI"
    assert all(e["stage"] != "NOT_A_STAGE" and e["at"] for e in t["history"])


def test_ci_observed_after_merge_is_its_own_state(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, _ = published(stage="CI")
        hist[-1]["evidence"]["ci_state"] = "observed after merge"
        task(s, history=hist)
    assert get(env)["tasks"][0]["ci"] == {"state": "MERGED_OBSERVED", "sha": HEAD}


def test_last_ready_pr_is_found_outside_the_twenty_task_window(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        hist, approval = published()
        hist.append(h("MERGED", merge_commit=OLD))
        task(s, key="TASK-0900", history=hist, approval=approval)
        for i in range(21):
            task(s, key=f"TASK-{1000 + i}", status=enums.TaskStatus.READY)
    out = get(env)
    assert "TASK-0900" not in [t["key"] for t in out["tasks"]]
    assert out["last_ready_pr"] == {"task_key": "TASK-0900", "number": 61, "head_sha": HEAD, "stage": "MERGED"}


def test_last_ready_pr_is_independent_of_the_run_sample_and_needs_no_run(env, monkeypatch):
    from app import stage_c_state
    heartbeat(monkeypatch)
    monkeypatch.setattr(stage_c_state, "MAX_RUNS", 1)
    with db(env) as s:
        hist, approval = published()
        ready = task(s, key="TASK-0900", history=hist, approval=approval)
        s.query(models.AgentRun).filter_by(task_id=ready.id).delete()
        s.commit()
        task(s, key="TASK-0901")
        task(s, key="TASK-0902")
    out = get(env)
    assert out["metrics"]["truncated"] is True and out["metrics"]["attempts"]["total"] == 1
    assert out["last_ready_pr"]["task_key"] == "TASK-0900"


def test_reading_changes_no_rows_and_calls_no_external_boundary(env, monkeypatch):
    from sqlalchemy import event
    from app import providers, stage_c_ci, stage_c_runner

    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
        hist, approval = published()
        task(s, history=hist, approval=approval)

    def forbidden(*args, **kwargs):
        pytest.fail("the read model reached a write or external boundary")

    for module, name in ((providers.Provider, "run"), (stage_c_ci, "gh_facts"), (stage_c_runner, "_git")):
        monkeypatch.setattr(module, name, forbidden)

    def read_only(connection, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT"), statement.split()[0]

    event.listen(env[0], "before_cursor_execute", read_only)
    try:
        assert get(env)["version"] == 1
        assert get(env)["version"] == 1
    finally:
        event.remove(env[0], "before_cursor_execute", read_only)


def test_daily_usage_uses_utc_even_with_an_offset_clock_and_after_revoke(env, monkeypatch):
    from app import stage_c_state
    heartbeat(monkeypatch)
    clock = datetime(2026, 11, 5, 1, tzinfo=timezone(timedelta(hours=3)))
    utc_day = "2026-11-04"
    with db(env) as s:
        control.issue_standing(s, [PATH], SHA, founder=FID, max_per_day=3, now=clock - timedelta(hours=2))
        row = s.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "used": {"day": utc_day, "count": 2}}
        s.commit()
        out = stage_c_state.build(s, now=clock)
        assert out["generated_at"] == "2026-11-04T22:00:00+00:00"
        assert (out["authorizations"]["standing"]["used_today"], out["authorizations"]["standing"]["remaining_today"]) == (2, 1)
        control.revoke_standing(s, actor=FID)
        st = stage_c_state.build(s, now=clock)["authorizations"]["standing"]
        assert st["revoked"] and st["used_today"] == 2 and st["remaining_today"] == 0


@pytest.mark.parametrize("status", ["PAUSED", "DEGRADED"])
def test_an_unhealthy_worker_cannot_report_ready(env, monkeypatch, status):
    heartbeat(monkeypatch, status=status)
    with db(env) as s:
        green(s)
        control.issue_standing(s, [PATH], SHA, founder=FID)
    out = get(env)
    assert out["state"] == "BLOCKED" and "worker_not_healthy" in out["blockers"]


def test_unknown_runtime_shas_are_explicitly_not_verified(env, monkeypatch):
    heartbeat(monkeypatch, sha="untrusted")
    monkeypatch.delenv("COMPANY_OS_GIT_SHA")
    out = get(env)
    rt = out["runtime"]
    assert rt["api_sha"] is rt["worker_sha"] is rt["reasoner_sha"] is rt["sha_agree"] is None
    assert all(rt["host_facts"][key] == "NOT_VERIFIED" for key in ("api_sha", "worker_sha", "reasoner_sha"))
    assert out["state"] == "UNKNOWN"


def test_malformed_json_never_leaks_or_crashes_the_contract(env, monkeypatch):
    heartbeat(monkeypatch, status={"unsafe": "text"})
    with db(env) as s:
        control.issue_standing(s, [PATH], SHA, founder=FID)
        row = s.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "paths": [["unsafe"], {"unsafe": "text"}, PATH],
                     "used": {"day": NOW.date().isoformat(), "count": -1}}
        hist, approval = published()
        hist.extend([None, {"stage": ["unsafe"]}, {"stage": "TESTING", "evidence": ["unsafe"],
                                               "at": "0001-01-01T00:00:00+23:00"}])
        t = task(s, history=hist[:-3], approval=approval, tool_calls={"stage_c": True, "outcome": {"unsafe": "text"}})
        t.context = {**t.context, "improvement": {**t.context["improvement"], "history": hist, "stage": ["unsafe"]}}
        event = s.get(models.AuditLog, t.context["improvement"]["approval"]["audit_id"])
        event.after = ["unsafe"]
        s.commit()
    out = get(env)
    assert "unsafe" not in json.dumps(out)
    assert out["authorizations"]["standing"]["paths"] == [PATH]
    assert out["authorizations"]["standing"]["used_today"] is None
    assert out["tasks"][0]["history"][-1] == {"stage": "TESTING", "at": None}
    assert out["tasks"][0]["reviewer_verdict"] == "UNVERIFIED"


def test_task_counts_do_not_count_attempts_and_unrecorded_model_splits_are_absent(env, monkeypatch):
    heartbeat(monkeypatch)
    with db(env) as s:
        t = task(s, tool_calls={"stage_c": True})
        s.add(models.AgentRun(task_id=t.id, tool_calls={"stage_c": True}))
        s.commit()
    out = get(env)
    assert out["metrics"]["tasks"]["started"] == 1
    assert out["metrics"]["attempts"]["total"] == out["tasks"][0]["attempts"] == 2
    assert out["metrics"]["tokens"] is None and out["metrics"]["model_calls"] == {"total": None}


@pytest.mark.parametrize("blockers,action", [
    ([], "none"), (["sha_mismatch", "no_authorization"], "redeploy_one_sha"),
    (["preflight_stale", "task_needs_founder"], "run_preflight"), (["standing_expired"], "issue_authorization"),
    (["one_task_consumed", "standing_daily_limit"], "wait_daily_reset")])
def test_next_action_code(blockers, action):
    from app import stage_c_state
    assert stage_c_state._next_action(blockers) == action


# ---------------------------------------------------------------------------------------------- revoke is enforced
# Rows (founder 2026-11-04): 1 valid grant allows; 2 revoke recorded server-side; 3 next task denied with a
# grant-specific reason; 4 a fresh session ("worker restart") still denies; 5 the old grant written back is refused;
# 6 an attempt already running when the revoke lands (current behaviour, pinned below, not changed here).
GRANTS = {
    "standing": (control.STANDING_AUTHORIZATION, "/stage-c/standing/revoke", "stage-c.standing.revoke",
                 lambda s: control.issue_standing(s, [PATH], SHA, founder=FID, max_per_day=2),
                 lambda s, t: control._standing_denial(s, s.get(models.ControlState, control.STANDING_AUTHORIZATION),
                                                       [PATH], SHA, datetime.now(timezone.utc)),
                 "stage_c_standing_missing", "stage_c_standing_invalid"),
    "one_task": (control.PILOT_AUTHORIZATION, "/stage-c/authorization/revoke", "stage-c.authorization.revoke",
                 lambda s: control.authorize_pilot(s, "TASK-0301", SHA, founder=FID),
                 lambda s, t: control.pilot_authorization_denial(s, t.key, SHA),
                 "stage_c_authorization_missing", "stage_c_authorization_invalid"),
}


def scoped_task(s, key="TASK-0301"):
    t = models.Task(key=key, title="t", success_criteria="s", status=enums.TaskStatus.READY,
                    context={"stage_c": {"paths": [PATH]}})
    s.add(t)
    s.commit()
    return t.id


@pytest.mark.parametrize("grant", list(GRANTS))
def test_a_revoke_is_enforced_server_side(env, monkeypatch, grant):
    key, route, revoke_action, issue, own_denial, missing, invalid = GRANTS[grant]
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        task_id = scoped_task(s)
        value = dict(issue(s))
        t = s.get(models.Task, task_id)
        assert own_denial(s, t) is None and control.write_authorization_denial(s, t, SHA) is None   # row 1
    assert _founder().post(route, json={"reason": "stop"}, headers=ASSERT).json() == {"revoked": True}
    with db(env) as s:                                                                      # row 4: a fresh session
        t = s.get(models.Task, scoped_task(s, "TASK-0302"))  # a new task after the revoke
        assert s.get(models.ControlState, key) is None                                       # row 2: row removed ...
        rows = [a for a in s.query(models.AuditLog).filter_by(entity_key=key, action=revoke_action)]
        assert len(rows) == 1 and rows[0].actor == FID and rows[0].before["issued_at"] == value["issued_at"]  # ... audited
        assert own_denial(s, t) == missing                                                   # row 3: grant-specific
        assert control.write_authorization_denial(s, t, SHA) is not None
        assert control.consume_write_authorization(s, t, SHA, 41) is not None
        s.rollback()
    with db(env) as s:  # a second fresh session still observes the revoke
        assert s.get(models.ControlState, key) is None
        t = s.get(models.Task, task_id)
        assert own_denial(s, t) == missing
        # row 5: replaying the old value without its issue audit cannot restore authority.
        for entry in s.query(models.AuditLog).filter_by(entity_key=key, action=revoke_action.replace(".revoke", ".issue")):
            s.delete(entry)
        s.add(models.ControlState(key=key, value=value, updated_by=FID))
        s.commit()
        assert own_denial(s, t) == invalid
        assert control.write_authorization_denial(s, t, SHA) is not None
        assert control.consume_write_authorization(s, t, SHA, 42) is not None
        s.rollback()
    st = get(env)["authorizations"][grant]
    assert st["status"] == "INVALID"
    with db(env) as s:                                                                      # only a new issue restores
        s.delete(s.get(models.ControlState, key))
        s.commit()
        issue(s)
        assert own_denial(s, s.get(models.Task, task_id)) is None


@pytest.mark.parametrize("grant", list(GRANTS))
def test_the_state_view_reads_a_revoked_grant_as_revoked(env, monkeypatch, grant):
    heartbeat(monkeypatch)
    with db(env) as s:
        green(s)
        scoped_task(s)
        GRANTS[grant][3](s)
    assert _founder().post(GRANTS[grant][1], json={"reason": "stop"}, headers=ASSERT).json() == {"revoked": True}
    out = get(env)
    st = out["authorizations"][grant]
    assert st["status"] == "REVOKED" and f"{grant}_revoked" in out["blockers"] and out["state"] == "BLOCKED"
    if grant == "standing":
        assert st["revoked"] is True and st["remaining_today"] == 0


@pytest.mark.parametrize("grant", list(GRANTS))
def test_an_attempt_already_running_is_not_stopped_by_a_revoke_current_behaviour(monkeypatch, tmp_path, grant):
    """Row 6, CURRENT behaviour (documented, not changed in this PR). The authorization is checked at the claim
    (worker._eligible -> write_authorization_denial) and spent once when the attempt starts
    (stage_c_worker.execute -> consume_write_authorization). The pre-publish re-check in execute() is
    _admission_gate + ready() + agents_paused(), none of which reads the authorization, so a revoke during a running
    attempt does not stop its push or PR; the next claim (a new task or a CI repair, which re-enters READY) is denied.
    The kill switch (agents_paused) is what stops a running attempt before publication."""
    from app import stage_c_runner, worker
    from tests.test_stage_c_worker import SHA_A, armed
    from tests.test_worker import SPECS

    s, t, ps = armed(monkeypatch, tmp_path)
    if grant == "standing":
        control.issue_standing(s, [PATH], SHA_A, founder=FID)
    else:
        control.authorize_pilot(s, t.key, SHA_A, founder=FID)
    monkeypatch.setenv("STAGE_C_JOBS_ROOT", str(tmp_path))
    monkeypatch.setattr(stage_c_worker, "ready", lambda db: None)
    monkeypatch.setattr(stage_c_worker, "publication_role", lambda: ("PASS", "test"))
    monkeypatch.setattr(stage_c_runner, "_git", lambda *a, **kw: SHA_A)
    monkeypatch.setattr(stage_c_runner, "run", lambda *a, **kw: {
        "outcome": "READY_FOR_REVIEW", "diff": b"checked", "worktree": str(tmp_path)})
    monkeypatch.setattr(stage_c_runner, "approved_review", lambda *a: False)

    def review(*args, **kwargs):
        assert t.status == enums.TaskStatus.IN_PROGRESS
        if grant == "standing":
            assert s.get(models.ControlState, control.STANDING_AUTHORIZATION).value["used"]["count"] == 1
            assert control.revoke_standing(s, actor=FID)
        else:
            assert s.get(models.ControlState, control.PILOT_AUTHORIZATION).value["consumed_at"]
            assert control.revoke_pilot_authorization(s, actor=FID)
        return {"outcome": "APPROVED"}

    published = []
    def publish(*args, **kwargs):
        kwargs["before_publish"]()  # exercise the real final admission check, with no Git or network
        published.append(t.key)
        return {"outcome": "PR_OPEN", "pr": "https://github.com/seerxo/acthelm/pull/1", "commit": SHA_A}

    monkeypatch.setattr(stage_c_runner, "review", review)
    monkeypatch.setattr(stage_c_runner, "publish", publish)
    out = worker.run_once(s, ps, SPECS)
    assert out["verdict"] == "PR_OPEN" and published == [t.key]
    with Session(s.get_bind()) as fresh:
        assert fresh.get(models.ControlState, GRANTS[grant][0]) is None
    t.status = enums.TaskStatus.READY
    s.commit()
    assert worker._eligible(s, t, SPECS)[0] == "stage_c_authorization_missing"
