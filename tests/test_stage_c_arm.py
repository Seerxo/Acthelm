"""Founder Start/Stop Pilot arms the Stage C worker only while the one-task authorization exists.
Break caught: the code-write flag stays on without live founder authority, or a read error leaves it on."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import time

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
    if case in ("ok", "pause", "acc", "stale", "run", "lease", "idle", "rev_run", "old_run"):
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
    if case in ("run", "idle", "old_run"):
        _consume(db, task, started=case != "idle")
    if case == "old_run":   # an interrupted run never recovered: STARTED, no lease, older than the consume gap
        run = db.query(models.AgentRun).filter_by(task_id=task.id).one()
        run.started_at = now - stage_c_arm.CONSUME_GAP - timedelta(minutes=1)
        db.commit()
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
    ("old_run", ("DISARM", "stage_c_authorization_consumed")),
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
def override_json(task="TASK-0100"):
    return ('{"services": {"company-worker": {"environment": {"ENABLE_AUTONOMOUS_CODE_WRITE": "true", '
            f'"STAGE_C_ARMED_TASK": "{task}"}}}}}}}}\n')


OVERRIDE_JSON = override_json()
IMAGES = {f"COMPANY_OS_IMAGE_{n}": "sha256:" + c * 64 for n, c in zip(("API", "WORKER", "REASONER", "EGRESS"), "1234")}
FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
# FAKE_HANG hangs the first matching call of a run only: a test bounds ONE hung call, not every identical read-back.
[ -z "${FAKE_HANG:-}" ] || [ "$*" != "$FAKE_HANG" ] || [ -e "$FAKE_STATE.hung" ] || { : > "$FAKE_STATE.hung"; exec sleep 60; }
case "$*" in "${FAKE_HANG_PREFIX:-none}"*) trap '' TERM; sleep 60; exit 0 ;; esac   # ignores TERM: needs the KILL
case "$*" in
  "exec -i -e PGOPTIONS"*) cat > "$FAKE_SNIPPET"
    if [ -e "$FAKE_STATE.recreated" ] && [ -n "${FAKE_DECISION_AFTER:-}" ]; then echo "$FAKE_DECISION_AFTER"; else echo "$FAKE_DECISION"; fi
    exit "${FAKE_DECISION_RC:-0}" ;;
  "exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE")
    if [ ! -e "$FAKE_STATE.recreated" ]; then [ "$FAKE_ERROR" != flag ] && [ "$FAKE_ERROR" != stopped ] || exit 1; fi
    cat "$FAKE_STATE" ;;
  "exec company-worker sh -c "*STAGE_C_ARMED_TASK*)
    [ "$FAKE_ERROR" != task ] || exit 1                      # a Docker setup error can also exit 1
    [ "$FAKE_ERROR" != flag ] && [ "$FAKE_ERROR" != stopped ] || exit 1
    [ -s "$FAKE_STATE.task" ] && cat "$FAKE_STATE.task" || echo - ;;
  "exec company-api printenv CODER_PAUSE_FENCE_ENABLED")
    if [ -e "$FAKE_STATE.recreated" ] && [ -n "${FAKE_FENCE_AFTER:-}" ]; then echo "$FAKE_FENCE_AFTER"; else echo "${FAKE_FENCE:-true}"; fi
    exit "${FAKE_FENCE_RC:-0}" ;;
  "exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE STAGE_C_ARMED_TASK COMPANY_OS_GIT_SHA")
    cat "$FAKE_STATE"; [ -s "$FAKE_STATE.task" ] && [ -s "$FAKE_STATE.sha" ] || exit 1
    cat "$FAKE_STATE.task" "$FAKE_STATE.sha" ;;
  "inspect -f {{.Image}} company-worker") [ -s "$FAKE_STATE.image" ] && cat "$FAKE_STATE.image" || echo "sha256:$(printf 0%.0s $(seq 64))" ;;
  "exec company-worker printenv COMPANY_OS_GIT_SHA") [ -e "$FAKE_STATE.sha" ] && cat "$FAKE_STATE.sha" || echo ;;
  inspect*)
    if [ -s "$FAKE_STATE.stopped" ]; then [ "$(cat "$FAKE_STATE.stopped")" = false ] || exit 1; echo false; exit 0; fi
    [ "$FAKE_ERROR" != inspect ] || exit 1; [ "$FAKE_ERROR" = stopped ] && echo false || echo true ;;
  stop*)
    case "$FAKE_STOP" in
      fail) exit 1 ;;
      ignored) ;;
      unreadable) echo unreadable > "$FAKE_STATE.stopped" ;;
      *) echo false > "$FAKE_STATE.stopped" ;;
    esac ;;
  compose*) [ "$FAKE_COMPOSE_FAIL" != 1 ] || exit 1
    [ "${FAKE_COMPOSE_NOOP:-}" != 1 ] || exit 0          # reports success, changes nothing
    : > "$FAKE_STATE.recreated"                           # this run's errors were about the old container
    rm -f "$FAKE_STATE.stopped"
    echo "$COMPANY_OS_IMAGE_WORKER" > "$FAKE_STATE.image"; echo "$COMPANY_OS_GIT_SHA" > "$FAKE_STATE.sha"
    [ "${FAKE_COMPOSE_NOSHA:-}" != 1 ] || : > "$FAKE_STATE.sha"     # a base Compose change dropped the SHA
    env | grep '^COMPANY_OS_' | sort >> "$FAKE_LOG"
    case "$*" in
      *stage-c-pilot*) echo true > "$FAKE_STATE"
        sed -n 's/.*"STAGE_C_ARMED_TASK": "\\([^"]*\\)".*/\\1/p' "$(printf '%s\\n' "$@" | grep stage-c-pilot)" > "$FAKE_STATE.task" ;;
      *) echo false > "$FAKE_STATE"; rm -f "$FAKE_STATE.task" ;;
    esac ;;
esac
"""


