"""The privileged launcher cannot import app code; its fixed classifier must stay in sync."""
import pytest

from app import providers
from tests.test_coder_launch import FakeProcess, _load


def test_patterns_pinned():
    assert _load().CAPACITY_PATTERNS == providers.CAPACITY_PATTERNS


@pytest.mark.parametrize("message", [
    "Usage limit reached", "You've hit your limit", "session limit", "quota exceeded", "not enough credit", "insufficient balance",
    "billing unavailable", "payment required", "rate_limit", "rate-limit", "429", "overloaded",
    "too many requests", "log in", "login", "not logged in", "unauthorized", "unauthorised", "401",
    "invalid api key", "authentication failed", "OAuth error",
    "Your organization has disabled Claude subscription access for Claude Code",
    "unexpected failure", "",
], ids=lambda v: None if isinstance(v, (int, bool)) or v in ("CODER_ERROR", "QUOTA_EXHAUSTED", "RATE_LIMITED", "AUTH_REQUIRED", "TIMEOUT", "stdout", "stderr") else "")
def test_capacity_parity(message):
    health = providers.classify(message).value
    assert _load().capacity_class(message) == ("CODER_ERROR" if health == "BROKEN" else health)


@pytest.mark.parametrize("message,expected", [
    ("insufficient_quota", "QUOTA_EXHAUSTED"),
    ("quota_exceeded", "QUOTA_EXHAUSTED"),
    ("rate_limit_error", "RATE_LIMITED"),
    ("overloaded_error", "RATE_LIMITED"),
    ("authentication_error", "AUTH_REQUIRED"),
    ("authentication_failed", "AUTH_REQUIRED"),
    ("Not authenticated", "AUTH_REQUIRED"),
    ("status 429.", "RATE_LIMITED"),
    ("HTTP 429.", "RATE_LIMITED"),
    ("usage_limit", "QUOTA_EXHAUSTED"),
    ("session_limit", "QUOTA_EXHAUSTED"),
    ("insufficient_funds", "QUOTA_EXHAUSTED"),
    ("authenticating", "AUTH_REQUIRED"),
    ("HTTP 401.", "AUTH_REQUIRED"),
], ids=lambda v: None if isinstance(v, (int, bool)) or v in ("CODER_ERROR", "QUOTA_EXHAUSTED", "RATE_LIMITED", "AUTH_REQUIRED", "TIMEOUT", "stdout", "stderr") else "")
def test_capacity_variants(message, expected):
    assert providers.classify(message).value == expected
    assert _load().capacity_class(message) == expected


@pytest.mark.parametrize("message", ["catalog in use", "insufficient context", "0.401", "0.429", "blogin"])
def test_not_capacity(message):
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
    (b'{"is_error":true,"result":"unexpected failure","duration_ms":429}', b"", "CODER_ERROR"),
    (b'{"is_error":true,"result":"unexpected failure","total_cost_usd":0.401}', b"", "CODER_ERROR"),
], ids=lambda v: None if isinstance(v, (int, bool)) or v in ("CODER_ERROR", "QUOTA_EXHAUSTED", "RATE_LIMITED", "AUTH_REQUIRED", "TIMEOUT", "stdout", "stderr") else "")
def test_envelope_class(monkeypatch, stdout, stderr, expected):
    import io
    launcher = _load()
    proc = FakeProcess(stdout)
    proc.returncode = 1
    proc.stderr = io.BytesIO(stderr)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: proc)
    assert launcher.run_container(["docker"], b"", 5, "test", kind="code") == {
        "exit": 1, "timed_out": False, "failure_class": expected}
