"""Withheld text cannot stand in for the deliverable the founder requested."""
import pytest

from app import enums, redact, worker
from tests.test_worker import SPECS, FakeProvider, _db, _good, _needs_items


@pytest.mark.parametrize("action", ["[unavailable]", "[withheld: too large to scan]",
                                    "[withheld: credential-shaped content]", "***",
                                    "token=" + "Q9w8E7r6T5y4U3i2O1p0" * 2,
                                    "x" * (redact.MAX_SCAN_CHARS + 1)])
def test_supervisor_does_not_count_placeholders_or_oversized_actions(action):
    db = _db()
    task = _needs_items(db, 2)
    result = worker.run_once(db, {"claude": FakeProvider("claude", [
        _good(task.key, actions=["Useful deliverable", f"  {action}  "])
    ])}, SPECS)
    assert result["verdict"] == "REVISION_REQUIRED"
    assert "needs 2 action item(s), got 1" in result["notes"]
    assert task.status == enums.TaskStatus.READY


def test_scanner_failure_does_not_count_as_a_deliverable(monkeypatch):
    db = _db()
    task = _needs_items(db)

    def broken(text):
        raise RuntimeError("scanner failed")

    monkeypatch.setattr(redact, "_redact_secrets", broken)
    raw = _good(task.key, actions=["Cannot be scanned"])
    verdict, notes, _ = worker.supervise(task, raw)
    assert verdict == enums.SupervisorVerdict.REVISION_REQUIRED
    assert "got 0" in notes
