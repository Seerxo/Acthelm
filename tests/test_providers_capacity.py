"""The privileged launcher cannot import app code; its fixed classifier must stay in sync."""
import pytest

from app import providers
from tests.test_coder_launch import _load


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
