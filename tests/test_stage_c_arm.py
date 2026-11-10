"""Founder Start/Stop Pilot arms the Stage C worker only while the one-task authorization exists.
Break caught: the code-write flag stays on without live founder authority, or a read error leaves it on."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from app import control, enums, models, stage_c, stage_c_arm, stage_c_worker
from tests.test_stage_c_worker import FOUNDER_ID, SHA_A, SHA_B, fully_accepted, runtime_report, write_task

ROOT = Path(__file__).resolve().parents[1]
LEASE = stage_c.lease_key(stage_c_worker.REPOSITORY)


def _consume(db, task, *, started=True, lease=False):
    task.status = enums.TaskStatus.IN_PROGRESS
    run = models.AgentRun(task_id=task.id, status=enums.AgentRunStatus.STARTED if started else
                          enums.AgentRunStatus.FAILED)
    db.add(run)
    db.flush()
    assert control.consume_pilot_authorization(db, task.key, SHA_A, run.id) is None
    if lease:
        stage_c.acquire(db, stage_c_worker.REPOSITORY, f"stage-c-runner:{task.key}", timedelta(minutes=30))
    db.commit()


def _setup(db, task, case):
    now = datetime.now(timezone.utc)
    if case in ("ok", "pause", "acc", "stale", "run", "lease", "idle", "rev_run"):
        control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID)
    if case == "rev":
        control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID)
        control.revoke_pilot_authorization(db, actor=FOUNDER_ID)
    if case == "exp":
        control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID, now=now - timedelta(hours=1))
    if case == "sha":
        control.authorize_pilot(db, task.key, SHA_B, founder=FOUNDER_ID)
    if case == "pause":
        control.set_agents_paused(db, True, actor="operator:test", reason="test")
    if case == "acc":
        db.delete(db.get(models.ControlState, control.ACCEPTANCE))
        db.commit()
    if case == "stale":
        runtime_report(db, SHA_A, age=timedelta(minutes=20))
    if case in ("run", "idle"):
        _consume(db, task, started=case == "run")
    if case in ("lease", "rev_run"):
        _consume(db, task, started=False, lease=True)
    if case == "rev_run":
        control.revoke_pilot_authorization(db, actor=FOUNDER_ID)


CASES = [
    ("ok", ("ARM", "TASK-0100")),
    ("none", ("DISARM", "stage_c_authorization_missing")),
    ("rev", ("DISARM", "stage_c_authorization_missing")),
    ("exp", ("DISARM", "stage_c_authorization_expired")),
    ("sha", ("DISARM", "stage_c_authorization_invalid")),
    ("pause", ("DISARM", "paused")),
    ("acc", ("DISARM", "not_accepted")),
    ("stale", ("DISARM", "not_ready")),
    ("run", ("ARM", "TASK-0100")),
    ("lease", ("ARM", "TASK-0100")),
    ("idle", ("DISARM", "stage_c_authorization_consumed")),
    ("rev_run", ("DISARM", "stage_c_authorization_missing")),
]


@pytest.mark.parametrize("case,want", CASES, ids=[c for c, _ in CASES])
def test_decide(monkeypatch, tmp_path, case, want):
    db = fully_accepted(monkeypatch, tmp_path, SHA_A)
    runtime_report(db, SHA_A)
    task = write_task(db)
    _setup(db, task, case)
    had_lease = db.get(models.ControlState, LEASE) is not None
    assert stage_c_arm.decide(db, SHA_A) == want
    # Read-only: nothing added or changed, and no lease row created where there was none.
    assert not db.new and not db.dirty and (db.get(models.ControlState, LEASE) is not None) == had_lease


def test_decide_errors(monkeypatch, tmp_path):
    db = fully_accepted(monkeypatch, tmp_path, SHA_A)
    runtime_report(db, SHA_A)
    control.authorize_pilot(db, write_task(db).key, SHA_A, founder=FOUNDER_ID)
    assert stage_c_arm.decide(object(), SHA_A) == ("DISARM", "read_error")      # unreadable database
    monkeypatch.setenv("COMPANY_OS_GIT_SHA", SHA_B)
    assert stage_c_arm.decide(db, SHA_A) == ("DISARM", "api_sha_mismatch")


# ------------------------------------------------------------------------------- the root script, fake docker
OVERRIDE_JSON = '{"services": {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true"}}}}\n'
IMAGES = {f"COMPANY_OS_IMAGE_{n}": "sha256:" + c * 64 for n, c in zip(("API", "WORKER", "REASONER", "EGRESS"), "1234")}
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  "exec -i -e PGOPTIONS"*) cat > "$FAKE_SNIPPET"; echo "$FAKE_DECISION" ;;
  "exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE")
    [ "$FAKE_ERROR" != flag ] && [ "$FAKE_ERROR" != stopped ] || exit 1
    cat "$FAKE_STATE" ;;
  "exec company-api printenv CODER_PAUSE_FENCE_ENABLED") echo true ;;
  inspect*) [ "$FAKE_ERROR" != inspect ] || exit 1; [ "$FAKE_ERROR" = stopped ] && echo false || echo true ;;
  compose*) [ "$FAKE_COMPOSE_FAIL" != 1 ] || exit 1
    env | grep '^COMPANY_OS_' | sort >> "$FAKE_LOG"
    case "$*" in *stage-c-pilot*) echo true > "$FAKE_STATE" ;; *) echo false > "$FAKE_STATE" ;; esac ;;
esac
"""


