"""Stage C standing authorization: the founder approves named files for a few days and runs a day, once.
It widens the authority in time only. Break caught: any form of it that a non-founder can issue, that outlives a
deploy, covers a file nobody named, exceeds its daily cap, or turns on code writing by itself."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import control, models, stage_c_worker, worker
from tests.test_coder_launch import CFG, FakeProcess, L  # noqa: F401 (fixture)
from tests.test_founder_authorization import (ASSERT, CALLERS, FID, SHA, _c, _founder, env)  # noqa: F401 (fixture)
from tests.test_stage_c_worker import (_same_repository_publishing, FOUNDER_ID, SHA_A, SHA_B, armed, assert_untouched, authorization, tick)
from tests.test_worker import SPECS

PATH = "tests/test_metrics.py"          # the file write_task() scopes its task to
STANDING = control.STANDING_AUTHORIZATION


def standing(db):
    return db.get(models.ControlState, STANDING)


def audit_actions(db):
    return [a.action for a in db.scalars(select(models.AuditLog).order_by(models.AuditLog.id))]


# ------------------------------------------------------------------------------------------- using it
def test_a_standing_authorization_runs_a_task_inside_its_scope_and_counts_the_run(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=2)
    assert control.write_authorization_denial(db, task, SHA_A) is None
    out = worker.run_once(db, ps, SPECS)
    assert out["task"] == task.key and out["run_id"]                      # claimed through normal admission
    used = standing(db).value["used"]
    assert used["count"] == 1 and used["day"] == datetime.now(timezone.utc).date().isoformat()
    assert "stage-c.standing.consume" in audit_actions(db)
    assert authorization(db) is None                                       # no one-task row was created or spent


def test_the_daily_cap_stops_further_runs_and_resets_the_next_day(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 1, now) is None
    db.commit()
    assert control.consume_write_authorization(db, task, SHA_A, 2, now) == "stage_c_standing_daily_limit"
    assert control.write_authorization_denial(db, task, SHA_A, now) == "stage_c_standing_daily_limit"
    assert standing(db).value["used"]["count"] == 1                        # the refused attempt spent nothing
    tomorrow = now + timedelta(days=1)
    assert control.consume_write_authorization(db, task, SHA_A, 3, tomorrow) is None
    assert standing(db).value["used"] == {"day": tomorrow.date().isoformat(), "count": 1}


def test_a_one_task_authorization_is_spent_first_and_leaves_the_standing_count_alone(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID)
    assert control.consume_write_authorization(db, task, SHA_A, 7) is None
    assert authorization(db).value["consumed_at"] and standing(db).value["used"]["count"] == 0


@pytest.mark.parametrize("label", ["other file", "no paths", "wrong sha", "expired", "revoked", "no row"])
def test_anything_outside_the_grant_is_denied_and_spends_nothing(monkeypatch, tmp_path, label):
    db, task, ps = armed(monkeypatch, tmp_path)
    issued = datetime.now(timezone.utc)
    if label == "expired":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, days=1, now=issued - timedelta(days=3))
    elif label == "other file":
        control.issue_standing(db, ["tests/test_goals.py"], SHA_A, founder=FOUNDER_ID)
    elif label == "no paths":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
        task.context = {**task.context, "stage_c": {}}
        db.commit()
    elif label == "wrong sha":
        control.issue_standing(db, [PATH], SHA_B, founder=FOUNDER_ID)
    elif label == "revoked":
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
        assert control.revoke_standing(db, actor=FOUNDER_ID)
    out, skipped = tick(db, ps)
    assert skipped and skipped <= {"stage_c_standing_scope", "stage_c_standing_invalid", "stage_c_standing_expired",
                                   "stage_c_authorization_missing", "stage_c_scope_refused"}, (label, skipped)
    assert_untouched(db, task, out)
    row = standing(db)
    assert row is None or row.value["used"]["count"] == 0



@pytest.mark.parametrize("value", [None, [], "yes", {}, {"paths": [PATH]},
                                   {"repository": "seerxo/acthelm", "source_sha": SHA_A, "issued_by": "founder",
                                    "action": "stage_c.code_write.standing", "paths": [PATH], "max_per_day": 99,
                                    "issued_at": "yesterday", "expires_at": "tomorrow"}])
def test_a_malformed_or_hand_written_row_is_denied(monkeypatch, tmp_path, value):
    db, task, ps = armed(monkeypatch, tmp_path)
    db.add(models.ControlState(key=STANDING, value=value, updated_by="founder")); db.commit()
    out, skipped = tick(db, ps)
    assert skipped == {"stage_c_standing_invalid"}
    assert_untouched(db, task, out)


def test_a_row_written_without_the_issue_audit_is_denied(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    value = control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    for a in db.scalars(select(models.AuditLog).where(models.AuditLog.action == "stage-c.standing.issue")):
        db.delete(a)
    db.commit()
    assert control.write_authorization_denial(db, task, SHA_A) == "stage_c_standing_invalid", value


def test_the_global_flag_and_host_acceptance_still_decide(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", False)
    out, skipped = tick(db, ps)
    assert skipped == {"code_write_disabled"} and standing(db).value["used"]["count"] == 0
    assert_untouched(db, task, out)


# ------------------------------------------------------------------------------------------- issuing it
@pytest.mark.parametrize("who", ["founder", "company-worker", "operator:local", "service:admin", "anonymous", "", None,
                                 "founder:access:", "founder:access:UPPER@x.io"])
def test_no_non_founder_string_can_issue_it(monkeypatch, tmp_path, who):
    db, task, ps = armed(monkeypatch, tmp_path)
    with pytest.raises(PermissionError):
        control.issue_standing(db, [PATH], SHA_A, founder=who)
    assert standing(db) is None and "stage-c.standing.issue" not in audit_actions(db)


@pytest.mark.parametrize("kwargs", [
    {"paths": ["app/main.py"]}, {"paths": ["app/api_auth.py"]}, {"paths": ["docker-compose.yml"]},
    {"paths": ["../escape.py"]}, {"paths": []}, {"paths": [PATH, PATH]}, {"paths": "tests/test_metrics.py"},
    {"days": 8}, {"days": 0}, {"max_per_day": 4}, {"max_per_day": 0}, {"max_per_day": True}, {"sha": "short"}])
def test_it_cannot_name_a_protected_file_or_exceed_the_bounds(monkeypatch, tmp_path, kwargs):
    db, task, ps = armed(monkeypatch, tmp_path)
    args = {"paths": [PATH], "sha": SHA_A, "days": 3, "max_per_day": 1, **kwargs}
    with pytest.raises(ValueError):
        control.issue_standing(db, args["paths"], args["sha"], founder=FOUNDER_ID, days=args["days"],
                               max_per_day=args["max_per_day"])
    assert standing(db) is None


def test_issuing_is_audited_and_revoking_removes_it(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID)
    assert audit_actions(db)[-1] == "stage-c.standing.issue"
    for who in ("founder", "anonymous", None):
        with pytest.raises(PermissionError):
            control.revoke_standing(db, actor=who)
    assert standing(db) is not None
    assert control.revoke_standing(db, actor="operator:host-root") is True
    assert standing(db) is None and audit_actions(db)[-1] == "stage-c.standing.revoke"
    assert control.revoke_standing(db, actor=FOUNDER_ID) is False


def test_the_standing_grant_is_never_longer_than_a_week(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, days=7)
    row = standing(db)
    row.value = {**row.value, "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}; db.commit()
    assert control.write_authorization_denial(db, task, SHA_A) == "stage_c_standing_invalid"


# ------------------------------------------------------------------------------------------- the route
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_only_the_verified_founder_can_issue_it_over_http(env, label, headers, host):   # noqa: F811
    engine, _ = env
    r = _c(host).post("/stage-c/standing", json={"paths": [PATH], "days": 3, "max_per_day": 1}, headers=headers)
    assert r.status_code in (401, 403), label
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        assert standing(s) is None


def test_the_founder_issues_it_bound_to_the_deployed_sha_and_revokes_it(env):   # noqa: F811
    engine, _ = env
    r = _founder().post("/stage-c/standing", json={"paths": [PATH], "days": 2, "max_per_day": 2, "source_sha": "e" * 40},
                        headers=ASSERT)
    assert r.status_code == 201 and r.json()["source_sha"] == SHA and r.json()["issued_by"] == FID
    assert r.json()["paths"] == [PATH] and r.json()["max_per_day"] == 2
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        assert standing(s).updated_by == FID
    assert _founder().post("/stage-c/standing", json={"paths": ["app/main.py"]}, headers=ASSERT).status_code == 422
    assert _founder().post("/stage-c/standing", json={"paths": [PATH], "days": 9}, headers=ASSERT).status_code == 422
    assert _founder().post("/stage-c/standing/revoke", json={"reason": "done"}, headers=ASSERT).json() == {"revoked": True}


def test_standing_routes_are_enforce_always_and_founder_scoped():
    from app import api_auth
    for probe in ("/stage-c/standing", "/stage-c/standing/revoke"):
        assert api_auth._match(api_auth.ENFORCE_ALWAYS, "POST", probe) and api_auth._match(api_auth.FOUNDER_SCOPE, "POST", probe)


# ------------------------------------------------------------------------- provider capacity failures
def test_a_capacity_failure_gives_the_standing_run_back_exactly_once(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 7, now) is None
    db.commit()
    # Break caught: a Codex quota outage burns the day's only run twice (seen on the host, TASK-0055 runs 33 and 34).
    assert control.refund_standing_run(db, task, 7, now) is True
    db.commit()
    assert standing(db).value["used"]["count"] == 0
    assert control.refund_standing_run(db, task, 7, now) is False          # not refundable twice
    assert control.refund_standing_run(db, task, 8, now) is False          # never consumed
    assert standing(db).value["used"]["count"] == 0
    assert control.consume_write_authorization(db, task, SHA_A, 9, now) is None      # the run is usable again
    db.commit()
    assert control.consume_write_authorization(db, task, SHA_A, 10, now) == "stage_c_standing_daily_limit"


def test_the_cap_still_holds_and_a_rolled_over_day_refunds_nothing(monkeypatch, tmp_path):
    db, task, ps = armed(monkeypatch, tmp_path)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    now = datetime.now(timezone.utc)
    assert control.consume_write_authorization(db, task, SHA_A, 1, now) is None
    db.commit()
    assert control.refund_standing_run(db, task, 1, now + timedelta(days=1)) is False
    assert standing(db).value["used"]["count"] == 1


def _spy_refunds(monkeypatch):
    refunds, real = [], control.refund_standing_run
    monkeypatch.setattr(control, "refund_standing_run", lambda *a, **k: refunds.append(a[1].key) or real(*a, **k))
    return refunds


def test_the_stage_c_worker_refunds_a_capacity_failure(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    from app import stage_c_runner
    pipeline(monkeypatch, tmp_path)
    refunds = _spy_refunds(monkeypatch)
    def quota(*a, **k):
        raise stage_c_runner.CoderFailed("QUOTA_EXHAUSTED")
    monkeypatch.setattr(stage_c_runner, "launcher_coder", lambda **kw: quota)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == "CODER_FAILED" and refunds == [task.key]      # Break caught: the call site is missing


def test_the_stage_c_worker_does_not_refund_a_completed_run(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    pipeline(monkeypatch, tmp_path)
    refunds = _spy_refunds(monkeypatch)
    db = _db()
    write_task(db)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == "PR_OPEN" and refunds == []


def _cli_failures(L, monkeypatch, task, real_coder, outputs):
    """Drive the real launcher and adapter with bounded CLI streams, without Docker."""
    outputs, results = iter(outputs), []

    def host_start(_):
        real_popen = L.subprocess.Popen
        def popen(argv, **kw):
            if argv[0] != "docker":
                return real_popen(argv, **kw)
            stdout, stderr = next(outputs)
            mount = next(a for a in argv if a.endswith("target=/run/company-coder"))
            ack = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
            proc = FakeProcess(stdout, read=lambda: (ack / "started").touch())
            proc.returncode = 1
            proc.stderr = io.BytesIO(stderr)
            return proc
        with monkeypatch.context() as patch:
            patch.setattr(L.subprocess, "Popen", popen)
            result = L.handle(task.key, CFG)
        results.append(json.loads((L.JOBS / task.key / "done/result.json").read_text()))
        assert result == results[-1]

    def launcher_coder(**kw):
        kw.update(poll_s=0, sleep=host_start)
        return real_coder(**kw)
    monkeypatch.setattr(stage_c_worker.stage_c_runner, "launcher_coder", launcher_coder)
    return results


@pytest.mark.parametrize("message,channel,code,refunded", [
    ("You've hit your usage limit", "stdout", "QUOTA_EXHAUSTED", True),
    ("You've hit your limit", "stdout", "QUOTA_EXHAUSTED", True),
    ("subscription usage limit reached", "stderr", "QUOTA_EXHAUSTED", True),
    ("HTTP 429: too many requests", "stderr", "RATE_LIMITED", True),
    ("Not logged in: please authenticate", "stderr", "AUTH_REQUIRED", True),
    ("unexpected coder failure", "stdout", "CODER_ERROR", False),
    ({"duration_ms": 429}, "stdout", "CODER_ERROR", False),
    ({"total_cost_usd": 0.401}, "stdout", "CODER_ERROR", False),
    ({"is_error": False, "result": "I updated the login form..."}, "stdout", "CODER_ERROR", False),
    ("catalog in use", "stderr", "CODER_ERROR", False),
    ("insufficient context", "stderr", "CODER_ERROR", False),
])
def test_coder_cli_failure_reaches_standing_refund_without_raw_text(L, monkeypatch, tmp_path,
                                                                  message, channel, code, refunded):
    from app import enums, stage_c_runner
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    real_coder = stage_c_runner.launcher_coder
    pipeline(monkeypatch, tmp_path, mock_authorization=False)
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", SHA_A)
    db = _db()
    task = write_task(db)
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=2)
    # Preserve a previous spent run: a refund must restore the original count, not clear the day.
    assert control.consume_write_authorization(db, task, SHA_A, 999) is None
    db.commit()
    canary = "RAW_CODER_CANARY_7ac452"
    old_summary = "last line has no capacity hint"
    text = f"{message if isinstance(message, str) else 'unexpected failure'}\n{canary}\n{old_summary} {canary}"
    envelope = {"type": "result", "subtype": "success", "is_error": True, "result": text}
    if isinstance(message, dict):
        envelope.update(message)
    envelope["errors"] = [text]
    # Compact JSON keeps the canary on stdout's last line; stderr also ends with it.
    stdout = json.dumps(envelope).encode() if channel == "stdout" else b"unhelpful stdout\n"
    stderr = text.encode() if channel == "stderr" else b""
    results = _cli_failures(L, monkeypatch, task, real_coder, [(stdout, stderr)])
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    db.expire_all()
    run = db.get(models.AgentRun, out["run_id"])
    assert out["verdict"] == "CODER_FAILED" and out["model_calls"] == 1
    assert run.tool_calls["failure_class"] == code
    assert results[0]["failure_class"] == code
    assert standing(db).value["used"]["count"] == (1 if refunded else 2)
    assert task.retry_count == (0 if refunded else 1)
    assert ("stage-c.standing.refund" in audit_actions(db)) is refunded
    cooldown = worker._cooldowns(db).get("claude")
    if refunded:
        assert task.status == enums.TaskStatus.READY and worker._capacity_only(run)
        assert cooldown["health"] == code
        assert datetime.fromisoformat(cooldown["until"]) > datetime.now(timezone.utc)
    else:
        assert not cooldown and not worker._capacity_only(run)
    persisted = [dict(row) for table in models.Base.metadata.sorted_tables
                 for row in db.execute(table.select()).mappings()]
    for value in [results, out, *persisted]:
        saved = json.dumps(value, default=str)
        assert canary not in saved
        assert old_summary not in saved


def test_launcher_refund_cooldown_retry_and_daily_cap(L, monkeypatch, tmp_path):
    from app import enums, stage_c_runner
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    real_coder = stage_c_runner.launcher_coder
    pipeline(monkeypatch, tmp_path, mock_authorization=False)
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", SHA_A)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(worker, "_now", lambda: now)
    db = _db()
    task = write_task(db)
    ps = {n: Reviewer(n) for n in ("claude", "codex")}
    control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, max_per_day=1)
    results = _cli_failures(L, monkeypatch, task, real_coder, [
        (b'{"is_error":true,"subtype":"success","result":"usage limit reached"}', b""),
        (b'{"is_error":false,"result":"I updated the login form"}', b""),
    ])
    first = worker.run_once(db, ps, SPECS)
    assert first["verdict"] == "CODER_FAILED" and results[0]["failure_class"] == "QUOTA_EXHAUSTED"
    assert standing(db).value["used"]["count"] == 0
    assert control.refund_standing_run(db, task, first["run_id"]) is False
    blocked = worker.run_once(db, ps, SPECS)
    assert blocked["model_calls"] == 0 and db.query(models.AgentRun).count() == 1
    assert task.status == enums.TaskStatus.READY and len(results) == 1

    now = datetime.fromisoformat(worker._cooldowns(db)["claude"]["until"]) + timedelta(seconds=1)
    second = worker.run_once(db, ps, SPECS)
    assert second["verdict"] == "CODER_FAILED" and second["run_id"] != first["run_id"]
    assert results[1]["failure_class"] == "CODER_ERROR" and len(results) == 2
    assert standing(db).value["used"]["count"] == 1 and task.retry_count == 1
    assert control.refund_standing_run(db, task, first["run_id"]) is False
    assert audit_actions(db).count("stage-c.standing.refund") == 1
    assert audit_actions(db).count("stage-c.standing.consume") == 2
    out, skipped = tick(db, ps)
    assert skipped == {"stage_c_standing_daily_limit"} and out["model_calls"] == 0
    assert db.query(models.AgentRun).count() == 2 and len(results) == 2
    assert standing(db).value["used"]["count"] == 1


def test_denied_authorization_with_capacity_output_never_claims_or_refunds(L, monkeypatch, tmp_path):
    from app import stage_c_runner
    from tests.test_stage_c_worker import Reviewer, _db, pipeline, write_task
    real_coder = stage_c_runner.launcher_coder
    pipeline(monkeypatch, tmp_path, mock_authorization=False)
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", SHA_A)
    db = _db()
    task = write_task(db)
    control.issue_standing(db, [PATH], SHA_B, founder=FOUNDER_ID)
    results = _cli_failures(L, monkeypatch, task, real_coder, [(b"", b"usage limit reached")])
    refunds = _spy_refunds(monkeypatch)
    out, skipped = tick(db, {n: Reviewer(n) for n in ("claude", "codex")})
    assert skipped == {"stage_c_standing_invalid"}
    assert_untouched(db, task, out)
    assert results == [] and refunds == [] and standing(db).value["used"]["count"] == 0
    assert "stage-c.standing.consume" not in audit_actions(db)
    assert "stage-c.standing.refund" not in audit_actions(db)