def _run(tmp_path, decision, armed, *args, error="", compose_fail=False, stop="", fence="true", hang="", extra=None, pins_extra=""):
    bin_, host, root = tmp_path / "bin", tmp_path / "opt", tmp_path / "root"
    for d in (bin_, host, root):
        d.mkdir(exist_ok=True)
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "logger").write_text('#!/bin/sh\necho "logger $*" >> "$FAKE_LOG"\n')
    for f in ("docker", "logger"):
        (bin_ / f).chmod(0o755)
    (host / ".pilot-source-sha").write_text(SHA_A + "\n")
    (host / "pilot-images.env").write_text("".join(f"{k}={v}\n" for k, v in IMAGES.items()) + pins_extra)
    (tmp_path / "state").write_text(armed + "\n")
    (tmp_path / "state.recreated").unlink(missing_ok=True)
    (tmp_path / "state.hung").unlink(missing_ok=True)
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "STAGE_C_ARM_DIR": str(host),
           "STAGE_C_ARM_OVERRIDE": str(root / "stage-c-pilot.compose.yml"), "FAKE_LOG": str(tmp_path / "log"),
           "FAKE_STATE": str(tmp_path / "state"), "FAKE_DECISION": decision,
           "FAKE_ERROR": error, "FAKE_COMPOSE_FAIL": str(int(compose_fail)), "FAKE_STOP": stop, "FAKE_FENCE": fence, "FAKE_HANG": hang,
           # 1 s only where a call is meant to hang: under load a healthy fake call can take longer than that
           "STAGE_C_ARM_DOCKER_TIMEOUT": "1" if hang else "10", **(extra or {}),
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


def test_sh_already_armed_rechecks_the_pause_fence(tmp_path):
    """R4a pre-review finding 3: a worker already armed for the requested task is not left armed once the API runs
    with the pause fence off; every ARM tick re-checks the fence before the already-armed shortcut."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true", fence="false")
    assert out.returncode == 1
    assert "reason=pause_fence_off" in log2[len(log):] and not ovr.exists()


def test_sh_hung_docker_is_bounded_and_disarms(tmp_path):
    """R4a-1 review finding 1: every Docker call is time-bounded. A hung read of the worker state is killed by the
    timeout, reads as UNKNOWN and the armed worker is disarmed: the timer never sits on an armed worker."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    started = time.monotonic()
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true",
                          hang="exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE")
    assert time.monotonic() - started < 20
    assert out.returncode == 1 and "reason=worker_state_unknown" in log2[len(log):] and not ovr.exists()


