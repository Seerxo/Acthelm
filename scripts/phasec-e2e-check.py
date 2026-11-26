#!/usr/bin/env python3
"""Phase C real-pilot checker (docs/runbooks/PHASEC_E2E_PILOT.md section 2a). READ-ONLY BY CONSTRUCTION.

It makes no network call, starts no process and writes no file: it only reads JSON/text evidence the founder captured
with GET requests (and `gh pr view`, `stage-c-arm --dry-run`) into one directory, and judges every stage transition:

  01 CREATE -> 02 RELEASE -> 03 IMPORT -> 04 ACTIVATION -> 05 AUTHORIZATION -> 06 CODER -> 07 PR -> 08 CI
  -> 09 MERGED -> 10 VERIFIED -> 11 DONE

Each step is PASS (evidence shown), WAIT (not reached yet: the pilot is in progress) or STOP (a stop condition:
Stop Pilot, then investigate; never retry blindly). Steps after the first WAIT are reported as WAIT too.
Exit code: 0 every step PASS, 1 some step WAITs, 3 any STOP, 2 usage or unreadable evidence.

Evidence files (missing = not captured yet = WAIT for the steps that need it):
  prod-task.json      GET <prod>/tasks/<P>
  prod-audit.json     docker exec company-api curl -fsS 'http://localhost:8000/audit?limit=5000'  (prod host)
  sc-task.json        GET <stage-c>/tasks/<S>
  sc-audit.json       the same GET on the Stage C host
  pr.json             gh pr view <N> --repo seerxo/acthelm --json number,state,headRefName,headRepositoryOwner,files,mergedBy,mergeCommit
  arm.txt             sudo /opt/company-os/scripts/stage-c-arm --dry-run   (optional)

Usage: phasec-e2e-check.py <evidence-dir> --founder-login <github login> [--path tests/test_metrics.py]
                           [--fork-owner seerxo-agent] [--json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

SHA40 = re.compile(r"^[0-9a-f]{40}$")
# A poll or delivery error that is a stop condition (runbook section 3); others (UNREACHABLE, HTTP_5xx) are WAIT.
STOP_CODES = re.compile(r"^(BAD_MAC|BAD_RESPONSE|BINDING_MISMATCH|PACKET_INVALID|RELEASE_.*|TASK_CHANGED|HTTP_4\d\d)$")
STEPS = ("CREATE", "RELEASE", "IMPORT", "ACTIVATION", "AUTHORIZATION", "CODER", "PR", "CI", "MERGED", "VERIFIED",
         "DONE")


def load(directory: Path, name: str):
    """The parsed evidence file, or None when it was not captured. Unparseable evidence is an error, never a guess."""
    path = directory / name
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return text if name.endswith(".txt") else json.loads(text)


def _audit(rows, action: str, key: str | None = None) -> list[dict]:
    rows = rows if isinstance(rows, list) else []
    return sorted((r for r in rows if isinstance(r, dict) and r.get("action") == action
                   and (key is None or r.get("entity_key") == key)), key=lambda r: r.get("id") or 0)


class Stop(Exception):
    pass


class Wait(Exception):
    pass


def need(cond, why: str):
    if not cond:
        raise Wait(why)


def stop_if(cond, why: str):
    if cond:
        raise Stop(why)


def check(ev: dict, *, founder_login: str, path: str = "tests/test_metrics.py",
          fork_owner: str = "seerxo-agent") -> list[dict]:
    """Pure: evidence dict (file name -> parsed content or None) -> one result per step."""
    pt, pa, st, sa, pr = (ev.get(k) for k in ("prod-task.json", "prod-audit.json", "sc-task.json", "sc-audit.json",
                                               "pr.json"))
    pt = pt if isinstance(pt, dict) else None
    h = (pt or {}).get("remote_stage_c") or {}
    snap = h.get("snapshot") or {}
    rtask = snap.get("task") or {}
    merge = snap.get("merge") or {}
    verification = h.get("verification") or {}
    P = (pt or {}).get("key")
    S = h.get("remote_task_key")

    def create():
        need(pt and P, "prod-task.json not captured")
        rows = _audit(pa, "task.create", P)
        need(rows, "no task.create audit row for the production task yet (prod-audit.json)")
        stop_if(not str(rows[0].get("actor", "")).startswith("founder:"), f"task.create actor is {rows[0].get('actor')}")
        return f"{P} created by {rows[0]['actor']}"

    def release():
        need(h.get("id"), "not released (no remote_stage_c)")
        rows = _audit(pa, "handoff.release", P)
        need(rows, "no handoff.release audit row yet")
        stop_if(len(rows) > 1 and not _audit(pa, "handoff.supersede", P), f"{len(rows)} releases for one task")
        stop_if(pt.get("status") not in ("WAITING_EXTERNAL", "DONE"), f"production status {pt.get('status')}")
        return f"handoff {h['id']} digest {str(h.get('payload_sha256'))[:12]}"

    def import_():
        code = h.get("last_error_code") or ""
        stop_if(STOP_CODES.match(code), f"last_error_code {code}")
        stop_if(h.get("delivery") in ("REFUSED", "SUSPENDED"), f"delivery {h.get('delivery')} ({code or '-'})")
        need(h.get("delivery") in ("IMPORTED", "DELIVERED") and S, f"delivery {h.get('delivery')}")
        stop_if(len(_audit(pa, "handoff.delivery", P)) > 1, "more than one handoff.delivery")
        if sa is not None:
            imports = [r for r in _audit(sa, "handoff.import") if r.get("entity_key") == f"handoff:{h['id']}"]
            stop_if(len(imports) > 1, "Stage C imported this handoff more than once")
        return f"Stage C task {S}"

    def activation():
        need(h.get("delivery") == "DELIVERED", f"delivery {h.get('delivery')}")
        need(_audit(pa, "handoff.activation", P), "no handoff.activation audit row yet")
        stop_if(len(_audit(pa, "handoff.activation", P)) > 1, "more than one handoff.activation")
        need(snap.get("activation") == "ACTIVATED", f"Stage C reports activation {snap.get('activation')}")
        return "activated on both sides"

    def authorization():
        grant = (snap.get("authorization") or {}).get("status")
        stop_if(grant == "INVALID", "Stage C reports the grant INVALID")
        need(grant in ("ACTIVE", "CONSUMED"), f"grant {grant or 'NONE'} (Start Pilot not done yet)")
        if sa is not None:
            issues = _audit(sa, "stage-c.authorization.issue")
            stop_if(len(issues) > 1, f"{len(issues)} Stage C authorizations issued (expected one)")
        return f"grant {grant}"

    def coder():
        need(isinstance(st, dict), "sc-task.json not captured")
        stop_if(st.get("key") != S, f"sc-task.json is {st.get('key')}, not {S}")
        runs = st.get("runs") or []
        stop_if(len(runs) > 1, f"{len(runs)} agent runs (expected one)")
        stop_if(rtask.get("status") in ("BLOCKED", "FAILED", "CANCELLED"),
                f"Stage C task {rtask.get('status')}: {rtask.get('failure_category') or st.get('error') or '-'}")
        need(runs and runs[0].get("status") == "SUCCEEDED", f"run {runs[0].get('status') if runs else 'not started'}")
        need((snap.get("authorization") or {}).get("status") == "CONSUMED", "the grant is not consumed")
        need(rtask.get("reviewer_verdict") == "APPROVED" and rtask.get("fingerprint_match") is True,
             "no independent approval bound to the tested fingerprint")
        return f"run {runs[0].get('id')} SUCCEEDED, reviewed fingerprint matches"

    def pr_():
        number = (rtask.get("pr") or {}).get("number")
        need(number, "no PR reported")
        need(isinstance(pr, dict), "pr.json not captured")
        stop_if(pr.get("number") != number, f"pr.json is PR {pr.get('number')}, reported {number}")
        stop_if((pr.get("headRepositoryOwner") or {}).get("login") != fork_owner,
                f"PR head owner {(pr.get('headRepositoryOwner') or {}).get('login')}, not the fork {fork_owner}")
        stop_if(pr.get("headRefName") != f"task/{S}", f"PR head branch {pr.get('headRefName')}")
        files = [f.get("path") for f in pr.get("files") or []]
        stop_if(files != [path], f"PR files {files} (expected only {path})")
        return f"PR #{number} from {fork_owner}:task/{S}, one file"

    def ci():
        c = rtask.get("ci") or {}
        stop_if(c.get("state") == "FAILURE", "CI failed on the PR head")
        need(c.get("state") in ("SUCCESS", "MERGED_OBSERVED") or merge.get("state") == "MERGED", f"CI {c.get('state')}")
        stop_if(c.get("state") == "SUCCESS" and c.get("sha") != (rtask.get("pr") or {}).get("head_sha"),
                "CI SUCCESS on another SHA than the PR head")
        return f"CI {c.get('state') or 'passed before merge'}"

    def merged():
        stop_if(merge.get("state") == "VERIFIED", "Stage C claims VERIFIED (only production may decide it)")
        stop_if(isinstance(pr, dict) and pr.get("mergedBy") and (pr["mergedBy"] or {}).get("login") != founder_login,
                f"merged by {(pr.get('mergedBy') or {}).get('login')}, not the founder")
        stop_if(rtask.get("status") == "BLOCKED", f"Stage C task BLOCKED after merge: {st.get('error') if st else '-'}")
        need(merge.get("state") == "MERGED" and SHA40.match(merge.get("commit") or ""), "no founder merge reported")
        if isinstance(pr, dict) and pr.get("mergeCommit"):
            stop_if((pr["mergeCommit"] or {}).get("oid") != merge["commit"], "reported merge commit != GitHub's")
        stop_if(pt.get("status") == "DONE" and verification.get("state") != "VERIFIED"
                and not _done_after_verified(pa, P), "production DONE without production verification")
        return f"merged as {merge['commit'][:12]}"

    def verified():
        ci_ = merge.get("main_ci") or {}
        stop_if(ci_.get("state") == "FAILURE", "main CI failed on the merge commit")
        if pt.get("status") == "DONE":      # after DONE the poll stops; the audit order is the evidence
            stop_if(not _done_after_verified(pa, P), "DONE was accepted before production verified the merge")
            return "production verified before DONE (audit order)"
        need(ci_.get("state") == "SUCCESS" and ci_.get("sha") == merge.get("commit"), "main CI on the merge not green")
        need(h.get("freshness") == "FRESH", "no fresh Stage C report")
        need(verification.get("state") == "VERIFIED", f"not verified: {verification.get('reason')}")
        stop_if(verification.get("production_api_sha") != merge.get("commit"), "production API SHA != merge commit")
        return f"production runs {merge['commit'][:12]}, main CI green"

    def done():
        need(pt.get("status") == "DONE", f"production status {pt.get('status')} (founder DONE pending)")
        stop_if(not _done_after_verified(pa, P), "DONE has no earlier production VERIFIED audit row")
        return "founder closed it after production verified"

    results, waiting = [], None
    for name, fn in zip(STEPS, (create, release, import_, activation, authorization, coder, pr_, ci, merged,
                                verified, done)):
        if waiting is not None:
            results.append({"step": name, "status": "WAIT", "evidence": f"after {waiting}"})
            continue
        try:
            results.append({"step": name, "status": "PASS", "evidence": fn()})
        except Stop as e:
            results.append({"step": name, "status": "STOP", "evidence": str(e)})
            waiting = name
        except Wait as e:
            results.append({"step": name, "status": "WAIT", "evidence": str(e)})
            waiting = name
    if ev.get("arm.txt") and "decision: ARM" in ev["arm.txt"] and pt and pt.get("status") == "DONE":
        results.append({"step": "ARM", "status": "STOP", "evidence": "the worker is still armed after DONE"})
    return results


def _done_after_verified(rows, key) -> bool:
    """The founder's DONE (a task.status row) comes after a production VERIFIED row for this task."""
    verified = [r for r in _audit(rows, "handoff.production_verification", key)
                if str(r.get("reason") or "").startswith("VERIFIED ")]
    status = _audit(rows, "task.status", key)
    return bool(verified and status and status[-1].get("id", 0) > verified[0].get("id", 0))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--founder-login", required=True)
    parser.add_argument("--path", default="tests/test_metrics.py")
    parser.add_argument("--fork-owner", default="seerxo-agent")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    names = ("prod-task.json", "prod-audit.json", "sc-task.json", "sc-audit.json", "pr.json", "arm.txt")
    try:
        ev = {n: load(args.evidence, n) for n in names}
    except (OSError, ValueError) as e:
        print(f"unreadable evidence: {e}", file=sys.stderr)
        return 2
    results = check(ev, founder_login=args.founder_login, path=args.path, fork_owner=args.fork_owner)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for i, r in enumerate(results, 1):
            print(f"{i:02d} {r['step']:<13} {r['status']:<4} {r['evidence']}")
    states = {r["status"] for r in results}
    return 3 if "STOP" in states else 1 if "WAIT" in states else 0


if __name__ == "__main__":
    sys.exit(main())
