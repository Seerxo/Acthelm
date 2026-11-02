"""Stage C hardening (2026-11-03 review): what a public PR, the task history and a repair prompt may carry, and what
counts as checks having run. Break caught: reviewer findings stored unredacted; a JWT or a JSON "refresh_token" not
masked; task title / success criteria in the public PR or commit; a credential-shaped added line published; a changed
test file that never ran passing on the rest of the suite.
Fake credentials are assembled at runtime so no literal credential shape sits in the repository."""
import json
import subprocess

import pytest

from app import enums, stage_c_runner, worker
from tests.test_stage_c_worker import Reviewer, pipeline, write_task
from tests.test_worker import SPECS, _db

FORK = "seerxo-agent/seerxo-os"   # the repository policy publishes from the B5 fork
PAT = "github_pat_" + "A1b2C3" * 6
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJ4In0xMjM0" + "." + "s1gnature" + "ABCDEFGH"
REFRESH = '"refresh_token": "' + "r" * 24 + '"'
SENSITIVE = {"title": "INTERNAL customer acme@private.example password=hunter2xyz",
             "success_criteria": f"verify with {PAT}", "objective": f"call the API with {JWT}"}
PRIVATE = ("INTERNAL", "acme@private.example", "hunter2xyz", PAT, JWT, "verify with", "call the API")


def _providers(cls=Reviewer):
    return {n: cls(n) for n in ("claude", "codex")}


def _coder(monkeypatch, text):
    def launcher_coder(**kw):
        def call(directory, job):
            kw["before_enqueue"]()
            kw["after_start"](True)
            (directory / "tests/test_metrics.py").write_text(text)
        return call
    monkeypatch.setattr(stage_c_runner, "launcher_coder", launcher_coder)


def _checks(monkeypatch, result):
    def checks(directory):
        (directory.parent / "done").mkdir(exist_ok=True)
        (directory.parent / "done" / "result.json").write_text('{"exit": 0}')
        return result
    monkeypatch.setattr(stage_c_runner, "launcher_checks", lambda repo, **kw: checks)


def _commit_message(remote, key="TASK-0100"):
    return subprocess.run(["git", "--git-dir", str(remote), "log", "-1", "--format=%B", f"task/{key}"],
                          capture_output=True, text=True, check=True).stdout


def test_redaction_masks_jwts_and_json_quoted_token_keys():
    text = stage_c_runner._redact_secrets(f'{{"access_token": "{"a" * 20}", {REFRESH}, "id_token": "{JWT}"}} {JWT}')
    assert JWT not in text and "a" * 20 not in text and "r" * 24 not in text


def test_reviewer_findings_are_stored_masked(monkeypatch, tmp_path):
    class Rejecting(Reviewer):
        def _run(self, prompt, schema, **kw):
            result = super()._run(prompt, schema, **kw)
            if schema == stage_c_runner.REVIEW_SCHEMA:
                result.output = {**result.output, "verdict": "REJECT", "findings": [f"leaked {JWT}", REFRESH]}
            return result
    pipeline(monkeypatch, tmp_path, publish_repository=FORK)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(Rejecting), SPECS)
    findings = task.context["improvement"]["history"][-1]["evidence"]["findings"]
    assert out["verdict"] == "REJECTED", out
    assert len(findings) == 2 and all("***" in f for f in findings)
    stored = json.dumps(task.context)
    assert JWT not in stored and "r" * 24 not in stored


def test_checks_summary_is_masked_before_history_and_feedback(monkeypatch, tmp_path):
    pipeline(monkeypatch, tmp_path, publish_repository=FORK)
    _checks(monkeypatch, {"passed": False, "summary": f"1 failed token={PAT}", "failures": [f"E  {JWT}"]})
    db = _db()
    task = write_task(db)
    assert worker.run_once(db, _providers(), SPECS)["verdict"] == "CHECKS_FAILED"
    stored = json.dumps(task.context)
    assert PAT not in stored and JWT not in stored


@pytest.mark.parametrize("passed_files", [[], ["tests/test_other.py"], None, "tests/test_metrics.py"])
def test_checks_fail_closed_unless_every_changed_test_file_reported_a_pass(monkeypatch, tmp_path, passed_files):
    pipeline(monkeypatch, tmp_path, publish_repository=FORK)
    _checks(monkeypatch, {"passed": True, "summary": "5 passed in 0.1s", "passed_files": passed_files})
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(), SPECS)
    evidence = task.context["improvement"]["history"][-1]["evidence"]
    assert out["verdict"] == "CHECKS_FAILED", out
    assert evidence["checks_passed"] is False
    assert evidence["checks"] == "changed test files reported no passed test: tests/test_metrics.py"


