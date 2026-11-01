"""tests/fixtures/ci_snapshot is exactly the repository's CI: what the CI-contract tests assert is visible to the
release reviewer, whose packet covers only the deployed paths (not .github/)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests/fixtures/ci_snapshot"
WORKFLOWS = ROOT / ".github/workflows"
# No git needed. Only a tree exported without .github (a limited Stage C export) has nothing to compare: named skip,
# on the real workflows only (a missing or different snapshot still fails).
needs_workflows = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="this tree has no .github/workflows to compare")


@needs_workflows
def test_the_snapshot_equals_the_real_workflows_byte_for_byte():
    real = {p.name: p.read_bytes() for p in WORKFLOWS.glob("*.yml")}
    copy = {p.name: p.read_bytes() for p in SNAPSHOT.glob("*.yml")}
    assert real and copy == real


def test_no_circleci_configuration():
    assert not (ROOT / ".circleci").exists()


@needs_workflows
def test_no_jules_workflow():
    assert not any("jules" in p.name.lower() for p in WORKFLOWS.iterdir())