@pytest.mark.parametrize("drift", ["image", "sha"])
def test_sh_armed_on_another_release_is_rearmed(tmp_path, drift):
    """R4a-1v2 review finding 2: the already-armed shortcut also requires the pinned worker image and the source SHA
    the decision was validated against; a worker armed for the same task on other code is re-armed, not kept."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    other = {"image": "sha256:" + "9" * 64, "sha": "f" * 40}[drift]
    (tmp_path / f"state.{drift}").write_text(other + "\n")
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true")
    assert out.returncode == 0, out.stderr
    assert log2.count("docker compose") == 2 * log.count("docker compose")        # recreated from the pinned images
    assert (tmp_path / "state.image").read_text().strip() == IMAGES["COMPANY_OS_IMAGE_WORKER"]
    assert (tmp_path / "state.sha").read_text().strip() == SHA_A


def test_timeout_budget_covers_the_worst_case():
    """R4a-1v2 review finding 1: systemd must never kill a run before the fallback stop and its read-back. The unit's
    limit covers every bounded Docker call site (10 s + 5 s grace) and both Compose runs (60 s + 5 s grace)."""
    import re
    script = (ROOT / "scripts/stage-c-arm").read_text()
    calls = len(re.findall(r"\$\(docker |^\s*docker stop |docker exec company-api printenv", script, re.M))
    calls += script.count("post=$(decide)")                      # the decision probe runs a second time on ARM
    compose_runs = len(re.findall(r"(?:^\s*if |&& )compose ", script, re.M))   # the arm and the disarm recreate
    assert (calls, compose_runs) == (14, 2), "update the documented worst case and this test together"
    assert 'STAGE_C_ARM_DOCKER_TIMEOUT:-10' in script and 'STAGE_C_ARM_COMPOSE_TIMEOUT:-60' in script
    unit = (ROOT / "scripts/systemd/stage-c-arm.service").read_text()
    limit = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M).group(1))
    assert limit > calls * (10 + 5) + compose_runs * (60 + 5)


def test_sh_hung_compose_is_killed_and_the_worker_stopped(tmp_path):
    """R4a-1v3 pre-review finding 1: Compose runs through `env`, which bypassed the docker() limit. A Compose that
    hangs and ignores SIGTERM is killed after its limit plus the kill grace, both on arm and on the disarm recreate;
    the worker is then stopped and read back, and no ARM is ever logged."""
    started = time.monotonic()
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false",
                         extra={"FAKE_HANG_PREFIX": "compose", "STAGE_C_ARM_COMPOSE_TIMEOUT": "1"})
    assert time.monotonic() - started < 25            # 2 x (1 s + 5 s kill grace) + the short calls
    assert out.returncode == 1 and log.count("docker compose") == 2
    assert "action=ARM " not in log and "action=DISARM_STOP reason=compose_failed" in log and not ovr.exists()


@pytest.mark.parametrize("probe", ["decision", "fence"])
def test_sh_answer_then_failure_never_arms(tmp_path, probe):
    """R4a-1v3 pre-review finding 2: a probe that prints an acceptable answer and then fails (or times out) is a
    failure. An unarmed worker is not armed; an armed one is disarmed (decision -> read_error, fence -> off)."""
    extra = {"decision": {"FAKE_DECISION_RC": "1"}, "fence": {"FAKE_FENCE_RC": "1"}}[probe]
    reason = {"decision": "read_error", "fence": "pause_fence_off"}[probe]
    out, log, ovr = _run(tmp_path / "unarmed", "ARM TASK-0100", "false", extra=extra) if (tmp_path / "unarmed").mkdir() \
        is None else None
    assert "stage-c-pilot" not in log and "action=ARM " not in log and not ovr.exists()
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")                     # armed normally first
    assert out.returncode == 0 and ovr.exists()
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true", extra=extra)
    tail = log2[len(log):]
    assert f"reason={reason}" in tail and "action=ARM " not in tail and not ovr.exists()
    assert (tmp_path / "state").read_text().strip() == "false"                  # really recreated unarmed


def test_sh_arm_never_starts_a_stopped_worker(tmp_path):
    """R4a-1v3 pre-review finding 4: an operator's deliberate stop is kept on ARM too: no Compose, no start."""
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "true", error="stopped")
    assert out.returncode == 0, out.stderr
    assert "compose" not in log and "docker stop" not in log and not ovr.exists()
    assert "action=ARM_SKIPPED reason=worker_stopped task=TASK-0100" in log


