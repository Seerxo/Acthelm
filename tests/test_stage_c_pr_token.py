"""A fork-scoped push token cannot open a PR on the upstream (fine-grained tokens are one-owner; pilot 2026-11-02,
TASK-8001/8002: Codex APPROVE, fork push done, PR_FAILED). An optional PR-only token (GH_PR_TOKEN, upstream Pull
requests: write, never Contents) is used for `gh pr create` alone. Break caught: the PR token reaching a push, a
provider CLI or the coder; a PR token that can write upstream; the single-token path changing."""
import subprocess
from types import SimpleNamespace

from app import providers, stage_c_runner, stage_c_worker

FORK, UPSTREAM, COMMIT = "seerxo-agent/seerxo-os", "seerxo/acthelm", "a" * 40


def _capture(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get("env")))
        return SimpleNamespace(stdout="https://github.com/Seerxo/Acthelm/pull/77\n", returncode=0)
    monkeypatch.setattr(stage_c_runner.subprocess, "run", run)
    return seen


def test_the_pr_is_opened_with_the_pr_token_only(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fork-token")
    monkeypatch.setenv("GH_PR_TOKEN", "pr-token")
    seen = _capture(monkeypatch)
    url = stage_c_runner.gh_pr_opener(UPSTREAM, "main", FORK)("task/TASK-0001", "t", "b", COMMIT)
    assert url == "https://github.com/seerxo/acthelm/pull/77"
    (argv, env), = seen
    assert argv[:3] == ["gh", "pr", "create"] and env["GH_TOKEN"] == "pr-token"


def test_without_a_pr_token_the_single_token_path_is_unchanged(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fork-token")
    monkeypatch.delenv("GH_PR_TOKEN", raising=False)
    seen = _capture(monkeypatch)
    stage_c_runner.gh_pr_opener(UPSTREAM, "main", FORK)("task/TASK-0001", "t", "b", COMMIT)
    assert seen[0][1] is None                                    # inherits the worker environment as before


def test_pushes_keep_the_fork_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "fork-token")
    monkeypatch.setenv("GH_PR_TOKEN", "pr-token")
    seen = []
    monkeypatch.setattr(stage_c_runner.subprocess, "run",
                        lambda argv, **kw: seen.append(kw.get("env")) or SimpleNamespace(stdout=b"", returncode=0))
    stage_c_runner._git(tmp_path, "push", "https://github.com/seerxo-agent/seerxo-os.git", "x", protocol="https")
    assert seen[0].get("GH_TOKEN") == "fork-token" and "GH_PR_TOKEN" not in seen[0]


def test_a_pr_token_that_can_write_upstream_stops_publication(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fork-token")
    monkeypatch.setenv("GH_PR_TOKEN", "pr-token")

    def role(env=None, run=subprocess.run, repository=UPSTREAM):
        token = (env or {}).get("GH_TOKEN", "fork-token")
        return ("FAIL", "can push") if token == "pr-token" else ("PASS", "read-only")
    monkeypatch.setattr(stage_c_worker.stage_c_credential_boundary, "upstream_role", role)
    assert stage_c_worker.publication_role()[0] == "FAIL"
    monkeypatch.delenv("GH_PR_TOKEN")
    assert stage_c_worker.publication_role()[0] == "PASS"


def test_the_pr_token_never_reaches_a_provider_cli():
    env = providers.child_env({"GH_PR_TOKEN": "pr-token", "GH_TOKEN": "fork-token", "PATH": "/usr/bin"})
    assert "GH_PR_TOKEN" not in env and "GH_TOKEN" not in env


# --------------------------------------------------------------------------- recovery of a pushed, never-opened PR
import pytest

from app import enums, worker
from tests.test_stage_c_worker import Reviewer, pipeline, write_task
from tests.test_worker import SPECS, _db


def _first_publish_fails_after_the_push(monkeypatch, tmp_path):
    """Pilot 2026-11-02 shape: checks pass, Codex approves, the branch is pushed, the PR create fails."""
    failing = {"on": True}

    def during_open_pr(db, task):
        if failing["on"]:
            raise RuntimeError("fork-only token cannot open the upstream PR")
    pipeline(monkeypatch, tmp_path, {"during_open_pr": during_open_pr}, publish_repository="seerxo-agent/seerxo-os")
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup",
                        lambda *a, **k: (lambda branch, commit: (_ for _ in ()).throw(RuntimeError("no PR"))))
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == "PR_FAILED" and task.status == enums.TaskStatus.BLOCKED
    failing["on"] = False
    task.status, task.error = enums.TaskStatus.READY, None            # the founder's unblock after the fix
    db.commit()
    return db, task


@pytest.mark.parametrize("absent, verdict", [(True, "PR_OPEN"), (False, "PR_FAILED"), (None, "PR_FAILED")])
def test_recovery_opens_the_missing_pr_only_when_github_has_none(monkeypatch, tmp_path, absent, verdict):
    db, task = _first_publish_fails_after_the_push(monkeypatch, tmp_path)
    asked = []

    def gh_pr_absent(*a, **k):
        def check(branch):
            asked.append(branch)
            if absent is None:
                raise RuntimeError("GitHub unreachable")     # gh_pr_absent itself maps errors to False
            return absent
        return check
    monkeypatch.setattr(stage_c_runner, "gh_pr_absent", gh_pr_absent)
    out = worker.run_once(db, {n: Reviewer(n) for n in ("claude", "codex")}, SPECS)
    assert out["verdict"] == verdict, out
    assert asked == [f"task/{task.key}"]
    if verdict == "PR_OPEN":
        assert task.status == enums.TaskStatus.REVIEW
        assert task.artifacts["pr"] == "https://github.com/seerxo/acthelm/pull/999"
        assert out["model_calls"] == 0                       # the approved change is published, never re-coded


def test_gh_pr_absent_fails_closed(monkeypatch):
    calls = []

    def api(path, jq):
        calls.append(path)
        return answers.pop(0)
    monkeypatch.setattr(stage_c_runner.repo_guardian_github, "api", api)
    check = stage_c_runner.gh_pr_absent(UPSTREAM, "main", FORK)
    answers = [[]]
    assert check("task/TASK-0001") is True and "state=all" in calls[0] and "seerxo-agent%3Atask%2FTASK-0001" in calls[0]
    for answer in ([{"number": 5, "state": "closed"}], {"message": "x"}, None):
        answers = [answer]
        assert check("task/TASK-0001") is False
    monkeypatch.setattr(stage_c_runner.repo_guardian_github, "api",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert check("task/TASK-0001") is False