def test_public_pr_and_commit_carry_no_task_text(monkeypatch, tmp_path):
    hooks = {"opened": []}
    remote = pipeline(monkeypatch, tmp_path, hooks, publish_repository=FORK)
    db = _db()
    task = write_task(db, **SENSITIVE)
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == "PR_OPEN", out
    (branch, title, body, _commit), = hooks["opened"]
    message = _commit_message(remote)
    assert branch == "task/TASK-0100" and title == "TASK-0100: Stage C change"
    assert message.startswith("TASK-0100: Stage C change\n\nFingerprint: ")
    for text in (title, body, message):
        assert not any(value in text for value in PRIVATE), text
    assert task.context["improvement"]["approval"]["fingerprint"] in body


def test_recovery_opens_the_missing_pr_with_the_same_public_text(monkeypatch, tmp_path):
    failing = {"on": True}

    def during_open_pr(db, task):
        if failing["on"]:
            raise RuntimeError("fork-only token cannot open the upstream PR")
    hooks = {"opened": [], "during_open_pr": during_open_pr}
    remote = pipeline(monkeypatch, tmp_path, hooks, publish_repository=FORK)
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup",
                        lambda *a, **k: (lambda branch, commit: (_ for _ in ()).throw(RuntimeError("no PR"))))
    monkeypatch.setattr(stage_c_runner, "gh_pr_absent", lambda *a, **k: (lambda branch: True))
    db = _db()
    task = write_task(db, **SENSITIVE)
    assert worker.run_once(db, _providers(), SPECS)["verdict"] == "PR_FAILED"
    failing["on"] = False
    task.status, task.error = enums.TaskStatus.READY, None
    db.commit()
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == "PR_OPEN", out
    assert len(hooks["opened"]) == 2 and hooks["opened"][0][:3] == hooks["opened"][1][:3]
    _branch, title, body, _commit = hooks["opened"][1]
    for text in (title, body, _commit_message(remote)):
        assert not any(value in text for value in PRIVATE), text


@pytest.mark.parametrize("key, approval", [
    ("TASK-0100; rm", {"fingerprint": "f" * 64, "reviewer": "codex"}),
    ("TASK-0100", {"fingerprint": "f" * 64, "reviewer": "gemini"}),
    ("TASK-0100", {"fingerprint": "f" * 64, "reviewer": ["codex"]}),
    ("TASK-0100", {"fingerprint": "F" * 64, "reviewer": "codex"}),
    ("TASK-0100", {"fingerprint": None, "reviewer": "codex"}),
])
def test_public_text_refuses_unvalidated_identifiers(key, approval):
    with pytest.raises(ValueError):
        stage_c_runner._public_text(key, approval)


def test_a_credential_shaped_added_line_stops_publication(monkeypatch, tmp_path):
    hooks = {"opened": []}
    remote = pipeline(monkeypatch, tmp_path, hooks, publish_repository=FORK)
    _coder(monkeypatch, f'TOKEN = "{PAT}"\n\n\ndef test_metric():\n    assert 1 == 1\n')
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == "DRIFT" and task.status == enums.TaskStatus.BLOCKED, out
    assert hooks["opened"] == []
    assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", "task/TASK-0100"],
                          capture_output=True).returncode != 0   # nothing pushed


def test_outgoing_scan_reads_every_added_line_including_forged_headers():
    diff = (f"diff --git a/tests/a.py b/tests/a.py\n--- a/tests/a.py\n+++ b/tests/a.py\n@@ -1 +1,2 @@\n x\n"
            f"+++ {PAT}\n").encode()
    assert stage_c_runner._outgoing_credentials(diff) == ["tests/a.py"]
    assert stage_c_runner._outgoing_credentials(b"diff --git a/t.py b/t.py\n+token = acquire(db)\n") == []


def _b64(obj) -> str:
    import base64
    return base64.urlsafe_b64encode(json.dumps(obj, indent=None).encode()).decode().rstrip("=")


# Valid JWTs the old `eyJ...{8,}` shape missed: tiny claims, empty claims, formatted JSON, alg=none (no signature).
ODD_JWTS = [_b64({"alg": "HS256"}) + "." + _b64({"a": 1}) + ".c2ln",
            _b64({"alg": "HS256"}) + "." + _b64({}) + ".c2lnbmF0dXJl",
            "ewogICJhbGciOiAiSFMyNTYiCn0" + "." + "ewogICJzdWIiOiAieCIKfQ" + ".c2ln",
            _b64({"alg": "none"}) + "." + _b64({"sub": "x"}) + "."]


@pytest.mark.parametrize("jwt", ODD_JWTS)
def test_any_valid_jwt_is_masked_and_stops_publication(jwt):
    assert jwt not in stage_c_runner._redact_secrets(f"found x.{jwt} here")
    assert stage_c_runner._outgoing_credentials(f"diff --git a/t.py b/t.py\n+X = '{jwt}'\n".encode()) == ["t.py"]