def test_sh_rearms_for_a_new_task(tmp_path):
    """Armed for one task, a Start Pilot for another re-arms the worker for that task only (R3 finding 1)."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and (tmp_path / "state.task").read_text().strip() == "TASK-0100"
    out, log2, ovr = _run(tmp_path, "ARM TASK-0200", "true")
    assert out.returncode == 0, out.stderr
    assert log2.count("docker compose") == 2 * log.count("docker compose")    # recreated (cumulative log), not left as TASK-0100
    assert ovr.read_text() == override_json("TASK-0200") and json.loads(ovr.read_text())
    assert (tmp_path / "state.task").read_text().strip() == "TASK-0200"
    assert "action=ARM reason=authorized task=TASK-0200" in log2


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
    assert ("docker stop -t 5 company-worker" in log) == fail
    assert f"action={'DISARM_STOP' if fail else 'DISARM'} " in log


@pytest.mark.parametrize("stop", ["fail", "ignored", "unreadable"])
@pytest.mark.parametrize("arm", [False, True], ids=["d", "a"])
def test_sh_stop_unverified(tmp_path, stop, arm):
    """R3 review finding 3: when the worker cannot be recreated and `docker stop` fails, is ignored, or its result
    cannot be read back, the worker may still be running armed. The timer must not claim a disarm: DISARM_FAILED,
    exit 1, and the override is still removed so nothing can re-arm from it. The next tick tries again."""
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    decision = "ARM TASK-0100" if arm else "DISARM stage_c_authorization_missing"
    out, log, ovr = _run(tmp_path, decision, "true", compose_fail=True, stop=stop)
    assert out.returncode == 1, out.stderr
    assert "docker stop -t 5 company-worker" in log and "action=DISARM_FAILED " in log
    assert "DISARM_STOP" not in log and "action=DISARM " not in log
    assert not ovr.exists()
    out, log2, _ = _run(tmp_path, "DISARM stage_c_authorization_missing", "true", compose_fail=True)   # stop works now
    assert out.returncode == 0 and "action=DISARM_STOP " in log2[len(log):]


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


@pytest.mark.parametrize("case", ["same", "other", "unarmed", "unverified"])
def test_sh_symlink_override_disarms_before_the_shortcut(tmp_path, case):
    """R4a-1v4 review finding 1: an override path that is a symlink disarms the worker, also when it is already armed
    for the very task, image and SHA the decision names (the shortcut runs after this check). Only the link goes."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    if case == "other":
        (tmp_path / "state.task").write_text("TASK-0200\n")
    target = tmp_path / "target.yml"
    target.write_text("keep me\n")
    ovr.unlink()
    ovr.symlink_to(target)
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "false" if case == "unarmed" else "true",
                          compose_fail=case == "unverified", stop="ignored" if case == "unverified" else "")
    tail = log2[len(log):]
    assert out.returncode == 1 and "action=ARM " not in tail and "stage-c-pilot" not in tail
    assert not ovr.is_symlink() and not ovr.exists() and target.read_text() == "keep me\n"
    if case == "unverified":
        assert "action=DISARM_FAILED reason=override_symlink" in tail
    else:
        assert "action=DISARM reason=override_symlink task=TASK-0100" in tail
        assert (tmp_path / "state").read_text().strip() == "false"           # recreated unarmed


def test_sh_symlink_override_keeps_a_stopped_worker_stopped(tmp_path):
    (tmp_path / "root").mkdir()
    target = tmp_path / "target.yml"
    target.write_text("keep me\n")
    (tmp_path / "root/stage-c-pilot.compose.yml").symlink_to(target)
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "true", error="stopped")
    assert out.returncode == 0 and "compose" not in log and "docker stop" not in log
    assert not ovr.is_symlink() and target.read_text() == "keep me\n"


