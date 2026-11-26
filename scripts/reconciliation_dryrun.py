#!/usr/bin/env python3
"""Offline reconciliation dry run: the read-only production task inventory log -> a Markdown report.

Runs app/reconciliation.classify (the same rules as GET /desk/reconciliation) over the facts a read-only inventory
log carries (the read-only task inventory log format). It reads one file and writes one file; it never
connects to a database or a host. Facts the log does not carry are UNKNOWN and are never treated as admissible.

Usage: python3 scripts/reconciliation_dryrun.py INVENTORY.log OUT.md
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import reconciliation as R  # noqa: E402

ROW = re.compile(r"^\s+(?P<key>[A-Z]+-\d+)\s+(?P<status>[A-Z_]+)\s+(?P<prio>P\d)\s+(?P<risk>[A-Z]+)\s+"
                 r"retry=(?P<retry>\d+)\s+appr=(?P<appr>[TF])\s+age=(?P<age>\d+)d\s+agent=(?P<agent>\S+)\s+"
                 r"auto=(?P<auto>\S+)\s+write=(?P<write>\S+)\s+scoped=(?P<scoped>[TF])\s+handoff=(?P<handoff>[TF])\s+"
                 r"err='(?P<err>.*)'\s*$")
LOCK = re.compile(r"^\s+(?P<key>lock:repo:\S+)\s+(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d)\s*$")
BOOL = {"true": True, "false": False, "None": None}
CURRENT_REPOSITORY = "seerxo/acthelm"   # app/stage_c_worker.REPOSITORY


def facts(m: re.Match, paused: bool, no_started_runs: bool) -> R.Facts:
    auto, write = BOOL.get(m["auto"]), BOOL.get(m["write"])
    handoff, appr, risk = m["handoff"] == "T", m["appr"] == "T", m["risk"]
    if handoff:
        admission = "handed_off_to_stage_c"
    elif auto is not True:
        admission = "not_opted_in"                   # _admission_gate's first task check
    elif risk != "LOW" or appr:
        admission = "needs_human_gate"
    else:
        admission = R.UNKNOWN                        # project, owner, criteria, registry, deps: not in the log
    err = m["err"]
    error_class = R._error_class(err)
    stage = None if write is not True else R.UNKNOWN
    merge_recoverable = R.UNKNOWN
    if error_class == "MERGE_NOT_RECORDED":
        hit = re.search(r"merged at ([A-Z_]+)", err)
        if hit:
            stage = hit.group(1)
            merge_recoverable = stage in ("PR_OPEN", "CI", "READY_TO_MERGE")   # stage_c_ci.merge_recovery_block
    # worker.ready_refusal: an open run first (the inventory shows none), then for code tasks the pause.
    if not no_started_runs:
        ready = R.UNKNOWN
    elif write is True:
        ready = "agents are paused" if paused else R.UNKNOWN
    else:
        ready = None
    notes = []
    if m["agent"] == "None":
        notes.append("no owner agent")
    return R.Facts(key=m["key"], status=m["status"], age_h=int(m["age"]) * 24, in_status_h=None,
                   in_status_basis="unknown (age since creation is an upper bound)", retry_count=int(m["retry"]),
                   approval_required=appr, risk=risk, agent=None if m["agent"] == "None" else m["agent"],
                   autonomous=auto, write=write, handoff=handoff, error_class=error_class, open_run=False,
                   stage=stage, admission=admission, ready_refusal=ready, merge_recoverable=merge_recoverable,
                   resolution_evidence=R.UNKNOWN, waits_on=R.UNKNOWN if not handoff else "handoff",
                   stale_claim=False, notes=tuple(notes)), err


def main(src: str, out: str) -> int:
    text = Path(src).read_text()
    header = text.splitlines()[0] if text else ""
    paused = re.search(r"^\s+agents_paused\s", text, re.M) is not None
    started_section = text.split("== agent runs still STARTED", 1)[-1].split("==", 1)[0]
    no_started = not re.search(r"[A-Z]+-\d+", started_section)
    system = R.System(paused=paused, code_write_flag=R.UNKNOWN, auto_resume_flag=False)
    rows, errors = [], {}
    for line in text.splitlines():
        m = ROW.match(line)
        if m and m["status"] in R.OPEN:
            f, err = facts(m, paused, no_started)
            rows.append(R.classify(f, system))
            errors[f.key] = err
    locks = []
    for line in text.splitlines():
        m = LOCK.match(line)
        if m:
            row = R.classify_lock(m["key"], None, CURRENT_REPOSITORY, None)
            locks.append({**row, "updated": m["at"]})
    summary = R.summarize(rows)
    by_status = Counter(r["status"] for r in rows)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(render(src, header, rows, errors, locks, summary, by_status, paused, no_started))
    print(f"{len(rows)} open tasks -> {out}: " + ", ".join(f"{b} {v['count']}" for b, v in summary.items()))
    return 0


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render(src, header, rows, errors, locks, summary, by_status, paused, no_started) -> str:
    L = []
    w = L.append
    w("# Open-task reconciliation — offline dry run (read-only)")
    w("")
    w(f"Source: `{Path(src).name}` ({header.lstrip('# ')}). Rules: `app/reconciliation.classify`, the same function "
      "behind `GET /desk/reconciliation`; generated by `scripts/reconciliation_dryrun.py`. Nothing in production was "
      "read or changed to make this report: it is the log, re-classified.")
    w("")
    w(f"Open tasks: **{len(rows)}** ({', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))}). "
      f"agents_paused: **{'set' if paused else 'not set'}**. Agent runs STARTED: **{'none' if no_started else 'some'}**. "
      "ENABLE_BLOCKED_AUTO_RESUME: **off** (default; not in docker-compose).")
    w("")
    w("## Summary")
    w("")
    labels = {R.B_SYSTEM: "System progresses it itself (now or once a named switch is on)",
              R.B_CLICK: "One founder click", R.B_DECISION: "Founder decision needed",
              R.B_STUCK: "Truly stuck (every forward path refused by code; only cancel)"}
    w("| Bucket | Count | Tasks |")
    w("|---|---:|---|")
    for b in R.BUCKETS:
        w(f"| {labels[b]} | {summary[b]['count']} | {', '.join(summary[b]['tasks']) or '-'} |")
    w("")
    rec = Counter(r["recommended"] for r in rows)
    w("Recommended actions: " + ", ".join(f"`{k}` {v}" for k, v in sorted(rec.items())) + ".")
    w("")
    w("### What a safe automatic path could ever recover")
    w("")
    system_rows = summary[R.B_SYSTEM]["tasks"]
    unverified = [r["key"] for r in rows if r["blocker_class"] == "ADMISSION_UNVERIFIED"]
    auto_classes = [r["key"] for r in rows if r["recommended"] == R.AUTO_RESUMABLE]
    w(f"- System-progressable now or once a switch is on: **{len(system_rows)}**"
      + (f" ({', '.join(system_rows)})." if system_rows else ". The automatic paths that exist are: claim of "
         "admitted READY work (behind the pause, the code-write flag and per-task authorization), stale-claim "
         "recovery (needs a claim without a run; none is STARTED), dependency/external-event release (needs an "
         "opted-in wait), and the opt-in BLOCKED auto-resume (needs CODER_CAPACITY/LAUNCHER_REFUSED with a later "
         f"proof run; {len(auto_classes)} task(s) qualify, flag off)."))
    if unverified:
        w(f"- Possibly system-progressable after unpause, not decidable offline: {', '.join(unverified)} (opted in, LOW, "
          "no approval; project/owner/criteria/registry are not in the log — the endpoint answers it). Every other "
          "READY/BACKLOG/WAITING task fails a named admission check from the log itself.")
    w("- BLOCKED and FAILED tasks: their classes are deliberately not automatic (see each row). No check was loosened "
      "to make any of them recoverable.")
    for r in rows:
        if r["bucket"] == R.B_STUCK:
            w(f"- **{r['key']} is truly stuck** ({r['blocker_class']}): " + "; ".join(r["blocker_evidence"])
              + f". Only operation left: {r['operation']}.")
    w("")
    w("## Per task")
    w("")
    w("Columns: age is since creation (an upper bound on time in status; the log has no audit history). "
      "Admissible: `False` = refused by the named admission check, `None` = not decidable from the log.")
    w("")
    w("| Task | Status | Age | Retry | Blocker (evidence) | Admissible | Recommended | Who: operation | Precondition |")
    w("|---|---|---:|---|---|---|---|---|---|")
    order = {s: i for i, s in enumerate(R.OPEN)}
    for r in sorted(rows, key=lambda r: (R.BUCKETS.index(r["bucket"]), order[r["status"]], r["key"])):
        err = errors.get(r["key"]) or ""
        evidence = "; ".join(r["blocker_evidence"] + ([f"err='{err}'"] if err else []) + r["notes"])
        blocker = f"{r['blocker_class'] or '-'}" + (f" ({evidence})" if evidence else "")
        w(f"| {r['key']} | {r['status']} | {int(r['age_h'] // 24)}d | {r['retry']} | {_cell(blocker)} | "
          f"{r['admissible']} | `{r['recommended']}` | {r['actor']}: {_cell(r['operation'])} | {_cell(r['precondition'])} |")
    w("")
    w("## Control-state locks")
    w("")
    w("| Lock | Updated | Recommended | Who: operation | Blocks | Evidence |")
    w("|---|---|---|---|---|---|")
    for lk in locks:
        w(f"| `{lk['key']}` | {lk['updated']} | `{lk['recommended']}` | {lk['actor']}: {_cell(lk['operation'])} | "
          f"{lk['blocks']} | {_cell('; '.join(lk['evidence']))} |")
    w("")
    w("## Limits of this offline run (honest)")
    w("")
    w("- Not in the log, so UNKNOWN here and answered only by the endpoint: time in status (audit), max_attempts, "
      "task result / newest run verdict (escalation proof for the BLOCKED tasks without error text), pending approval "
      "rows, project/owner/criteria/registry admission for opted-in tasks, Stage C stage for code tasks, lock holder "
      "and expiry, and the worker heartbeat flags.")
    w("- FAILED tasks: `_set_failure` marks FAILED only when retries are exhausted, so retry=1/2 most likely means "
      "max_attempts was reached (or the founder set FAILED); a READY on an exhausted task is never claimed "
      "(`attempts_exhausted`). Either way the founder decides: cancel, or a new task with a changed approach.")
    w("- Errors are the log's truncated text; the class is taken from its prefix (stage_c_state.ERROR_PREFIXES).")
    w("- Any change these rows recommend is a production task mutation and needs its own founder GO; this report "
      "changes nothing.")
    w("")
    return "\n".join(L)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
