"""scripts/phasec-e2e-check.py: the pilot checker is read-only by construction and fails closed.
Breaks caught: the checker gains a network, process or write path (it must never mutate anything), DONE before
production verification or a Stage C VERIFIED claim reads as a pass, and missing evidence reads as success.
The full joined flow drives it over real API reads in tests/test_phasec_e2e_flow.py."""
import ast
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/phasec-e2e-check.py"
M = "9" * 40


def _mod():
    spec = importlib.util.spec_from_file_location("phasec_e2e_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def assert_read_only():
    tree = ast.parse(SCRIPT.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert imported <= {"argparse", "json", "re", "sys", "pathlib"}, imported
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"write_text", "write_bytes", "open", "unlink", "mkdir", "system", "popen", "run", "rename",
                        "remove", "rmdir", "touch", "exec", "eval"}, calls & {"open", "run", "write_text"}


def _evidence(status="DONE", verified_first=True, merge_state="MERGED", ci="SUCCESS"):
    snap = {"activation": "ACTIVATED", "authorization": {"status": "CONSUMED"},
            "task": {"status": "REVIEW", "reviewer_verdict": "APPROVED", "fingerprint_match": True,
                     "pr": {"number": 7, "head_sha": "e" * 40}, "ci": {"state": "SUCCESS", "sha": "e" * 40}},
            "merge": {"state": merge_state, "commit": M, "main_ci": {"state": ci, "sha": M}}}
    rows = [{"id": 1, "action": "task.create", "entity_key": "TASK-0001", "actor": "founder:access:f"},
            {"id": 2, "action": "handoff.release", "entity_key": "TASK-0001"},
            {"id": 3, "action": "handoff.delivery", "entity_key": "TASK-0001"},
            {"id": 4, "action": "handoff.activation", "entity_key": "TASK-0001"}]
    verify = {"action": "handoff.production_verification", "entity_key": "TASK-0001", "reason": f"VERIFIED {M}"}
    done = {"action": "task.status", "entity_key": "TASK-0001"}
    rows += [{**verify, "id": 5}, {**done, "id": 6}] if verified_first else [{**done, "id": 5}, {**verify, "id": 6}]
    return {"prod-task.json": {"key": "TASK-0001", "status": status, "remote_stage_c": {
                "id": "h", "payload_sha256": "d" * 64, "delivery": "DELIVERED", "remote_task_key": "TASK-0042",
                "freshness": "FRESH", "snapshot": snap,
                "verification": {"state": "UNKNOWN", "production_api_sha": None}}},
            "prod-audit.json": rows, "sc-audit.json": [],
            "sc-task.json": {"key": "TASK-0042", "runs": [{"id": 1, "status": "SUCCEEDED"}]},
            "pr.json": {"number": 7, "headRefName": "task/TASK-0042", "headRepositoryOwner": {"login": "seerxo-agent"},
                        "files": [{"path": "tests/test_metrics.py"}], "mergedBy": {"login": "founder"},
                        "mergeCommit": {"oid": M}}, "arm.txt": None}


def _status(ev):
    return {r["step"]: r["status"] for r in _mod().check(ev, founder_login="founder")}


def test_check(tmp_path):
    assert_read_only()
    assert set(_status(_evidence()).values()) == {"PASS"}
    assert _status(_evidence(verified_first=False))["MERGED"] == "STOP"      # DONE before production VERIFIED
    assert _status(_evidence(merge_state="VERIFIED"))["MERGED"] == "STOP"    # Stage C may not claim VERIFIED
    ev = _evidence(status="WAITING_EXTERNAL", ci="FAILURE")
    assert _status(ev)["VERIFIED"] == "STOP"
    ev = _evidence()
    ev["pr.json"]["mergedBy"] = {"login": "someone-else"}
    assert _status(ev)["MERGED"] == "STOP"
    ev = _evidence()
    ev["sc-task.json"]["runs"].append({"id": 2, "status": "STARTED"})
    assert _status(ev)["CODER"] == "STOP"
    assert_waits(tmp_path)


def assert_waits(tmp_path):
    mod = _mod()
    assert mod.main([str(tmp_path), "--founder-login", "f"]) == 1               # nothing captured: WAIT, never PASS
    ev = _evidence()
    for name, value in ev.items():
        if value is not None:
            (tmp_path / name).write_text(json.dumps(value))
    assert mod.main([str(tmp_path), "--founder-login", "founder"]) == 0
    (tmp_path / "pr.json").write_text("{not json")
    assert mod.main([str(tmp_path), "--founder-login", "founder"]) == 2          # unreadable: error, no guess
