# Acthelm

**Agents work. You steer.**

An agent-driven operating system for a small company. Acthelm keeps the company's operational
state in PostgreSQL, decides deterministically when there is work to do, hands that work to AI agents
inside strict boundaries, and records what happened so the next session, human or model, starts from
facts instead of memory.

> The guiding rule: *the company must remember even when the AI forgets.* Models are replaceable
> workers. PostgreSQL holds operational state, Git holds policy, and a conversation is temporary.

**The problem it solves.** AI coding and operations agents are capable but forgetful, and they are
easy to over-trust. Their context disappears between sessions, a successful model response is easy
to mistake for a verified result, and handing an agent broad credentials turns one bad output into a
production incident. Acthelm keeps state outside the model, gives each agent only the authority one
task needs, and requires independent evidence (tests, a different reviewer, exact-head CI, a signed
release) before work moves forward.

Status: **V1, under active development.** See [Project maturity](#project-maturity-and-known-limitations).

Acthelm was previously named Seerxo OS. The repository is `Seerxo/Acthelm` (renamed from
`Seerxo/seerxo-os` on 2026-11-01); its signed production release identity is migrated separately; see the [naming decision](docs/operations/DECISIONS.md#dec-009--acthelm-product-name).

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Stage C: from task to pull request](#stage-c-from-task-to-pull-request)
- [Founder Desk](#founder-desk)
- [Agent lifecycle](#agent-lifecycle)
- [Autonomy levels and approval boundaries](#autonomy-levels-and-approval-boundaries)
- [Technology stack](#technology-stack)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Running the tests](#running-the-tests)
- [Security and trust boundaries](#security-and-trust-boundaries)
- [Production deployment concepts](#production-deployment-concepts)
- [CI](#ci)
- [Project structure](#project-structure)
- [Development workflow](#development-workflow)
- [Contributing](#contributing)
- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Project maturity and known limitations](#project-maturity-and-known-limitations)
- [License](#license)

---

## What it does

- **Operational memory.** Business units, projects, services, tasks, events, incidents, approvals,
  decisions, learnings, goals, metric observations and experiments are rows in PostgreSQL, with an
  audit trail. Nothing important lives only in a model's context window.
- **Deterministic work detection.** A reconciler evaluates goals against measured metrics and opens a
  task only when the evidence says one is needed. No work means no model call.
- **An unattended worker.** Eligible low-risk tasks are claimed atomically, run through a pluggable
  provider (the reference providers drive the Claude and Codex command-line tools), checkpointed, and
  recovered after crashes without repeating completed model calls.
- **Isolated code changes ("Stage C").** A coding agent edits a scoped set of files inside an isolated
  container, the project's checks run offline, a *different* provider reviews the exact diff, and the
  change is published as a pull request from a fork. Stage C can run unattended under a bounded
  standing authorization, but it never merges or deploys: preparing a pull request grants no
  permission to merge or deploy it. See [Stage C](#stage-c-from-task-to-pull-request).
- **A signed release gateway.** Production only applies a release manifest that a host-side verifier
  checks against a signed policy and a release ledger. Release authority is separate from merge
  authority; who signs a release, and under what conditions, is defined by the signed policy described
  in [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md): the gateway signs low-risk
  releases (currently `docs/*` only) automatically, and the founder signs every other release. The risk
  class is judged on everything changed since the last applied release, so a docs commit on top of
  unreleased code is not low-risk.
- **Business feedback.** Collectors record metrics with their source, unit and observation time. Goals,
  experiments and task context use those measurements; missing data stays unknown, never zero.
- **Operations.** A watchdog, deduplicated notifications, encrypted off-site backups with restore tests,
  and a [Founder Desk](#founder-desk) web UI that shows the real state of the system.

## Architecture

The diagram shows the Compose services defined in [`docker-compose.yml`](docker-compose.yml) plus the
founder, the host coder launcher, GitHub and the release gateway; the authenticated tunnel node is the
`cloudflared` service. The coder and the offline-checks containers are not Compose services: a host-side launcher
(`scripts/company-coder-launch`) starts one per job from a directory it shares with the worker.

```mermaid
flowchart LR
    founder["Founder browser"] --> tunnel["Authenticated tunnel"]
    tunnel --> api["company-api<br/>FastAPI, serves the Desk"]
    api <--> db[("company-postgres<br/>operational state")]
    worker["company-worker<br/>tasks, Stage C control plane"] <--> db
    worker -- "reasoner-net, internal" --> reasoner["company-reasoner<br/>Claude and Codex CLIs, reviewer"]
    reasoner --> regress["company-reasoner-egress<br/>provider allowlist"]
    regress --> providers["Model providers"]
    worker -- "jobs directory" --> launcher["Host coder launcher"]
    launcher --> coder["Coder container<br/>Claude, coder-net, internal"]
    coder --> cegress["company-egress<br/>provider allowlist"]
    cegress --> providers
    launcher --> checks["Checks container<br/>no network"]
    worker -- "fork push, PR" --> github["GitHub<br/>fork PR, exact-head CI"]
    signer["company-review-signer<br/>release profile"] -- "review-net, internal" --> reasoner
    gateway["Host release gateway<br/>and verifier"] -- "Unix socket" --> signer
```

| Component | Where | Role |
|---|---|---|
| API | `app/main.py` | State, tasks, events, approvals, Desk endpoints; serves the Desk at `/` |
| Database | `company-postgres` in `docker-compose.yml` | Operational state; no published port |
| Worker | `app/worker.py` | Claims eligible tasks, runs providers, checkpoints results, recovers after crashes |
| Reasoner | `app/reasoner.py` | Runs the provider CLIs away from the worker's database and GitHub credentials |
| Egress proxies | `company-reasoner-egress`, `company-egress` | The only route out for the reasoner and the coder: provider hosts only |
| Providers | `app/providers.py` | One class per provider; no metered API fallback |
| Reconciler | `app/business_reconciler.py`, `app/goals.py`, `app/goal_evidence.py` | Turns measured goal gaps into deduplicated tasks |
| Stage C | `app/stage_c.py`, `app/stage_c_worker.py`, `app/stage_c_runner.py`, `app/stage_c_ci.py` and the other `stage_c` modules | Isolated coding, offline checks, independent review, fork PR, CI observation |
| Release gateway | `app/release_gateway.py`, `app/release_gateway_service.py`, `app/release_host.py`, `app/review_signer.py` | Signed, host-verified releases |
| Founder Desk | `desk/` (React + Vite), built into `app/static/` | The web UI |
| Agents | `agents/` | Agent identities, permissions, cost and release policy |

The `release` and `stage-c` Compose profiles are not started by a plain `docker compose up`.

Design documents: [docs/AUTONOMOUS_AGENT_ARCHITECTURE.md](docs/AUTONOMOUS_AGENT_ARCHITECTURE.md),
[docs/AGENT_EXECUTION.md](docs/AGENT_EXECUTION.md), [docs/CODER_RUNTIME.md](docs/CODER_RUNTIME.md),
[docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md).

## Stage C: from task to pull request

Stage C turns a scoped engineering task into a pull request that a human can merge. Each step is a
recorded state change on the task (`app/stage_c.py`), and a failed test, review or CI run sends the
task back to implementation with a bounded number of repairs.

```
PLAN → CODE → OFFLINE TESTS → CODEX REVIEW → PR FROM A FORK → EXACT-HEAD CI → READY_TO_MERGE
                                                                   then the founder merges
```

| Step | What happens |
|---|---|
| PLAN (`PLANNED`) | The task names the files it may change; they must be on the Stage C allowlist (`app/stage_c_worker.py`). |
| CODE (`IMPLEMENTING`) | Claude edits the files in an isolated, read-only, capability-free container whose only route out is the provider egress proxy. It holds no GitHub credential. |
| OFFLINE TESTS (`TESTING`) | The project's checks run in a separate container with no network (`--network=none` in `scripts/company-coder-launch`). Every changed test file must report a passing test. |
| CODEX REVIEW (`INDEPENDENT_REVIEW`) | A different provider (Codex) reviews the exact diff. The approval records a SHA-256 fingerprint of that diff; the reviewer may not be the coder (`app/stage_c_runner.py`). |
| PR FROM A FORK (`PR_OPEN`) | The control plane pushes the branch to a fork and opens a pull request. The agent account has read-only access to the upstream repository's contents; an optional PR-only token can open pull requests (`docker-compose.yml`). |
| EXACT-HEAD CI (`CI`) | The required workflows must pass on the PR's exact head commit, and that head must match the reviewed fingerprint (`app/stage_c_ci.py`). |
| READY_TO_MERGE (`READY_TO_MERGE`) | Stage C stops here. Only the founder can move a task to `MERGED` (`app/stage_c.py`, tested in `tests/test_stage_c.py`). |

**Demonstrated.** Stage C has run this workflow unattended, from coding to pull request:
[PR #61](https://github.com/Seerxo/Acthelm/pull/61) and [PR #62](https://github.com/Seerxo/Acthelm/pull/62)
were each created by the system, passed offline tests, independent review and exact-head CI, and were
then merged by the founder.

**Not enabled.** That result does not imply automatic merge or deployment. Stage C never merges or
deploys. Automatic merge is not enabled. Production release is decided separately by the signed gateway
policy, which can release low-risk (currently documentation-only) changes after a merge without a human
step; all other releases need the founder's signature.

**Behind `main`.** A Stage C PR that falls behind `main` cannot be merged unchanged. Updating it changes
the head, so it needs new CI and a fingerprint check, and Stage C will not record that merge
automatically (no automatic branch refresh yet).

**Bounded authorization.** Stage C code writing is off by default. It runs only for a task with its
own founder authorization or under a *standing authorization* that:

- is issued only by a verified founder identity, and every issue and revocation is audited;
- covers only named files from the Stage C allowlist;
- lasts at most 7 days and allows at most 3 runs per UTC day;
- is bound to one source commit, so a deploy of a different commit voids it;
- is revocable at any time;
- enables nothing on its own: the global code-write flag and the host's acceptance checks still decide.

The bounds are enforced in `app/control.py` and tested in `tests/test_stage_c_standing.py`. Production
release authority is separate from all of this and is not granted by any Stage C authorization.

## Founder Desk

The Founder Desk is a React single-page app in `desk/`, built into `app/static/desk.html` and served by
the API at `/`. It shows the system's real state from the database: tasks and their evidence,
approvals, agents, automation, projects, growth and finance metrics, incidents, activity and memory.
Founder-only decisions and issuing Stage C authorizations require a verified founder identity; the host
operator may also decide approvals and withdraw authority (revoke)
([docs/APPROVAL_IDENTITY.md](docs/APPROVAL_IDENTITY.md)). In production the Desk is reachable only
through an authenticated tunnel.

## Agent lifecycle

```
GOAL → TASK → ELIGIBILITY → PLANNED → IMPLEMENTING → TESTING → INDEPENDENT_REVIEW → PR_OPEN → CI
     → READY_TO_MERGE → MERGED → VERIFYING → MEASURED → NEXT TASK
```

Every transition is a database state change with an owner and evidence. Retries are bounded, a
provider quota limit means waiting (never paying for a fallback), and an agent never approves its own
work. A successful model response is not a passed test, and a passed CI run is not a verified release.

## Autonomy levels and approval boundaries

| Stage | Scope | Default |
|---|---|---|
| A | Observe and report (watchdog, briefs, Desk) | on |
| B | Automatic low-risk tasks with no tools and an empty scratch directory | on for opted-in tasks |
| C | Reversible engineering in an isolated worktree, producing a reviewed pull request | off by default; enabled per task or by a bounded, revocable standing authorization (see [Stage C](#stage-c-from-task-to-pull-request)) |
| D | Selected production workflows behind approval gates | not enabled |

The human owner (called *the founder* in this codebase) remains the authority for strategy, spending,
publishing, merges, production deploys, credentials and policy changes. In particular:

- Agents can plan, code, test, review each other's work and open pull requests from a fork.
- Agents cannot approve their own work, merge, deploy, change policy, or issue themselves an
  authorization.
- Merge authority and production release authority are separate permissions; holding one does not
  grant the other.

Automation flags default to off, and a pause stops autonomous execution without stopping production
services. See the Stage C security design in
[docs/AUTONOMOUS_AGENT_ARCHITECTURE.md](docs/AUTONOMOUS_AGENT_ARCHITECTURE.md) and
[docs/APPROVAL_IDENTITY.md](docs/APPROVAL_IDENTITY.md).

## Technology stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic, Uvicorn
- **Database:** PostgreSQL 18
- **Frontend:** React, TypeScript, Vite, Tailwind CSS (`desk/`)
- **Runtime:** Docker and Docker Compose; separate images for the API, worker, reasoner, coder, review
  signer and an egress proxy (`Dockerfile`, `docker-compose.yml`)
- **Tests:** pytest with pytest-xdist; Node's test runner for the frontend
- **Backups:** `pg_dump`, `age` encryption, S3-compatible object storage

## Quickstart

This runs the API and the Desk locally against a disposable PostgreSQL. It needs Python 3.12 and Docker.

```bash
# 1. A disposable database (development only)
docker run -d --name seerxo-dev-db -e POSTGRES_USER=company -e POSTGRES_PASSWORD=dev-only \
  -e POSTGRES_DB=company -p 127.0.0.1:55432:5432 postgres:18-alpine

# 2. Dependencies
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Schema and an example organisation
export DATABASE_URL="postgresql+psycopg://company:dev-only@127.0.0.1:55432/company"
export ENVIRONMENT=development
alembic upgrade head
python -m scripts.seed --fresh-install

# 4. Run the API
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open:

- `http://127.0.0.1:8000/`: the Founder Desk
- `http://127.0.0.1:8000/docs`: the interactive API reference
- `http://127.0.0.1:8000/health`: a health check

`scripts/seed.py` creates a small fictional company so the Desk has something to show. Replace it with
your own organisation. To rebuild the Desk after changing `desk/`, run `npm ci --prefix desk` and
`npm run build --prefix desk`.

The worker, the coder and the release components need provider logins, container isolation and a
host setup that this quickstart does not create. Read [docs/CODER_RUNTIME.md](docs/CODER_RUNTIME.md)
before enabling them.

### Operator CLI

On a configured host, run `./scripts/acthelm --help` or `./scripts/acthelm brief` from the
release checkout. `acthelm` uses the existing operator CLI; `companyctl` remains compatible.
The `acthelm` entry point becomes available with this source release. Existing host commands,
service names, environment variables and signed release identities keep their current names.

## Configuration

All configuration comes from environment variables; `app/config.py` reads them and never from a file
committed to Git. [`.env.example`](.env.example) lists every variable with a harmless placeholder.
Copy it to `.env` (which is ignored by Git), fill in your own values and keep the file private
(`chmod 600 .env`). Empty notification or storage credentials disable that integration; events are
still recorded.

Never commit a real value. Secrets are referenced by name only in code, tasks, prompts and logs.

## Running the tests

The backend suite needs a disposable PostgreSQL whose database is named
`company_external_release_test`; the tests refuse to run against any other name.

```bash
docker run -d --name seerxo-test-db -e POSTGRES_USER=release_test -e POSTGRES_PASSWORD=disposable-ci-only \
  -e POSTGRES_DB=company_external_release_test -p 127.0.0.1:55433:5432 \
  --tmpfs /var/lib/postgresql postgres:18-alpine

export EXTERNAL_RELEASE_TEST_DATABASE_URL="postgresql+psycopg://release_test:disposable-ci-only@127.0.0.1:55433/company_external_release_test"
python -m pytest -q -n 4 --dist loadgroup
```

Some tests exercise the release and archive tooling and need a Git checkout (not an exported tree) and
Node.js. The exact commands CI runs are in
[`.github/workflows/python-tests.yml`](.github/workflows/python-tests.yml). The security-focused tests
and how to run them are listed in [SECURITY.md](SECURITY.md#running-the-security-tests).

Frontend:

```bash
npm ci --prefix desk
npm test --prefix desk
npm run lint --prefix desk
npm run build --prefix desk
```

Documentation links and repository paths cited in the main documents are checked with
`python3 scripts/check_docs.py --public-profile`.

## Security and trust boundaries

- **Preparing a pull request is not permission to merge or deploy it.** The coding agent works in an
  isolated container without GitHub credentials, its account has read-only access to the upstream
  repository's contents (an optional PR-only token can open pull requests), and it publishes only to a
  fork, including under a standing authorization.
- **Independent review.** Code written by one provider is reviewed by another; the reviewed diff is
  bound to the published commit by a fingerprint.
- **Signed releases.** Production applies only a manifest that the host-side verifier checks against a
  founder-signed policy and an append-only ledger. A merge to `main` does not deploy anything by
  itself.
- **No secrets in the repository.** Production configuration lives on the protected host or in the
  deployment platform, never in Git. No workflow in this repository names a secret.
- **Pull request code never runs on persistent infrastructure.** All CI runs on fresh GitHub-hosted
  virtual machines with a read-only token.
- **Tripwires, not boundaries.** `tests/test_trust_boundary.py` inventories what a repository change
  can reach. Anyone who can edit the repository can edit those tests, so the real boundaries are the
  host verifier, the signed policy and the credential separation.

Limits of this model, stated plainly:

- **Repository settings are not code.** GitHub branch protection on `main` requires the three checks
  listed under [CI](#ci) and an up-to-date branch, but it is a repository setting: it is not versioned
  in this repository, and the tests here cannot detect a change to it.
- **One owner.** The founder account and its signing key are the final merge and release authority; a
  compromise of either bypasses the human gate. The gateway key can release low-risk paths on its own.
- **Model review is not a proof.** An independent model review lowers, but does not remove, the risk of
  a harmful change inside an allowed path.
- **Scanners are not complete.** Secret scanning and redaction are pattern-based and can miss a secret.

Details: [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md),
[docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md),
[docs/REPOSITORY_GUARDIAN.md](docs/REPOSITORY_GUARDIAN.md), [SECURITY.md](SECURITY.md).

## Production deployment concepts

A production installation is a single host running the Compose services, reachable only through an
authenticated tunnel, with the database on an internal network and no public ports. Releases are
built from an exact commit, staged, verified by the host-side verifier against the signed policy, and
recorded in the release ledger; a failed verification refuses the release and a failed apply rolls
back to anchored images. Backups are encrypted before they leave the host, and restore tests run on a
schedule. This repository contains tooling only: no production addresses or credentials.

## CI

Every workflow runs on GitHub-hosted `ubuntu-24.04` runners with a read-only token and no secrets:

| Workflow | Purpose | Required |
|---|---|---|
| `python-tests` | Backend suite and PostgreSQL, concurrency and crash-recovery tests | yes |
| `frontend` (`frontend-tests`) | Desk build, tests and lint, and a check that the built bundle is committed | yes |
| `secret-scan` (`trufflehog`) | TruffleHog over the pull request's commits and every push to `main` | yes |
| `dependency-scan` | OSV scan of Python and npm dependencies | no |
| `workflow-lint` | zizmor and actionlint | no |
| `prowler` | Infrastructure-as-code scan (scheduled or manual) | no |
| `ci` | A manually started readiness check for one exact `main` commit; it deploys nothing | no |

GitHub branch protection on `main` requires the `python-tests`, `frontend-tests` and `trufflehog`
checks and a branch that is up to date with `main`. On top of that, the merge tooling
(`scripts/merge-pr`) and the Stage C merge gate apply their own exact-head check: they refuse a merge
unless each workflow listed in [`agents/repository_guardian.yaml`](agents/repository_guardian.yaml)
passed on the exact head commit being merged.
These exact-head checks apply only when `merge-pr` is used; a direct GitHub merge is limited by
branch protection alone, and Stage C reconciliation records or blocks it afterwards.
Third-party actions are pinned to full commit SHAs. The contribution flow is in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Project structure

```
app/          FastAPI application, worker, reconciler, Stage C, release gateway
desk/         Founder Desk frontend (React, TypeScript, Vite)
migrations/   Alembic migrations
agents/       Agent registry, cost policy, release policy, repository guardian policy
skills/       Reusable agent procedures
scripts/      Operator CLI (acthelm; companyctl compatible), backup and restore, deploy and release tooling
tests/        pytest suite
evals/        Golden evaluations for agent behaviour
ops/          Compose overlays
docs/         Design documents
```

## Development workflow

Trunk-based: `main` is the only permanent branch, every change goes through a pull request from a
short-lived branch, and required checks must pass on the exact head commit. See
[docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md). Coding agents follow [CLAUDE.md](CLAUDE.md) (also
available as `AGENTS.md`).

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md) before
opening an issue or a pull request. Until a license and a contributor agreement are published, code and
documentation pull requests from outside the maintainer team cannot be accepted; bug reports, security
reports, questions and design discussion in issues are welcome.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting as described in
[SECURITY.md](SECURITY.md).

## Project maturity and known limitations

- **Active development.** The project is under active development; interfaces, policies and documents
  change frequently.
- **V1.** The API, worker, reconciler, Desk, backups and release gateway run in production for one
  company.
- **Stage C is narrow.** It has demonstrated an unattended coding-to-pull-request workflow
  ([PR #61](https://github.com/Seerxo/Acthelm/pull/61), [PR #62](https://github.com/Seerxo/Acthelm/pull/62)),
  but only on a short allowlist of low-risk files; those two changes were test additions. Coding is
  Claude-only, so the independent reviewer is Codex.
- **Stage C does not merge or deploy.** Merging remains a founder decision; release authority is
  separate (see [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md)).
- **Single-host design.** The reference deployment is one host; there is no horizontal scaling.
- **Opinionated providers.** The reference providers drive command-line tools tied to subscription
  logins; other providers need a new provider class.
- **Documentation.** Some design documents describe decisions made for the original deployment and use
  placeholders such as `<production-host>` where an address was removed.

## License

No license has been chosen yet. Until a license file is added, all rights are reserved: you may view
and fork the code on GitHub, but no permission to use, modify or distribute it is granted.
