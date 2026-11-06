"""The privileged launcher cannot import app code; its fixed classifier must stay in sync."""
import pytest

from app import providers
from tests.test_coder_launch import FakeProcess, _load


def test_launcher_capacity_patterns_match_providers():
    assert _load().CAPACITY_PATTERNS == providers.CAPACITY_PATTERNS


@pytest.mark.parametrize("message", [
    "Usage limit reached", "You've hit your limit", "session limit", "quota exceeded", "not enough credit", "insufficient balance",
    "billing unavailable", "payment required", "rate_limit", "rate-limit", "429", "overloaded",
    "too many requests", "log in", "login", "not logged in", "unauthorized", "unauthorised", "401",
    "invalid api key", "authentication failed", "OAuth error",
    "Your organization has disabled Claude subscription access for Claude Code",
    "unexpected failure", "",
])
def test_launcher_classifies_capacity_like_providers(message):
    health = providers.classify(message).value
    assert _load().capacity_class(message) == ("CODER_ERROR" if health == "BROKEN" else health)


@pytest.mark.parametrize("message", ["catalog in use", "insufficient context", "0.401", "0.429"])
def test_non_capacity_phrases_are_spent_errors(message):
    assert _load().capacity_class(message) == "CODER_ERROR"
    assert providers.classify(message) == providers.Health.BROKEN


@pytest.mark.parametrize("stdout,stderr,expected", [
    (b'{"is_error":true,"subtype":"error_max_turns","result":"login","errors":["quota"]}', b"", "CODER_ERROR"),
    (b'{"is_error":true,"subtype":"error_during_execution","result":"login","errors":["quota"]}', b"", "CODER_ERROR"),
    (b'{"is_error":false,"result":"login","permission_denials":[{"path":"quota/429"}]}', b"", "CODER_ERROR"),
    (b'{"is_error":true,"errors":["too many requests"]}', b"", "RATE_LIMITED"),
    (b'{"is_error":true,"result":"unexpected failure","permission_denials":[{"path":"quota/429"}]}', b"", "CODER_ERROR"),
    (b'not json: quota login 429', b"", "CODER_ERROR"),
    (b'not json: quota', b"not logged in", "AUTH_REQUIRED"),
    (b'[' * 2000, b"not logged in", "AUTH_REQUIRED"),
    (b'\xffquota', b"", "CODER_ERROR"),
    (b'{"is_error":true,"subtype":"error_max_turns","result":"quota"}', b"HTTP 429", "RATE_LIMITED"),
    (b'[]', b"usage limit reached", "QUOTA_EXHAUSTED"),
    (b'{"is_error":"true","result":"quota"}', b"", "CODER_ERROR"),
    (b'{"is_error":true,"result":429,"errors":[401,{"path":"quota"}]}', b"", "CODER_ERROR"),
])
def test_launcher_uses_only_stderr_and_eligible_envelope_error_text(monkeypatch, stdout, stderr, expected):
    import io
    launcher = _load()
    proc = FakeProcess(stdout)
    proc.returncode = 1
    proc.stderr = io.BytesIO(stderr)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: proc)
    assert launcher.run_container(["docker"], b"", 5, "test", kind="code") == {
        "exit": 1, "timed_out": False, "failure_class": expected}