def _run(tmp_path, decision, armed, *args, error="", compose_fail=False):
    bin_, host, root = tmp_path / "bin", tmp_path / "opt", tmp_path / "root"
    for d in (bin_, host, root):
        d.mkdir(exist_ok=True)
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "logger").write_text('#!/bin/sh\necho "logger $*" >> "$FAKE_LOG"\n')
    for f in ("docker", "logger"):
        (bin_ / f).chmod(0o755)
    (host / ".pilot-source-sha").write_text(SHA_A + "\n")
    (host / "pilot-images.env").write_text("".join(f"{k}={v}\n" for k, v in IMAGES.items()))
    (tmp_path / "state").write_text(armed + "\n")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "STAGE_C_ARM_DIR": str(host),
           "STAGE_C_ARM_OVERRIDE": str(root / "stage-c-pilot.compose.yml"), "FAKE_LOG": str(tmp_path / "log"),
           "FAKE_STATE": str(tmp_path / "state"), "FAKE_DECISION": decision,
           "FAKE_ERROR": error, "FAKE_COMPOSE_FAIL": str(int(compose_fail)),
           "FAKE_SNIPPET": str(tmp_path / "snippet.py")}
    out = subprocess.run(["bash", str(ROOT / "scripts/stage-c-arm"), *args], env=env, capture_output=True,
                         text=True, timeout=30)
    log = (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    return out, log, root / "stage-c-pilot.compose.yml"


def test_sh_dry(tmp_path):
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false", "--dry-run")
    assert out.returncode == 0 and "decision: ARM TASK-0100" in out.stdout
    assert "compose" not in log and "logger" not in log and not ovr.exists()
    compile((tmp_path / "snippet.py").read_text(), "snippet", "exec")      # the in-container snippet parses


def test_sh_arm(tmp_path):
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0, out.stderr
    assert ovr.read_text() == OVERRIDE_JSON and json.loads(ovr.read_text())
    assert os.stat(ovr).st_mode & 0o777 == 0o600
    assert (f"docker compose -p company-os --env-file .env -f docker-compose.yml -f {ovr} "
            "up -d --no-deps --no-build --pull never company-worker") in log
    assert all(f"{k}={v}" in log for k, v in IMAGES.items()) and f"COMPANY_OS_GIT_SHA={SHA_A}" in log
    assert "action=ARM reason=authorized task=TASK-0100" in log
    out, log2, _ = _run(tmp_path, "ARM TASK-0100", "true")                # already armed: nothing more
    assert log2.count("compose") == log.count("compose")


@pytest.mark.parametrize("error", ["inspect", "flag"], ids=["i", "f"])
@pytest.mark.parametrize("arm", [False, True], ids=["d", "a"])
@pytest.mark.parametrize("fail", [False, True], ids=["ok", "stop"])
@pytest.mark.parametrize("armed", ["true", "false"], ids=["on", "off"])
def test_sh_unknown(tmp_path, error, arm, fail, armed):
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    decision = "ARM TASK-0100" if arm else "DISARM stage_c_authorization_missing"
    out, log, ovr = _run(tmp_path, decision, armed, error=error, compose_fail=fail)
    assert out.returncode == (1 if arm else 0), out.stderr
    assert not ovr.exists()
    assert ("docker compose -p company-os --env-file .env -f docker-compose.yml "
            "up -d --no-deps --no-build --pull never company-worker") in log
    assert ("docker stop company-worker" in log) == fail
    assert f"action={'DISARM_STOP' if fail else 'DISARM'} " in log


@pytest.mark.parametrize("decision", ["DISARM stage_c_authorization_missing", "junk"], ids=["auth", "junk"])
def test_sh_disarm(tmp_path, decision):
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    out, log, ovr = _run(tmp_path, decision, "true")
    assert out.returncode == 0, out.stderr
    assert not ovr.exists()
    assert ("docker compose -p company-os --env-file .env -f docker-compose.yml "
            "up -d --no-deps --no-build --pull never company-worker") in log
    reason = "stage_c_authorization_missing" if decision != "junk" else "read_error"
    assert f"action=DISARM reason={reason} task=-" in log
    out, log2, _ = _run(tmp_path, decision, "false")                        # already off: nothing more
    assert log2.count("compose") == log.count("compose")


def test_sh_stopped(tmp_path):
    """A verified-stopped worker is off (printenv fails on it): DISARM leaves it stopped, never restarts it."""
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    out, log, ovr = _run(tmp_path, "DISARM stage_c_authorization_missing", "true", error="stopped")
    assert out.returncode == 0, out.stderr
    assert not ovr.exists() and "compose" not in log and "docker stop" not in log