def test_sh_recreate_starts_a_stopped_armed_worker_unarmed(tmp_path):
    """R4a-1v4 review finding 2: `docker start` would resume a stopped worker with the environment it was armed with.
    The supported restart, `stage-c-arm --recreate`, recreates it from the pinned images without the override, and
    only a later ARM decision arms it again; a revoked authorization leaves it unarmed."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    (tmp_path / "state.stopped").write_text("false\n")                       # stopped while armed
    out, log2, ovr = _run(tmp_path, "DISARM stage_c_authorization_missing", "true", "--recreate")
    tail = log2[len(log):]
    assert out.returncode == 0, out.stderr
    assert ("docker compose -p company-os --env-file .env -f docker-compose.yml "
            "up -d --force-recreate --no-deps --no-build --pull never company-worker") in tail and "stage-c-pilot" not in tail
    assert "action=DISARM reason=operator_recreate task=-" in tail and not ovr.exists()
    assert (tmp_path / "state").read_text().strip() == "false" and not (tmp_path / "state.task").exists()
    assert (tmp_path / "state.image").read_text().strip() == IMAGES["COMPANY_OS_IMAGE_WORKER"]
    assert (tmp_path / "state.sha").read_text().strip() == SHA_A
    out, log3, ovr = _run(tmp_path, "DISARM stage_c_authorization_missing", "false")   # next tick: stays unarmed
    assert out.returncode == 0 and "compose" not in log3[len(log2):] and not ovr.exists()


def test_sh_recreate_that_cannot_recreate_fails_and_stops(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root/stage-c-pilot.compose.yml").write_text(OVERRIDE_JSON)
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "true", "--recreate", compose_fail=True)
    assert out.returncode == 1 and "action=DISARM_STOP reason=operator_recreate" in log and not ovr.exists()
    assert f"-f {ovr}" not in log and "docker stop -t 5 company-worker" in log


# ------------------------------------------------- R4a-1v5: every exit path acts on the worker (state matrix)
def _armed_first(tmp_path):
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false")
    assert out.returncode == 0 and ovr.exists()
    return log


def test_sh_override_dir_is_refused_and_disarms(tmp_path):
    """An override path that is not a regular file (here a directory) is treated like a symlink: disarm, never arm."""
    log = _armed_first(tmp_path)
    (tmp_path / "state.task").write_text("TASK-0200\n")                       # armed for another task
    ovr = tmp_path / "root/stage-c-pilot.compose.yml"
    ovr.unlink()
    ovr.mkdir()
    out, log2, _ = _run(tmp_path, "ARM TASK-0100", "true")
    tail = log2[len(log):]
    assert out.returncode == 1 and "action=ARM " not in tail and "stage-c-pilot" not in tail
    assert "action=DISARM reason=override_symlink" in tail and (tmp_path / "state").read_text().strip() == "false"
    assert "action=OVERRIDE_LEFT reason=override_symlink" in tail                # reported, the tick fails
    assert ovr.is_dir()                                                        # never removed recursively
    out, log3, _ = _run(tmp_path, "DISARM stage_c_authorization_missing", "false")   # already off: still reported
    assert out.returncode == 1 and "action=OVERRIDE_LEFT" in log3[len(log2):]


def test_sh_override_write_failure_disarms_a_worker_armed_elsewhere(tmp_path):
    """set -e must not end the run with a worker still armed for another task: an override that cannot be written
    (unwritable directory, full disk) disarms it, verified."""
    log = _armed_first(tmp_path)
    (tmp_path / "state.task").write_text("TASK-0200\n")
    root = tmp_path / "root"
    (root / "stage-c-pilot.compose.yml").unlink()
    root.chmod(0o500)
    try:
        out, log2, _ = _run(tmp_path, "ARM TASK-0100", "true")
    finally:
        root.chmod(0o700)
    tail = log2[len(log):]
    assert out.returncode == 1 and "action=ARM " not in tail
    assert "action=DISARM reason=override_write_failed" in tail and (tmp_path / "state").read_text().strip() == "false"
    assert not [p for p in root.iterdir() if p.name.startswith(".stage-c-pilot.")]   # no temp file left


def test_sh_missing_dir_stops_an_armed_worker(tmp_path):
    """A missing /opt/company-os used to end the run (set -e) before anything was done. Now the inputs are unreadable:
    the armed worker is stopped and read back."""
    log = _armed_first(tmp_path)
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true", extra={"STAGE_C_ARM_DIR": str(tmp_path / "missing")})
    tail = log2[len(log):]
    assert out.returncode == 0 and "docker stop -t 5 company-worker" in tail and "compose" not in tail
    assert "action=DISARM_STOP reason=bad_inputs" in tail
    assert not ovr.exists()


@pytest.mark.parametrize("decision", ["DISARM stage_c_authorization_revoked", "ARM TASK-0200"], ids=["rev", "oth"])
def test_sh_disarm_is_verified_by_read_back(tmp_path, decision):
    """A Compose run that reports success but leaves the worker armed (or, on ARM, armed for another task) is neither
    a disarm nor an arm: the worker is stopped and read back."""
    log = _armed_first(tmp_path)
    out, log2, ovr = _run(tmp_path, decision, "true", extra={"FAKE_COMPOSE_NOOP": "1"})
    tail = log2[len(log):]
    assert "action=DISARM " not in tail and "action=ARM " not in tail
    assert "docker stop -t 5 company-worker" in tail and "action=DISARM_STOP" in tail and not ovr.exists()


@pytest.mark.parametrize("drift", ["fence", "sha"])
def test_sh_arm_is_verified_after_compose(tmp_path, drift):
    """An arm counts only when the recreated worker reads back armed for the task on the pinned SHA, and the pause
    fence is still on after Compose (the API may be recreated meanwhile). Otherwise it is disarmed, never ARM-logged."""
    extra = {"fence": {"FAKE_FENCE_AFTER": "false"}, "sha": {"FAKE_COMPOSE_NOSHA": "1"}}[drift]
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false", extra=extra)
    assert out.returncode == 1 and "action=ARM " not in log and not ovr.exists()
    assert "action=DISARM reason=compose_failed" in log and (tmp_path / "state").read_text().strip() == "false"


def test_sh_duplicate_or_malformed_pin_is_bad_input(tmp_path):
    """A later malformed or duplicate definition of a pin never lets an earlier valid one through."""
    log = _armed_first(tmp_path)
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true",
                          pins_extra="COMPANY_OS_IMAGE_WORKER=sha256:" + "9" * 63 + "\n")
    tail = log2[len(log):]
    assert "PGOPTIONS" not in tail and "compose" not in tail                      # no decision, no Compose
    assert "action=DISARM_STOP reason=bad_inputs" in tail and not ovr.exists()


def test_sh_recreate_forces_a_new_container(tmp_path):
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "true", "--recreate")
    assert out.returncode == 0 and "up -d --force-recreate --no-deps --no-build --pull never company-worker" in log
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "false")                  # the timer never forces it
    assert "--force-recreate" not in log2[len(log):]


def test_sh_zero_timeout_cannot_disable_the_bound(tmp_path):
    """A limit of 0 would disable `timeout`; it falls back to the default bound (10 s) instead. One hung call: the
    fake hangs only the first matching read, so 25 s leaves 15 s for the rest of the run even on one slow CPU (the
    production gateway's checks container); it hung the identical disarm read-back too and ran out of margin there."""
    started = time.monotonic()
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "true", hang="exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE",
                         extra={"STAGE_C_ARM_DOCKER_TIMEOUT": "0"})
    assert time.monotonic() - started < 25 and out.returncode == 1 and "reason=worker_state_unknown" in log