def test_dotted_code_is_not_a_jwt():
    text = "os.path.join(a.b.c, self.db.session.query) v1.2.3"
    assert stage_c_runner._redact_secrets(text) == text
    assert stage_c_runner._outgoing_credentials(f"diff --git a/t.py b/t.py\n+{text}\n".encode()) == []


@pytest.mark.parametrize("sep", ["\r", "\x0b", " ", "\x1c", "\x85"])
def test_a_credential_after_an_embedded_line_separator_is_still_caught(sep):
    """Break caught: splitlines() split inside an added line and dropped the `+` before the credential."""
    diff = f"diff --git a/t.py b/t.py\n+# note{sep}# credential = {PAT}\n".encode()
    assert stage_c_runner._outgoing_credentials(diff) == ["t.py"]


def test_a_jwt_split_over_adjacent_literals_or_lines_is_caught():
    jwt = ODD_JWTS[0]
    head, rest = jwt.split(".", 1)
    diff = f'diff --git a/t.py b/t.py\n+VALUE = ("{head}."\n+         "{rest}")\n'.encode()
    assert stage_c_runner._outgoing_credentials(diff) == ["t.py"]
    assert jwt.split(".")[1] not in stage_c_runner._redact_secrets(f"token part {head}.\n{rest}")
    padded = _b64({"alg": "HS256"}) + "==." + _b64({"sub": "x"}) + "=.c2ln"
    assert stage_c_runner._outgoing_credentials(f"diff --git a/t.py b/t.py\n+X = '{padded}'\n".encode()) == ["t.py"]


@pytest.mark.parametrize("prefix", ["", "r", "b", "f", "br", "rb", "fr", "rf", "R", "B", "F"])
def test_prefixed_adjacent_literals_are_detected_before_redaction(prefix):
    head, rest = ODD_JWTS[0].split(".", 1)
    text = f'VALUE = ({prefix}"{head}."\n {prefix}"{rest}")'
    diff = "diff --git a/t.py b/t.py\n" + "\n".join("+" + line for line in text.split("\n")) + "\n"
    assert stage_c_runner._outgoing_credentials(diff.encode()) == ["t.py"]
    assert stage_c_runner._redact_secrets(text) == "[withheld: credential-shaped content]"


def test_wrapped_token_is_withheld_before_its_header_is_masked():
    head, rest = ODD_JWTS[0].split(".", 1)
    assert stage_c_runner._redact_secrets(f"token={head}.\n{rest}") == "[withheld: credential-shaped content]"


def test_adjacent_literals_with_a_wrapping_comment_are_detected():
    head, rest = ODD_JWTS[0].split(".", 1)
    text = f'VALUE = ("{head}." # wrapped literal\n "{rest}")'
    diff = "diff --git a/t.py b/t.py\n" + "\n".join("+" + line for line in text.split("\n")) + "\n"
    assert stage_c_runner._outgoing_credentials(diff.encode()) == ["t.py"]
    assert stage_c_runner._redact_secrets(text) == "[withheld: credential-shaped content]"


def test_jwt_header_with_leading_json_whitespace_is_detected():
    import base64
    head = base64.urlsafe_b64encode(b'  {"alg":"HS256"}').decode().rstrip("=")
    jwt = head + "." + _b64({"sub": "synthetic"}) + ".c2ln"
    text = f'VALUE = "{jwt}"'
    assert stage_c_runner._outgoing_credentials(f"diff --git a/t.py b/t.py\n+{text}\n".encode()) == ["t.py"]
    assert jwt not in stage_c_runner._redact_secrets(text)


@pytest.mark.parametrize("name", ["task-repository-guardian", "task-notification-worker"])
def test_ordinary_task_names_are_not_credentials(name):
    text = f'branch = "{name}"'
    assert stage_c_runner._outgoing_credentials(f"diff --git a/t.py b/t.py\n+{text}\n".encode()) == []
    assert stage_c_runner._redact_secrets(text) == text


@pytest.mark.parametrize("text", ["a" * (1 << 20), ("e30." * (1 << 18)), ("a." * (1 << 19)),
                                  " " * (1 << 20), "\t" * (1 << 20),
                                  '"' + " " * (1 << 20) + "!", "token" + " " * (1 << 20) + "!"],
                         ids=["letters", "json-dotted", "dotted", "spaces", "tabs", "literal-spaces", "key-spaces"])
def test_jwt_scanning_stays_linear_on_hostile_megabyte_inputs(text):
    import time
    import signal
    def timed_out(*_):
        raise AssertionError("credential scanning exceeded five seconds")
    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.monotonic()
    try:
        stage_c_runner._redact_secrets(text)
        stage_c_runner._outgoing_credentials(("diff --git a/t.py b/t.py\n+" + text + "\n").encode())
        assert time.monotonic() - started < 5   # was over 4 s for 16 KiB of "a" alone
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