def test_sh_failed_task_read_disarms_not_rearms(tmp_path):
    """A Docker error or timeout reading the armed task is a failed read, not an unarmed worker: verified disarm."""
    log = _armed_first(tmp_path)
    out, log2, ovr = _run(tmp_path, "ARM TASK-0100", "true", error="task")
    tail = log2[len(log):]
    assert out.returncode == 1 and "stage-c-pilot" not in tail and "action=ARM " not in tail
    assert "action=DISARM reason=worker_state_unknown" in tail and (tmp_path / "state").read_text().strip() == "false"


def test_sh_stop_pilot_during_compose_disarms_at_once(tmp_path):
    """The decision is asked again after the arming Compose run: a Stop Pilot in between disarms in the same tick."""
    out, log, ovr = _run(tmp_path, "ARM TASK-0100", "false", extra={"FAKE_DECISION_AFTER": "DISARM stage_c_authorization_missing"})
    assert out.returncode == 1 and "action=ARM " not in log and not ovr.exists()
    assert "action=DISARM reason=compose_failed" in log and (tmp_path / "state").read_text().strip() == "false"


@pytest.mark.parametrize("decision,armed,error,rc", [
    ("ARM TASK-0100", "true", "inspect", 1),                      # running state unreadable
    ("DISARM stage_c_authorization_missing", "true", "", 1),      # armed against the decision
    ("ARM TASK-0100", "true", "flag", 1),                         # unreadable
    ("DISARM stage_c_authorization_missing", "false", "", 0),
    ("ARM TASK-0100", "false", "", 0),                            # pending arm, nothing unsafe
], ids=["norun", "armed", "unread", "off", "pending"])
def test_sh_dry_run_exit_reflects_safety(tmp_path, decision, armed, error, rc):
    out, log, ovr = _run(tmp_path, decision, armed, "--dry-run", error=error)
    assert out.returncode == rc and "compose" not in log and "logger" not in log
